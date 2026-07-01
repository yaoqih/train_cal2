from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from . import legacy_adapter as legacy
from .contracts import build_contracts
from .delta import build_contract_delta, simulate_candidate
from .domain import BorrowedBlockerDebt, CaseResult, CandidateEnvelope, SolverState, StepTrace
from .episodes import EPISODES
from .frontier import AccessFrontier, AccessFrontierRecord
from .gate import AcceptRejectGate
from .phase import HumanPhaseGate, PhaseGateRecord
from .policy import BaselinePolicy, EvaluatedCandidate, PolicyContext
from .resources import StationResourceGraph


def _remote_unsatisfied_count(cars: list[dict[str, Any]], depot_assignment: Any) -> int:
    loads = legacy.line_loads(cars)
    count = 0
    for car in legacy.unsatisfied_cars(cars, depot_assignment):
        target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
        if car["Line"] in legacy.REMOTE_INTERACTION_LINES or target_line in legacy.REMOTE_INTERACTION_LINES:
            count += 1
    return count


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
) -> StepTrace:
    candidate = envelope.candidate
    request = resource_delta.request if resource_delta else envelope.resource_request
    phase_state = policy_context.phase_state
    return StepTrace(
        case_id=state.case_id,
        hook_index=state.hook_index,
        phase=phase_state.phase.value,
        phase_reason=phase_state.reason,
        phase_front_debt=phase_state.front_debt,
        phase_cun4_port_debt=phase_state.cun4_port_debt,
        phase_remote_debt=phase_state.remote_debt,
        phase_closeout_debt=phase_state.closeout_debt,
        remote_session_open=policy_context.remote_session_open,
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
        borrowed_blockers="|".join(request.borrowed_blockers),
        restored_borrowed_blockers="|".join(request.restored_borrowed_blockers),
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
    def __init__(self, max_hooks: int = 300) -> None:
        self.max_hooks = max_hooks
        self.physical = legacy.PhysicalAdapter()
        self.resources = StationResourceGraph()
        self.gate = AcceptRejectGate()
        self.frontier = AccessFrontier()
        self.phase_gate = HumanPhaseGate()
        self.policy = BaselinePolicy()

    def solve_case(
        self,
        truth_path: Path,
        output_dir: Path,
    ) -> tuple[CaseResult, list[StepTrace], list[Any], list[PhaseGateRecord], list[AccessFrontierRecord]]:
        case_id, _payload, cars, depot_assignment, loco_location = legacy.read_case(truth_path)
        state = SolverState(case_id=case_id, cars=cars, depot_assignment=depot_assignment, loco_location=loco_location)
        state.visited_signatures.add(legacy.state_signature(state.cars, state.loco_location))
        initial_unsatisfied = len(legacy.unsatisfied_cars(state.cars, state.depot_assignment))
        traces: list[StepTrace] = []
        phase_records: list[PhaseGateRecord] = []
        frontier_records: list[AccessFrontierRecord] = []
        operations: list[Any] = []
        blocked_reason = ""
        hard_physical_accepted = 0
        accepted_without_phase_permission = 0
        phase_gate_bypass = 0
        previous_phase = ""
        hook_count_by_phase: dict[str, int] = {}
        last_policy_context: PolicyContext | None = None

        while state.hook_index <= self.max_hooks:
            if not legacy.unsatisfied_cars(state.cars, state.depot_assignment):
                break
            selected: EvaluatedCandidate | None = None
            policy_context = self.policy.context(state)
            last_policy_context = policy_context
            frontier_records.append(
                self.frontier.snapshot(
                    case_id=state.case_id,
                    hook_index=state.hook_index,
                    cars=state.cars,
                    depot_assignment=state.depot_assignment,
                    graph=self.physical.graph,
                    loco_location=state.loco_location,
                )
            )
            contracts = self.policy.order_contracts(
                build_contracts(state.cars, state.depot_assignment),
                policy_context,
            )
            if not contracts:
                blocked_reason = "no_active_contract"
                break
            rejected_this_round = 0
            generated_this_round = 0

            def evaluate_episodes(episodes: tuple[Any, ...]) -> tuple[EvaluatedCandidate | None, int, int]:
                selected_candidate: EvaluatedCandidate | None = None
                rejected_count = 0
                generated_count = 0
                for contract in contracts:
                    for episode in episodes:
                        if not episode.applies(contract):
                            continue
                        for envelope in episode.generate(
                            case_id=state.case_id,
                            hook_index=state.hook_index,
                            cars=state.cars,
                            depot_assignment=state.depot_assignment,
                            graph=self.physical.graph,
                            loco_location=state.loco_location,
                            contract=contract,
                        ):
                            generated_count += 1
                            resource_request = self.resources.request_for(envelope)
                            envelope = CandidateEnvelope(
                                candidate=envelope.candidate,
                                contract=envelope.contract,
                                intent=envelope.intent,
                                resource_request=resource_request,
                                template_name=envelope.template_name,
                            )
                            validation = self.physical.validate(
                                envelope.candidate,
                                state.cars,
                                state.loco_location,
                                state.depot_assignment,
                            )
                            resource_delta = self.resources.acquire(
                                resource_request,
                                candidate=envelope.candidate,
                                validation=validation,
                                cars=state.cars,
                                depot_assignment=state.depot_assignment,
                                borrowed_debts=state.borrowed_blocker_debts,
                            )
                            if validation.reasons:
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
                                continue
                            prospective = simulate_candidate(envelope.candidate, state.cars, validation)
                            next_loco_location = self.physical.next_loco_location(envelope.candidate, validation)
                            prospective_signature = legacy.state_signature(prospective, next_loco_location)
                            contract_delta = build_contract_delta(
                                envelope,
                                cars=state.cars,
                                prospective_cars=prospective,
                                depot_assignment=state.depot_assignment,
                            )
                            decision = self.gate.decide(contract_delta, resource_delta)
                            if decision.accepted and prospective_signature in state.visited_signatures:
                                decision = self.gate.loop_reject(contract_delta, resource_delta)
                            traces.append(
                                _trace_row(
                                    state=state,
                                    policy_context=policy_context,
                                    envelope=envelope,
                                    gate_accepted=decision.accepted,
                                    selected=False,
                                    gate_reason=decision.reason,
                                    physical_reasons=(),
                                    contract_delta=contract_delta,
                                    resource_delta=resource_delta,
                                )
                            )
                            if not decision.accepted:
                                rejected_count += 1
                                continue
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
                return selected_candidate, generated_count, rejected_count

            selected, generated_this_round, rejected_this_round = evaluate_episodes(EPISODES)
            if selected is None:
                blocked_reason = (
                    "no_episode_candidate_generated"
                    if generated_this_round == 0
                    else f"all_episode_candidates_rejected:{rejected_this_round}"
                )
                break

            envelope = selected.envelope
            validation = selected.validation
            prospective = selected.prospective_cars
            next_loco_location = selected.next_loco_location
            prospective_signature = selected.prospective_signature
            for index in range(len(traces) - 1, -1, -1):
                if traces[index].candidate_id == envelope.candidate.candidate_id and traces[index].hook_index == state.hook_index:
                    trace = traces[index]
                    traces[index] = StepTrace(
                        **{**asdict(trace), "selected": True}
                    )
                    break
            if validation.reasons:
                hard_physical_accepted += 1
            current_phase = self.phase_gate.active_phase(
                previous_phase=previous_phase,
                proposed=policy_context.phase_state,
            )
            active_phase_state = replace(
                policy_context.phase_state,
                phase=self.phase_gate.phase_kind(current_phase),
            )
            hook_count_by_phase[current_phase] = hook_count_by_phase.get(current_phase, 0) + 1
            phase_permission = self.phase_gate.permission(
                phase_state=active_phase_state,
                envelope=envelope,
                contract_delta=selected.contract_delta,
                resource_delta=selected.resource_delta,
                remote_session_open=policy_context.remote_session_open,
            )
            if not phase_permission.allowed:
                accepted_without_phase_permission += 1
            phase_record = self.phase_gate.record(
                case_id=state.case_id,
                step_index=state.hook_index,
                previous_phase=previous_phase,
                current_phase=current_phase,
                phase_state=active_phase_state,
                envelope=envelope,
                contract_delta=selected.contract_delta,
                permission=phase_permission,
                hook_count_in_phase=hook_count_by_phase[current_phase],
            )
            if phase_record.transition_type == "fail":
                phase_gate_bypass += 1
            phase_records.append(phase_record)
            previous_phase = current_phase
            start_operation_index = len(operations) + 1
            operations.extend(self.physical.operation_rows(envelope.candidate, validation, start_operation_index))
            state.cars = prospective
            state.loco_location = next_loco_location
            state.visited_signatures.add(prospective_signature)
            touched_remote = any(
                line in legacy.REMOTE_INTERACTION_LINES
                for line in envelope.resource_request.touched_lines
            )
            state.remote_session_open = bool(
                _remote_unsatisfied_count(state.cars, state.depot_assignment)
                and touched_remote
                and (
                    state.remote_session_open
                    or self.policy.opens_remote_session(selected)
                )
            )
            business_lines = [
                step.line
                for step in legacy.candidate_plan_steps(envelope.candidate)
                if step.action in {"Get", "Put"}
            ]
            if business_lines:
                state.last_business_remote = business_lines[-1] in legacy.REMOTE_INTERACTION_LINES
            if envelope.resource_request.restored_borrowed_blockers:
                restored_key = tuple(sorted(envelope.resource_request.restored_borrowed_blockers))
                state.borrowed_blocker_debts.pop(restored_key, None)
            if envelope.resource_request.borrowed_blockers:
                debt_key = tuple(sorted(envelope.resource_request.borrowed_blockers))
                state.borrowed_blocker_debts[debt_key] = BorrowedBlockerDebt(
                    debt_key=debt_key,
                    blocker_nos=debt_key,
                    origin_line=envelope.candidate.source_line,
                    owner_contract_id=envelope.contract.contract_id,
                    opened_hook_index=state.hook_index,
                )
            state.accepted_candidate_ids.add(envelope.candidate.candidate_id)
            state.hook_index += 1

        final_unsatisfied = len(legacy.unsatisfied_cars(state.cars, state.depot_assignment))
        closed_door_reasons = legacy.closed_door_replay_violation_reasons(operations, state.cars)
        if closed_door_reasons and not blocked_reason:
            blocked_reason = "|".join(closed_door_reasons)
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
            self.physical.write_json(
                response_path,
                {
                    "Success": status == "completed",
                    "Message": "" if status == "completed" else blocked_reason,
                    "StatusCode": 200 if status == "completed" else 409,
                    "Data": {
                        "Operations": [self.physical.response_operation(row) for row in operations],
                        "GeneratedEndStatus": [
                            {
                                "No": legacy.car_no(car),
                                "Line": car["Line"],
                                "Position": int(car.get("Position") or 0),
                            }
                            for car in sorted(state.cars, key=legacy.car_no)
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
            remote_business_transition_count=legacy.operation_remote_business_transition_count(operations),
            initial_unsatisfied=initial_unsatisfied,
            final_unsatisfied=final_unsatisfied,
            blocked_reason=blocked_reason,
            step_count=len(accepted_traces),
            accepted_without_contract_delta_count=0,
            accepted_without_resource_delta_count=0,
            accepted_without_phase_permission_count=accepted_without_phase_permission,
            phase_gate_bypass_count=phase_gate_bypass,
            orphan_resource_request_count=0,
            hard_physical_violation_accepted_count=hard_physical_accepted,
        )
        return result, traces, operations, phase_records, frontier_records


def write_artifacts(
    output_dir: Path,
    results: list[CaseResult],
    traces: list[StepTrace],
    phase_records: list[PhaseGateRecord],
    frontier_records: list[AccessFrontierRecord],
) -> None:
    adapter = legacy.PhysicalAdapter()
    adapter.write_csv(output_dir / "case_summary.csv", [asdict(row) for row in results])
    adapter.write_csv(output_dir / "step_trace.csv", [asdict(row) for row in traces])
    adapter.write_csv(output_dir / "phase_gate_records.csv", [asdict(row) for row in phase_records])
    adapter.write_csv(output_dir / "access_frontier_records.csv", [asdict(row) for row in frontier_records])
    route_blocked_counts = [row.route_blocked_line_count for row in frontier_records]
    serial_blocker_counts = [row.serial_blocker_line_count for row in frontier_records]
    summary = {
        "case_count": len(results),
        "completed": sum(1 for row in results if row.status == "completed"),
        "blocked": sum(1 for row in results if row.status == "blocked"),
        "accepted_without_contract_delta_count": sum(row.accepted_without_contract_delta_count for row in results),
        "accepted_without_resource_delta_count": sum(row.accepted_without_resource_delta_count for row in results),
        "accepted_without_phase_permission_count": sum(row.accepted_without_phase_permission_count for row in results),
        "phase_gate_bypass_count": sum(row.phase_gate_bypass_count for row in results),
        "orphan_resource_request_count": sum(row.orphan_resource_request_count for row in results),
        "hard_physical_violation_accepted_count": sum(row.hard_physical_violation_accepted_count for row in results),
        "hook_count": sum(row.hook_count for row in results),
        "remote_business_transition_count": sum(row.remote_business_transition_count for row in results),
        "phase_gate_record_count": len(phase_records),
        "access_frontier_record_count": len(frontier_records),
        "max_route_blocked_line_count": max(route_blocked_counts, default=0),
        "max_serial_blocker_line_count": max(serial_blocker_counts, default=0),
    }
    adapter.write_json(output_dir / "vnext_summary.json", summary)
