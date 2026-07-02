from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from . import plan_facts
from .domain import CandidateEnvelope, ContractDelta, ContractFamily, FlowContract, IntentKind, PhaseKind, PhaseState, ResourceDelta, SolverState
from .domain import RemoteSessionState
from .flow import classify_flow_facts
from .phase import HumanPhaseGate


@dataclass(frozen=True)
class PolicyContext:
    phase_state: PhaseState
    remote_session: RemoteSessionState
    remote_open: bool
    last_business_remote: bool | None

    @property
    def remote_session_open(self) -> bool:
        return self.remote_session.active


@dataclass(frozen=True)
class EvaluatedCandidate:
    envelope: CandidateEnvelope
    validation: Any
    prospective_cars: list[dict[str, Any]]
    contract_delta: ContractDelta
    resource_delta: ResourceDelta
    next_loco_location: Any
    prospective_signature: tuple[str, str, tuple[tuple[str, str, int], ...]]


class BaselinePolicy:
    """Strategy layer for vNext.

    Mechanisms generate and validate candidates.  This policy only chooses
    which contracts to consider first and how accepted candidates are ranked.
    """

    def __init__(self) -> None:
        self.phase_gate = HumanPhaseGate()

    def context(self, state: SolverState) -> PolicyContext:
        flow_facts = classify_flow_facts(state.cars, state.depot_assignment)
        phase_state = self.phase_gate.classify_state(
            front_debt=flow_facts.front_debt,
            cun4_port_debt=flow_facts.cun4_port_debt,
            remote_debt=flow_facts.remote_debt,
            closeout_debt=flow_facts.closeout_debt,
            remote_session=state.remote_session,
            cun4_release_ready=flow_facts.cun4_release_ready,
            cun4_port_mode=flow_facts.cun4_port_mode,
            cun4_release_count=flow_facts.cun4_release_count,
            cun4_prefix_hold_count=flow_facts.cun4_prefix_hold_count,
            active_variant=flow_facts.active_variant,
        )
        remote_open = phase_state.phase == PhaseKind.H4_REMOTE_DEPOT
        return PolicyContext(
            phase_state=phase_state,
            remote_session=state.remote_session,
            remote_open=remote_open,
            last_business_remote=state.last_business_remote,
        )

    def order_contracts(self, contracts: list[FlowContract], context: PolicyContext) -> list[FlowContract]:
        return sorted(contracts, key=lambda contract: self._contract_key(contract, context))

    def better(
        self,
        candidate: EvaluatedCandidate,
        incumbent: EvaluatedCandidate | None,
        context: PolicyContext,
    ) -> bool:
        if incumbent is None:
            return True
        return self.candidate_key(candidate, context) < self.candidate_key(incumbent, context)

    LARGE_DEPOT_OUTBOUND_RELEASE_MIN = 10
    LARGE_DEPOT_OUTBOUND_CAR_MIN = 8

    def candidate_key(self, candidate: EvaluatedCandidate, context: PolicyContext) -> tuple[Any, ...]:
        envelope = candidate.envelope
        hook = envelope.candidate
        delta = candidate.contract_delta
        touched_remote = plan_facts.touches_remote(candidate.resource_delta.request)
        remote_transition_cost = plan_facts.remote_transition_cost(hook, context.last_business_remote)
        hook_count = max(1, plan_facts.hook_count(hook))
        lane = self._candidate_lane(candidate, context)
        return (
            lane,
            self._special_candidate_rank(candidate, context),
            self._contract_phase_key(envelope.contract, context),
            self._remote_penalty(touched_remote, context),
            remote_transition_cost,
            round(-delta.contract_reduction / hook_count, 4),
            round(-delta.effective_gain / hook_count, 4),
            round(-delta.total_reduction / hook_count, 4),
            -delta.contract_reduction,
            -delta.effective_gain,
            -delta.total_reduction,
            plan_facts.put_count(hook),
            hook_count,
            -len(hook.move_car_nos),
            self._contract_key(envelope.contract, context),
            hook.candidate_id,
        )

    def _special_candidate_rank(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        if self.resolves_spotting_closeout(candidate, context):
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_ACCEPT:
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_GROUP:
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_OUTBOUND_HOLD:
            return 1
        if self.fills_cun4_port(candidate, context):
            return 2
        if self.opens_remote_session(candidate):
            return 1
        if candidate.envelope.contract.family == ContractFamily.REMOTE_SESSION:
            return 2
        return 3

    def _candidate_lane(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_ACCEPT:
            return 0
        if context.phase_state.phase in {PhaseKind.H1_FRONT_SERVICE, PhaseKind.H2_CUN4_PORT} and self.front_access_shaping(candidate, context):
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_GROUP:
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_OUTBOUND_HOLD:
            return 1
        if self.fills_cun4_port(candidate, context):
            return 2
        if self.opens_remote_session(candidate):
            return 3
        if self.releases_depot_outbound(candidate, context):
            return 3
        if self.releases_depot_slot(candidate, context):
            return 4
        if self.swaps_depot_slot(candidate, context):
            return 5
        if self.digests_remote_prefix_batch(candidate, context):
            return 5
        if self.digests_remote_session_batch(candidate, context):
            return 6
        if self.digests_depot_inbound_batch(candidate, context):
            return 7
        if self.opens_remote_prefix_lease(candidate, context):
            return 8
        if self.continues_remote_work(candidate, context):
            return 9
        if self.resolves_spotting_closeout(candidate, context):
            if context.phase_state.phase == PhaseKind.H4_REMOTE_DEPOT:
                return 20
            return 0
        if self.fragments_depot_inbound(candidate, context):
            return 30
        if candidate.envelope.intent == IntentKind.BLOCKER_STAGING:
            return 40
        return 10

    def resolves_spotting_closeout(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase not in {PhaseKind.H4_REMOTE_DEPOT, PhaseKind.H5_CLOSEOUT}:
            return False
        if candidate.contract_delta.contract_reduction <= 0:
            return False
        request = candidate.resource_delta.request
        if not physical.is_spotting_line(request.target_line):
            return False
        return candidate.envelope.template_name in {"spotting_repack", "spotting_target_repack"}

    def continues_remote_work(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        if candidate.contract_delta.contract_reduction <= 0:
            return False
        if not plan_facts.touches_remote(candidate.resource_delta.request):
            return False
        return candidate.envelope.contract.family in {
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
        }

    def opens_remote_prefix_lease(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        return (
            candidate.envelope.intent == IntentKind.REMOTE_PREFIX_LEASE
            and "remote_prefix_lease_opened" in candidate.contract_delta.fulfilled
            and candidate.contract_delta.support_gain > 0
        )

    def front_access_shaping(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase not in {PhaseKind.H1_FRONT_SERVICE, PhaseKind.H2_CUN4_PORT}:
            return False
        if candidate.contract_delta.contract_reduction <= 0:
            return False
        family = candidate.envelope.contract.family
        request = candidate.resource_delta.request
        if plan_facts.touches_remote(request):
            return False
        if request.target_line == "存4线":
            return False
        if request.source_line in {"存5线北", "存5线南"}:
            if family == ContractFamily.DISPATCH_SHED_QUEUE and request.target_line == "调梁棚":
                return candidate.contract_delta.contract_reduction >= 3
            return (
                family in {
                    ContractFamily.FUNCTION_LINE_SERVICE,
                    ContractFamily.DISPATCH_SHED_QUEUE,
                    ContractFamily.PRE_REPAIR_STAGING,
                }
                and request.target_line in {"调梁棚", "调梁线北", "抛丸线", "预修线"}
            )
        if request.source_line == "调梁棚":
            return (
                family in {
                    ContractFamily.FUNCTION_LINE_SERVICE,
                    ContractFamily.LOCO_AREA_STAGING,
                }
                and request.target_line in {"洗罐站", "抛丸线", "机走棚"}
            )
        return False

    def fills_cun4_port(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase not in {PhaseKind.H1_FRONT_SERVICE, PhaseKind.H2_CUN4_PORT}:
            return False
        return "存4线" in candidate.resource_delta.request.put_lines and candidate.contract_delta.total_reduction > 0

    def opens_remote_session(self, candidate: EvaluatedCandidate) -> bool:
        envelope = candidate.envelope
        hook = envelope.candidate
        delta = candidate.contract_delta
        request = candidate.resource_delta.request
        if (
            plan_facts.is_remote_outbound_session_release(envelope, request)
            and delta.contract_reduction >= self.LARGE_DEPOT_OUTBOUND_RELEASE_MIN
            and len(hook.move_car_nos) >= self.LARGE_DEPOT_OUTBOUND_CAR_MIN
        ):
            return True
        return False

    def releases_depot_slot(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        envelope = candidate.envelope
        hook = envelope.candidate
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        return (
            envelope.contract.family in {ContractFamily.DEPOT_SLOT, ContractFamily.DEPOT_OUTBOUND}
            and hook.source_line in physical.DEPOT_LINES
            and request.target_line in physical.DEPOT_OUTSIDE_LINES
            and delta.contract_reduction > 0
        )

    def releases_depot_outbound(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        hook = envelope.candidate
        request = candidate.resource_delta.request
        return (
            envelope.contract.family == ContractFamily.DEPOT_OUTBOUND
            and hook.source_line in physical.DEPOT_LINES
            and request.target_line not in physical.DEPOT_TARGET_LINES
            and candidate.contract_delta.contract_reduction > 0
        )

    def swaps_depot_slot(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        return (
            candidate.envelope.intent == IntentKind.DEPOT_SLOT_SWAP
            and candidate.contract_delta.contract_reduction > 0
        )

    def digests_remote_session_batch(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        return (
            envelope.contract.family == ContractFamily.REMOTE_SESSION
            and envelope.intent == IntentKind.REMOTE_SESSION
            and any(line in physical.DEPOT_LINES for line in request.put_lines)
            and delta.contract_reduction >= 3
        )

    def digests_remote_prefix_batch(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        return (
            candidate.envelope.template_name == "remote_session_prefix_batch_digest_restore"
            and candidate.contract_delta.contract_reduction >= 2
        )

    def digests_depot_inbound_batch(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        return (
            envelope.contract.family == ContractFamily.REPAIR_INBOUND
            and envelope.intent == IntentKind.REMOTE_DEPOT
            and any(line in physical.DEPOT_LINES for line in request.put_lines)
            and not request.same_plan_source_return_nos
            and plan_facts.depot_put_line_count(request) >= 2
            and (
                delta.contract_reduction >= 2
                or len(envelope.candidate.move_car_nos) >= 3
            )
        )

    def fragments_depot_inbound(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        return (
            envelope.contract.family == ContractFamily.REPAIR_INBOUND
            and envelope.intent == IntentKind.REMOTE_DEPOT
            and any(line in physical.DEPOT_LINES for line in request.put_lines)
            and plan_facts.depot_put_line_count(request) == 1
            and plan_facts.put_count(envelope.candidate) == 1
            and delta.contract_reduction <= 1
            and len(envelope.candidate.move_car_nos) <= 2
        )

    def _contract_key(self, contract: FlowContract, context: PolicyContext) -> tuple[int, int, str, str, str]:
        return (
            self._phase_family_rank(contract.family, context),
            contract.priority,
            contract.family.value,
            contract.source_lines[0] if contract.source_lines else "",
            contract.target_lines[0] if contract.target_lines else "",
        )

    def _contract_phase_key(self, contract: FlowContract, context: PolicyContext) -> tuple[int, int, str]:
        return (
            self._phase_family_rank(contract.family, context),
            contract.priority,
            contract.family.value,
        )

    def _phase_family_rank(self, family: ContractFamily, context: PolicyContext) -> int:
        phase = context.phase_state.phase
        if phase == PhaseKind.H1_FRONT_SERVICE:
            front_order = {
                ContractFamily.FUNCTION_LINE_SERVICE: 0,
                ContractFamily.DISPATCH_SHED_QUEUE: 1,
                ContractFamily.PRE_REPAIR_STAGING: 2,
                ContractFamily.LOCO_AREA_STAGING: 3,
                ContractFamily.YARD_REBALANCE: 4,
                ContractFamily.REPAIR_INBOUND: 5,
                ContractFamily.CUN4_PORT_STAGING: 5,
                ContractFamily.DEPOT_OUTBOUND: 6,
                ContractFamily.DEPOT_SLOT: 8,
                ContractFamily.REMOTE_SESSION: 9,
            }
            return front_order.get(family, 50)
        if phase == PhaseKind.H2_CUN4_PORT:
            cun4_order = {
                ContractFamily.CUN4_PORT_STAGING: 0,
                ContractFamily.DEPOT_OUTBOUND: 1,
                ContractFamily.REMOTE_SESSION: 2,
                ContractFamily.REPAIR_INBOUND: 3,
                ContractFamily.DEPOT_SLOT: 4,
                ContractFamily.FUNCTION_LINE_SERVICE: 8,
                ContractFamily.DISPATCH_SHED_QUEUE: 9,
                ContractFamily.PRE_REPAIR_STAGING: 10,
            }
            return cun4_order.get(family, 50)
        if phase in {PhaseKind.H3_RELEASE_ACCEPT, PhaseKind.H4_REMOTE_DEPOT}:
            remote_order = {
                ContractFamily.REMOTE_SESSION: 0,
                ContractFamily.DEPOT_OUTBOUND: 1,
                ContractFamily.REPAIR_INBOUND: 2,
                ContractFamily.DEPOT_SLOT: 3,
                ContractFamily.CUN4_PORT_STAGING: 4,
                ContractFamily.FUNCTION_LINE_SERVICE: 8,
                ContractFamily.DISPATCH_SHED_QUEUE: 9,
                ContractFamily.PRE_REPAIR_STAGING: 10,
                ContractFamily.YARD_REBALANCE: 11,
                ContractFamily.LOCO_AREA_STAGING: 12,
            }
            return remote_order.get(family, 50)
        closeout_order = {
            ContractFamily.LOCO_AREA_STAGING: 0,
            ContractFamily.SPECIAL_REPAIR_PROCESS: 1,
            ContractFamily.FUNCTION_LINE_SERVICE: 2,
            ContractFamily.YARD_REBALANCE: 3,
            ContractFamily.PRE_REPAIR_STAGING: 4,
            ContractFamily.DISPATCH_SHED_QUEUE: 5,
            ContractFamily.REMOTE_SESSION: 8,
            ContractFamily.REPAIR_INBOUND: 9,
            ContractFamily.DEPOT_SLOT: 10,
            ContractFamily.DEPOT_OUTBOUND: 11,
        }
        return closeout_order.get(family, 50)

    def _remote_penalty(self, touched_remote: bool, context: PolicyContext) -> int:
        if context.remote_open:
            return 0 if touched_remote else 1
        return 1 if touched_remote else 0
