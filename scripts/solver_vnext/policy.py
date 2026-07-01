from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from .domain import CandidateEnvelope, ContractDelta, ContractFamily, FlowContract, PhaseKind, PhaseState, ResourceDelta, SolverState
from .phase import HumanPhaseGate


@dataclass(frozen=True)
class PolicyContext:
    phase_state: PhaseState
    remote_open: bool
    remote_session_open: bool
    last_business_remote: bool | None
    non_remote_unsatisfied: int
    remote_unsatisfied: int
    cun4_count: int


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
        unsatisfied = legacy.unsatisfied_cars(state.cars, state.depot_assignment)
        loads = legacy.line_loads(state.cars)
        remote_count = 0
        front_count = 0
        cun4_count = 0
        closeout_count = 0
        for car in unsatisfied:
            target_line, _position, _reason = legacy.planned_target_for_car(
                car,
                state.cars,
                state.depot_assignment,
                loads,
            )
            if car["Line"] in legacy.REMOTE_INTERACTION_LINES or target_line in legacy.REMOTE_INTERACTION_LINES:
                remote_count += 1
            elif target_line == "存4线":
                cun4_count += 1
            elif target_line in {
                "调梁棚",
                "调梁线北",
                "预修线",
                "洗罐站",
                "洗罐线北",
                "油漆线",
                "抛丸线",
                "卸轮线",
                "机走棚",
                "机库线",
                "机走北",
            }:
                front_count += 1
            else:
                closeout_count += 1
        phase_state = self.phase_gate.classify_state(
            front_debt=front_count,
            cun4_port_debt=cun4_count,
            remote_debt=remote_count,
            closeout_debt=closeout_count,
            remote_session_open=state.remote_session_open,
        )
        remote_open = phase_state.phase == PhaseKind.H4_REMOTE_DEPOT
        return PolicyContext(
            phase_state=phase_state,
            remote_open=remote_open,
            remote_session_open=state.remote_session_open,
            last_business_remote=state.last_business_remote,
            non_remote_unsatisfied=front_count + cun4_count + closeout_count,
            remote_unsatisfied=remote_count,
            cun4_count=sum(1 for car in state.cars if car["Line"] == "存4线"),
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
        touched_remote = any(line in legacy.REMOTE_INTERACTION_LINES for line in candidate.resource_delta.request.touched_lines)
        remote_transition_cost = self._remote_transition_cost(hook, context)
        hook_count = max(1, self._planlet_hook_count(hook))
        lane = self._candidate_lane(candidate, context)
        if envelope.contract.family == ContractFamily.REMOTE_SESSION:
            return (
                lane,
                self._contract_phase_key(envelope.contract, context),
                self._remote_penalty(touched_remote, context),
                remote_transition_cost,
                round(-delta.contract_reduction / hook_count, 4),
                round(-delta.effective_gain / hook_count, 4),
                -delta.contract_reduction,
                hook_count,
                -len(hook.move_car_nos),
                self._contract_key(envelope.contract, context),
                hook.candidate_id,
            )
        return (
            lane,
            self._contract_phase_key(envelope.contract, context),
            round(-delta.contract_reduction / hook_count, 4),
            round(-delta.effective_gain / hook_count, 4),
            round(-delta.total_reduction / hook_count, 4),
            -delta.contract_reduction,
            -delta.effective_gain,
            -delta.total_reduction,
            self._remote_penalty(touched_remote, context),
            remote_transition_cost,
            -len(hook.move_car_nos),
            self._planlet_put_count(hook),
            self._contract_key(envelope.contract, context),
            hook.candidate_id,
        )

    def _candidate_lane(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        if self.opens_remote_session(candidate):
            return 0
        if self.releases_depot_slot(candidate, context):
            return 1
        if self.digests_remote_session_batch(candidate, context):
            return 2
        if self.digests_depot_inbound_batch(candidate, context):
            return 3
        if self.fragments_depot_inbound(candidate, context):
            return 30
        return 10

    def opens_remote_session(self, candidate: EvaluatedCandidate) -> bool:
        envelope = candidate.envelope
        hook = envelope.candidate
        delta = candidate.contract_delta
        request = candidate.resource_delta.request
        if (
            envelope.template_name == "depot_outbound_session"
            and envelope.contract.family == ContractFamily.REMOTE_SESSION
            and request.target_line == "存4线"
            and hook.source_line in legacy.REMOTE_INTERACTION_LINES
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
            and hook.source_line in legacy.DEPOT_LINES
            and request.target_line in legacy.legacy.DEPOT_OUTSIDE_LINES
            and delta.contract_reduction > 0
        )

    def digests_remote_session_batch(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        return (
            envelope.contract.family == ContractFamily.REMOTE_SESSION
            and envelope.template_name == "remote_session_directional_digest"
            and any(line in legacy.DEPOT_LINES for line in request.put_lines)
            and delta.contract_reduction >= 3
        )

    def digests_depot_inbound_batch(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        delta = candidate.contract_delta
        return (
            envelope.contract.family == ContractFamily.REPAIR_INBOUND
            and any(line in legacy.DEPOT_LINES for line in request.put_lines)
            and envelope.template_name
            in {
                "depot_multi_drop_accessible_prefix",
                "remote_session_directional_digest",
            }
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
            envelope.template_name == "remote_depot_direct_accessible_prefix"
            and envelope.contract.family == ContractFamily.REPAIR_INBOUND
            and any(line in legacy.DEPOT_LINES for line in request.put_lines)
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
                ContractFamily.CUN4_PORT_STAGING: 5,
                ContractFamily.DEPOT_OUTBOUND: 6,
                ContractFamily.REPAIR_INBOUND: 7,
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

    def _remote_transition_cost(self, candidate: Any, context: PolicyContext) -> int:
        lines = [
            step.line
            for step in legacy.candidate_plan_steps(candidate)
            if step.action in {"Get", "Put"}
        ]
        if not lines:
            return 0
        remote_flags = [line in legacy.REMOTE_INTERACTION_LINES for line in lines]
        cost = sum(1 for left, right in zip(remote_flags, remote_flags[1:]) if left != right)
        if context.last_business_remote is not None and remote_flags[0] != context.last_business_remote:
            cost += 1
        return cost

    def _planlet_put_count(self, candidate: Any) -> int:
        return sum(1 for step in legacy.candidate_plan_steps(candidate) if step.action == "Put")

    def _planlet_hook_count(self, candidate: Any) -> int:
        steps = legacy.candidate_plan_steps(candidate)
        if not steps:
            return 2
        return sum(1 for step in steps if step.action in {"Get", "Put"})
