from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from math import sqrt
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Mapping, Sequence


JsonObject = dict[str, object]

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from solver_vnext import physical  # noqa: E402
from stage4_simple.scope import build_scope  # noqa: E402
from stage4_simple.search import (  # noqa: E402
    OperationTransitions,
    SearchNode,
    Stage4Problem,
)
from stage4_simple.solve import normalize_car  # noqa: E402


@dataclass(frozen=True)
class SequenceMetrics:
    operations: int
    get_hooks: int
    put_hooks: int
    get_car_moves: int
    put_car_moves: int
    partial_put_car_moves: int
    singleton_put_hooks: int
    multi_get_put_hooks: int
    partial_put_hooks: int
    multi_source_get_runs: int
    multi_put_runs: int
    max_get_run: int
    max_put_run: int
    max_carry_cars: int
    repeated_get_car_moves: int
    repeated_get_cars: int
    same_line_return_car_moves: int
    same_line_return_hooks: int


@dataclass(frozen=True)
class CoherenceMetrics:
    case_id: str
    business_hooks: int
    hook_lower_bound: int
    hook_optimality_gap: int
    active_count: int
    repeated_get_car_moves: int
    repeated_get_cars: int
    active_repeated_get_car_moves: int
    active_repeated_get_cars: int
    temporary_put_car_moves: int
    temporary_put_cars: int
    temporary_put_hooks: int
    temporary_recovery_get_hooks: int
    temporary_cycle_involved_hooks: int
    pure_temporary_put_hooks: int
    pure_temporary_recovery_get_hooks: int
    pure_temporary_cycle_hooks: int
    active_temporary_put_car_moves: int
    active_temporary_put_cars: int
    active_temporary_cycle_involved_hooks: int
    zero_net_relocation_car_moves: int
    zero_net_relocation_hooks: int
    active_zero_net_relocation_car_moves: int
    immediate_temporary_cycles: int
    average_temporary_cycle_span: float
    split_owner_put_excess: int
    split_owner_count: int
    target_reopens: int
    reopened_targets: Mapping[str, int]
    debt_only_target_reopens: int
    debt_only_reopened_targets: Mapping[str, int]
    semantic_target_reopens: int
    semantic_reopened_targets: Mapping[str, int]
    stage_hooks: int
    same_target_join_events: int
    cross_flow_join_events: int
    continued_owner_stack_events: int
    search_stop_reason: str
    evaluated_labels: int
    sequence: SequenceMetrics
    owner_fragments: Mapping[str, JsonObject]


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def operations_from_response(response: JsonObject) -> list[JsonObject]:
    data = response.get("Data")
    if not isinstance(data, dict):
        raise ValueError("response.Data must be an object")
    operations = data.get("Operations")
    if not isinstance(operations, list):
        raise ValueError("response.Data.Operations must be an array")
    return [operation for operation in operations if isinstance(operation, dict)]


def end_status_from_response(response: JsonObject) -> list[JsonObject]:
    data = response.get("Data")
    if not isinstance(data, dict):
        raise ValueError("response.Data must be an object")
    status = data.get("GeneratedEndStatus")
    if not isinstance(status, list):
        raise ValueError("response.Data.GeneratedEndStatus must be an array")
    return [car for car in status if isinstance(car, dict)]


def moved_cars(operation: JsonObject) -> tuple[str, ...]:
    value = operation.get("MoveCars")
    if not isinstance(value, list):
        return ()
    return tuple(str(no) for no in value)


def train_cars(operation: JsonObject) -> tuple[str, ...]:
    value = operation.get("TrainCars")
    if not isinstance(value, list):
        return ()
    return tuple(str(no) for no in value)


def action(operation: JsonObject) -> str:
    return str(operation.get("Action") or "")


def line(operation: JsonObject) -> str:
    return str(operation.get("Line") or "")


def run_lengths(operations: Sequence[JsonObject], wanted: str) -> list[int]:
    runs: list[int] = []
    current = 0
    for operation in operations:
        if action(operation) == wanted:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def sequence_metrics(operations: Sequence[JsonObject]) -> SequenceMetrics:
    get_runs = run_lengths(operations, "Get")
    put_runs = run_lengths(operations, "Put")
    gets_since_put: list[JsonObject] = []
    multi_get_put_hooks = 0
    multi_source_get_runs = 0
    get_counts: Counter[str] = Counter()
    carried_origins: dict[str, str] = {}
    zero_net_car_moves = 0
    zero_net_hooks: set[int] = set()
    for index, operation in enumerate(operations, start=1):
        current_action = action(operation)
        if current_action == "Get":
            gets_since_put.append(operation)
            for no in moved_cars(operation):
                get_counts[no] += 1
                carried_origins[no] = line(operation)
            continue
        if current_action != "Put":
            continue
        for no in moved_cars(operation):
            if carried_origins.get(no) == line(operation):
                zero_net_car_moves += 1
                zero_net_hooks.add(index)
            carried_origins.pop(no, None)
        if len(gets_since_put) >= 2:
            multi_get_put_hooks += 1
            if len({line(item) for item in gets_since_put}) >= 2:
                multi_source_get_runs += 1
        gets_since_put.clear()
    get_hooks = [operation for operation in operations if action(operation) == "Get"]
    put_hooks = [operation for operation in operations if action(operation) == "Put"]
    partial_puts = [operation for operation in put_hooks if train_cars(operation)]
    return SequenceMetrics(
        operations=len(operations),
        get_hooks=len(get_hooks),
        put_hooks=len(put_hooks),
        get_car_moves=sum(len(moved_cars(operation)) for operation in get_hooks),
        put_car_moves=sum(len(moved_cars(operation)) for operation in put_hooks),
        partial_put_car_moves=sum(
            len(moved_cars(operation)) for operation in partial_puts
        ),
        singleton_put_hooks=sum(
            len(moved_cars(operation)) == 1 for operation in put_hooks
        ),
        multi_get_put_hooks=multi_get_put_hooks,
        partial_put_hooks=sum(
            action(operation) == "Put" and bool(train_cars(operation))
            for operation in operations
        ),
        multi_source_get_runs=multi_source_get_runs,
        multi_put_runs=sum(length >= 2 for length in put_runs),
        max_get_run=max(get_runs, default=0),
        max_put_run=max(put_runs, default=0),
        max_carry_cars=max((len(train_cars(operation)) for operation in operations), default=0),
        repeated_get_car_moves=sum(max(0, count - 1) for count in get_counts.values()),
        repeated_get_cars=sum(count > 1 for count in get_counts.values()),
        same_line_return_car_moves=zero_net_car_moves,
        same_line_return_hooks=len(zero_net_hooks),
    )


def final_lines(status: Iterable[JsonObject]) -> dict[str, str]:
    return {
        str(car.get("No")): str(car.get("Line") or "")
        for car in status
        if car.get("No")
    }


def trace_event_counts(trace: Sequence[JsonObject]) -> Counter[str]:
    return Counter(str(event.get("event") or "") for event in trace)


def analyze_case(
    case_id: str,
    summary: JsonObject,
    response: JsonObject,
    trace: Sequence[JsonObject],
) -> CoherenceMetrics:
    operations = operations_from_response(response)
    destinations = final_lines(end_status_from_response(response))
    active = {str(no) for no in summary.get("active_nos", [])}
    get_counts: Counter[str] = Counter()
    active_get_counts: Counter[str] = Counter()
    carried_origins: dict[str, str] = {}
    temporary_open: dict[str, tuple[int, str]] = {}
    temporary_put_cars: set[str] = set()
    temporary_put_hooks: set[int] = set()
    temporary_get_hooks: set[int] = set()
    pure_temporary_put_hooks: set[int] = set()
    pure_temporary_get_hooks: set[int] = set()
    active_temporary_put_cars: set[str] = set()
    active_temporary_put_hooks: set[int] = set()
    active_temporary_get_hooks: set[int] = set()
    zero_net_hooks: set[int] = set()
    zero_net_car_moves = 0
    active_zero_net_car_moves = 0
    cycle_spans: list[int] = []
    owner_final_puts: Counter[str] = Counter()
    owner_temporary_lines: defaultdict[str, set[str]] = defaultdict(set)
    owner_temporary_puts: Counter[str] = Counter()
    sealed_targets: set[str] = set()
    reopened_targets: Counter[str] = Counter()

    for index, operation in enumerate(operations, start=1):
        current_action = action(operation)
        current_line = line(operation)
        moved = tuple(no for no in moved_cars(operation) if no in destinations)
        moved_active = tuple(no for no in moved if no in active)
        if current_action == "Get":
            if current_line in sealed_targets:
                reopened_targets[current_line] += 1
                sealed_targets.remove(current_line)
            recovered = {no for no in moved if no in temporary_open}
            if moved and recovered == set(moved):
                pure_temporary_get_hooks.add(index)
            for no in moved:
                get_counts[no] += 1
                if no in active:
                    active_get_counts[no] += 1
                carried_origins[no] = current_line
                temporary = temporary_open.pop(no, None)
                if temporary is not None:
                    put_index, parked_line = temporary
                    if parked_line != current_line:
                        raise ValueError(
                            f"{case_id}: car {no} recovered from {current_line}, "
                            f"expected {parked_line}"
                        )
                    temporary_get_hooks.add(index)
                    if no in active:
                        active_temporary_get_hooks.add(index)
                    cycle_spans.append(index - put_index)
            continue
        if current_action != "Put":
            continue
        if any(destinations.get(no) == current_line for no in moved):
            sealed_targets.add(current_line)
        if moved and all(destinations.get(no) != current_line for no in moved):
            pure_temporary_put_hooks.add(index)
        final_owners = {
            destinations.get(no, "")
            for no in moved_active
            if destinations.get(no)
        }
        for owner in final_owners:
            if any(destinations.get(no) == owner for no in moved_active):
                if current_line == owner:
                    owner_final_puts[owner] += 1
        for no in moved:
            destination = destinations.get(no, "")
            if not destination or current_line == destination:
                carried_origins.pop(no, None)
                continue
            temporary_put_cars.add(no)
            temporary_put_hooks.add(index)
            temporary_open[no] = (index, current_line)
            if no in active:
                active_temporary_put_cars.add(no)
                active_temporary_put_hooks.add(index)
                owner_temporary_lines[destination].add(current_line)
                owner_temporary_puts[destination] += 1
            if carried_origins.get(no) == current_line:
                zero_net_car_moves += 1
                zero_net_hooks.add(index)
                if no in active:
                    active_zero_net_car_moves += 1
            carried_origins.pop(no, None)

    repeated_gets = sum(max(0, count - 1) for count in get_counts.values())
    if temporary_open:
        raise ValueError(
            f"{case_id}: unrecovered temporary cars {sorted(temporary_open)}"
        )
    active_repeated_gets = sum(
        max(0, count - 1) for count in active_get_counts.values()
    )
    split_owners = {
        owner: count for owner, count in owner_final_puts.items() if count > 1
    }
    owner_fragments: dict[str, JsonObject] = {}
    for owner in sorted(set(owner_final_puts) | set(owner_temporary_lines)):
        owner_fragments[owner] = {
            "final_put_batches": owner_final_puts[owner],
            "temporary_put_car_moves": owner_temporary_puts[owner],
            "temporary_lines": sorted(owner_temporary_lines[owner]),
        }

    events = trace_event_counts(trace)
    objective = summary.get("objective")
    if (
        isinstance(objective, list)
        and len(objective) >= 2
        and repeated_gets != int(objective[1])
    ):
        raise ValueError(
            f"{case_id}: replayed repeated gets {repeated_gets} "
            f"do not match objective {objective[1]}"
        )
    target_reopens = (
        int(objective[2])
        if isinstance(objective, list) and len(objective) >= 3
        else 0
    )
    if target_reopens != sum(reopened_targets.values()):
        raise ValueError(
            f"{case_id}: replayed target reopens {sum(reopened_targets.values())} "
            f"do not match objective {target_reopens}"
        )
    stage_events = {
        "stage",
        "stage_owner_block",
        "target_stack",
        "lease_paint_tail_on_c4",
        "resource_owner_merge",
        "park_capacity_holdout",
    }
    return CoherenceMetrics(
        case_id=case_id,
        business_hooks=int(summary.get("business_hooks") or len(operations)),
        hook_lower_bound=int(summary.get("hook_lower_bound") or 0),
        hook_optimality_gap=int(summary.get("hook_optimality_gap") or 0),
        active_count=len(active),
        repeated_get_car_moves=repeated_gets,
        repeated_get_cars=sum(count > 1 for count in get_counts.values()),
        active_repeated_get_car_moves=active_repeated_gets,
        active_repeated_get_cars=sum(
            count > 1 for count in active_get_counts.values()
        ),
        temporary_put_car_moves=sum(
            destinations.get(no, "") != line(operation)
            for operation in operations
            if action(operation) == "Put"
            for no in moved_cars(operation)
            if no in destinations
        ),
        temporary_put_cars=len(temporary_put_cars),
        temporary_put_hooks=len(temporary_put_hooks),
        temporary_recovery_get_hooks=len(temporary_get_hooks),
        temporary_cycle_involved_hooks=len(temporary_put_hooks | temporary_get_hooks),
        pure_temporary_put_hooks=len(pure_temporary_put_hooks),
        pure_temporary_recovery_get_hooks=len(pure_temporary_get_hooks),
        pure_temporary_cycle_hooks=len(
            pure_temporary_put_hooks | pure_temporary_get_hooks
        ),
        active_temporary_put_car_moves=sum(owner_temporary_puts.values()),
        active_temporary_put_cars=len(active_temporary_put_cars),
        active_temporary_cycle_involved_hooks=len(
            active_temporary_put_hooks | active_temporary_get_hooks
        ),
        zero_net_relocation_car_moves=zero_net_car_moves,
        zero_net_relocation_hooks=len(zero_net_hooks),
        active_zero_net_relocation_car_moves=active_zero_net_car_moves,
        immediate_temporary_cycles=sum(span <= 2 for span in cycle_spans),
        average_temporary_cycle_span=round(mean(cycle_spans), 3) if cycle_spans else 0.0,
        split_owner_put_excess=sum(count - 1 for count in split_owners.values()),
        split_owner_count=len(split_owners),
        target_reopens=target_reopens,
        reopened_targets=dict(sorted(reopened_targets.items())),
        debt_only_target_reopens=0,
        debt_only_reopened_targets={},
        semantic_target_reopens=0,
        semantic_reopened_targets={},
        stage_hooks=sum(events[event] for event in stage_events),
        same_target_join_events=events["same_target_join"],
        cross_flow_join_events=events["cross_flow_join"],
        continued_owner_stack_events=events["continue_owner_stack"],
        search_stop_reason=str(summary.get("search_stop_reason") or ""),
        evaluated_labels=int(summary.get("evaluated_labels") or 0),
        sequence=sequence_metrics(operations),
        owner_fragments=owner_fragments,
    )


def target_window_reopens(
    case_id: str,
    request: JsonObject,
    response: JsonObject,
    assignment: physical.DepotAssignment,
) -> tuple[Counter[str], Counter[str]]:
    start_status = request.get("StartStatus")
    if not isinstance(start_status, list):
        raise ValueError(f"{case_id}: stage4 request StartStatus missing")
    cars = [normalize_car(car) for car in start_status if isinstance(car, dict)]
    scope = build_scope(cars, assignment)
    loco_node = request.get("locoNode")
    if not isinstance(loco_node, dict):
        raise ValueError(f"{case_id}: stage4 request locoNode missing")
    problem = Stage4Problem(
        case_id=case_id,
        cars=cars,
        loco_location=physical.LocoLocation(
            physical.normalize_line(loco_node.get("Line"))
        ),
        depot_assignment=assignment,
        target_by_no=scope.target_by_no,
        active_nos=scope.active_nos,
        protected_nos=scope.protected_nos,
        capacity_holdout_nos=scope.infeasible_nos,
    )
    transitions = OperationTransitions(problem)
    node = SearchNode(physical.initial_planlet_state(cars, problem.loco_location))
    closed: set[str] = set()
    debt_reopened: Counter[str] = Counter()
    semantic_reopened: Counter[str] = Counter()
    active_targets = {
        problem.target_by_no.get(no, "") for no in problem.active_nos
    }
    for operation in operations_from_response(response):
        current_action = action(operation)
        current_line = physical.normalize_line(line(operation))
        if current_action == "Get" and current_line in closed:
            debt_reopened[current_line] += 1
            closed.remove(current_line)
        positions = operation.get("Positions")
        planned_positions = {
            str(no): int(position)
            for no, position in positions.items()
        } if isinstance(positions, dict) else {}
        successor = transitions.apply_step(
            node,
            physical.plan_step(
                current_action,
                current_line,
                moved_cars(operation),
                planned_positions,
            ),
        )
        if successor is None:
            raise ValueError(
                f"{case_id}: semantic replay rejected operation "
                f"{operation.get('Index')}"
            )
        if successor.cost.target_reopens > node.cost.target_reopens:
            semantic_reopened[current_line] += 1
        node = successor
        if current_action != "Put" or current_line not in active_targets:
            continue
        pending = problem.unsatisfied_active(node.state)
        if not any(
            problem.target_by_no.get(no) == current_line for no in pending
        ):
            closed.add(current_line)
    return debt_reopened, semantic_reopened


def stage4_cases(root: Path, data_root: Path) -> list[CoherenceMetrics]:
    cases: list[CoherenceMetrics] = []
    for summary_path in sorted(root.glob("truth[23]/[0-9]*_summary.json")):
        case_id = summary_path.name.removesuffix("_summary.json")
        base = summary_path.parent / case_id
        summary = load_json(summary_path)
        response_data = load_json(base.with_name(f"{case_id}_response.json"))
        trace_data = load_json(base.with_name(f"{case_id}_trace.json"))
        request_data = load_json(base.with_name(f"{case_id}_stage4_request.json"))
        if not isinstance(summary, dict) or not isinstance(response_data, dict):
            raise ValueError(f"{case_id}: invalid Stage4 artifact object")
        if not isinstance(request_data, dict):
            raise ValueError(f"{case_id}: invalid Stage4 request object")
        if not isinstance(trace_data, list):
            raise ValueError(f"{case_id}: trace must be an array")
        response = {
            "Data": response_data.get("Data"),
        }
        source_cases = list(
            (data_root / summary_path.parent.name).glob(f"*2026{case_id}.json")
        )
        if len(source_cases) != 1:
            raise ValueError(
                f"{case_id}: expected one source case, found {len(source_cases)}"
            )
        _source_id, _request, _cars, assignment, _loco = physical.read_case(
            source_cases[0]
        )
        debt_only, semantic = target_window_reopens(
            case_id,
            request_data,
            response,
            assignment,
        )
        cases.append(replace(
            analyze_case(case_id, summary, response, trace_data),
            debt_only_target_reopens=sum(debt_only.values()),
            debt_only_reopened_targets=dict(debt_only.most_common()),
            semantic_target_reopens=sum(semantic.values()),
            semantic_reopened_targets=dict(semantic.most_common()),
        ))
    return cases


def manual_sequences(root: Path) -> list[tuple[str, SequenceMetrics]]:
    result: list[tuple[str, SequenceMetrics]] = []
    for path in sorted(root.glob("*.json")):
        bundle = load_json(path)
        if not isinstance(bundle, dict):
            continue
        response = bundle.get("Response")
        if not isinstance(response, dict):
            continue
        result.append((path.name.split("_", 1)[0], sequence_metrics(
            operations_from_response(response)
        )))
    return result


def manual_final_lines(root: Path) -> tuple[dict[str, dict[str, str]], set[str]]:
    result: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()
    for path in sorted(root.glob("*.json")):
        bundle = load_json(path)
        if not isinstance(bundle, dict):
            continue
        response = bundle.get("Response")
        if not isinstance(response, dict):
            continue
        case_id = path.name.split("_", 1)[0]
        if case_id in result:
            duplicates.add(case_id)
            continue
        result[case_id] = final_lines(end_status_from_response(response))
    return result, duplicates


def manual_goal_compatibility(stage4_root: Path, manual_root: Path) -> JsonObject:
    manual, duplicates = manual_final_lines(manual_root)
    rows: list[JsonObject] = []
    target_changes: Counter[tuple[str, str]] = Counter()
    target_domain_sizes: Counter[int] = Counter()
    within_domain_changes = 0
    outside_domain_changes = 0
    for summary_path in sorted(stage4_root.glob("truth[23]/[0-9]*_summary.json")):
        case_id = summary_path.name.removesuffix("_summary.json")
        if case_id not in manual or case_id in duplicates:
            continue
        summary = load_json(summary_path)
        response = load_json(summary_path.with_name(f"{case_id}_response.json"))
        if not isinstance(summary, dict) or not isinstance(response, dict):
            continue
        data = response.get("Data")
        if not isinstance(data, dict):
            continue
        generated = data.get("GeneratedEndStatus")
        if not isinstance(generated, list):
            continue
        request = load_json(summary_path.with_name(f"{case_id}_stage4_request.json"))
        if not isinstance(request, dict):
            continue
        start_status = request.get("StartStatus")
        if not isinstance(start_status, list):
            continue
        start_by_no = {
            str(car.get("No")): car
            for car in start_status
            if isinstance(car, dict) and car.get("No")
        }
        stage4_lines = final_lines([
            car for car in generated
            if isinstance(car, dict)
        ])
        active = {str(no) for no in summary.get("active_nos", [])}
        overlap = active & set(stage4_lines) & set(manual[case_id])
        changed = {
            no for no in overlap
            if stage4_lines[no] != manual[case_id][no]
        }
        for no in overlap:
            targets = start_by_no.get(no, {}).get("TargetLines")
            target_domain_sizes[len(set(targets or []))] += 1
        for no in changed:
            target_changes[(manual[case_id][no], stage4_lines[no])] += 1
            targets = set(start_by_no.get(no, {}).get("TargetLines") or [])
            if manual[case_id][no] in targets:
                within_domain_changes += 1
            else:
                outside_domain_changes += 1
        rows.append({
            "case_id": case_id,
            "active_overlap": len(overlap),
            "different_targets": len(changed),
            "business_hooks": int(summary.get("business_hooks") or 0),
            "hook_optimality_gap": int(summary.get("hook_optimality_gap") or 0),
        })
    overlap_count = sum(int(row["active_overlap"]) for row in rows)
    changed_count = sum(int(row["different_targets"]) for row in rows)
    return {
        "comparable_cases": len(rows),
        "duplicate_case_ids_skipped": sorted(duplicates),
        "active_vehicle_overlap": overlap_count,
        "different_target_lines": changed_count,
        "different_target_share": round(changed_count / overlap_count, 4)
        if overlap_count else 0.0,
        "different_within_stage4_domain": within_domain_changes,
        "manual_outside_stage4_domain": outside_domain_changes,
        "stage4_target_domain_size_counts": {
            str(size): count for size, count in sorted(target_domain_sizes.items())
        },
        "top_target_changes": [
            {"manual": pair[0], "stage4": pair[1], "count": count}
            for pair, count in target_changes.most_common(30)
        ],
        "top_cases": sorted(
            rows,
            key=lambda row: (
                int(row["different_targets"]),
                int(row["business_hooks"]),
            ),
            reverse=True,
        )[:30],
    }


def percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def pearson(left: Sequence[int], right: Sequence[int]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    denominator = sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return round(numerator / denominator, 4) if denominator else 0.0


def cohort_summary(cases: Sequence[CoherenceMetrics]) -> JsonObject:
    if not cases:
        return {"cases": 0}
    return {
        "cases": len(cases),
        "average_hooks": round(mean(case.business_hooks for case in cases), 3),
        "average_cycle_hooks": round(
            mean(case.temporary_cycle_involved_hooks for case in cases), 3
        ),
        "average_repeated_get_car_moves": round(
            mean(case.repeated_get_car_moves for case in cases), 3
        ),
        "average_target_reopens": round(
            mean(case.target_reopens for case in cases), 3
        ),
        "average_split_owner_put_excess": round(
            mean(case.split_owner_put_excess for case in cases), 3
        ),
    }


def sequence_aggregate(values: Sequence[SequenceMetrics]) -> JsonObject:
    put_hooks = sum(value.put_hooks for value in values)
    get_hooks = sum(value.get_hooks for value in values)
    partial_put_hooks = sum(value.partial_put_hooks for value in values)
    operations = sum(value.operations for value in values)
    return {
        "plans": len(values),
        "operations": operations,
        "get_hooks": get_hooks,
        "put_hooks": put_hooks,
        "average_get_batch": round(
            sum(value.get_car_moves for value in values) / get_hooks, 3
        ) if get_hooks else 0.0,
        "average_put_batch": round(
            sum(value.put_car_moves for value in values) / put_hooks, 3
        ) if put_hooks else 0.0,
        "average_partial_put_batch": round(
            sum(value.partial_put_car_moves for value in values) / partial_put_hooks,
            3,
        ) if partial_put_hooks else 0.0,
        "singleton_put_hooks": sum(value.singleton_put_hooks for value in values),
        "singleton_put_share": round(
            sum(value.singleton_put_hooks for value in values) / put_hooks, 4
        ) if put_hooks else 0.0,
        "multi_get_put_hooks": sum(value.multi_get_put_hooks for value in values),
        "multi_get_put_share_of_puts": round(
            sum(value.multi_get_put_hooks for value in values) / put_hooks, 4
        ) if put_hooks else 0.0,
        "partial_put_hooks": partial_put_hooks,
        "partial_put_share_of_puts": round(
            sum(value.partial_put_hooks for value in values) / put_hooks, 4
        ) if put_hooks else 0.0,
        "multi_source_get_runs": sum(value.multi_source_get_runs for value in values),
        "multi_put_runs": sum(value.multi_put_runs for value in values),
        "max_get_run": max((value.max_get_run for value in values), default=0),
        "max_put_run": max((value.max_put_run for value in values), default=0),
        "max_carry_cars": max((value.max_carry_cars for value in values), default=0),
        "repeated_get_car_moves": sum(
            value.repeated_get_car_moves for value in values
        ),
        "repeated_get_car_moves_per_100_hooks": round(
            100 * sum(value.repeated_get_car_moves for value in values) / operations,
            3,
        ) if operations else 0.0,
        "repeated_get_cars": sum(value.repeated_get_cars for value in values),
        "same_line_return_car_moves": sum(
            value.same_line_return_car_moves for value in values
        ),
        "same_line_return_hooks": sum(
            value.same_line_return_hooks for value in values
        ),
    }


def stage4_aggregate(cases: Sequence[CoherenceMetrics]) -> JsonObject:
    hooks = [case.business_hooks for case in cases]
    gaps = [case.hook_optimality_gap for case in cases]
    cycle_hooks = [case.temporary_cycle_involved_hooks for case in cases]
    repeated_gets = [case.repeated_get_car_moves for case in cases]
    reopens = [case.target_reopens for case in cases]
    split_owners = [case.split_owner_put_excess for case in cases]
    stage_hooks = [case.stage_hooks for case in cases]
    reopened_targets: Counter[str] = Counter()
    debt_only_reopened_targets: Counter[str] = Counter()
    semantic_reopened_targets: Counter[str] = Counter()
    for case in cases:
        reopened_targets.update(case.reopened_targets)
        debt_only_reopened_targets.update(case.debt_only_reopened_targets)
        semantic_reopened_targets.update(case.semantic_reopened_targets)
    return {
        "cases": len(cases),
        "business_hooks": sum(hooks),
        "average_hooks": round(mean(hooks), 3),
        "median_hooks": median(hooks),
        "p90_hooks": percentile(hooks, 0.9),
        "average_optimality_gap": round(mean(gaps), 3),
        "repeated_get_car_moves": sum(case.repeated_get_car_moves for case in cases),
        "repeated_get_cars": sum(case.repeated_get_cars for case in cases),
        "active_repeated_get_car_moves": sum(
            case.active_repeated_get_car_moves for case in cases
        ),
        "active_repeated_get_cars": sum(
            case.active_repeated_get_cars for case in cases
        ),
        "temporary_put_car_moves": sum(case.temporary_put_car_moves for case in cases),
        "temporary_put_cars": sum(case.temporary_put_cars for case in cases),
        "temporary_put_hooks": sum(case.temporary_put_hooks for case in cases),
        "temporary_recovery_get_hooks": sum(
            case.temporary_recovery_get_hooks for case in cases
        ),
        "temporary_cycle_involved_hooks": sum(cycle_hooks),
        "temporary_cycle_hook_share": round(sum(cycle_hooks) / sum(hooks), 4),
        "pure_temporary_put_hooks": sum(
            case.pure_temporary_put_hooks for case in cases
        ),
        "pure_temporary_recovery_get_hooks": sum(
            case.pure_temporary_recovery_get_hooks for case in cases
        ),
        "pure_temporary_cycle_hooks": sum(
            case.pure_temporary_cycle_hooks for case in cases
        ),
        "pure_temporary_cycle_hook_share": round(
            sum(case.pure_temporary_cycle_hooks for case in cases) / sum(hooks),
            4,
        ),
        "active_temporary_put_car_moves": sum(
            case.active_temporary_put_car_moves for case in cases
        ),
        "active_temporary_put_cars": sum(
            case.active_temporary_put_cars for case in cases
        ),
        "active_temporary_cycle_involved_hooks": sum(
            case.active_temporary_cycle_involved_hooks for case in cases
        ),
        "zero_net_relocation_car_moves": sum(
            case.zero_net_relocation_car_moves for case in cases
        ),
        "zero_net_relocation_hooks": sum(case.zero_net_relocation_hooks for case in cases),
        "active_zero_net_relocation_car_moves": sum(
            case.active_zero_net_relocation_car_moves for case in cases
        ),
        "split_owner_put_excess": sum(case.split_owner_put_excess for case in cases),
        "target_reopens": sum(case.target_reopens for case in cases),
        "reopened_targets": dict(reopened_targets.most_common()),
        "debt_only_target_reopens": sum(
            case.debt_only_target_reopens for case in cases
        ),
        "debt_only_reopened_targets": dict(
            debt_only_reopened_targets.most_common()
        ),
        "semantic_target_reopens": sum(
            case.semantic_target_reopens for case in cases
        ),
        "semantic_reopened_targets": dict(
            semantic_reopened_targets.most_common()
        ),
        "stage_hooks": sum(case.stage_hooks for case in cases),
        "same_target_join_events": sum(case.same_target_join_events for case in cases),
        "cross_flow_join_events": sum(case.cross_flow_join_events for case in cases),
        "continued_owner_stack_events": sum(
            case.continued_owner_stack_events for case in cases
        ),
        "label_budget_exhausted_cases": sum(
            case.search_stop_reason == "label_budget_exhausted" for case in cases
        ),
        "correlations_with_hooks": {
            "temporary_cycle_hooks": pearson(hooks, cycle_hooks),
            "repeated_get_car_moves": pearson(hooks, repeated_gets),
            "target_reopens": pearson(hooks, reopens),
            "split_owner_put_excess": pearson(hooks, split_owners),
            "stage_hooks": pearson(hooks, stage_hooks),
            "active_count": pearson(hooks, [case.active_count for case in cases]),
        },
        "correlations_with_optimality_gap": {
            "temporary_cycle_hooks": pearson(gaps, cycle_hooks),
            "repeated_get_car_moves": pearson(gaps, repeated_gets),
            "target_reopens": pearson(gaps, reopens),
            "split_owner_put_excess": pearson(gaps, split_owners),
            "stage_hooks": pearson(gaps, stage_hooks),
        },
        "cohorts": {
            "hooks_at_least_25": cohort_summary([
                case for case in cases if case.business_hooks >= 25
            ]),
            "hooks_below_25": cohort_summary([
                case for case in cases if case.business_hooks < 25
            ]),
            "label_budget_exhausted": cohort_summary([
                case for case in cases
                if case.search_stop_reason == "label_budget_exhausted"
            ]),
            "label_frontier_exhausted": cohort_summary([
                case for case in cases
                if case.search_stop_reason == "label_frontier_exhausted"
            ]),
        },
        "sequence": sequence_aggregate([case.sequence for case in cases]),
    }


def report(stage4_root: Path, manual_root: Path, data_root: Path) -> JsonObject:
    cases = stage4_cases(stage4_root, data_root)
    manual = manual_sequences(manual_root)
    ranked = sorted(
        cases,
        key=lambda case: (
            case.temporary_cycle_involved_hooks,
            case.repeated_get_car_moves,
            case.business_hooks,
        ),
        reverse=True,
    )
    return {
        "definitions": {
            "temporary_put": "car Put to a line different from its generated final line",
            "active_temporary_put": "currently unsatisfied active car Put to a line different from its generated final line",
            "temporary_cycle_involved_hook": "Put or later Get hook participating in at least one temporary parking cycle",
            "zero_net_relocation": "active car is Put back to the line from which its current carry segment was obtained",
            "multi_get_put": "Put immediately preceded by at least two consecutive Get hooks",
            "partial_put": "Put leaves at least one car on the locomotive",
            "target_reopen": "SearchCost soft metric: a line is Get after any target-compatible Put",
            "debt_only_target_reopen": "a line is Get after active target and position debt temporarily reached zero",
            "semantic_target_reopen": "a SEALED line is Get after target debt, restore obligations, source use and gate use all reached zero",
        },
        "stage4": stage4_aggregate(cases),
        "manual_structure": sequence_aggregate([value for _case, value in manual]),
        "manual_goal_compatibility": manual_goal_compatibility(
            stage4_root,
            manual_root,
        ),
        "top_coherence_burden": [asdict(case) for case in ranked[:30]],
        "cases": [asdict(case) for case in sorted(cases, key=lambda case: case.case_id)],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Stage4 plan coherence")
    parser.add_argument(
        "--stage4-root",
        type=Path,
        default=Path("artifacts/stage4_block_flow_final"),
    )
    parser.add_argument(
        "--manual-root",
        type=Path,
        default=Path("artifacts/manual_restored_interface/bundles"),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = report(args.stage4_root, args.manual_root, args.data_root)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out is None:
        print(rendered, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
