from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import physical
from . import plan_facts
from . import serial
from . import strategic_plan as strategic
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
    strategic_plan: strategic.StrategicPlan

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
        self.graph = physical.TrackGraph()

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

        def build_plan(phase: PhaseKind) -> strategic.StrategicPlan:
            return strategic.build_strategic_plan(
                phase=phase,
                cars=state.cars,
                depot_assignment=state.depot_assignment,
                remote_session=state.remote_session,
                remote_debt=flow_facts.remote_debt,
                depot_inbound_assembly_accepted=state.depot_inbound_assembly_accepted,
            )

        plan = build_plan(phase_state.phase)
        if (
            not state.remote_session.active
            and plan.depot_inbound_assembly_accepted
            and plan.depot_outbound.outbound_nos
            and phase_state.phase != PhaseKind.H4_REMOTE_DEPOT
        ):
            phase_state = replace(
                phase_state,
                phase=PhaseKind.H4_REMOTE_DEPOT,
                reason="depot_outbound_after_inbound_assembly_accepted",
            )
            plan = build_plan(phase_state.phase)
        elif (
            not state.remote_session.active
            and plan.depot_inbound_assembly_accepted
            and self._depot_inbound_release_pending(state.cars, state.depot_assignment)
            and phase_state.phase != PhaseKind.H4_REMOTE_DEPOT
        ):
            phase_state = replace(
                phase_state,
                phase=PhaseKind.H4_REMOTE_DEPOT,
                reason="depot_inbound_release_after_assembly_accepted",
            )
            plan = build_plan(phase_state.phase)
        elif (
            not state.remote_session.active
            and not plan.depot_inbound_assembly_accepted
            and plan.front_topology.must_finish_before_remote
            and phase_state.phase != PhaseKind.H1_FRONT_SERVICE
        ):
            phase_state = replace(
                phase_state,
                phase=PhaseKind.H1_FRONT_SERVICE,
                reason=f"front_topology_priority_before_remote:{','.join(plan.front_topology.priority_lines)}",
            )
            plan = build_plan(phase_state.phase)
        elif (
            not state.remote_session.active
            and flow_facts.remote_debt
            and phase_state.phase == PhaseKind.H1_FRONT_SERVICE
            and plan.front_topology.clear_for_remote
            and not plan.depot_inbound_assembly_accepted
            and plan.depot_inbound.ungrouped_nos
        ):
            phase_state = replace(
                phase_state,
                phase=PhaseKind.H4_REMOTE_DEPOT,
                reason="front_topology_clear_depot_inbound_priority",
            )
            plan = build_plan(phase_state.phase)
        phase_state = replace(
            phase_state,
            front_topology_clear_for_remote=plan.front_topology.clear_for_remote,
            depot_inbound_assembly_complete=plan.depot_inbound.assembly_complete,
            depot_outbound_assembly_complete=plan.depot_outbound.assembly_complete,
            strategic_plan_reason=plan.completion.reason,
        )
        remote_open = phase_state.phase == PhaseKind.H4_REMOTE_DEPOT
        return PolicyContext(
            phase_state=phase_state,
            remote_session=state.remote_session,
            remote_open=remote_open,
            last_business_remote=state.last_business_remote,
            strategic_plan=plan,
        )

    def _depot_inbound_release_pending(self, cars: list[dict[str, Any]], depot_assignment: Any) -> bool:
        loads = physical.line_loads(cars)
        for car in physical.unsatisfied_cars(cars, depot_assignment):
            if car["Line"] not in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
                return True
        return False

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
            self._depot_inbound_prospective_route_debt(candidate, context),
            self._plan_candidate_rank(candidate, context),
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
        if self.releases_depot_inbound_assembly(candidate, context):
            return 0
        if self.clears_depot_inbound_assembly_line(candidate, context):
            return 0
        if self.opens_cun4_for_depot_outbound(candidate, context):
            return 0
        if self.resolves_spotting_closeout(candidate, context):
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_ACCEPT:
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_GROUP:
            return 0
        if self.forms_depot_inbound_assembly(candidate, context):
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
        if self.clears_depot_inbound_assembly_line(candidate, context):
            return 0
        if candidate.envelope.intent == IntentKind.CUN4_RELEASE_GROUP:
            return 0
        if self.opens_cun4_for_depot_outbound(candidate, context):
            return 0
        if self.releases_depot_inbound_assembly(candidate, context):
            return 0
        if self.forms_depot_inbound_assembly(candidate, context):
            route_debt = self._depot_inbound_prospective_route_debt(candidate, context)
            if route_debt and route_debt >= self._depot_inbound_remaining_after_move_count(candidate, context):
                return 35
            if self.closes_depot_inbound_route_before_complete(candidate, context):
                return 35
            if (
                context.phase_state.phase == PhaseKind.H1_FRONT_SERVICE
                and context.strategic_plan.front_topology.must_finish_before_remote
                and candidate.resource_delta.request.source_line not in context.strategic_plan.front_topology.priority_lines
            ):
                return 25
            return 0
        if context.phase_state.phase in {PhaseKind.H1_FRONT_SERVICE, PhaseKind.H2_CUN4_PORT} and self.front_access_shaping(candidate, context):
            if (
                context.strategic_plan.depot_inbound.ungrouped_nos
                and not self._front_topology_priority_move(candidate, context)
            ):
                return 2
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

    def _front_topology_priority_move(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if not context.strategic_plan.front_topology.must_finish_before_remote:
            return False
        request = candidate.resource_delta.request
        priority_lines = set(context.strategic_plan.front_topology.priority_lines)
        return request.source_line in priority_lines or request.target_line in priority_lines

    def resolves_spotting_closeout(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase not in {PhaseKind.H4_REMOTE_DEPOT, PhaseKind.H5_CLOSEOUT}:
            return False
        if candidate.contract_delta.contract_reduction <= 0:
            return False
        request = candidate.resource_delta.request
        if not physical.is_spotting_line(request.target_line):
            return False
        return candidate.envelope.template_name == "spotting_repack"

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

    def forms_depot_inbound_assembly(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if candidate.envelope.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return False
        if candidate.contract_delta.support_gain <= 0:
            return False
        moved = set(candidate.envelope.candidate.move_car_nos)
        if moved & set(context.strategic_plan.depot_inbound.ungrouped_nos):
            return True
        if not context.strategic_plan.depot_inbound.ungrouped_nos:
            return False
        return bool(
            set(candidate.contract_delta.reduced)
            & {
                "serial_line_gate_released",
                "side_target_completion",
                "inner_target_segment_extended",
            }
        )

    def closes_depot_inbound_route_before_complete(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if candidate.envelope.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return False
        remaining = set(context.strategic_plan.depot_inbound.ungrouped_nos) - set(candidate.envelope.candidate.move_car_nos)
        if not remaining:
            return False
        after_by_no = {physical.car_no(car): car for car in candidate.prospective_cars}
        planned_lines = context.strategic_plan.depot_inbound.temporary_line_by_no
        remaining_lines = self._depot_inbound_remaining_route_lines(
            remaining=remaining,
            after_by_no=after_by_no,
            planned_lines=planned_lines,
        )
        for put_line in candidate.resource_delta.request.put_lines:
            if remaining_lines & serial.downstream_lines(put_line):
                return True
        return False

    def _depot_inbound_prospective_route_debt(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        if candidate.envelope.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return 0
        remaining = set(context.strategic_plan.depot_inbound.ungrouped_nos) - set(candidate.envelope.candidate.move_car_nos)
        if not remaining:
            return 0
        after_by_no = {physical.car_no(car): car for car in candidate.prospective_cars}
        remaining_by_line: dict[str, int] = {}
        planned_lines = context.strategic_plan.depot_inbound.temporary_line_by_no
        for no in remaining:
            car = after_by_no.get(no)
            if not car:
                continue
            for line in self._depot_inbound_remaining_route_lines(
                remaining={no},
                after_by_no=after_by_no,
                planned_lines=planned_lines,
            ):
                remaining_by_line[line] = remaining_by_line.get(line, 0) + 1
        if not remaining_by_line:
            return 0
        reachable = {
            line
            for line in remaining_by_line
            if self._line_reachable_after_depot_inbound_candidate(
                cars=candidate.prospective_cars,
                loco_line=getattr(candidate.next_loco_location, "line", ""),
                line=line,
            )
        }
        if reachable:
            return sum(count for line, count in remaining_by_line.items() if line not in reachable)
        return sum(remaining_by_line.values())

    def _depot_inbound_remaining_route_lines(
        self,
        *,
        remaining: set[str],
        after_by_no: dict[str, dict[str, Any]],
        planned_lines: dict[str, str],
    ) -> set[str]:
        lines: set[str] = set()
        for no in remaining:
            car = after_by_no.get(no)
            if car:
                lines.add(car["Line"])
            planned_line = planned_lines.get(no, "")
            if planned_line:
                lines.add(planned_line)
        return lines

    def _depot_inbound_remaining_after_move_count(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        if candidate.envelope.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return 0
        return len(
            set(context.strategic_plan.depot_inbound.ungrouped_nos)
            - set(candidate.envelope.candidate.move_car_nos)
        )

    def _line_reachable_after_depot_inbound_candidate(
        self,
        *,
        cars: list[dict[str, Any]],
        loco_line: str,
        line: str,
    ) -> bool:
        if not loco_line or not line:
            return False
        occupied = physical.occupied_lines_for_get_route(cars, set(), line)
        route = self.graph.route_avoiding_occupied(
            loco_line,
            line,
            occupied,
            source_departure_lines=physical.route_departure_lines_for_source(loco_line, cars, set()),
            target_approach_lines=physical.route_approach_lines_for_get(line),
            cars=cars,
            moving_nos=set(),
            train_length_m=0.0,
        )
        return bool(route)

    def clears_depot_inbound_assembly_line(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        dirty_nos = set(context.strategic_plan.depot_inbound.purity_violation_nos)
        if not dirty_nos:
            return False
        request = candidate.resource_delta.request
        dirty_lines = set(context.strategic_plan.depot_inbound.purity_violation_lines)
        if request.source_line not in dirty_lines:
            return False
        moved_dirty_nos = set(candidate.envelope.candidate.move_car_nos) & dirty_nos
        if not moved_dirty_nos:
            return False
        origin_lines = {
            no: step.line
            for step in physical.candidate_plan_steps(candidate.envelope.candidate)
            if step.action == "Get"
            for no in step.move_car_nos
        }
        if not any(origin_lines.get(no) in dirty_lines for no in moved_dirty_nos):
            return False
        after_by_no = {physical.car_no(car): car for car in candidate.prospective_cars}
        return all(
            after_by_no.get(no, {}).get("Line") not in physical.DEPOT_INBOUND_ASSEMBLY_LINES
            for no in moved_dirty_nos
        )

    def releases_depot_inbound_assembly(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        envelope = candidate.envelope
        request = candidate.resource_delta.request
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        if envelope.template_name != "depot_inbound_assembly_release":
            return False
        return (
            any(line in physical.DEPOT_INBOUND_DESTINATION_LINES for line in request.put_lines)
            and request.source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
            and candidate.contract_delta.contract_reduction > 0
        )

    def opens_cun4_for_depot_outbound(self, candidate: EvaluatedCandidate, context: PolicyContext) -> bool:
        if context.phase_state.phase != PhaseKind.H4_REMOTE_DEPOT:
            return False
        if not context.strategic_plan.depot_inbound.assembly_complete:
            return False
        if not context.strategic_plan.depot_outbound.outbound_nos:
            return False
        return candidate.envelope.template_name == "depot_inbound_cun4_open_release"

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

    def _plan_candidate_rank(self, candidate: EvaluatedCandidate, context: PolicyContext) -> int:
        request = candidate.resource_delta.request
        plan = context.strategic_plan
        if (
            context.phase_state.phase == PhaseKind.H1_FRONT_SERVICE
            and plan.front_topology.must_finish_before_remote
        ):
            if self.clears_depot_inbound_assembly_line(candidate, context):
                return -20
            if candidate.envelope.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
                if request.source_line in plan.front_topology.priority_lines:
                    return -10
                return 25 + self._depot_inbound_source_rank(request.source_line)
            if request.source_line in plan.front_topology.priority_lines or request.target_line in plan.front_topology.priority_lines:
                return 0
            if plan_facts.touches_remote(request):
                return 30
            return 10
        if self.clears_depot_inbound_assembly_line(candidate, context):
            return -20
        if candidate.envelope.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
            source_rank = self._depot_inbound_source_rank(request.source_line)
            return -100 + source_rank
        if self.opens_cun4_for_depot_outbound(candidate, context):
            return -30
        if self.releases_depot_inbound_assembly(candidate, context):
            return -40 if plan.depot_inbound.assembly_complete else 30
        if candidate.envelope.intent == IntentKind.CUN4_OUTBOUND_HOLD:
            moved = set(candidate.envelope.candidate.move_car_nos)
            if candidate.envelope.candidate.candidate_kind in {
                "vnext_depot_cun4_inbound_outbound_exchange",
                "vnext_depot_cun4_source_repack_exchange",
            }:
                return -20
            if candidate.envelope.candidate.candidate_kind == "vnext_depot_outbound_plan_session":
                return -10
            if moved & set(plan.depot_outbound.outbound_nos):
                return 0
        if plan.remote_session.should_continue_remote:
            return 0 if plan_facts.touches_remote(request) else 20
        return 10

    def _depot_inbound_source_rank(self, line: str) -> int:
        priority = (
            "洗罐线北",
            "洗罐站",
            "油漆线",
            "抛丸线",
            "卸轮线",
            "存5线北",
            "存5线南",
            "存4南",
            "存3线",
            "存2线",
            "存1线",
            "调梁棚",
            "调梁线北",
            "机库线",
            "预修线",
            "存4线",
            "机南",
            "机走棚",
            "机走北",
            "洗油北",
        )
        try:
            return priority.index(line)
        except ValueError:
            return len(priority)

    def _contract_key(self, contract: FlowContract, context: PolicyContext) -> tuple[int, int, int, str, str, str]:
        return (
            self._strategic_contract_rank(contract, context),
            self._phase_family_rank(contract.family, context),
            contract.priority,
            contract.family.value,
            contract.source_lines[0] if contract.source_lines else "",
            contract.target_lines[0] if contract.target_lines else "",
        )

    def _contract_phase_key(self, contract: FlowContract, context: PolicyContext) -> tuple[int, int, int, str]:
        return (
            self._strategic_contract_rank(contract, context),
            self._phase_family_rank(contract.family, context),
            contract.priority,
            contract.family.value,
        )

    def _strategic_contract_rank(self, contract: FlowContract, context: PolicyContext) -> int:
        plan = context.strategic_plan
        if plan.depot_inbound.purity_violation_nos:
            lines = set(contract.source_lines)
            if lines & set(plan.depot_inbound.purity_violation_lines):
                if set(contract.subject_nos) & set(plan.depot_inbound.purity_violation_nos):
                    return 0
        if (
            context.phase_state.phase == PhaseKind.H1_FRONT_SERVICE
            and plan.front_topology.must_finish_before_remote
        ):
            lines = set(contract.source_lines) | set(contract.target_lines)
            if lines & set(plan.front_topology.priority_lines):
                return 0
            if any(line in physical.REMOTE_INTERACTION_LINES for line in lines):
                return 30
            return 10
        if contract.family == ContractFamily.REMOTE_SESSION and plan.depot_outbound.outbound_nos:
            if set(contract.subject_nos) & set(plan.depot_outbound.outbound_nos):
                return 0
        if contract.family == ContractFamily.REMOTE_SESSION and plan.depot_inbound.ungrouped_nos:
            if set(contract.subject_nos) & set(plan.depot_inbound.ungrouped_nos):
                return 0
        return 10

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
