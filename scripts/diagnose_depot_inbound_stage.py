#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import depot_inbound_plan
from solver_vnext import depot_outbound_plan
from solver_vnext import physical
from analyze_depot_inbound_stage import (
    _analyze_case,
    _initial_depot_inbound_debt,
    _is_assembly_contamination_line,
    _read_operations,
)


ASSEMBLY_LINES = set(physical.DEPOT_INBOUND_ASSEMBLY_LINES)
DESTINATION_LINES = physical.DEPOT_INBOUND_DESTINATION_LINES


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose the depot inbound first-stage assembly chain.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "depot_inbound_stage_diagnosis"
    output_dir.mkdir(parents=True, exist_ok=True)

    initial = _initial_depot_inbound_debt(truth_dir)
    operations = _read_operations(artifact_dir / "operation_trace.csv")
    case_rows = {
        case_id: _analyze_case(case_id=case_id, case=case, operations=operations.get(case_id, {}))
        for case_id, case in sorted(initial.items())
    }
    stage_limits = {
        case_id: _stage_trace_limit(row)
        for case_id, row in case_rows.items()
    }

    step_rows = _read_csv(artifact_dir / "step_trace.csv")
    selected_stage_steps = [
        row for row in step_rows
        if _selected(row) and _in_stage(row, stage_limits)
    ]
    generation_rows = _read_csv(artifact_dir / "generation_gap_records.csv")
    stage_generation_rows = [
        row for row in generation_rows
        if _in_stage(row, stage_limits)
    ]
    structure_rows = _read_csv(artifact_dir / "resource_structure_records.csv")
    inbound_plan_rows = [
        row for row in structure_rows
        if row.get("structure") == "DEPOT_INBOUND_ASSEMBLY_PLAN" and _in_stage(row, stage_limits)
    ]

    contamination_events, contamination_final = _contamination_diagnostics(
        initial=initial,
        operations=operations,
        case_rows=case_rows,
    )
    ungrouped_rows = _ungrouped_rows(initial=initial, case_rows=case_rows)
    source_prefix_rows, source_prefix_blocker_rows = _source_prefix_rows(
        truth_dir=truth_dir,
        ungrouped_rows=ungrouped_rows,
    )
    checkpoint_prefix_rows, checkpoint_route_rows = _checkpoint_blocker_rows(
        truth_dir=truth_dir,
        operations=operations,
        case_rows=case_rows,
    )
    mixed_window_rows = _mixed_extraction_window_rows(
        truth_dir=truth_dir,
        operations=operations,
        case_rows=case_rows,
    )
    plan_rows = _inbound_plan_rows(inbound_plan_rows)
    selected_summary = _selected_summary(selected_stage_steps)
    generation_summary = _generation_summary(stage_generation_rows)

    summary = {
        "artifact_dir": str(artifact_dir),
        "case_count": len(case_rows),
        "debt_count": sum(row["debt_count"] for row in case_rows.values()),
        "grouped_at_checkpoint_count": sum(row["grouped_at_checkpoint_count"] for row in case_rows.values()),
        "ungrouped_at_checkpoint_count": sum(row["ungrouped_at_checkpoint_count"] for row in case_rows.values()),
        "grouping_rate": _ratio(
            sum(row["grouped_at_checkpoint_count"] for row in case_rows.values()),
            sum(row["debt_count"] for row in case_rows.values()),
        ),
        "stage_complete_case_count": sum(row["stage_complete"] for row in case_rows.values()),
        "contaminated_case_count": sum(1 for row in case_rows.values() if row["contamination_count"]),
        "contamination_count": sum(row["contamination_count"] for row in case_rows.values()),
        "polluting_event_count": len(contamination_events),
        "selected_hook_count_by_template": selected_summary["hook_count_by_template"],
        "selected_car_count_by_template": selected_summary["car_count_by_template"],
        "selected_hook_count_by_phase_intent": selected_summary["hook_count_by_phase_intent"],
        "generation_gap_count_by_reason": generation_summary["reason_counts"],
        "generation_gap_count_by_family": generation_summary["family_counts"],
        "inbound_plan_status_counts": dict(Counter(row["status"] for row in plan_rows)),
        "inbound_plan_violation_counts": dict(Counter(row["violation"] for row in plan_rows if row["violation"])),
        "latest_plan_reason_counts": _latest_plan_reason_counts(plan_rows),
        "ungrouped_by_source_counts": dict(Counter(row["source_line"] for row in ungrouped_rows)),
        "ungrouped_by_target_counts": dict(Counter(row["target_line"] for row in ungrouped_rows)),
        "ungrouped_prefix_blocker_count": sum(row["prefix_blocker_count"] for row in source_prefix_rows),
        "ungrouped_non_inbound_prefix_blocker_count": sum(row["non_inbound_prefix_blocker_count"] for row in source_prefix_rows),
        "ungrouped_prefix_blocker_by_source_counts": dict(
            _sum_counter(source_prefix_rows, "source_line", "prefix_blocker_count")
        ),
        "ungrouped_non_inbound_prefix_blocker_by_source_counts": dict(
            _sum_counter(source_prefix_rows, "source_line", "non_inbound_prefix_blocker_count")
        ),
        "prefix_blocker_fact_count": len(source_prefix_blocker_rows),
        "prefix_blocker_fact_by_source_counts": dict(Counter(row["source_line"] for row in source_prefix_blocker_rows)),
        "prefix_blocker_fact_by_target_counts": dict(Counter(row["blocker_target_line"] for row in source_prefix_blocker_rows)),
        "non_inbound_prefix_blocker_fact_by_target_counts": dict(
            Counter(row["blocker_target_line"] for row in source_prefix_blocker_rows if not row["blocker_is_inbound_target"])
        ),
        "checkpoint_prefix_blocker_count": sum(row["prefix_blocker_count"] for row in checkpoint_prefix_rows),
        "checkpoint_non_inbound_prefix_blocker_count": sum(row["non_inbound_prefix_blocker_count"] for row in checkpoint_prefix_rows),
        "checkpoint_prefix_blocker_by_current_line_counts": dict(
            _sum_counter(checkpoint_prefix_rows, "current_line", "prefix_blocker_count")
        ),
        "checkpoint_route_blocker_fact_count": len(checkpoint_route_rows),
        "checkpoint_cross_line_route_blocker_fact_count": sum(
            1 for row in checkpoint_route_rows if row["blocker_line"] != row["current_line"]
        ),
        "checkpoint_route_blocker_by_blocker_line_counts": dict(Counter(row["blocker_line"] for row in checkpoint_route_rows)),
        "checkpoint_route_blocker_by_current_line_counts": dict(Counter(row["current_line"] for row in checkpoint_route_rows)),
        "checkpoint_route_blocker_by_kind_counts": dict(Counter(row["route_kind"] for row in checkpoint_route_rows)),
        "checkpoint_route_blocker_by_assembly_line_counts": dict(
            Counter(row["assembly_line"] for row in checkpoint_route_rows if row["assembly_line"])
        ),
        "mixed_extraction_window_count": len(mixed_window_rows),
        "mixed_extraction_covered_ungrouped_count": sum(row["ungrouped_count"] for row in mixed_window_rows),
        "mixed_extraction_uncovered_ungrouped_count": (
            sum(row["ungrouped_at_checkpoint_count"] for row in case_rows.values())
            - sum(row["ungrouped_count"] for row in mixed_window_rows)
        ),
        "mixed_extraction_window_by_shape_counts": dict(Counter(row["shape"] for row in mixed_window_rows)),
        "mixed_extraction_ungrouped_by_shape_counts": dict(
            _sum_counter(mixed_window_rows, "shape", "ungrouped_count")
        ),
        "mixed_extraction_window_by_line_counts": dict(Counter(row["line"] for row in mixed_window_rows)),
        "mixed_extraction_ungrouped_by_line_counts": dict(
            _sum_counter(mixed_window_rows, "line", "ungrouped_count")
        ),
        "mixed_extraction_window_by_output_kind_counts": dict(Counter(row["output_kind"] for row in mixed_window_rows)),
        "mixed_extraction_ungrouped_by_output_kind_counts": dict(
            _sum_counter(mixed_window_rows, "output_kind", "ungrouped_count")
        ),
        "mixed_extraction_monotone_prefix_window_count": sum(
            1 for row in mixed_window_rows if row["best_prefix_ungrouped_count"]
        ),
        "mixed_extraction_monotone_prefix_ungrouped_count": sum(
            row["best_prefix_ungrouped_count"] for row in mixed_window_rows
        ),
        "mixed_extraction_repeated_monotone_prefix_window_count": sum(
            1 for row in mixed_window_rows
            if row["shape"] == "repeated_destination" and row["best_prefix_ungrouped_count"]
        ),
        "mixed_extraction_repeated_monotone_prefix_ungrouped_count": sum(
            row["best_prefix_ungrouped_count"]
            for row in mixed_window_rows
            if row["shape"] == "repeated_destination"
        ),
        "mixed_extraction_monotone_prefix_ungrouped_by_shape_counts": dict(
            _sum_counter(mixed_window_rows, "shape", "best_prefix_ungrouped_count")
        ),
        "contamination_by_line_counts": dict(Counter(row["line"] for row in contamination_final)),
        "contamination_by_target_counts": dict(Counter(row["target_line"] for row in contamination_final)),
        "contamination_by_event_template_counts": dict(Counter(row["template_name"] for row in contamination_events)),
    }

    physical.write_json(output_dir / "summary.json", summary)
    physical.write_csv(output_dir / "case_chain.csv", [_case_chain_row(row) for row in case_rows.values()])
    physical.write_csv(output_dir / "ungrouped_debt.csv", ungrouped_rows)
    physical.write_csv(output_dir / "source_prefix_blockers.csv", source_prefix_rows)
    physical.write_csv(output_dir / "source_prefix_blocker_facts.csv", source_prefix_blocker_rows)
    physical.write_csv(output_dir / "checkpoint_ungrouped_blockers.csv", checkpoint_prefix_rows)
    physical.write_csv(output_dir / "checkpoint_route_blockers.csv", checkpoint_route_rows)
    physical.write_csv(output_dir / "mixed_extraction_windows.csv", mixed_window_rows)
    physical.write_csv(output_dir / "contamination_events.csv", contamination_events)
    physical.write_csv(output_dir / "contamination_final.csv", contamination_final)
    physical.write_csv(output_dir / "inbound_plan_snapshots.csv", plan_rows)
    physical.write_json(output_dir / "selected_stage_steps_summary.json", selected_summary)
    physical.write_json(output_dir / "generation_gap_summary.json", generation_summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _selected(row: dict[str, str]) -> bool:
    return row.get("selected") == "True"


def _in_stage(row: dict[str, str], stage_limits: dict[str, int]) -> bool:
    case_id = row.get("case_id", "")
    if case_id not in stage_limits:
        return False
    hook_index = int(row.get("hook_index") or 0)
    return 0 < hook_index < stage_limits[case_id]


def _stage_trace_limit(row: dict[str, Any]) -> int:
    completion_hook = int(row.get("completion_hook") or 0)
    if completion_hook:
        return completion_hook + 1
    release_hook = int(row.get("release_hook") or 0)
    if release_hook:
        return release_hook
    return 10**9


def _contamination_diagnostics(
    *,
    initial: dict[str, dict[str, Any]],
    operations: dict[str, dict[int, list[dict[str, Any]]]],
    case_rows: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    for case_id, case in sorted(initial.items()):
        line_by_no = dict(case["line_by_no"])
        target_by_no = case["target_by_no"]
        source_by_no = {no: line for no, line in line_by_no.items()}
        release_hook = _stage_trace_limit(case_rows[case_id])
        for hook_index in sorted(operations.get(case_id, {})):
            if hook_index >= release_hook:
                break
            for op in operations[case_id][hook_index]:
                if op["action"] != "Put":
                    continue
                for no in op["move_nos"]:
                    line_by_no[no] = op["line"]
                    target_line = target_by_no.get(no, "")
                    if op["line"] in ASSEMBLY_LINES and _is_assembly_contamination_line(
                        line=op["line"],
                        target_line=target_line,
                    ):
                        events.append(
                            {
                                "case_id": case_id,
                                "hook_index": hook_index,
                                "line": op["line"],
                                "no": no,
                                "initial_line": source_by_no.get(no, ""),
                                "target_line": target_line,
                                "candidate_id": op.get("candidate_id", ""),
                                "template_name": _template_from_candidate_id(op.get("candidate_id", "")),
                            }
                        )
        for no, line in sorted(line_by_no.items()):
            target_line = target_by_no.get(no, "")
            if line in ASSEMBLY_LINES and _is_assembly_contamination_line(
                line=line,
                target_line=target_line,
            ):
                final_rows.append(
                    {
                        "case_id": case_id,
                        "line": line,
                        "no": no,
                        "initial_line": source_by_no.get(no, ""),
                        "target_line": target_line,
                    }
                )
    return events, final_rows


def _template_from_candidate_id(candidate_id: str) -> str:
    if ":vnext_" not in candidate_id:
        return ""
    tail = candidate_id.split(":vnext_", 1)[1]
    return "vnext_" + tail.split(":", 1)[0]


def _ungrouped_rows(
    *,
    initial: dict[str, dict[str, Any]],
    case_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id, result in sorted(case_rows.items()):
        debt = initial[case_id]["debt"]
        for no in result["ungrouped_nos"]:
            rows.append(
                {
                    "case_id": case_id,
                    "no": no,
                    "source_line": debt[no]["source_line"],
                    "target_line": debt[no]["target_line"],
                    "debt_count": result["debt_count"],
                    "grouped_at_checkpoint_count": result["grouped_at_checkpoint_count"],
                    "contamination_count": result["contamination_count"],
                    "release_hook": result["release_hook"],
                }
            )
    return rows


def _source_prefix_rows(
    *,
    truth_dir: Path,
    ungrouped_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_case_no = {(row["case_id"], row["no"]): row for row in ungrouped_rows}
    rows: list[dict[str, Any]] = []
    blocker_rows: list[dict[str, Any]] = []
    for path in sorted(truth_dir.glob("*.json")):
        case_id = physical.case_id_from_path(path)
        wanted_nos = {no for current_case, no in by_case_no if current_case == case_id}
        if not wanted_nos:
            continue
        _case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        loads = physical.line_loads(cars)
        target_by_no: dict[str, str] = {}
        for car in cars:
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            target_by_no[physical.car_no(car)] = target_line
        for source_line in sorted({by_case_no[(case_id, no)]["source_line"] for no in wanted_nos}):
            ordered = physical.line_cars_in_access_order(cars=cars, line=source_line)
            prefix: list[str] = []
            for car in ordered:
                no = physical.car_no(car)
                if no in wanted_nos:
                    blocker_targets = [target_by_no[item] for item in prefix]
                    blocker_nos = list(prefix)
                    non_inbound = [
                        item for item in prefix
                        if target_by_no[item] not in DESTINATION_LINES
                    ]
                    rows.append(
                        {
                            "case_id": case_id,
                            "no": no,
                            "source_line": source_line,
                            "target_line": target_by_no[no],
                            "access_index": len(prefix) + 1,
                            "prefix_blocker_count": len(prefix),
                            "inbound_prefix_blocker_count": len(prefix) - len(non_inbound),
                            "non_inbound_prefix_blocker_count": len(non_inbound),
                            "prefix_blocker_nos": "|".join(blocker_nos),
                            "prefix_blocker_targets": "|".join(blocker_targets),
                            "non_inbound_prefix_blocker_nos": "|".join(non_inbound),
                        }
                    )
                    for blocker_no in blocker_nos:
                        blocker_target = target_by_no[blocker_no]
                        blocker_rows.append(
                            {
                                "case_id": case_id,
                                "source_line": source_line,
                                "blocked_no": no,
                                "blocked_target_line": target_by_no[no],
                                "blocker_no": blocker_no,
                                "blocker_target_line": blocker_target,
                                "blocker_is_inbound_target": blocker_target in DESTINATION_LINES,
                            }
                        )
                prefix.append(no)
    return rows, blocker_rows


def _checkpoint_blocker_rows(
    *,
    truth_dir: Path,
    operations: dict[str, dict[int, list[dict[str, Any]]]],
    case_rows: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prefix_rows: list[dict[str, Any]] = []
    route_rows: list[dict[str, Any]] = []
    graph = physical.TrackGraph()
    for path in sorted(truth_dir.glob("*.json")):
        case_id = physical.case_id_from_path(path)
        case_result = case_rows.get(case_id)
        if not case_result or not case_result["ungrouped_nos"]:
            continue
        _case_id, _payload, cars, depot_assignment, loco_location = physical.read_case(path)
        checkpoint_cars, checkpoint_loco = _replay_to_stage_checkpoint(
            cars=cars,
            loco_location=loco_location,
            operations=operations.get(case_id, {}),
            release_hook=_stage_trace_limit(case_result),
        )
        loads = physical.line_loads(checkpoint_cars)
        target_by_no = _target_by_no(
            cars=checkpoint_cars,
            depot_assignment=depot_assignment,
            loads=loads,
        )
        car_by_no = {physical.car_no(car): car for car in checkpoint_cars}
        for no in case_result["ungrouped_nos"]:
            car = car_by_no.get(no)
            if car is None:
                continue
            current_line = str(car.get("Line") or "")
            if not current_line:
                continue
            prefix = _prefix_before_no(checkpoint_cars, current_line, no)
            moving_nos = {no, *prefix}
            prefix_targets = [target_by_no.get(item, "") for item in prefix]
            non_inbound_prefix = [
                item for item in prefix
                if target_by_no.get(item, "") not in DESTINATION_LINES
            ]
            prefix_rows.append(
                {
                    "case_id": case_id,
                    "no": no,
                    "current_line": current_line,
                    "target_line": target_by_no.get(no, ""),
                    "access_index": len(prefix) + 1,
                    "prefix_blocker_count": len(prefix),
                    "inbound_prefix_blocker_count": len(prefix) - len(non_inbound_prefix),
                    "non_inbound_prefix_blocker_count": len(non_inbound_prefix),
                    "prefix_blocker_nos": "|".join(prefix),
                    "prefix_blocker_targets": "|".join(prefix_targets),
                    "non_inbound_prefix_blocker_nos": "|".join(non_inbound_prefix),
                    "pull_equivalent_to_target": physical.pull_equivalent(
                        [car_by_no[item] for item in [*prefix, no] if item in car_by_no]
                    ),
                }
            )
            route_rows.extend(
                _route_blocker_rows_for_debt(
                    case_id=case_id,
                    no=no,
                    current_line=current_line,
                    target_line=target_by_no.get(no, ""),
                    cars=checkpoint_cars,
                    graph=graph,
                    loco_location=checkpoint_loco,
                    moving_nos=moving_nos,
                )
            )
    return prefix_rows, route_rows


def _replay_to_stage_checkpoint(
    *,
    cars: list[dict[str, Any]],
    loco_location: Any,
    operations: dict[int, list[dict[str, Any]]],
    release_hook: int,
) -> tuple[list[dict[str, Any]], Any]:
    checkpoint_cars = [dict(car) for car in cars]
    checkpoint_loco = loco_location
    for hook_index in sorted(operations):
        if hook_index >= release_hook:
            break
        for op in sorted(operations[hook_index], key=lambda row: int(row.get("operation_index") or 0)):
            move_nos = tuple(no for no in op["move_nos"] if no)
            if op["action"] == "Get":
                physical.apply_physical_get_order(checkpoint_cars, op["line"], move_nos)
            elif op["action"] == "Put":
                physical.apply_physical_put_order(checkpoint_cars, op["line"], list(move_nos))
            else:
                continue
            checkpoint_loco = physical.LocoLocation(op["line"])
    return checkpoint_cars, checkpoint_loco


def _target_by_no(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    loads: Counter[str],
) -> dict[str, str]:
    targets: dict[str, str] = {}
    for car in cars:
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        targets[physical.car_no(car)] = target_line
    return targets


def _prefix_before_no(cars: list[dict[str, Any]], line: str, no: str) -> tuple[str, ...]:
    prefix: list[str] = []
    for car in physical.line_cars_in_access_order(cars=cars, line=line):
        current_no = physical.car_no(car)
        if current_no == no:
            return tuple(prefix)
        prefix.append(current_no)
    return tuple(prefix)


def _route_blocker_rows_for_debt(
    *,
    case_id: str,
    no: str,
    current_line: str,
    target_line: str,
    cars: list[dict[str, Any]],
    graph: Any,
    loco_location: Any,
    moving_nos: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(
        _route_blocker_rows(
            case_id=case_id,
            no=no,
            current_line=current_line,
            target_line=target_line,
            route_kind="get_source",
            assembly_line="",
            cars=cars,
            graph=graph,
            start_line=loco_location.line,
            end_line=current_line,
            moving_nos=moving_nos,
            get_route=True,
        )
    )
    for assembly_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
        rows.extend(
            _route_blocker_rows(
                case_id=case_id,
                no=no,
                current_line=current_line,
                target_line=target_line,
                route_kind="put_assembly",
                assembly_line=assembly_line,
                cars=cars,
                graph=graph,
                start_line=current_line,
                end_line=assembly_line,
                moving_nos=moving_nos,
                get_route=False,
            )
        )
    return rows


def _route_blocker_rows(
    *,
    case_id: str,
    no: str,
    current_line: str,
    target_line: str,
    route_kind: str,
    assembly_line: str,
    cars: list[dict[str, Any]],
    graph: Any,
    start_line: str,
    end_line: str,
    moving_nos: set[str],
    get_route: bool,
) -> list[dict[str, Any]]:
    static_path = graph.route(start_line, end_line)
    if not static_path:
        return []
    occupied = physical.occupied_lines_for_route(cars, moving_nos)
    if get_route:
        target_approach_lines = physical.route_approach_lines_for_get(end_line)
    else:
        target_approach_lines = physical.route_approach_lines_for_put(end_line, cars, moving_nos)
    available_path = graph.route_avoiding_occupied(
        start_line,
        end_line,
        occupied,
        source_departure_lines=physical.route_departure_lines_for_source(start_line, cars, moving_nos),
        target_approach_lines=target_approach_lines,
        cars=cars,
        moving_nos=moving_nos,
        train_length_m=physical.train_length_for_nos(cars, moving_nos),
    )
    if available_path:
        return []
    endpoints = {physical.normalize_line(start_line), physical.normalize_line(end_line)}
    blocker_lines = tuple(
        dict.fromkeys(
            line for line in static_path
            if line in occupied and line not in endpoints
        )
    )
    rows: list[dict[str, Any]] = []
    for blocker_line in blocker_lines:
        blocker_nos = tuple(
            physical.car_no(car)
            for car in sorted(
                cars,
                key=lambda item: (int(item.get("Position") or 0), physical.car_no(item)),
            )
            if car["Line"] == blocker_line and physical.car_no(car) not in moving_nos
        )
        rows.append(
            {
                "case_id": case_id,
                "no": no,
                "current_line": current_line,
                "target_line": target_line,
                "route_kind": route_kind,
                "assembly_line": assembly_line,
                "start_line": start_line,
                "end_line": end_line,
                "static_path": "|".join(static_path),
                "blocker_line": blocker_line,
                "blocker_nos": "|".join(blocker_nos),
                "blocker_count": len(blocker_nos),
            }
        )
    return rows


def _mixed_extraction_window_rows(
    *,
    truth_dir: Path,
    operations: dict[str, dict[int, list[dict[str, Any]]]],
    case_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(truth_dir.glob("*.json")):
        case_id = physical.case_id_from_path(path)
        case_result = case_rows.get(case_id)
        if not case_result or not case_result["ungrouped_nos"]:
            continue
        _case_id, _payload, cars, depot_assignment, loco_location = physical.read_case(path)
        checkpoint_cars, _checkpoint_loco = _replay_to_stage_checkpoint(
            cars=cars,
            loco_location=loco_location,
            operations=operations.get(case_id, {}),
            release_hook=_stage_trace_limit(case_result),
        )
        loads = physical.line_loads(checkpoint_cars)
        target_by_no = _target_by_no(
            cars=checkpoint_cars,
            depot_assignment=depot_assignment,
            loads=loads,
        )
        assembly_plan = depot_inbound_plan.build_depot_inbound_assembly_plan(
            cars=checkpoint_cars,
            depot_assignment=depot_assignment,
            cun4_outbound_hold_nos=set(),
            depot_outbound_nos=set(
                depot_outbound_plan.build_depot_outbound_assembly_plan(
                    cars=checkpoint_cars,
                    depot_assignment=depot_assignment,
                ).outbound_nos
            ),
        )
        temporary_line_by_no = assembly_plan.temporary_line_by_no
        ungrouped_nos = set(case_result["ungrouped_nos"])
        for line in sorted({car["Line"] for car in checkpoint_cars if car.get("Line")}):
            window = _pull_limited_line_window(cars=checkpoint_cars, line=line)
            if not window:
                continue
            window_nos = tuple(physical.car_no(car) for car in window)
            window_ungrouped = tuple(no for no in window_nos if no in ungrouped_nos)
            if not window_ungrouped:
                continue
            output_sequence = tuple(
                _mixed_extraction_output_line(
                    no=no,
                    target_by_no=target_by_no,
                    temporary_line_by_no=temporary_line_by_no,
                )
                for no in window_nos
            )
            drop_segments = _drop_segments(output_sequence)
            repeat_dest_count = len(drop_segments) - len(set(drop_segments))
            non_inbound_nos = tuple(
                no for no in window_nos
                if target_by_no.get(no, "") not in DESTINATION_LINES
            )
            inbound_nos = tuple(
                no for no in window_nos
                if target_by_no.get(no, "") in DESTINATION_LINES
            )
            best_prefix = _best_monotone_prefix(
                window_nos=window_nos,
                output_sequence=output_sequence,
                ungrouped_nos=set(window_ungrouped),
                non_inbound_nos=set(non_inbound_nos),
                min_ungrouped_count=2 if repeat_dest_count else 1,
            )
            output_kind = "mixed" if non_inbound_nos and inbound_nos else "pure_inbound"
            shape = "repeated_destination" if repeat_dest_count else "contiguous_destinations"
            rows.append(
                {
                    "case_id": case_id,
                    "line": line,
                    "window_count": len(window_nos),
                    "ungrouped_count": len(window_ungrouped),
                    "non_inbound_count": len(non_inbound_nos),
                    "inbound_count": len(inbound_nos),
                    "segment_count": len(drop_segments),
                    "repeat_dest_count": repeat_dest_count,
                    "output_kind": output_kind,
                    "shape": shape,
                    "window_nos": "|".join(window_nos),
                    "ungrouped_nos": "|".join(window_ungrouped),
                    "non_inbound_nos": "|".join(non_inbound_nos),
                    "inbound_nos": "|".join(inbound_nos),
                    "output_sequence": "|".join(output_sequence),
                    "drop_segments": "|".join(drop_segments),
                    **best_prefix,
                }
            )
    return rows


def _pull_limited_line_window(
    *,
    cars: list[dict[str, Any]],
    line: str,
) -> tuple[dict[str, Any], ...]:
    window: list[dict[str, Any]] = []
    for car in physical.line_cars_in_access_order(cars=cars, line=line):
        if physical.pull_equivalent([*window, car]) > physical.PULL_LIMIT_EQUIVALENT:
            break
        window.append(car)
    return tuple(window)


def _mixed_extraction_output_line(
    *,
    no: str,
    target_by_no: dict[str, str],
    temporary_line_by_no: dict[str, str],
) -> str:
    target_line = target_by_no.get(no, "")
    if target_line in DESTINATION_LINES:
        return temporary_line_by_no.get(no, "INBOUND_UNPLANNED")
    return target_line


def _drop_segments(output_sequence: tuple[str, ...]) -> tuple[str, ...]:
    segments: list[str] = []
    for destination in reversed(output_sequence):
        if not segments or segments[-1] != destination:
            segments.append(destination)
    return tuple(segments)


def _best_monotone_prefix(
    *,
    window_nos: tuple[str, ...],
    output_sequence: tuple[str, ...],
    ungrouped_nos: set[str],
    non_inbound_nos: set[str],
    min_ungrouped_count: int,
) -> dict[str, Any]:
    best: tuple[tuple[int, int, int, int, tuple[str, ...]], dict[str, Any]] | None = None
    for index, no in enumerate(window_nos):
        if no not in ungrouped_nos:
            continue
        prefix_nos = window_nos[: index + 1]
        prefix_outputs = output_sequence[: index + 1]
        if "INBOUND_UNPLANNED" in prefix_outputs:
            continue
        prefix_ungrouped = tuple(item for item in prefix_nos if item in ungrouped_nos)
        prefix_non_inbound = tuple(item for item in prefix_nos if item in non_inbound_nos)
        if len(prefix_ungrouped) < min_ungrouped_count or not prefix_non_inbound:
            continue
        prefix_segments = _drop_segments(prefix_outputs)
        if len(prefix_segments) != len(set(prefix_segments)):
            continue
        score = (
            -len(prefix_ungrouped),
            len(prefix_segments),
            len(prefix_non_inbound),
            len(prefix_nos),
            prefix_nos,
        )
        candidate = {
            "best_prefix_count": len(prefix_nos),
            "best_prefix_ungrouped_count": len(prefix_ungrouped),
            "best_prefix_non_inbound_count": len(prefix_non_inbound),
            "best_prefix_segment_count": len(prefix_segments),
            "best_prefix_nos": "|".join(prefix_nos),
            "best_prefix_ungrouped_nos": "|".join(prefix_ungrouped),
            "best_prefix_non_inbound_nos": "|".join(prefix_non_inbound),
            "best_prefix_output_sequence": "|".join(prefix_outputs),
            "best_prefix_drop_segments": "|".join(prefix_segments),
        }
        if best is None or score < best[0]:
            best = (score, candidate)
    if best is None:
        return {
            "best_prefix_count": 0,
            "best_prefix_ungrouped_count": 0,
            "best_prefix_non_inbound_count": 0,
            "best_prefix_segment_count": 0,
            "best_prefix_nos": "",
            "best_prefix_ungrouped_nos": "",
            "best_prefix_non_inbound_nos": "",
            "best_prefix_output_sequence": "",
            "best_prefix_drop_segments": "",
        }
    return best[1]


def _inbound_plan_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        detail = _detail_map(row.get("detail", ""))
        result.append(
            {
                "case_id": row["case_id"],
                "hook_index": int(row["hook_index"] or 0),
                "status": row["status"],
                "mode": row["mode"],
                "violation": row["violation"],
                "reason": detail.get("reason", ""),
                "source_lines": detail.get("sources", ""),
                "target_lines": detail.get("targets", ""),
                "grouped_count": _count_csv(detail.get("grouped", "")),
                "ungrouped_count": _count_csv(detail.get("ungrouped", "")),
                "unassigned_count": _count_csv(detail.get("unassigned", "")),
                "purity_count": _count_csv(detail.get("purity_nos", "")),
                "purity_lines": detail.get("purity_lines", ""),
                "total_length_m": detail.get("total_length_m", ""),
                "pullout_required_m": detail.get("pullout_required_m", ""),
                "depot_free_m": detail.get("depot_free_m", ""),
                "depot_surplus_after_pull_m": detail.get("depot_surplus_after_pull_m", ""),
                "cun4_budget_m": detail.get("cun4_budget_m", ""),
                "assembly_free_m": detail.get("assembly_free_m", ""),
                "groups": detail.get("groups", ""),
            }
        )
    return result


def _detail_map(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def _count_csv(text: str) -> int:
    if not text:
        return 0
    return sum(1 for item in text.split(",") if item)


def _selected_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    hook_count_by_template: Counter[str] = Counter()
    car_count_by_template: Counter[str] = Counter()
    hook_count_by_phase_intent: Counter[str] = Counter()
    for row in rows:
        template = row["template_name"]
        hook_count_by_template[template] += 1
        car_count_by_template[template] += _count_pipe(row.get("move_nos", ""))
        hook_count_by_phase_intent[f"{row['phase']}|{row['intent']}"] += 1
    return {
        "hook_count_by_template": dict(hook_count_by_template),
        "car_count_by_template": dict(car_count_by_template),
        "hook_count_by_phase_intent": dict(hook_count_by_phase_intent),
    }


def _generation_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    source_reason_counts: Counter[str] = Counter()
    target_reason_counts: Counter[str] = Counter()
    for row in rows:
        reason = row.get("reason", "")
        reason_counts[reason] += 1
        family_counts[row.get("family", "")] += 1
        for source_line in _split_pipe_or_csv(row.get("source_lines", "")):
            source_reason_counts[f"{source_line}|{reason}"] += 1
        for target_line in _split_pipe_or_csv(row.get("target_lines", "")):
            target_reason_counts[f"{target_line}|{reason}"] += 1
    return {
        "reason_counts": dict(reason_counts),
        "family_counts": dict(family_counts),
        "source_reason_counts": dict(source_reason_counts),
        "target_reason_counts": dict(target_reason_counts),
    }


def _latest_plan_reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    latest_by_case: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = latest_by_case.get(row["case_id"])
        if current is None or row["hook_index"] > current["hook_index"]:
            latest_by_case[row["case_id"]] = row
    return dict(Counter(row["reason"] for row in latest_by_case.values()))


def _sum_counter(rows: list[dict[str, Any]], key_field: str, value_field: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter[str(row[key_field])] += int(row[value_field])
    return counter


def _case_chain_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "debt_count": row["debt_count"],
        "grouped_at_checkpoint_count": row["grouped_at_checkpoint_count"],
        "ungrouped_at_checkpoint_count": row["ungrouped_at_checkpoint_count"],
        "grouping_rate": f"{row['grouping_rate']:.6f}",
        "stage_complete": row["stage_complete"],
        "release_hook": row["release_hook"],
        "stage_hook_count": row["stage_hook_count"],
        "assembly_hook_count": row["assembly_hook_count"],
        "contamination_count": row["contamination_count"],
        "complete_then_contaminated": row["complete_then_contaminated"],
        "ungrouped_by_source_counts": _counter_text(row["ungrouped_by_source_counts"]),
        "contamination_by_line_counts": _counter_text(row["contamination_by_line_counts"]),
    }


def _count_pipe(text: str) -> int:
    if not text:
        return 0
    return sum(1 for item in text.split("|") if item)


def _split_pipe_or_csv(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    separator = "|" if "|" in text else ","
    return tuple(item for item in text.split(separator) if item)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def _counter_text(counter: Counter[Any]) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common())


if __name__ == "__main__":
    main()
