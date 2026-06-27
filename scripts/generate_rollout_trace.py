#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from generate_p5_candidate_trace import RESOURCE_REQUEST_BY_FAMILY
from generate_p8_optimization_trace import HOOK_COST_BY_ACTION
from validate_phase_gates import (
    DEFAULT_REPRESENTATIVE_CASES,
    PHASES,
    PHASE_ALLOWED_CANDIDATE_FAMILIES,
    PHASE_RANK,
    PhaseGateContract,
    build_phase_gate_contracts,
    case_id_from_path,
    current_line,
    hook_tolerance,
    infer_manual_phase_audit,
    is_satisfied,
    load_truth_case,
    parse_manual_hooks,
    phase_bounds,
    target_lines,
    try_case_id_from_path,
    write_csv,
)


DEPOT_INSIDE_LINES = {"修1库内", "修2库内", "修3库内", "修4库内"}
DEPOT_TARGET_LINES = {
    "修1库内",
    "修2库内",
    "修3库内",
    "修4库内",
    "修1库外",
    "修2库外",
    "修3库外",
    "修4库外",
}
STORAGE_LINES = {"存1线", "存2线", "存3线", "存4线", "存5线", "存5线北", "存5线南"}
FUNCTION_LINES = {"洗罐站", "洗罐线北", "油漆线", "抛丸线", "卸轮线", "机走棚", "机库线", "机走北"}
FRONT_SERVICE_ACTIONS = {
    "PRE_REPAIR_STAGING",
    "DISPATCH_SHED_QUEUE",
    "FUNCTION_LINE_SERVICE",
    "YARD_REBALANCE",
    "CUN4_PORT_SHAPING",
}
DEPOT_ACTIONS = {"REPAIR_INBOUND", "DEPOT_OUTBOUND", "DEPOT_SLOT", "DEPOT_DIGEST"}
GATE_ACTIONS = {"STRICT_RELEASE", "MACHINE_ACCEPT", "DEPOT_DIGEST", "TAIL_CLOSEOUT"}


@dataclass(frozen=True)
class RolloutCandidate:
    case_id: str
    rollout_step: int
    phase: str
    candidate_id: str
    action_family: str
    candidate_kind: str
    from_line: str
    to_line: str
    vehicle_ids: tuple[str, ...]
    moved_vehicle_count: int
    resource_request: str
    resource_status: str
    hard_violation_count: int
    gate_decision: str
    local_hook_cost: int
    why_generated: str
    evidence_ids: str


@dataclass
class RolloutState:
    cars: list[dict[str, Any]]
    done_gates: set[str] = field(default_factory=set)
    depot_assignments: dict[str, str] = field(default_factory=dict)
    phase_hook_counts: Counter[str] = field(default_factory=Counter)
    action_counts: Counter[str] = field(default_factory=Counter)
    total_hooks: int = 0
    hard_violation_count: int = 0
    state_loop_count: int = 0
    visited_signatures: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RolloutStepRecord:
    case_id: str
    variant: str
    rollout_step: int
    phase: str
    candidate_id: str
    action_family: str
    candidate_kind: str
    p5_generated_candidate_count: int
    resource_request: str
    resource_status: str
    gate_decision: str
    hard_violation_count: int
    local_hook_cost: int
    hook_increment: int
    moved_vehicle_count: int
    from_line: str
    to_line: str
    pre_unsatisfied_vehicle_count: int
    post_unsatisfied_vehicle_count: int
    pre_state_signature: str
    post_state_signature: str
    phase_hook_count_after: int
    total_hook_count_after: int
    manual_total_soft_upper_bound: int
    manual_phase_soft_upper_bound: int | None
    phase_over_soft_upper_bound: bool
    evidence_ids: str
    status: str


@dataclass(frozen=True)
class RolloutPhaseSummary:
    case_id: str
    phase: str
    expected: bool
    variant: str
    manual_phase_hook_count: int | None
    manual_phase_soft_upper_bound: int | None
    rollout_phase_hook_count: int
    within_soft_upper_bound: bool
    action_family_counts: str
    status: str


@dataclass(frozen=True)
class RolloutCaseSummary:
    case_id: str
    variant: str
    phase_path: str
    status: str
    vehicle_count: int
    initial_unsatisfied_vehicle_count: int
    final_unsatisfied_vehicle_count: int
    rollout_hook_count: int
    manual_observed_hook_count: int | None
    manual_soft_hook_upper_bound: int | None
    hook_delta_vs_manual_observed: int | None
    hook_within_manual_soft_bound: bool
    phase_hook_within_manual_soft_bound: bool
    hard_violation_count: int
    state_loop_count: int
    steps_executed: int
    phases_visited: str
    blocked_reason: str


@dataclass(frozen=True)
class ManualVsRolloutHookAudit:
    case_id: str
    variant: str
    manual_observed_hook_count: int | None
    manual_soft_hook_upper_bound: int | None
    rollout_hook_count: int
    total_hook_status: str
    phase_hook_status: str
    failing_phases: str
    final_target_status: str
    note: str


@dataclass(frozen=True)
class RolloutGapRecord:
    case_id: str
    variant: str
    gap_type: str
    phase: str
    rollout_value: int | None
    manual_soft_bound: int | None
    status: str
    action_family_counts: str
    failure_bucket: str
    next_required_structure: str


@dataclass(frozen=True)
class RolloutSummary:
    truth_case_count: int
    manual_case_count: int
    matched_case_count: int
    completed_case_count: int
    blocked_case_count: int
    missing_manual_case_count: int
    total_hook_soft_pass_count: int
    phase_hook_soft_pass_case_count: int
    hard_violation_count: int
    state_loop_count: int
    rollout_step_record_count: int
    rollout_candidate_record_count: int
    rollout_gap_record_count: int
    status_counts: dict[str, int]
    blocked_reason_counts: dict[str, int]


def load_manual_audits(manual_dir: Path, representative: set[str] | None) -> dict[str, Any]:
    audits_by_case: dict[str, Any] = {}
    for path in sorted(manual_dir.glob("*人工调车作业单/*.xlsx")):
        case_id = try_case_id_from_path(path)
        if not case_id or (representative is not None and case_id not in representative):
            continue
        audit = infer_manual_phase_audit(parse_manual_hooks(path))
        audit.source_path = str(path)
        audits_by_case.setdefault(audit.case_id, audit)
    return audits_by_case


def contracts_by_case(manual_audits: dict[str, Any]) -> dict[str, list[PhaseGateContract]]:
    grouped: dict[str, list[PhaseGateContract]] = defaultdict(list)
    for contract in build_phase_gate_contracts(list(manual_audits.values())):
        grouped[contract.case_id].append(contract)
    return dict(grouped)


def expected_phase_path(contracts: list[PhaseGateContract]) -> list[str]:
    phases = {
        contract.phase
        for contract in contracts
        if contract.expected or contract.compressed_with
    }
    variants = {contract.variant for contract in contracts}
    if "MIXED_SIGNAL_REPAIR" in variants and "H2" in phases:
        phases.add("H4")
    if "DEPOT_DIGEST_ONLY" in variants and "H4" in phases:
        phases.add("H1")
    return sorted(phases, key=lambda phase: PHASE_RANK[phase])


def contract_for_phase(contracts: list[PhaseGateContract], phase: str) -> PhaseGateContract | None:
    for contract in contracts:
        if contract.phase == phase:
            return contract
    return None


def unsatisfied_cars(cars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [car for car in cars if not is_satisfied(car)]


def line_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    return Counter(current_line(car) for car in cars)


def choose_target_line(targets: tuple[str, ...], loads: Counter[str]) -> str:
    if not targets:
        return ""
    depot_targets = [target for target in targets if target in DEPOT_TARGET_LINES]
    if depot_targets:
        return min(depot_targets, key=lambda target: (loads[target], target))
    return targets[0]


def base_action_for_target(line: str, target: str) -> str:
    if line in DEPOT_INSIDE_LINES and target not in DEPOT_TARGET_LINES:
        return "DEPOT_OUTBOUND"
    if target in DEPOT_TARGET_LINES:
        return "DEPOT_SLOT" if line in DEPOT_INSIDE_LINES else "REPAIR_INBOUND"
    if target == "预修线":
        return "PRE_REPAIR_STAGING"
    if target in {"调梁棚", "调梁线北"}:
        return "DISPATCH_SHED_QUEUE"
    if target in FUNCTION_LINES:
        return "FUNCTION_LINE_SERVICE"
    if target in STORAGE_LINES:
        return "YARD_REBALANCE"
    return "YARD_REBALANCE"


def action_for_phase(base_action: str, target: str, phase: str, path: list[str]) -> str:
    if base_action == "DEPOT_OUTBOUND" and target == "存4线" and phase == "H2":
        return "CUN4_PORT_SHAPING"
    if base_action == "YARD_REBALANCE" and target == "存4线" and phase == "H2":
        return "CUN4_PORT_SHAPING"
    if base_action == "YARD_REBALANCE" and target == "存4线" and "H2" in path and phase in {"H1", "H5"}:
        return "YARD_REBALANCE"
    return base_action


def action_can_run_in_phase(action: str, phase: str, variant: str) -> bool:
    allowed = PHASE_ALLOWED_CANDIDATE_FAMILIES.get(phase, set())
    if action not in allowed:
        return False
    if action in DEPOT_ACTIONS:
        return phase == "H4"
    if action == "STRICT_RELEASE":
        return phase == "H3" or (phase == "H2" and variant == "MIXED_SIGNAL_REPAIR")
    if action == "MACHINE_ACCEPT":
        return phase == "H3" and variant == "FULL_CHAIN_REPAIR"
    if action == "CUN4_PORT_SHAPING":
        return phase == "H2"
    if action in FRONT_SERVICE_ACTIONS:
        if variant == "DEPOT_DIGEST_ONLY":
            return phase in {"H1", "H5"}
        return phase in {"H1", "H2", "H5"}
    if action == "TAIL_CLOSEOUT":
        return phase == "H5"
    return True


def group_move_candidates(
    case_id: str,
    step_index: int,
    phase: str,
    variant: str,
    path: list[str],
    cars: list[dict[str, Any]],
) -> list[RolloutCandidate]:
    loads = line_loads(cars)
    grouped: dict[tuple[str, str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for car in unsatisfied_cars(cars):
        targets = tuple(target_lines(car))
        if not targets:
            continue
        target = choose_target_line(targets, loads)
        if target == "存4线" and "H2" in path and phase != "H2":
            continue
        base_action = base_action_for_target(current_line(car), target)
        action = action_for_phase(base_action, target, phase, path)
        if not action_can_run_in_phase(action, phase, variant):
            continue
        grouped[(action, current_line(car), targets)].append(car)

    candidates: list[RolloutCandidate] = []
    for index, ((action, source, targets), batch) in enumerate(sorted(grouped.items()), start=1):
        target = choose_target_line(targets, loads)
        action = action_for_phase(action, target, phase, path)
        vehicle_ids = tuple(sorted(str(car.get("No") or "") for car in batch))
        candidate_id = f"{case_id}:rollout:{step_index}:{phase}:{action}:move:{index}"
        request = RESOURCE_REQUEST_BY_FAMILY[action]
        status = resource_status(action, cars)
        candidates.append(
            RolloutCandidate(
                case_id=case_id,
                rollout_step=step_index,
                phase=phase,
                candidate_id=candidate_id,
                action_family=action,
                candidate_kind="move",
                from_line=source,
                to_line=target,
                vehicle_ids=vehicle_ids,
                moved_vehicle_count=len(batch),
                resource_request=request,
                resource_status=status,
                hard_violation_count=0,
                gate_decision="accept",
                local_hook_cost=HOOK_COST_BY_ACTION.get(action, 4),
                why_generated=(
                    f"phase={phase}; action={action}; source_line={source}; "
                    f"target_line={target}; batch_vehicle_count={len(batch)}"
                ),
                evidence_ids="|".join(
                    [
                        f"case:{case_id}",
                        f"phase:{phase}",
                        f"action_family:{action}",
                        f"source:{source}",
                        f"target:{target}",
                        f"vehicles:{','.join(vehicle_ids)}",
                    ]
                ),
            )
        )
    return candidates


def depot_target_assignments(cars: list[dict[str, Any]]) -> dict[str, str]:
    loads = line_loads(cars)
    assigned_loads: Counter[str] = Counter()
    assignments: dict[str, str] = {}
    for car in sorted(unsatisfied_cars(cars), key=lambda item: str(item.get("No") or "")):
        line = current_line(car)
        if line in DEPOT_INSIDE_LINES:
            continue
        depot_targets = [target for target in target_lines(car) if target in DEPOT_TARGET_LINES]
        if not depot_targets:
            continue
        target = min(depot_targets, key=lambda item: (loads[item] + assigned_loads[item], item))
        assignments[str(car.get("No") or "")] = target
        assigned_loads[target] += 1
    return assignments


def depot_macro_candidates(
    case_id: str,
    step_index: int,
    phase: str,
    state: RolloutState,
) -> list[RolloutCandidate]:
    if phase != "H4":
        return []

    if not state.depot_assignments:
        state.depot_assignments = depot_target_assignments(state.cars)
    assignments = state.depot_assignments
    grouped_inbound: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_outbound: dict[str, list[dict[str, Any]]] = defaultdict(list)
    loads = line_loads(state.cars)

    for car in unsatisfied_cars(state.cars):
        vehicle_id = str(car.get("No") or "")
        line = current_line(car)
        if vehicle_id in assignments:
            grouped_inbound[assignments[vehicle_id]].append(car)
        elif line in DEPOT_INSIDE_LINES:
            targets = tuple(target_lines(car))
            target = choose_target_line(targets, loads)
            if target and target != line:
                grouped_outbound[line].append(car)

    affected_lines = sorted(set(grouped_inbound) | set(grouped_outbound))
    candidates: list[RolloutCandidate] = []
    for index, depot_line in enumerate(affected_lines, start=1):
        inbound = grouped_inbound.get(depot_line, [])
        outbound = grouped_outbound.get(depot_line, [])
        vehicle_ids = tuple(sorted(str(car.get("No") or "") for car in inbound + outbound))
        if not vehicle_ids:
            continue
        source_lines = sorted({current_line(car) for car in inbound + outbound})
        outbound_targets = sorted(
            {
                choose_target_line(tuple(target_lines(car)), loads)
                for car in outbound
                if choose_target_line(tuple(target_lines(car)), loads)
            }
        )
        candidate_id = f"{case_id}:rollout:{step_index}:{phase}:DEPOT_DIGEST:depot_macro:{index}"
        candidates.append(
            RolloutCandidate(
                case_id=case_id,
                rollout_step=step_index,
                phase=phase,
                candidate_id=candidate_id,
                action_family="DEPOT_DIGEST",
                candidate_kind="depot_macro",
                from_line="+".join(source_lines),
                to_line="+".join([depot_line, *outbound_targets]),
                vehicle_ids=vehicle_ids,
                moved_vehicle_count=len(vehicle_ids),
                resource_request=RESOURCE_REQUEST_BY_FAMILY["DEPOT_DIGEST"],
                resource_status=resource_status("DEPOT_DIGEST", state.cars),
                hard_violation_count=0,
                gate_decision="accept",
                local_hook_cost=HOOK_COST_BY_ACTION["DEPOT_DIGEST"],
                why_generated=(
                    f"phase=H4; depot_line={depot_line}; "
                    f"inbound_vehicle_count={len(inbound)}; outbound_vehicle_count={len(outbound)}"
                ),
                evidence_ids="|".join(
                    [
                        f"case:{case_id}",
                        "phase:H4",
                        "action_family:DEPOT_DIGEST",
                        f"depot_line:{depot_line}",
                        f"inbound:{len(inbound)}",
                        f"outbound:{len(outbound)}",
                        f"vehicles:{','.join(vehicle_ids)}",
                    ]
                ),
            )
        )
    return candidates


def gate_candidates(
    case_id: str,
    step_index: int,
    phase: str,
    variant: str,
    state: RolloutState,
) -> list[RolloutCandidate]:
    action = ""
    gate_key = ""
    if phase == "H2" and variant == "MIXED_SIGNAL_REPAIR" and "H2_STRICT_RELEASE" not in state.done_gates:
        action = "STRICT_RELEASE"
        gate_key = "H2_STRICT_RELEASE"
    elif phase == "H3" and variant in {"FULL_CHAIN_REPAIR", "DIRECT_REPAIR_ENTRY"}:
        if "H3_STRICT_RELEASE" not in state.done_gates:
            action = "STRICT_RELEASE"
            gate_key = "H3_STRICT_RELEASE"
        elif variant == "FULL_CHAIN_REPAIR" and "H3_MACHINE_ACCEPT" not in state.done_gates:
            action = "MACHINE_ACCEPT"
            gate_key = "H3_MACHINE_ACCEPT"
    elif phase == "H4" and variant in {"FULL_CHAIN_REPAIR", "DEPOT_DIGEST_ONLY", "DIRECT_REPAIR_ENTRY"}:
        if "H4_DEPOT_DIGEST" not in state.done_gates:
            action = "DEPOT_DIGEST"
            gate_key = "H4_DEPOT_DIGEST"
    elif phase == "H5" and not unsatisfied_cars(state.cars) and "H5_TAIL_CLOSEOUT" not in state.done_gates:
        action = "TAIL_CLOSEOUT"
        gate_key = "H5_TAIL_CLOSEOUT"

    if not action or not action_can_run_in_phase(action, phase, variant):
        return []

    candidate_id = f"{case_id}:rollout:{step_index}:{phase}:{action}:gate"
    request = RESOURCE_REQUEST_BY_FAMILY[action]
    return [
        RolloutCandidate(
            case_id=case_id,
            rollout_step=step_index,
            phase=phase,
            candidate_id=candidate_id,
            action_family=action,
            candidate_kind="gate",
            from_line="",
            to_line="",
            vehicle_ids=(),
            moved_vehicle_count=0,
            resource_request=request,
            resource_status=resource_status(action, state.cars),
            hard_violation_count=0,
            gate_decision="accept",
            local_hook_cost=HOOK_COST_BY_ACTION.get(action, 4),
            why_generated=f"phase={phase}; gate={gate_key}; required_by_manual_variant={variant}",
            evidence_ids="|".join(
                [
                    f"case:{case_id}",
                    f"phase:{phase}",
                    f"action_family:{action}",
                    f"gate:{gate_key}",
                ]
            ),
        )
    ]


def build_candidates(
    case_id: str,
    step_index: int,
    phase: str,
    variant: str,
    path: list[str],
    state: RolloutState,
) -> list[RolloutCandidate]:
    if phase == "H3":
        gates = gate_candidates(case_id, step_index, phase, variant, state)
        if gates:
            return gates
    depot_candidates = depot_macro_candidates(case_id, step_index, phase, state)
    if depot_candidates:
        return depot_candidates
    moves = group_move_candidates(case_id, step_index, phase, variant, path, state.cars)
    if phase == "H4":
        moves = [candidate for candidate in moves if candidate.action_family != "DEPOT_SLOT"]
    if moves:
        return moves
    return gate_candidates(case_id, step_index, phase, variant, state)


def resource_status(action: str, cars: list[dict[str, Any]]) -> str:
    counts = line_loads(cars)
    depot_inside = sum(counts[line] for line in DEPOT_INSIDE_LINES)
    if action in {"DEPOT_DIGEST", "DEPOT_SLOT", "REPAIR_INBOUND", "DEPOT_OUTBOUND"} and depot_inside:
        return "constrained"
    if action == "MACHINE_ACCEPT" and depot_inside >= 20:
        return "constrained"
    return "available"


def candidate_sort_key(candidate: RolloutCandidate) -> tuple[int, int, str, str]:
    return (
        candidate.local_hook_cost,
        -candidate.moved_vehicle_count,
        candidate.action_family,
        candidate.candidate_id,
    )


def state_signature(state: RolloutState) -> str:
    vehicle_parts = [
        f"{car.get('No')}:{current_line(car)}:{car.get('Position')}"
        for car in sorted(state.cars, key=lambda item: str(item.get("No") or ""))
    ]
    gate_parts = sorted(state.done_gates)
    payload = json.dumps(
        {
            "vehicles": vehicle_parts,
            "gates": gate_parts,
            "depot_assignments": state.depot_assignments,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def renumber_positions(cars: list[dict[str, Any]]) -> None:
    by_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in cars:
        by_line[current_line(car)].append(car)
    for _line, line_cars in by_line.items():
        line_cars.sort(key=lambda item: (int(item.get("Position") or 0), str(item.get("No") or "")))
        for position, car in enumerate(line_cars, start=1):
            car["Position"] = position


def apply_candidate(state: RolloutState, candidate: RolloutCandidate) -> None:
    if candidate.candidate_kind == "move":
        vehicle_ids = set(candidate.vehicle_ids)
        for car in state.cars:
            if str(car.get("No") or "") in vehicle_ids:
                car["Line"] = candidate.to_line
        renumber_positions(state.cars)
    elif candidate.candidate_kind == "depot_macro":
        vehicle_ids = set(candidate.vehicle_ids)
        if not state.depot_assignments:
            state.depot_assignments = depot_target_assignments(state.cars)
        assignments = state.depot_assignments
        loads = line_loads(state.cars)
        for car in state.cars:
            vehicle_id = str(car.get("No") or "")
            if vehicle_id not in vehicle_ids:
                continue
            if vehicle_id in assignments:
                car["Line"] = assignments[vehicle_id]
            elif current_line(car) in DEPOT_INSIDE_LINES:
                target = choose_target_line(tuple(target_lines(car)), loads)
                if target:
                    car["Line"] = target
        state.done_gates.add("H4_DEPOT_DIGEST")
        renumber_positions(state.cars)
    else:
        state.done_gates.add(gate_key_for_candidate(candidate))
    state.phase_hook_counts[candidate.phase] += 1
    state.action_counts[f"{candidate.phase}:{candidate.action_family}"] += 1
    state.total_hooks += 1
    state.hard_violation_count += candidate.hard_violation_count


def gate_key_for_candidate(candidate: RolloutCandidate) -> str:
    if candidate.phase == "H2" and candidate.action_family == "STRICT_RELEASE":
        return "H2_STRICT_RELEASE"
    if candidate.phase == "H3" and candidate.action_family == "STRICT_RELEASE":
        return "H3_STRICT_RELEASE"
    if candidate.phase == "H3" and candidate.action_family == "MACHINE_ACCEPT":
        return "H3_MACHINE_ACCEPT"
    if candidate.phase == "H4" and candidate.action_family == "DEPOT_DIGEST":
        return "H4_DEPOT_DIGEST"
    if candidate.phase == "H5" and candidate.action_family == "TAIL_CLOSEOUT":
        return "H5_TAIL_CLOSEOUT"
    return f"{candidate.phase}_{candidate.action_family}"


def manual_phase_soft_bound(audit: Any, phase: str) -> int | None:
    start, end = phase_bounds(audit, phase)
    if start is None or end is None:
        return None
    count = max(0, end - start + 1)
    return count + hook_tolerance(count)


def manual_phase_hook_count(audit: Any, phase: str) -> int | None:
    start, end = phase_bounds(audit, phase)
    if start is None or end is None:
        return None
    return max(0, end - start + 1)


def step_record(
    audit: Any,
    contract: PhaseGateContract | None,
    candidate: RolloutCandidate,
    candidates: list[RolloutCandidate],
    pre_unsatisfied: int,
    post_unsatisfied: int,
    pre_signature: str,
    post_signature: str,
    state: RolloutState,
) -> RolloutStepRecord:
    phase_soft = manual_phase_soft_bound(audit, candidate.phase)
    phase_count = state.phase_hook_counts[candidate.phase]
    return RolloutStepRecord(
        case_id=audit.case_id,
        variant=audit.variant,
        rollout_step=candidate.rollout_step,
        phase=candidate.phase,
        candidate_id=candidate.candidate_id,
        action_family=candidate.action_family,
        candidate_kind=candidate.candidate_kind,
        p5_generated_candidate_count=len(candidates),
        resource_request=candidate.resource_request,
        resource_status=candidate.resource_status,
        gate_decision=candidate.gate_decision,
        hard_violation_count=candidate.hard_violation_count,
        local_hook_cost=candidate.local_hook_cost,
        hook_increment=1,
        moved_vehicle_count=candidate.moved_vehicle_count,
        from_line=candidate.from_line,
        to_line=candidate.to_line,
        pre_unsatisfied_vehicle_count=pre_unsatisfied,
        post_unsatisfied_vehicle_count=post_unsatisfied,
        pre_state_signature=pre_signature,
        post_state_signature=post_signature,
        phase_hook_count_after=phase_count,
        total_hook_count_after=state.total_hooks,
        manual_total_soft_upper_bound=audit.soft_hook_upper_bound,
        manual_phase_soft_upper_bound=phase_soft,
        phase_over_soft_upper_bound=phase_soft is not None and phase_count > phase_soft,
        evidence_ids="|".join(
            [
                candidate.evidence_ids,
                f"manual_variant:{audit.variant}",
                f"manual_phase_expected:{bool(contract and contract.expected)}",
                f"manual_phase_soft:{phase_soft if phase_soft is not None else ''}",
            ]
        ),
        status="passed",
    )


def rollout_case(
    truth_path: Path,
    audit: Any,
    contracts: list[PhaseGateContract],
    max_steps: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[RolloutPhaseSummary], RolloutCaseSummary, ManualVsRolloutHookAudit]:
    case_id = case_id_from_path(truth_path)
    payload = load_truth_case(truth_path)
    state = RolloutState(cars=json.loads(json.dumps(payload.get("StartStatus") or [], ensure_ascii=False)))
    initial_unsatisfied = len(unsatisfied_cars(state.cars))
    path = expected_phase_path(contracts)
    step_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    phases_visited: list[str] = []
    blocked_reason = ""
    rollout_step = 1
    state.visited_signatures.add(state_signature(state))

    for phase in path:
        phases_visited.append(phase)
        while rollout_step <= max_steps:
            candidates = build_candidates(case_id, rollout_step, phase, audit.variant, path, state)
            candidate_rows.extend(asdict(candidate) for candidate in candidates)
            if not candidates:
                break
            selected = sorted(candidates, key=candidate_sort_key)[0]
            pre_signature = state_signature(state)
            pre_unsatisfied = len(unsatisfied_cars(state.cars))
            contract = contract_for_phase(contracts, phase)
            apply_candidate(state, selected)
            post_signature = state_signature(state)
            if post_signature in state.visited_signatures:
                state.state_loop_count += 1
                blocked_reason = "state_signature_loop"
                break
            state.visited_signatures.add(post_signature)
            post_unsatisfied = len(unsatisfied_cars(state.cars))
            step_rows.append(
                asdict(
                    step_record(
                        audit=audit,
                        contract=contract,
                        candidate=selected,
                        candidates=candidates,
                        pre_unsatisfied=pre_unsatisfied,
                        post_unsatisfied=post_unsatisfied,
                        pre_signature=pre_signature,
                        post_signature=post_signature,
                        state=state,
                    )
                )
            )
            rollout_step += 1
        if blocked_reason or rollout_step > max_steps:
            break

    final_unsatisfied = len(unsatisfied_cars(state.cars))
    if not blocked_reason and rollout_step > max_steps:
        blocked_reason = "max_step_limit_reached"
    if not blocked_reason and final_unsatisfied:
        remaining = Counter()
        for car in unsatisfied_cars(state.cars):
            targets = tuple(target_lines(car))
            target = choose_target_line(targets, line_loads(state.cars))
            remaining[base_action_for_target(current_line(car), target)] += 1
        blocked_reason = "remaining_obligations_without_allowed_phase:" + json.dumps(
            dict(sorted(remaining.items())), ensure_ascii=False, sort_keys=True
        )

    phase_summaries = build_phase_summaries(case_id, audit, contracts, state)
    phase_within = all(row.within_soft_upper_bound for row in phase_summaries)
    hook_within = state.total_hooks <= audit.soft_hook_upper_bound
    completed = final_unsatisfied == 0 and not blocked_reason and state.hard_violation_count == 0 and state.state_loop_count == 0
    status = "completed" if completed else "blocked"
    case_summary = RolloutCaseSummary(
        case_id=case_id,
        variant=audit.variant,
        phase_path="->".join(path),
        status=status,
        vehicle_count=len(state.cars),
        initial_unsatisfied_vehicle_count=initial_unsatisfied,
        final_unsatisfied_vehicle_count=final_unsatisfied,
        rollout_hook_count=state.total_hooks,
        manual_observed_hook_count=audit.observed_hook_count,
        manual_soft_hook_upper_bound=audit.soft_hook_upper_bound,
        hook_delta_vs_manual_observed=state.total_hooks - audit.observed_hook_count,
        hook_within_manual_soft_bound=hook_within,
        phase_hook_within_manual_soft_bound=phase_within,
        hard_violation_count=state.hard_violation_count,
        state_loop_count=state.state_loop_count,
        steps_executed=len(step_rows),
        phases_visited="->".join(phases_visited),
        blocked_reason=blocked_reason,
    )
    hook_audit = build_hook_audit(audit, case_summary, phase_summaries)
    return step_rows, candidate_rows, phase_summaries, case_summary, hook_audit


def build_phase_summaries(
    case_id: str,
    audit: Any,
    contracts: list[PhaseGateContract],
    state: RolloutState,
) -> list[RolloutPhaseSummary]:
    rows: list[RolloutPhaseSummary] = []
    contract_by_phase = {contract.phase: contract for contract in contracts}
    for phase in PHASES:
        contract = contract_by_phase.get(phase)
        expected = bool(contract and (contract.expected or contract.compressed_with))
        manual_count = manual_phase_hook_count(audit, phase)
        soft = manual_phase_soft_bound(audit, phase)
        rollout_count = state.phase_hook_counts[phase]
        if soft is None:
            within = rollout_count == 0
        else:
            within = rollout_count <= soft
        action_counts = {
            key.split(":", 1)[1]: count
            for key, count in sorted(state.action_counts.items())
            if key.startswith(f"{phase}:")
        }
        status = "passed" if within else "over_soft_bound"
        if rollout_count and not expected:
            status = "unexpected_phase_used"
            within = False
        rows.append(
            RolloutPhaseSummary(
                case_id=case_id,
                phase=phase,
                expected=expected,
                variant=audit.variant,
                manual_phase_hook_count=manual_count,
                manual_phase_soft_upper_bound=soft,
                rollout_phase_hook_count=rollout_count,
                within_soft_upper_bound=within,
                action_family_counts=json.dumps(action_counts, ensure_ascii=False, sort_keys=True),
                status=status,
            )
        )
    return rows


def build_hook_audit(
    audit: Any,
    case_summary: RolloutCaseSummary,
    phase_rows: list[RolloutPhaseSummary],
) -> ManualVsRolloutHookAudit:
    failing_phases = [
        row.phase
        for row in phase_rows
        if not row.within_soft_upper_bound
    ]
    total_status = "passed" if case_summary.hook_within_manual_soft_bound else "over_manual_soft_bound"
    phase_status = "passed" if not failing_phases else "over_manual_phase_soft_bound"
    final_status = "satisfied" if case_summary.final_unsatisfied_vehicle_count == 0 else "unsatisfied"
    note = "structural_rollout_not_physical_route_solver"
    if case_summary.blocked_reason:
        note = f"{note}; blocked_reason={case_summary.blocked_reason}"
    return ManualVsRolloutHookAudit(
        case_id=audit.case_id,
        variant=audit.variant,
        manual_observed_hook_count=audit.observed_hook_count,
        manual_soft_hook_upper_bound=audit.soft_hook_upper_bound,
        rollout_hook_count=case_summary.rollout_hook_count,
        total_hook_status=total_status,
        phase_hook_status=phase_status,
        failing_phases=";".join(failing_phases),
        final_target_status=final_status,
        note=note,
    )


def missing_manual_case_summary(truth_path: Path) -> tuple[RolloutCaseSummary, ManualVsRolloutHookAudit]:
    case_id = case_id_from_path(truth_path)
    cars = load_truth_case(truth_path).get("StartStatus") or []
    final_unsatisfied = len(unsatisfied_cars(cars))
    summary = RolloutCaseSummary(
        case_id=case_id,
        variant="",
        phase_path="",
        status="blocked_missing_manual_baseline",
        vehicle_count=len(cars),
        initial_unsatisfied_vehicle_count=final_unsatisfied,
        final_unsatisfied_vehicle_count=final_unsatisfied,
        rollout_hook_count=0,
        manual_observed_hook_count=None,
        manual_soft_hook_upper_bound=None,
        hook_delta_vs_manual_observed=None,
        hook_within_manual_soft_bound=False,
        phase_hook_within_manual_soft_bound=False,
        hard_violation_count=0,
        state_loop_count=0,
        steps_executed=0,
        phases_visited="",
        blocked_reason="manual_baseline_missing",
    )
    hook_audit = ManualVsRolloutHookAudit(
        case_id=case_id,
        variant="",
        manual_observed_hook_count=None,
        manual_soft_hook_upper_bound=None,
        rollout_hook_count=0,
        total_hook_status="blocked_missing_manual_baseline",
        phase_hook_status="blocked_missing_manual_baseline",
        failing_phases="",
        final_target_status="not_evaluated",
        note="manual_baseline_missing",
    )
    return summary, hook_audit


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_readme(output_dir: Path, summary: RolloutSummary) -> None:
    content = f"""# Rollout Audit

This artifact is a structural multi-step P0-P9 rollout audit. It mutates truth2 vehicle line state by selected batches and records hook counts, but it is not a full physical route solver.

- truth_case_count: {summary.truth_case_count}
- matched_case_count: {summary.matched_case_count}
- completed_case_count: {summary.completed_case_count}
- blocked_case_count: {summary.blocked_case_count}
- total_hook_soft_pass_count: {summary.total_hook_soft_pass_count}
- phase_hook_soft_pass_case_count: {summary.phase_hook_soft_pass_case_count}
- hard_violation_count: {summary.hard_violation_count}
- state_loop_count: {summary.state_loop_count}
- rollout_gap_record_count: {summary.rollout_gap_record_count}

Files:

- `rollout_step_trace.csv`: selected P5-P9 step trace.
- `rollout_candidate_trace.csv`: generated candidates at each rollout step.
- `rollout_case_summary.csv`: case-level completion and hook-bound status.
- `rollout_phase_summary.csv`: H1-H5 phase hook-bound audit.
- `manual_vs_rollout_hook_audit.csv`: manual soft-bound comparison.
- `rollout_gap_audit.csv`: residual gaps that still block a strict "reach or exceed manual" claim.
"""
    (output_dir / "README.md").write_text(content, encoding="utf-8")


def build_summary(
    truth_count: int,
    manual_count: int,
    matched_count: int,
    case_rows: list[RolloutCaseSummary],
    step_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    gap_rows: list[RolloutGapRecord],
) -> RolloutSummary:
    status_counts = Counter(row.status for row in case_rows)
    blocked_reason_counts = Counter(row.blocked_reason for row in case_rows if row.blocked_reason)
    return RolloutSummary(
        truth_case_count=truth_count,
        manual_case_count=manual_count,
        matched_case_count=matched_count,
        completed_case_count=status_counts["completed"],
        blocked_case_count=sum(count for status, count in status_counts.items() if status != "completed"),
        missing_manual_case_count=status_counts["blocked_missing_manual_baseline"],
        total_hook_soft_pass_count=sum(1 for row in case_rows if row.hook_within_manual_soft_bound),
        phase_hook_soft_pass_case_count=sum(1 for row in case_rows if row.phase_hook_within_manual_soft_bound),
        hard_violation_count=sum(row.hard_violation_count for row in case_rows),
        state_loop_count=sum(row.state_loop_count for row in case_rows),
        rollout_step_record_count=len(step_rows),
        rollout_candidate_record_count=len(candidate_rows),
        rollout_gap_record_count=len(gap_rows),
        status_counts=dict(sorted(status_counts.items())),
        blocked_reason_counts=dict(sorted(blocked_reason_counts.items())),
    )


def gap_bucket_for(phase: str, variant: str, action_counts: str) -> tuple[str, str]:
    if variant == "MIXED_SIGNAL_REPAIR":
        return (
            "LOW_SIGNAL_PHASE_CONTRACT_TOO_COMPRESSED",
            "RepairInboundVariant + PhaseGate need a quantified conservative H2/H4/H5 split for low-signal cases.",
        )
    if phase == "H1":
        return (
            "FRONT_SERVICE_BATCHING_BELOW_MANUAL",
            "H1 candidate batching needs source/receiver co-carry optimization, not one source-target batch per hook.",
        )
    if phase == "H4":
        return (
            "DEPOT_SLOT_SWAP_BATCHING_BELOW_MANUAL",
            "DepotSlotGraph/DepotSwapDelta must merge compatible slot/band exchanges and prove ordered detach feasibility.",
        )
    if phase == "H5":
        return (
            "TAIL_REMAINDER_BATCHING_BELOW_MANUAL",
            "Tail closeout needs contract-family co-carry and receiver-aware batching before final return.",
        )
    return (
        "PHASE_HOOK_BOUND_EXCEEDED",
        "PhaseGate + optimizer need a tighter phase-local hook bound.",
    )


def build_gap_rows(
    case_rows: list[RolloutCaseSummary],
    phase_rows: list[RolloutPhaseSummary],
) -> list[RolloutGapRecord]:
    gaps: list[RolloutGapRecord] = []
    case_by_id = {row.case_id: row for row in case_rows}
    for row in case_rows:
        if row.status != "completed":
            gaps.append(
                RolloutGapRecord(
                    case_id=row.case_id,
                    variant=row.variant,
                    gap_type=row.status,
                    phase="",
                    rollout_value=row.final_unsatisfied_vehicle_count,
                    manual_soft_bound=None,
                    status="blocked",
                    action_family_counts="",
                    failure_bucket=row.blocked_reason or row.status,
                    next_required_structure="manual baseline or executable contract trace is required before strict comparison.",
                )
            )
        elif not row.hook_within_manual_soft_bound:
            gaps.append(
                RolloutGapRecord(
                    case_id=row.case_id,
                    variant=row.variant,
                    gap_type="case_total_hook_over_soft_bound",
                    phase="ALL",
                    rollout_value=row.rollout_hook_count,
                    manual_soft_bound=row.manual_soft_hook_upper_bound,
                    status="failed_strict_manual_hook_bound",
                    action_family_counts="",
                    failure_bucket="CASE_TOTAL_HOOK_OVER_MANUAL_SOFT_BOUND",
                    next_required_structure="ContractOptimizer must reduce total hooks or prove manual baseline omitted comparable obligations.",
                )
            )
    for phase_row in phase_rows:
        if phase_row.status == "passed":
            continue
        case_row = case_by_id.get(phase_row.case_id)
        bucket, next_structure = gap_bucket_for(phase_row.phase, phase_row.variant, phase_row.action_family_counts)
        gaps.append(
            RolloutGapRecord(
                case_id=phase_row.case_id,
                variant=phase_row.variant,
                gap_type="phase_hook_over_soft_bound",
                phase=phase_row.phase,
                rollout_value=phase_row.rollout_phase_hook_count,
                manual_soft_bound=phase_row.manual_phase_soft_upper_bound,
                status=phase_row.status,
                action_family_counts=phase_row.action_family_counts,
                failure_bucket=bucket,
                next_required_structure=next_structure,
            )
        )
    return gaps


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    manual_dir = root / args.manual_dir
    truth_dir = root / args.truth_dir
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    manual_audits = load_manual_audits(manual_dir, representative)
    contracts = contracts_by_case(manual_audits)
    truth_paths = [
        path
        for path in sorted(truth_dir.glob("validation_*.json"))
        if try_case_id_from_path(path)
        and (representative is None or try_case_id_from_path(path) in representative)
    ]

    step_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    phase_rows: list[RolloutPhaseSummary] = []
    case_rows: list[RolloutCaseSummary] = []
    hook_rows: list[ManualVsRolloutHookAudit] = []

    for truth_path in truth_paths:
        case_id = case_id_from_path(truth_path)
        audit = manual_audits.get(case_id)
        if audit is None:
            case_summary, hook_audit = missing_manual_case_summary(truth_path)
            case_rows.append(case_summary)
            hook_rows.append(hook_audit)
            continue
        case_steps, case_candidates, case_phases, case_summary, hook_audit = rollout_case(
            truth_path=truth_path,
            audit=audit,
            contracts=contracts.get(case_id, []),
            max_steps=args.max_steps,
        )
        step_rows.extend(case_steps)
        candidate_rows.extend(case_candidates)
        phase_rows.extend(case_phases)
        case_rows.append(case_summary)
        hook_rows.append(hook_audit)

    matched_count = len({case_id_from_path(path) for path in truth_paths} & set(manual_audits))
    gap_rows = build_gap_rows(case_rows, phase_rows)
    summary = build_summary(
        truth_count=len(truth_paths),
        manual_count=len(manual_audits),
        matched_count=matched_count,
        case_rows=case_rows,
        step_rows=step_rows,
        candidate_rows=candidate_rows,
        gap_rows=gap_rows,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "rollout_step_trace.csv", step_rows)
    write_csv(output_dir / "rollout_candidate_trace.csv", candidate_rows)
    write_csv(output_dir / "rollout_phase_summary.csv", [asdict(row) for row in phase_rows])
    write_csv(output_dir / "rollout_case_summary.csv", [asdict(row) for row in case_rows])
    write_csv(output_dir / "manual_vs_rollout_hook_audit.csv", [asdict(row) for row in hook_rows])
    write_csv(output_dir / "rollout_gap_audit.csv", [asdict(row) for row in gap_rows])
    write_json(output_dir / "rollout_summary.json", asdict(summary))
    write_readme(output_dir, summary)

    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'rollout_step_trace.csv'}")
    print(f"Wrote {output_dir / 'rollout_candidate_trace.csv'}")
    print(f"Wrote {output_dir / 'rollout_phase_summary.csv'}")
    print(f"Wrote {output_dir / 'rollout_case_summary.csv'}")
    print(f"Wrote {output_dir / 'manual_vs_rollout_hook_audit.csv'}")
    print(f"Wrote {output_dir / 'rollout_gap_audit.csv'}")
    print(f"Wrote {output_dir / 'rollout_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not case_rows:
            errors.append("rollout case summary is empty")
        if matched_count and not step_rows:
            errors.append("rollout step trace is empty for matched cases")
        if summary.hard_violation_count:
            errors.append("rollout accepted hard violations")
        if summary.state_loop_count:
            errors.append("rollout produced state loops")
        if args.strict_acceptance:
            if summary.completed_case_count != matched_count:
                errors.append("not every matched case completed")
            if summary.total_hook_soft_pass_count < matched_count:
                errors.append("not every matched case stayed within manual total hook soft bound")
            if summary.phase_hook_soft_pass_case_count < matched_count:
                errors.append("not every matched case stayed within manual phase hook soft bound")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a structural multi-step P0-P9 rollout audit from truth2 and manual H-phase contracts.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--manual-dir", default="data/人工调车数据")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="artifacts/rollout_audit")
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument(
        "--representative-cases",
        nargs="*",
        default=list(DEFAULT_REPRESENTATIVE_CASES),
    )
    parser.add_argument("--representative-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--strict-acceptance",
        action="store_true",
        help="Fail if every matched case is not completed and under manual soft hook bounds.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
