from __future__ import annotations

from dataclasses import asdict, replace
from inspect import signature
from pathlib import Path
from typing import Any

from . import depot_inbound_plan
from . import depot_outbound_plan
from . import physical
from . import release
from .connection import ConnectionMetricRecord
from .connection import records_for_selected as connection_records_for_selected
from .contracts import build_contracts
from .diagnostics import (
    CandidateRoundStats,
    GenerationGapRecord,
    StructureNodeMetricRecord,
    build_generation_gap_records,
    build_structure_node_record,
)
from .delta import build_contract_delta, simulate_candidate
from .domain import CaseResult, CandidateEnvelope, IntentKind, RemoteSessionState, ResourceDelta, SolverState, StepTrace
from .episodes import EPISODES
from .flow import FlowEdgeRecord, build_flow_edge_records
from .frontier import AccessFrontier, AccessFrontierRecord
from .gate import AcceptRejectGate
from .phase import HumanPhaseGate, PhaseGateRecord
from .policy import BaselinePolicy, EvaluatedCandidate, PolicyContext
from .resource_structures import (
    ResourceStructureRecord,
    hook_resource_records,
    next_serial_gate_leases,
    selected_resource_records,
)
from .resources import StationResourceGraph
from .staging import StagingIntentBuilder, StagingIntentRecord


def _generate_episode_candidates(
    *,
    episode: Any,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
    contract: Any,
    strategic_plan: Any,
) -> Any:
    kwargs = {
        "case_id": case_id,
        "hook_index": hook_index,
        "cars": cars,
        "depot_assignment": depot_assignment,
        "graph": graph,
        "loco_location": loco_location,
        "serial_gate_leases": serial_gate_leases,
        "contract": contract,
    }
    if "strategic_plan" in signature(episode.generate).parameters:
        kwargs["strategic_plan"] = strategic_plan
    return episode.generate(**kwargs)


def _remote_unsatisfied_count(cars: list[dict[str, Any]], depot_assignment: Any) -> int:
    loads = physical.line_loads(cars)
    count = 0
    for car in physical.unsatisfied_cars(cars, depot_assignment):
        target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
        if car["Line"] in physical.REMOTE_INTERACTION_LINES or target_line in physical.REMOTE_INTERACTION_LINES:
            count += 1
    return count


def _strict_depot_inbound_assembly_complete(cars: list[dict[str, Any]], depot_assignment: Any) -> bool:
    cun4_state = release.cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    depot_outbound = depot_outbound_plan.build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        cun4_released_nos=set(cun4_state.release_nos),
    )
    cun4_outbound_hold_nos = set(cun4_state.outbound_hold_nos) | {
        no
        for no, line in depot_outbound.temporary_line_by_no.items()
        if line == "存4线"
    }
    plan = depot_inbound_plan.build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        cun4_outbound_hold_nos=cun4_outbound_hold_nos,
        depot_outbound_nos=set(depot_outbound.outbound_nos),
        strict_cun4_unwheel_only=True,
    )
    return plan.assembly_complete


def _trace_row(
    *,
    state: SolverState,
    policy_context: PolicyContext,
    envelope: CandidateEnvelope,
    gate_accepted: bool,
    selected: bool,
    gate_reason: str,
    physical_reasons: tuple[str, ...],
    contract_delta: Any | None,
    resource_delta: Any | None,
    phase_name: str = "",
    phase_reason: str = "",
) -> StepTrace:
    candidate = envelope.candidate
    request = resource_delta.request if resource_delta else envelope.resource_request
    phase_state = policy_context.phase_state
    return StepTrace(
        case_id=state.case_id,
        hook_index=state.hook_index,
        phase=phase_name or phase_state.phase.value,
        phase_reason=phase_reason or phase_state.reason,
        phase_front_debt=phase_state.front_debt,
        phase_cun4_port_debt=phase_state.cun4_port_debt,
        phase_remote_debt=phase_state.remote_debt,
        phase_closeout_debt=phase_state.closeout_debt,
        remote_session_open=policy_context.remote_session_open,
        remote_session_id=policy_context.remote_session.session_id,
        remote_session_owner=policy_context.remote_session.owner_contract_id,
        remote_session_mode=policy_context.remote_session.mode,
        candidate_id=candidate.candidate_id,
        contract_id=envelope.contract.contract_id,
        family=envelope.contract.family.value,
        intent=envelope.intent.value,
        template_name=envelope.template_name,
        source_line=candidate.source_line,
        target_line=request.target_line,
        touched_lines="|".join(request.touched_lines),
        put_lines="|".join(request.put_lines),
        move_nos="|".join(candidate.move_car_nos),
        same_plan_source_return_nos="|".join(request.same_plan_source_return_nos),
        gate_accepted=gate_accepted,
        selected=selected,
        gate_reason=gate_reason,
        physical_reasons="|".join(physical_reasons),
        requested_resources="|".join(resource.value for resource in request.resources),
        acquired_resources=(
            "|".join(resource.value for resource in resource_delta.acquired)
            if resource_delta
            else ""
        ),
        resource_violations=(
            "|".join(resource_delta.violations)
            if resource_delta
            else ""
        ),
        before_unsatisfied=contract_delta.before_unsatisfied if contract_delta else 0,
        after_unsatisfied=contract_delta.after_unsatisfied if contract_delta else 0,
        before_contract_debt=contract_delta.before_contract_debt if contract_delta else 0,
        after_contract_debt=contract_delta.after_contract_debt if contract_delta else 0,
        contract_reduction=contract_delta.contract_reduction if contract_delta else 0,
        support_gain=contract_delta.support_gain if contract_delta else 0,
        effective_gain=contract_delta.effective_gain if contract_delta else 0,
        total_reduction=contract_delta.total_reduction if contract_delta else 0,
    )


class VNextSolver:
    def __init__(
        self,
        max_hooks: int = 300,
        trace_all_candidates: bool = False,
        trace_frontier: bool = False,
    ) -> None:
        self.max_hooks = max_hooks
        self.trace_all_candidates = trace_all_candidates
        self.trace_frontier = trace_frontier
        self.graph = physical.TrackGraph()
        self.resources = StationResourceGraph()
        self.gate = AcceptRejectGate()
        self.frontier = AccessFrontier()
        self.phase_gate = HumanPhaseGate()
        self.staging = StagingIntentBuilder()
        self.policy = BaselinePolicy()

    def solve_case(
        self,
        truth_path: Path,
        output_dir: Path,
    ) -> tuple[
        CaseResult,
        list[StepTrace],
        list[Any],
        list[PhaseGateRecord],
        list[AccessFrontierRecord],
        list[StagingIntentRecord],
        list[FlowEdgeRecord],
        list[ConnectionMetricRecord],
        list[StructureNodeMetricRecord],
        list[ResourceStructureRecord],
        list[GenerationGapRecord],
    ]:
        case_id, _payload, cars, depot_assignment, loco_location = physical.read_case(truth_path)
        state = SolverState(case_id=case_id, cars=cars, depot_assignment=depot_assignment, loco_location=loco_location)
        state.depot_inbound_assembly_accepted = _strict_depot_inbound_assembly_complete(
            state.cars,
            state.depot_assignment,
        )
        state.visited_signatures.add(physical.state_signature(state.cars, state.loco_location))
        initial_unsatisfied = len(physical.unsatisfied_cars(state.cars, state.depot_assignment))
        traces: list[StepTrace] = []
        phase_records: list[PhaseGateRecord] = []
        frontier_records: list[AccessFrontierRecord] = []
        staging_records: list[StagingIntentRecord] = []
        flow_edge_records: list[FlowEdgeRecord] = []
        connection_records: list[ConnectionMetricRecord] = []
        structure_node_records: list[StructureNodeMetricRecord] = []
        resource_structure_records: list[ResourceStructureRecord] = []
        generation_gap_records: list[GenerationGapRecord] = []
        operations: list[Any] = []
        blocked_reason = ""
        hard_physical_accepted = 0
        accepted_without_phase_permission = 0
        phase_gate_bypass = 0
        previous_phase = ""
        hook_count_by_phase: dict[str, int] = {}
        last_policy_context: PolicyContext | None = None

        while state.hook_index <= self.max_hooks:
            if not physical.unsatisfied_cars(state.cars, state.depot_assignment):
                break
            physical.clear_access_order_cache()
            selected: EvaluatedCandidate | None = None
            raw_policy_context = self.policy.context(state)
            current_phase = self.phase_gate.active_phase(
                previous_phase=previous_phase,
                proposed=raw_policy_context.phase_state,
            )

            def context_for_phase(phase_code: str) -> PolicyContext:
                phase_state = replace(
                    raw_policy_context.phase_state,
                    phase=self.phase_gate.phase_kind(phase_code),
                )
                return PolicyContext(
                    phase_state=phase_state,
                    remote_session=raw_policy_context.remote_session,
                    remote_open=phase_code == "H4",
                    last_business_remote=raw_policy_context.last_business_remote,
                    strategic_plan=replace(raw_policy_context.strategic_plan, phase=phase_state.phase),
                )

            policy_context = context_for_phase(current_phase)
            active_phase_state = policy_context.phase_state
            last_policy_context = policy_context
            hook_flow_edges = build_flow_edge_records(
                case_id=state.case_id,
                hook_index=state.hook_index,
                cars=state.cars,
                depot_assignment=state.depot_assignment,
            )
            flow_edge_records.extend(hook_flow_edges)
            resource_structure_records.extend(
                hook_resource_records(
                    case_id=state.case_id,
                    hook_index=state.hook_index,
                    cars=state.cars,
                    depot_assignment=state.depot_assignment,
                    serial_gate_leases=state.serial_gate_leases,
                    strategic_plan=policy_context.strategic_plan,
                )
            )
            if self.trace_frontier:
                frontier_records.append(
                    self.frontier.snapshot(
                        case_id=state.case_id,
                        hook_index=state.hook_index,
                        cars=state.cars,
                        depot_assignment=state.depot_assignment,
                        graph=self.graph,
                        loco_location=state.loco_location,
                    )
                )
            all_contracts = build_contracts(state.cars, state.depot_assignment)
            contracts = self.policy.order_contracts(all_contracts, policy_context)
            if not contracts:
                blocked_reason = "no_active_contract"
                break
            rejected_this_round = 0
            generated_this_round = 0

            def evaluate_episodes(episodes: tuple[Any, ...]) -> tuple[EvaluatedCandidate | None, int, int, CandidateRoundStats]:
                selected_candidate: EvaluatedCandidate | None = None
                rejected_count = 0
                generated_count = 0
                stats = CandidateRoundStats()
                validation_cache: dict[str, Any] = {}
                prospective_cache: dict[str, tuple[list[dict[str, Any]], Any, tuple[str, str, tuple[tuple[str, str, int], ...]]]] = {}
                for contract in contracts:
                    for episode in episodes:
                        if not episode.applies(contract):
                            continue
                        for envelope in _generate_episode_candidates(
                            episode=episode,
                            case_id=state.case_id,
                            hook_index=state.hook_index,
                            cars=state.cars,
                            depot_assignment=state.depot_assignment,
                            graph=self.graph,
                            loco_location=state.loco_location,
                            serial_gate_leases=state.serial_gate_leases,
                            contract=contract,
                            strategic_plan=policy_context.strategic_plan,
                        ):
                            generated_count += 1
                            stats.generated()
                            resource_request = self.resources.request_for(envelope)
                            envelope = CandidateEnvelope(
                                candidate=envelope.candidate,
                                contract=envelope.contract,
                                intent=envelope.intent,
                                resource_request=resource_request,
                                template_name=envelope.template_name,
                            )
                            validation = validation_cache.get(envelope.candidate.candidate_id)
                            if validation is None:
                                validation = physical.validate_candidate(
                                    self.graph,
                                    envelope.candidate,
                                    state.cars,
                                    state.loco_location,
                                    state.depot_assignment,
                                )
                                validation_cache[envelope.candidate.candidate_id] = validation
                            if validation.reasons:
                                resource_delta = ResourceDelta(
                                    request=resource_request,
                                    acquired=(),
                                    released_lines=(),
                                    violations=tuple(f"physical:{reason}" for reason in validation.reasons),
                                )
                                if self.trace_all_candidates:
                                    traces.append(
                                        _trace_row(
                                            state=state,
                                            policy_context=policy_context,
                                            envelope=envelope,
                                            gate_accepted=False,
                                            selected=False,
                                            gate_reason="physical_reject",
                                            physical_reasons=tuple(validation.reasons),
                                            contract_delta=None,
                                            resource_delta=resource_delta,
                                        )
                                    )
                                rejected_count += 1
                                stats.rejected("physical_reject")
                                continue
                            resource_delta = self.resources.acquire(
                                resource_request,
                                candidate=envelope.candidate,
                                validation=validation,
                                cars=state.cars,
                                depot_assignment=state.depot_assignment,
                                serial_gate_leases=state.serial_gate_leases,
                            )
                            cached_prospective = prospective_cache.get(envelope.candidate.candidate_id)
                            if cached_prospective is None:
                                prospective = simulate_candidate(envelope.candidate, state.cars, validation)
                                next_loco_location = physical.next_loco_location(envelope.candidate, validation)
                                prospective_signature = physical.state_signature(prospective, next_loco_location)
                                cached_prospective = (prospective, next_loco_location, prospective_signature)
                                prospective_cache[envelope.candidate.candidate_id] = cached_prospective
                            prospective, next_loco_location, prospective_signature = cached_prospective
                            contract_delta = build_contract_delta(
                                envelope,
                                cars=state.cars,
                                prospective_cars=prospective,
                                depot_assignment=state.depot_assignment,
                                strategic_plan=policy_context.strategic_plan,
                            )
                            decision = self.gate.decide(
                                contract_delta,
                                resource_delta,
                                strategic_plan=policy_context.strategic_plan,
                                candidate=envelope.candidate,
                            )
                            if decision.accepted and prospective_signature in state.visited_signatures:
                                decision = self.gate.loop_reject(contract_delta, resource_delta)
                            gate_accepted = decision.accepted
                            gate_reason = decision.reason
                            if gate_accepted:
                                phase_permission = self.phase_gate.permission(
                                    phase_state=active_phase_state,
                                    envelope=envelope,
                                    contract_delta=contract_delta,
                                    resource_delta=resource_delta,
                                    remote_session=policy_context.remote_session,
                                )
                                if not phase_permission.allowed:
                                    gate_accepted = False
                                    gate_reason = f"phase_veto:{phase_permission.reason}"
                            if self.trace_all_candidates:
                                traces.append(
                                    _trace_row(
                                        state=state,
                                        policy_context=policy_context,
                                        envelope=envelope,
                                        gate_accepted=gate_accepted,
                                        selected=False,
                                        gate_reason=gate_reason,
                                        physical_reasons=(),
                                        contract_delta=contract_delta,
                                        resource_delta=resource_delta,
                                    )
                                )
                            if not gate_accepted:
                                rejected_count += 1
                                stats.rejected(gate_reason)
                                continue
                            stats.accepted()
                            evaluated = EvaluatedCandidate(
                                envelope=envelope,
                                validation=validation,
                                prospective_cars=prospective,
                                contract_delta=contract_delta,
                                resource_delta=resource_delta,
                                next_loco_location=next_loco_location,
                                prospective_signature=prospective_signature,
                            )
                            if self.policy.better(evaluated, selected_candidate, policy_context):
                                selected_candidate = evaluated
                return selected_candidate, generated_count, rejected_count, stats

            selected, generated_this_round, rejected_this_round, round_stats = evaluate_episodes(EPISODES)
            while selected is None and generated_this_round > 0:
                next_phase = self.phase_gate.next_phase_after_exhaustion(
                    phase_state=active_phase_state,
                    current_phase=current_phase,
                )
                if not next_phase or next_phase == current_phase:
                    break
                structure_node_records.append(
                    build_structure_node_record(
                        state=state,
                        policy_context=policy_context,
                        flow_edges=hook_flow_edges,
                        contracts=contracts,
                        stats=round_stats,
                        selected=None,
                        blocked_reason=(
                            f"phase_exhausted:{current_phase}->{next_phase};"
                            f"{round_stats.top_reject_reasons()}"
                        ),
                    )
                )
                phase_records.append(
                    self.phase_gate.record(
                        case_id=state.case_id,
                        step_index=state.hook_index,
                        previous_phase=current_phase,
                        current_phase=next_phase,
                        phase_state=active_phase_state,
                        envelope=None,
                        contract_delta=None,
                        permission=None,
                        hook_count_in_phase=hook_count_by_phase.get(next_phase, 0),
                        reject_reason=f"phase_exhausted:{current_phase}->{next_phase}",
                        transition_override=self.phase_gate.transition_type(current_phase, next_phase),
                    )
                )
                current_phase = next_phase
                previous_phase = current_phase
                policy_context = context_for_phase(current_phase)
                active_phase_state = policy_context.phase_state
                last_policy_context = policy_context
                contracts = self.policy.order_contracts(all_contracts, policy_context)
                selected, generated_this_round, rejected_this_round, round_stats = evaluate_episodes(EPISODES)
            if selected is None:
                blocked_reason = (
                    "no_episode_candidate_generated"
                    if generated_this_round == 0
                    else f"all_episode_candidates_rejected:{rejected_this_round}"
                )
                if generated_this_round == 0:
                    frontier_record = None
                    if frontier_records and frontier_records[-1].case_id == state.case_id and frontier_records[-1].hook_index == state.hook_index:
                        frontier_record = frontier_records[-1]
                    else:
                        frontier_record = self.frontier.snapshot(
                            case_id=state.case_id,
                            hook_index=state.hook_index,
                            cars=state.cars,
                            depot_assignment=state.depot_assignment,
                            graph=self.graph,
                            loco_location=state.loco_location,
                        )
                    generation_gap_records.extend(
                        build_generation_gap_records(
                            state=state,
                            policy_context=policy_context,
                            contracts=contracts,
                            episodes=EPISODES,
                            frontier_record=frontier_record,
                        )
                    )
                structure_node_records.append(
                    build_structure_node_record(
                        state=state,
                        policy_context=policy_context,
                        flow_edges=hook_flow_edges,
                        contracts=contracts,
                        stats=round_stats,
                        selected=None,
                        blocked_reason=blocked_reason,
                    )
                )
                break

            envelope = selected.envelope
            validation = selected.validation
            prospective = selected.prospective_cars
            next_loco_location = selected.next_loco_location
            prospective_signature = selected.prospective_signature
            selected_trace_written = False
            if self.trace_all_candidates:
                for index in range(len(traces) - 1, -1, -1):
                    if traces[index].candidate_id == envelope.candidate.candidate_id and traces[index].hook_index == state.hook_index:
                        trace = traces[index]
                        traces[index] = StepTrace(
                            **{**asdict(trace), "selected": True}
                        )
                        selected_trace_written = True
                        break
            if validation.reasons:
                hard_physical_accepted += 1
            phase_permission = self.phase_gate.permission(
                phase_state=active_phase_state,
                envelope=envelope,
                contract_delta=selected.contract_delta,
                resource_delta=selected.resource_delta,
                remote_session=policy_context.remote_session,
            )
            if not phase_permission.allowed:
                accepted_without_phase_permission += 1
            execution_phase = self.phase_gate.execution_phase(
                current_phase=current_phase,
                permission=phase_permission,
            )
            hook_count_by_phase[execution_phase] = hook_count_by_phase.get(execution_phase, 0) + 1
            if not selected_trace_written:
                traces.append(
                    _trace_row(
                        state=state,
                        policy_context=policy_context,
                        envelope=envelope,
                        gate_accepted=True,
                        selected=True,
                        gate_reason="accepted",
                        physical_reasons=(),
                        contract_delta=selected.contract_delta,
                        resource_delta=selected.resource_delta,
                        phase_name=self.phase_gate.phase_kind(execution_phase).value,
                        phase_reason=f"{active_phase_state.reason};execution_target={phase_permission.target_phase}",
                    )
                )
            elif self.trace_all_candidates:
                for index in range(len(traces) - 1, -1, -1):
                    if traces[index].candidate_id == envelope.candidate.candidate_id and traces[index].hook_index == state.hook_index:
                        trace = traces[index]
                        traces[index] = StepTrace(
                            **{
                                **asdict(trace),
                                "phase": self.phase_gate.phase_kind(execution_phase).value,
                                "phase_reason": f"{active_phase_state.reason};execution_target={phase_permission.target_phase}",
                            }
                        )
                        break
            phase_record = self.phase_gate.record(
                case_id=state.case_id,
                step_index=state.hook_index,
                previous_phase=previous_phase,
                current_phase=execution_phase,
                phase_state=active_phase_state,
                envelope=envelope,
                contract_delta=selected.contract_delta,
                permission=phase_permission,
                hook_count_in_phase=hook_count_by_phase[execution_phase],
            )
            if phase_record.transition_type == "fail":
                phase_gate_bypass += 1
            phase_records.append(phase_record)
            connection_records.extend(
                connection_records_for_selected(
                    case_id=state.case_id,
                    hook_index=state.hook_index,
                    envelope=envelope,
                    contract_delta=selected.contract_delta,
                    resource_delta=selected.resource_delta,
                    phase_permission=phase_permission,
                )
            )
            resource_structure_records.extend(
                selected_resource_records(
                    case_id=state.case_id,
                    hook_index=state.hook_index,
                    cars=state.cars,
                    prospective_cars=prospective,
                    depot_assignment=state.depot_assignment,
                    envelope=envelope,
                    resource_delta=selected.resource_delta,
                    contract_delta=selected.contract_delta,
                    serial_gate_leases=state.serial_gate_leases,
                )
            )
            structure_node_records.append(
                build_structure_node_record(
                    state=state,
                    policy_context=policy_context,
                    flow_edges=hook_flow_edges,
                    contracts=contracts,
                    stats=round_stats,
                    selected=selected,
                )
            )
            staging_records.extend(
                self.staging.records_for_selected(
                    case_id=state.case_id,
                    hook_index=state.hook_index,
                    phase=execution_phase,
                    envelope=envelope,
                    resource_delta=selected.resource_delta,
                    phase_permission=phase_permission,
                )
            )
            previous_phase = execution_phase
            start_operation_index = len(operations) + 1
            operations.extend(physical.operation_rows(envelope.candidate, validation, start_operation_index))
            state.serial_gate_leases = next_serial_gate_leases(
                case_id=state.case_id,
                hook_index=state.hook_index,
                cars=state.cars,
                prospective_cars=prospective,
                depot_assignment=state.depot_assignment,
                envelope=envelope,
                contract_delta=selected.contract_delta,
                serial_gate_leases=state.serial_gate_leases,
            )
            state.cars = prospective
            state.loco_location = next_loco_location
            if not state.depot_inbound_assembly_accepted:
                state.depot_inbound_assembly_accepted = _strict_depot_inbound_assembly_complete(
                    state.cars,
                    state.depot_assignment,
                )
            state.visited_signatures.add(prospective_signature)
            touched_remote = any(
                line in physical.REMOTE_INTERACTION_LINES
                for line in envelope.resource_request.touched_lines
            )
            state.remote_session = self._next_remote_session_state(
                case_id=state.case_id,
                hook_index=state.hook_index,
                current_state=state.remote_session,
                selected=selected,
                phase_permission=phase_permission,
                touched_remote=touched_remote,
                remote_debt=policy_context.phase_state.remote_debt,
            )
            business_lines = [
                step.line
                for step in physical.candidate_plan_steps(envelope.candidate)
                if step.action in {"Get", "Put"}
            ]
            if business_lines:
                state.last_business_remote = business_lines[-1] in physical.REMOTE_INTERACTION_LINES
            state.accepted_candidate_ids.add(envelope.candidate.candidate_id)
            state.hook_index += 1

        final_unsatisfied = len(physical.unsatisfied_cars(state.cars, state.depot_assignment))
        final_length_warnings = physical.final_line_length_warnings(state.cars, baseline_cars=cars)
        closed_door_reasons = physical.closed_door_replay_violation_reasons(operations, state.cars)
        if closed_door_reasons and not blocked_reason:
            blocked_reason = "|".join(closed_door_reasons)
        if final_length_warnings and not blocked_reason:
            blocked_reason = "final_line_length_violation:" + "|".join(final_length_warnings)
        status = "completed" if final_unsatisfied == 0 and not blocked_reason else "blocked"
        if state.hook_index > self.max_hooks and final_unsatisfied:
            blocked_reason = "max_hook_limit_reached"
            status = "blocked"
        if status == "blocked":
            final_context = last_policy_context or self.policy.context(state)
            current_phase = previous_phase or self.phase_gate.phase_code(final_context.phase_state.phase)
            phase_records.append(
                self.phase_gate.fail_record(
                    case_id=state.case_id,
                    step_index=state.hook_index,
                    phase_state=final_context.phase_state,
                    current_phase=current_phase,
                    blocked_reason=blocked_reason or "blocked",
                    hook_count_in_phase=hook_count_by_phase.get(current_phase, 0),
                )
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        if operations:
            response_path = output_dir / "responses" / f"{case_id}.json"
            physical.write_json(
                response_path,
                {
                    "Success": status == "completed",
                    "Message": "" if status == "completed" else blocked_reason,
                    "StatusCode": 200 if status == "completed" else 409,
                    "Data": {
                        "Operations": [physical.response_operation(row) for row in operations],
                        "GeneratedEndStatus": [
                            {
                                "No": physical.car_no(car),
                                "Line": car["Line"],
                                "Position": int(car.get("Position") or 0),
                            }
                            for car in sorted(state.cars, key=physical.car_no)
                        ],
                    },
                },
            )

        accepted_traces = [trace for trace in traces if trace.selected]
        result = CaseResult(
            case_id=case_id,
            status=status,
            hook_count=sum(1 for row in operations if row.action in {"Get", "Put"}),
            operation_count=len(operations),
            remote_business_transition_count=physical.operation_remote_business_transition_count(operations),
            initial_unsatisfied=initial_unsatisfied,
            final_unsatisfied=final_unsatisfied,
            final_length_warning_count=len(final_length_warnings),
            blocked_reason=blocked_reason,
            step_count=len(accepted_traces),
            accepted_without_phase_permission_count=accepted_without_phase_permission,
            phase_gate_bypass_count=phase_gate_bypass,
            hard_physical_violation_accepted_count=hard_physical_accepted,
        )
        return (
            result,
            traces,
            operations,
            phase_records,
            frontier_records,
            staging_records,
            flow_edge_records,
            connection_records,
            structure_node_records,
            resource_structure_records,
            generation_gap_records,
        )

    def _next_remote_session_state(
        self,
        *,
        case_id: str,
        hook_index: int,
        current_state: RemoteSessionState,
        selected: EvaluatedCandidate,
        phase_permission: Any,
        touched_remote: bool,
        remote_debt: int,
    ) -> RemoteSessionState:
        should_be_active = bool(
            remote_debt
            and (
                current_state.active
                or touched_remote
                or (
                    phase_permission.allowed
                    and phase_permission.target_phase in {"H3", "H4"}
                    and selected.envelope.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY
                )
                or self.policy.opens_remote_session(selected)
            )
        )
        if not should_be_active:
            return RemoteSessionState()

        contract = selected.envelope.contract
        request = selected.resource_delta.request
        source_lines = tuple(dict.fromkeys((*current_state.source_lines, *contract.source_lines, request.source_line)))
        target_lines = tuple(dict.fromkeys((*current_state.target_lines, *contract.target_lines, request.target_line)))
        owner_contract_id = current_state.owner_contract_id or contract.contract_id
        session_id = current_state.session_id or f"{case_id}:remote:{owner_contract_id}:{current_state.opened_hook or hook_index}"
        opened_hook = current_state.opened_hook or hook_index
        return RemoteSessionState(
            active=True,
            session_id=session_id,
            owner_contract_id=owner_contract_id,
            opened_hook=opened_hook,
            last_touched_hook=hook_index,
            source_lines=source_lines,
            target_lines=target_lines,
            debt_nos=tuple(contract.subject_nos),
            mode=phase_permission.target_phase if getattr(phase_permission, "target_phase", "") else contract.family.value,
        )


def write_artifacts(
    output_dir: Path,
    results: list[CaseResult],
    traces: list[StepTrace],
    operations: list[Any],
    phase_records: list[PhaseGateRecord],
    frontier_records: list[AccessFrontierRecord],
    staging_records: list[StagingIntentRecord],
    flow_edge_records: list[FlowEdgeRecord],
    connection_records: list[ConnectionMetricRecord],
    structure_node_records: list[StructureNodeMetricRecord],
    resource_structure_records: list[ResourceStructureRecord],
    generation_gap_records: list[GenerationGapRecord],
) -> None:
    physical.write_csv(output_dir / "case_summary.csv", [asdict(row) for row in results])
    physical.write_csv(output_dir / "step_trace.csv", [asdict(row) for row in traces])
    physical.write_csv(output_dir / "operation_trace.csv", [asdict(row) for row in operations])
    physical.write_csv(output_dir / "phase_gate_records.csv", [asdict(row) for row in phase_records])
    physical.write_csv(output_dir / "access_frontier_records.csv", [asdict(row) for row in frontier_records])
    physical.write_csv(output_dir / "staging_intent_records.csv", [asdict(row) for row in staging_records])
    physical.write_csv(output_dir / "flow_edge_records.csv", [asdict(row) for row in flow_edge_records])
    physical.write_csv(output_dir / "connection_metrics.csv", [asdict(row) for row in connection_records])
    physical.write_csv(output_dir / "structure_node_metrics.csv", [asdict(row) for row in structure_node_records])
    physical.write_csv(output_dir / "resource_structure_records.csv", [asdict(row) for row in resource_structure_records])
    physical.write_csv(output_dir / "generation_gap_records.csv", [asdict(row) for row in generation_gap_records])
    physical.write_csv(
        output_dir / "structure_acceptance.csv",
        _structure_acceptance_rows(
            results=results,
            traces=traces,
            phase_records=phase_records,
            frontier_records=frontier_records,
            staging_records=staging_records,
            flow_edge_records=flow_edge_records,
            connection_records=connection_records,
            structure_node_records=structure_node_records,
            resource_structure_records=resource_structure_records,
        ),
    )

    route_blocked_counts = [row.route_blocked_line_count for row in frontier_records]
    serial_blocker_counts = [row.serial_blocker_line_count for row in frontier_records]
    summary = {
        "case_count": len(results),
        "completed": sum(1 for row in results if row.status == "completed"),
        "blocked": sum(1 for row in results if row.status == "blocked"),
        "accepted_without_phase_permission_count": sum(row.accepted_without_phase_permission_count for row in results),
        "phase_gate_bypass_count": sum(row.phase_gate_bypass_count for row in results),
        "hard_physical_violation_accepted_count": sum(row.hard_physical_violation_accepted_count for row in results),
        "final_length_warning_count": sum(row.final_length_warning_count for row in results),
        "hook_count": sum(row.hook_count for row in results),
        "remote_business_transition_count": sum(row.remote_business_transition_count for row in results),
        "phase_gate_record_count": len(phase_records),
        "access_frontier_record_count": len(frontier_records),
        "staging_intent_record_count": len(staging_records),
        "flow_edge_record_count": len(flow_edge_records),
        "connection_metric_record_count": len(connection_records),
        "connection_metric_failure_count": sum(1 for row in connection_records if row.status != "pass"),
        "structure_node_metric_record_count": len(structure_node_records),
        "resource_structure_record_count": len(resource_structure_records),
        "resource_structure_failure_count": sum(1 for row in resource_structure_records if row.status == "fail"),
        "resource_structure_warning_count": sum(1 for row in resource_structure_records if row.status == "warn"),
        "generation_gap_record_count": len(generation_gap_records),
        "max_route_blocked_line_count": max(route_blocked_counts, default=0),
        "max_serial_blocker_line_count": max(serial_blocker_counts, default=0),
    }
    physical.write_json(output_dir / "vnext_summary.json", summary)


def _structure_acceptance_rows(
    *,
    results: list[CaseResult],
    traces: list[StepTrace],
    phase_records: list[PhaseGateRecord],
    frontier_records: list[AccessFrontierRecord],
    staging_records: list[StagingIntentRecord],
    flow_edge_records: list[FlowEdgeRecord],
    connection_records: list[ConnectionMetricRecord],
    structure_node_records: list[StructureNodeMetricRecord],
    resource_structure_records: list[ResourceStructureRecord],
) -> list[dict[str, Any]]:
    accepted_without_phase = sum(row.accepted_without_phase_permission_count for row in results)
    phase_bypass = sum(row.phase_gate_bypass_count for row in results)
    hard_physical = sum(row.hard_physical_violation_accepted_count for row in results)
    connection_fail = sum(1 for row in connection_records if row.status != "pass")
    resource_fail_by_structure = _resource_status_counts(resource_structure_records, "fail")
    resource_warn_by_structure = _resource_status_counts(resource_structure_records, "warn")
    variants = sorted({row.variant for row in flow_edge_records if row.variant})
    rows = [
        _acceptance_row(
            "FlowFacts/FlowEdge",
            "pass" if flow_edge_records and variants else "fail",
            len(flow_edge_records),
            0 if flow_edge_records and variants else 1,
            0,
            "flow_edge_records.csv",
            "variants=" + ",".join(variants),
        ),
        _acceptance_row(
            "HumanPhaseGate",
            "pass" if accepted_without_phase == 0 and phase_bypass == 0 else "fail",
            len(phase_records),
            accepted_without_phase + phase_bypass,
            0,
            "phase_gate_records.csv",
            f"accepted_without_phase={accepted_without_phase};phase_bypass={phase_bypass}",
        ),
        _acceptance_row(
            "ConnectionChain",
            "pass" if connection_fail == 0 else "fail",
            len(connection_records),
            connection_fail,
            0,
            "connection_metrics.csv",
            "selected steps have contract/resource/delta/phase connection",
        ),
        _acceptance_row(
            "StructureNodeDiagnostics",
            "pass" if structure_node_records else "fail",
            len(structure_node_records),
            0 if structure_node_records else 1,
            0,
            "structure_node_metrics.csv",
            "per-hook candidate and reject bucket coverage",
        ),
        _acceptance_row(
            "AcceptedPhysicalGate",
            "pass" if hard_physical == 0 else "fail",
            len(results),
            hard_physical,
            0,
            "case_summary.csv",
            "physical validator accepted violations",
        ),
    ]
    rows.extend(
        _candidate_structure_acceptance_rows(
            traces=traces,
            frontier_records=frontier_records,
            staging_records=staging_records,
            phase_records=phase_records,
        )
    )
    for structure in (
        "FRONT_TOPOLOGY_PLAN",
        "CUN4_NORTH_BUFFER",
        "CUN4_NORTH_BUFFER_DELTA",
        "CUN4_RELEASE_PORT_PLAN",
        "DEPOT_SLOT_GRAPH",
        "DEPOT_SLOT_DELTA",
        "DEPOT_SWAP_DELTA",
        "DEPOT_OUTBOUND_ASSEMBLY_PLAN",
        "REMOTE_SESSION_CONTINUITY_PLAN",
        "PHASE_COMPLETION_PLAN",
        "SERIAL_GATE_LEASE",
        "SERIAL_GATE_LEASE_LIFECYCLE",
        "LOCO_CARRY_STATE",
    ):
        fail_count = resource_fail_by_structure.get(structure, 0)
        warn_count = resource_warn_by_structure.get(structure, 0)
        observed_count = sum(1 for row in resource_structure_records if row.structure == structure)
        status = "pass"
        if fail_count:
            status = "fail"
        elif warn_count:
            status = "warn"
        elif observed_count == 0:
            status = "unknown"
        rows.append(
            _acceptance_row(
                structure,
                status,
                observed_count,
                fail_count,
                warn_count,
                "resource_structure_records.csv",
                "resource structure coverage and hard-failure count",
            )
        )
    return rows


def _candidate_structure_acceptance_rows(
    *,
    traces: list[StepTrace],
    frontier_records: list[AccessFrontierRecord],
    staging_records: list[StagingIntentRecord],
    phase_records: list[PhaseGateRecord],
) -> list[dict[str, Any]]:
    expected_templates = {
        "depot_cun4_inbound_outbound_exchange",
        "depot_outbound_session",
        "depot_slot_swap",
        "direct_accessible_prefix",
        "remote_session_directional_digest",
        "tail_blocker_peel_digest",
        "tail_closeout_direct_accessible_prefix",
    }
    observed_templates = {row.template_name for row in traces if row.template_name}
    selected_templates = {row.template_name for row in traces if row.selected and row.template_name}
    unobserved = sorted(expected_templates - observed_templates)
    unselected = sorted((expected_templates & observed_templates) - selected_templates)
    trace_all_candidates = any(not row.selected for row in traces)

    rows = [
        _acceptance_row(
            "EpisodeTemplateCoverage",
            "pass" if observed_templates else "fail",
            len(observed_templates),
            0 if observed_templates else 1,
            len(unselected),
            "step_trace.csv",
            (
                f"observed={','.join(sorted(observed_templates))};"
                f"selected={','.join(sorted(selected_templates))};"
                f"unobserved={','.join(unobserved)};"
                f"unselected={','.join(unselected)}"
            ),
        ),
        _acceptance_row(
            "AccessFrontierTraceCoverage",
            "pass" if frontier_records else "unknown",
            len(frontier_records),
            0,
            0 if frontier_records else 1,
            "access_frontier_records.csv",
            "enable --trace-frontier to audit per-hook frontier facts"
            if not frontier_records
            else "frontier snapshots recorded",
        ),
    ]

    selected_source_returns = [
        row for row in traces if row.selected and row.same_plan_source_return_nos
    ]
    source_return_records = [
        row for row in staging_records if row.reason == "same_planlet_temporary_source_return"
    ]
    missing_staging = max(0, len(selected_source_returns) - len(source_return_records))
    rows.append(
        _acceptance_row(
            "TemporaryStagingIntent",
            "pass" if missing_staging == 0 else "fail",
            len(staging_records),
            missing_staging,
            0,
            "staging_intent_records.csv",
            (
                f"selected_source_return_steps={len(selected_source_returns)};"
                f"source_return_records={len(source_return_records)};"
                f"same_plan_temporary_records={len(staging_records)}"
            ),
        )
    )

    blank_manual = sum(1 for row in phase_records if not row.manual_phase_hook_count)
    rows.append(
        _acceptance_row(
            "ManualPhaseHookBenchmark",
            "unknown" if blank_manual else "pass",
            len(phase_records),
            0,
            blank_manual,
            "phase_gate_records.csv",
            "manual H1-H5 hook/session benchmark not yet populated"
            if blank_manual
            else "manual phase benchmark populated",
        )
    )

    if not trace_all_candidates:
        rows.append(
            _acceptance_row(
                "CandidateRejectProfile",
                "unknown",
                len(traces),
                0,
                1,
                "step_trace.csv",
                "run with --trace-all-candidates to audit rejected candidate structures",
            )
        )
        return rows

    reject_groups = {
        "RouteReachabilityGate": ("route_blocked_by_occupied_line", "route_missing"),
        "DepotSlotCandidateGate": ("depot_slot_rule_violation", "depot_section_after_factory_violation"),
        "SpottingCandidateGate": ("spotting_group_window_violation",),
        "RouteReversalGate": ("route_reversal_length_violation", "route_reversal_with_blocker_length_violation"),
    }
    physical_reasons = [
        reason
        for row in traces
        if row.physical_reasons
        for reason in row.physical_reasons.split("|")
        if reason
    ]
    for structure, prefixes in reject_groups.items():
        count = sum(
            1
            for reason in physical_reasons
            if any(reason == prefix or reason.startswith(f"{prefix}:") or prefix in reason for prefix in prefixes)
        )
        rows.append(
            _acceptance_row(
                structure,
                "pass" if count == 0 else "warn",
                count,
                0,
                count,
                "step_trace.csv",
                "remaining candidate rejects; generation gate not fully structural yet"
                if count
                else "no remaining rejects in traced candidates",
            )
        )
    return rows


def _resource_status_counts(records: list[ResourceStructureRecord], status: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in records:
        if row.status == status:
            counts[row.structure] = counts.get(row.structure, 0) + 1
    return counts


def _acceptance_row(
    structure: str,
    status: str,
    observed_count: int,
    failure_count: int,
    warning_count: int,
    evidence: str,
    notes: str,
) -> dict[str, Any]:
    return {
        "structure": structure,
        "status": status,
        "observed_count": observed_count,
        "failure_count": failure_count,
        "warning_count": warning_count,
        "evidence": evidence,
        "notes": notes,
    }
