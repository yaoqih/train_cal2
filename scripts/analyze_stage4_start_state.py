#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical


STAGE4_PATH = SCRIPTS / "stage4_simple" / "solve.py"
STAGE4_SPEC = importlib.util.spec_from_file_location("stage4_simple_solve_for_analysis", STAGE4_PATH)
if STAGE4_SPEC is None or STAGE4_SPEC.loader is None:
    raise RuntimeError(f"cannot import {STAGE4_PATH}")
stage4 = importlib.util.module_from_spec(STAGE4_SPEC)
sys.modules[STAGE4_SPEC.name] = stage4
STAGE4_SPEC.loader.exec_module(stage4)


KEY_TARGET_LINES = (
    "存1线",
    "存2线",
    "存3线",
    "存4线",
    "存5线北",
    "存5线南",
    "预修线",
    "调梁线北",
    "调梁棚",
    "洗罐线北",
    "洗罐站",
    "油漆线",
    "抛丸线",
    "机库线",
    "机走棚",
    "机走北",
    "洗油北",
    "机南",
    "卸轮线",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze the initial state seen by stage4_simple.")
    parser.add_argument("--truth-dir", type=Path, default=Path("data/truth2"))
    parser.add_argument("--stage3-out", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case", default="")
    parser.add_argument(
        "--include-stage3-partial",
        action="store_true",
        help="Try to initialize from stage3 artifacts even when the stage3 summary is not complete.",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    paths = sorted(args.truth_dir.glob("validation_*.json"))
    if args.case:
        paths = [path for path in paths if stage4.case_id_from_path(path) == args.case.upper()]
    if args.limit:
        paths = paths[: args.limit]

    case_rows: list[dict[str, Any]] = []
    line_rows: list[dict[str, Any]] = []
    vehicle_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    flow_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []

    for path in paths:
        case_id = stage4.case_id_from_path(path)
        try:
            loaded = load_solver(case_id, path, args.stage3_out, include_stage3_partial=args.include_stage3_partial)
        except Exception as exc:
            skipped_rows.append({"case_id": case_id, "reason": f"{type(exc).__name__}:{exc}"})
            continue
        if isinstance(loaded, dict):
            skipped_rows.append(loaded)
            continue
        solver = loaded
        analysis = analyze_solver(solver)
        case_rows.append(analysis["case"])
        line_rows.extend(analysis["lines"])
        vehicle_rows.extend(analysis["vehicles"])
        segment_rows.extend(analysis["segments"])
        flow_rows.extend(analysis["flows"])
        target_rows.extend(analysis["targets"])

    summary = summarize(case_rows, line_rows, vehicle_rows, segment_rows, flow_rows, target_rows, skipped_rows)
    write_csv(args.out / "stage4_start_cases.csv", case_rows)
    write_csv(args.out / "stage4_start_lines.csv", line_rows)
    write_csv(args.out / "stage4_start_vehicles.csv", vehicle_rows)
    write_csv(args.out / "stage4_start_segments.csv", segment_rows)
    write_csv(args.out / "stage4_start_flows.csv", flow_rows)
    write_csv(args.out / "stage4_start_targets.csv", target_rows)
    write_csv(args.out / "stage4_start_skipped.csv", skipped_rows)
    write_json(args.out / "stage4_start_summary.json", summary)
    print(json.dumps(summary_for_console(summary), ensure_ascii=False, indent=2))
    print(f"wrote: {args.out}")


def load_solver(
    case_id: str,
    truth_path: Path,
    stage3_out: Path,
    *,
    include_stage3_partial: bool,
) -> Any:
    stage3_summary_path = stage3_out / f"{case_id}_summary.json"
    stage3_request_path = stage3_out / f"{case_id}_stage3_request.json"
    stage3_response_path = stage3_out / f"{case_id}_response.json"
    combined_path = stage3_out / f"{case_id}_combined_response.json"
    if not stage3_summary_path.exists():
        return {"case_id": case_id, "reason": "stage3_summary_missing"}
    stage3_summary = stage4.read_json(stage3_summary_path)
    if stage3_summary.get("status") != "complete" and not include_stage3_partial:
        return {"case_id": case_id, "reason": f"stage3_not_complete:{stage3_summary.get('status')}"}
    missing = [
        name
        for name, file_path in (
            ("stage3_request_missing", stage3_request_path),
            ("stage3_response_missing", stage3_response_path),
            ("stage3_combined_response_missing", combined_path),
        )
        if not file_path.exists()
    ]
    if missing:
        return {"case_id": case_id, "reason": "|".join(missing)}

    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(truth_path)
    return stage4.Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage4.read_json(stage3_request_path),
        stage4.read_json(stage3_response_path),
        stage4.read_json(combined_path),
        time_budget_seconds=0.01,
        max_expansions=0,
    )


def analyze_solver(solver: Any) -> dict[str, Any]:
    cars = [dict(car) for car in solver.initial_cars]
    case_id = solver.case_id
    by_no = {physical.car_no(car): car for car in cars}
    line_order = {
        line: physical.line_access_order(cars, line)
        for line in sorted({car["Line"] for car in cars if car.get("Line")})
    }
    loads = physical.line_length_loads(cars)
    target_by_no = target_lookup(solver, cars)
    role_by_no = {no: car_role(solver, no) for no in by_no}
    satisfied_by_no = {no: no not in solver.initial_unsatisfied_nos for no in by_no}

    line_rows = []
    vehicle_rows = []
    segment_rows = []
    flow_rows = []
    target_rows = []

    all_lines = sorted(set(physical.TRACK_SPECS) | set(line_order))
    for line in all_lines:
        nos = line_order.get(line, [])
        line_cars = [by_no[no] for no in nos]
        spec = physical.TRACK_SPECS.get(line)
        length_m = float(spec.length_m) if spec else 0.0
        used_m = float(loads.get(line, 0.0))
        remaining_m = length_m - used_m if spec else 0.0
        active_nos = [no for no in nos if no in solver.active_nos]
        unsat_nos = [no for no in nos if no in solver.initial_unsatisfied_nos]
        first_active = min((nos.index(no) + 1 for no in active_nos), default=0)
        first_unsat = min((nos.index(no) + 1 for no in unsat_nos), default=0)
        target_counter = Counter(target_by_no.get(no, "") or "(none)" for no in nos)
        active_target_counter = Counter(target_by_no.get(no, "") or "(none)" for no in active_nos)
        role_counter = Counter(role_by_no.get(no, "") for no in nos)
        initial_line_counter = Counter(by_no[no].get("_InitialLine") or "" for no in nos)
        prefix_role, prefix_target, prefix_size = north_prefix(nos, role_by_no, target_by_no)
        line_rows.append(
            {
                "case_id": case_id,
                "line": line,
                "track_type": spec.track_type if spec else "",
                "length_m": fmt(length_m),
                "used_m": fmt(used_m),
                "remaining_m": fmt(remaining_m),
                "occupancy_ratio": fmt(used_m / length_m if length_m else 0.0),
                "remaining_bucket": remaining_bucket(remaining_m, bool(spec)),
                "car_count": len(nos),
                "active_count": len(active_nos),
                "managed_count": sum(1 for no in nos if no in solver.managed_nos),
                "repair_count": sum(1 for no in nos if no in solver.repair_nos),
                "unsatisfied_count": len(unsat_nos),
                "satisfied_count": len(nos) - len(unsat_nos),
                "protected_satisfied_count": sum(1 for no in nos if no in solver.protected_satisfied_nos),
                "out_of_scope_count": sum(1 for no in nos if no in solver.out_of_scope_nos),
                "excluded_line_count": sum(1 for no in nos if no in solver.excluded_line_nos),
                "heavy_count": sum(1 for car in line_cars if car.get("IsHeavy")),
                "pending_weigh_count": sum(1 for car in line_cars if pending_weigh(car)),
                "closed_door_count": sum(1 for car in line_cars if car.get("IsClosedDoor")),
                "target_mix_count": len([target for target in target_counter if target != "(none)"]),
                "active_target_mix_count": len([target for target in active_target_counter if target != "(none)"]),
                "targets": counter_text(target_counter),
                "active_targets": counter_text(active_target_counter),
                "roles": counter_text(role_counter),
                "initial_lines": counter_text(initial_line_counter),
                "first_active_index": first_active,
                "first_unsatisfied_index": first_unsat,
                "north_prefix_role": prefix_role,
                "north_prefix_target": prefix_target,
                "north_prefix_size": prefix_size,
                "sequence_targets": sequence_text(nos, target_by_no),
                "sequence_roles": sequence_text(nos, role_by_no),
                "sequence_nos": "|".join(nos),
            }
        )
        segment_rows.extend(build_segments(case_id, line, nos, by_no, role_by_no, target_by_no))

        grouped = defaultdict(list)
        for no in nos:
            grouped[(role_by_no[no], target_by_no.get(no, "") or "(none)")].append(no)
        for (role, target), group in sorted(grouped.items()):
            flow_rows.append(
                {
                    "case_id": case_id,
                    "current_line": line,
                    "target_line": target,
                    "role": role,
                    "count": len(group),
                    "length_m": fmt(sum(physical.car_length(by_no[no]) for no in group)),
                    "positions": "|".join(str(nos.index(no) + 1) for no in group),
                    "nos": "|".join(group),
                    "initial_lines": counter_text(Counter(by_no[no].get("_InitialLine") or "" for no in group)),
                }
            )

    for no, car in sorted(by_no.items(), key=lambda item: (item[1]["Line"], int(item[1].get("Position") or 0), item[0])):
        line = car["Line"]
        nos = line_order.get(line, [])
        access_index = nos.index(no) + 1 if no in nos else 0
        front = nos[: max(0, access_index - 1)]
        target = target_by_no.get(no, "")
        target_spec = physical.TRACK_SPECS.get(target)
        target_used = float(loads.get(target, 0.0))
        target_remaining = (float(target_spec.length_m) - target_used) if target_spec else 0.0
        vehicle_rows.append(
            {
                "case_id": case_id,
                "no": no,
                "line": line,
                "position": int(car.get("Position") or 0),
                "access_index": access_index,
                "line_car_count": len(nos),
                "length_m": fmt(physical.car_length(car)),
                "role": role_by_no[no],
                "satisfied": int(satisfied_by_no[no]),
                "active": int(no in solver.active_nos),
                "managed": int(no in solver.managed_nos),
                "repair": int(no in solver.repair_nos),
                "protected_satisfied": int(no in solver.protected_satisfied_nos),
                "out_of_scope": int(no in solver.out_of_scope_nos),
                "excluded_line": int(no in solver.excluded_line_nos),
                "initial_line": car.get("_InitialLine") or "",
                "target_line": target,
                "raw_targets": "|".join(physical.target_lines(car)),
                "target_reason": solver.target_reason_by_no.get(no, ""),
                "target_same_as_current": int(bool(target) and target == line),
                "force_positions": "|".join(str(item) for item in physical.force_positions(car)),
                "is_heavy": int(bool(car.get("IsHeavy"))),
                "is_weigh": int(bool(car.get("IsWeigh"))),
                "pending_weigh": int(pending_weigh(car)),
                "is_closed_door": int(bool(car.get("IsClosedDoor"))),
                "front_count": len(front),
                "front_length_m": fmt(sum(physical.car_length(by_no[item]) for item in front)),
                "front_active_count": sum(1 for item in front if item in solver.active_nos),
                "front_managed_count": sum(1 for item in front if item in solver.managed_nos),
                "front_unsatisfied_count": sum(1 for item in front if item in solver.initial_unsatisfied_nos),
                "front_protected_satisfied_count": sum(1 for item in front if item in solver.protected_satisfied_nos),
                "front_out_of_scope_count": sum(1 for item in front if item in solver.out_of_scope_nos),
                "front_targets": counter_text(Counter(target_by_no.get(item, "") or "(none)" for item in front)),
                "front_roles": counter_text(Counter(role_by_no.get(item, "") for item in front)),
                "front_nos": "|".join(front),
                "target_line_used_m": fmt(target_used),
                "target_line_remaining_m": fmt(target_remaining),
                "target_line_car_count": len(line_order.get(target, [])) if target else 0,
                "target_line_unsatisfied_count": sum(1 for item in line_order.get(target, []) if item in solver.initial_unsatisfied_nos),
            }
        )

    for target in sorted(set(KEY_TARGET_LINES) | {target for target in target_by_no.values() if target}):
        nos = line_order.get(target, [])
        unsat = [no for no in nos if no in solver.initial_unsatisfied_nos]
        active_target = [no for no in solver.active_nos if target_by_no.get(no) == target]
        spec = physical.TRACK_SPECS.get(target)
        used_m = float(loads.get(target, 0.0))
        remaining_m = (float(spec.length_m) - used_m) if spec else 0.0
        if not nos:
            state = "empty"
        elif unsat:
            state = "contains_unsatisfied"
        else:
            state = "all_satisfied"
        target_rows.append(
            {
                "case_id": case_id,
                "target_line": target,
                "state": state,
                "length_m": fmt(float(spec.length_m) if spec else 0.0),
                "used_m": fmt(used_m),
                "remaining_m": fmt(remaining_m),
                "car_count": len(nos),
                "unsatisfied_count": len(unsat),
                "active_debt_to_target_count": len(active_target),
                "active_debt_to_target_sources": counter_text(Counter(by_no[no]["Line"] for no in active_target if no in by_no)),
                "current_targets_on_line": counter_text(Counter(target_by_no.get(no, "") or "(none)" for no in nos)),
                "roles_on_line": counter_text(Counter(role_by_no.get(no, "") for no in nos)),
                "sequence_targets": sequence_text(nos, target_by_no),
                "sequence_roles": sequence_text(nos, role_by_no),
            }
        )

    active_cars = [by_no[no] for no in sorted(solver.active_nos) if no in by_no]
    active_front_counts = [
        int(row["front_count"])
        for row in vehicle_rows
        if int(row["active"])
    ]
    case_row = {
        "case_id": case_id,
        "stage4_start_loco": "|".join(solver.initial_loco),
        "total_cars": len(cars),
        "active_count": len(solver.active_nos),
        "managed_count": len(solver.managed_nos),
        "repair_count": len(solver.repair_nos),
        "out_of_scope_count": len(solver.out_of_scope_nos),
        "excluded_line_count": len(solver.excluded_line_nos),
        "unsatisfied_count": len(solver.initial_unsatisfied_nos),
        "protected_satisfied_count": len(solver.protected_satisfied_nos),
        "occupied_line_count": sum(1 for line in all_lines if line_order.get(line)),
        "active_current_lines": counter_text(Counter(car["Line"] for car in active_cars)),
        "active_targets": counter_text(Counter(target_by_no.get(physical.car_no(car), "") or "(none)" for car in active_cars)),
        "active_front_count_min": min(active_front_counts) if active_front_counts else 0,
        "active_front_count_p50": fmt(median(active_front_counts)) if active_front_counts else "0",
        "active_front_count_max": max(active_front_counts) if active_front_counts else 0,
        "front_accessible_active_count": sum(1 for value in active_front_counts if value == 0),
        "blocked_active_count": sum(1 for value in active_front_counts if value > 0),
        "active_same_line_unsatisfied_count": sum(
            1 for car in active_cars if target_by_no.get(physical.car_no(car)) == car["Line"]
        ),
    }
    return {
        "case": case_row,
        "lines": line_rows,
        "vehicles": vehicle_rows,
        "segments": segment_rows,
        "flows": flow_rows,
        "targets": target_rows,
    }


def target_lookup(solver: Any, cars: list[dict[str, Any]]) -> dict[str, str]:
    out = dict(solver.target_by_no)
    loads = physical.line_loads(cars)
    for car in cars:
        no = physical.car_no(car)
        if no in out:
            continue
        target, _pos, _reason = physical.planned_target_for_car(car, cars, solver.depot_assignment, loads)
        if target:
            out[no] = target
        else:
            targets = physical.target_lines(car)
            out[no] = "/".join(targets)
    return out


def car_role(solver: Any, no: str) -> str:
    if no in solver.active_nos:
        return "active_debt"
    if no in solver.repair_nos:
        return "repair"
    if no in solver.out_of_scope_nos:
        return "out_of_scope"
    if no in solver.excluded_line_nos:
        return "excluded_line"
    if no in solver.protected_satisfied_nos:
        return "protected_satisfied"
    if no in solver.initial_unsatisfied_nos:
        return "unmanaged_unsatisfied"
    return "fixed_satisfied"


def pending_weigh(car: dict[str, Any]) -> bool:
    return bool(car.get("IsWeigh")) and not bool(car.get("_Weighed"))


def north_prefix(nos: list[str], role_by_no: dict[str, str], target_by_no: dict[str, str]) -> tuple[str, str, int]:
    if not nos:
        return "", "", 0
    first_role = role_by_no.get(nos[0], "")
    first_target = target_by_no.get(nos[0], "")
    size = 0
    for no in nos:
        if role_by_no.get(no, "") != first_role or target_by_no.get(no, "") != first_target:
            break
        size += 1
    return first_role, first_target, size


def build_segments(
    case_id: str,
    line: str,
    nos: list[str],
    by_no: dict[str, dict[str, Any]],
    role_by_no: dict[str, str],
    target_by_no: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not nos:
        return rows
    start = 0
    segment_index = 1
    while start < len(nos):
        role = role_by_no.get(nos[start], "")
        target = target_by_no.get(nos[start], "")
        end = start + 1
        while end < len(nos) and role_by_no.get(nos[end], "") == role and target_by_no.get(nos[end], "") == target:
            end += 1
        group = nos[start:end]
        rows.append(
            {
                "case_id": case_id,
                "line": line,
                "segment_index": segment_index,
                "start_access_index": start + 1,
                "end_access_index": end,
                "size": len(group),
                "length_m": fmt(sum(physical.car_length(by_no[no]) for no in group)),
                "role": role,
                "target_line": target,
                "initial_lines": counter_text(Counter(by_no[no].get("_InitialLine") or "" for no in group)),
                "nos": "|".join(group),
            }
        )
        start = end
        segment_index += 1
    return rows


def summarize(
    case_rows: list[dict[str, Any]],
    line_rows: list[dict[str, Any]],
    vehicle_rows: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
    flow_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    skipped_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    active_vehicle_rows = [row for row in vehicle_rows if int(row["active"])]
    line_summary: dict[str, Any] = {}
    for line in sorted({row["line"] for row in line_rows}):
        rows = [row for row in line_rows if row["line"] == line]
        occupied = [row for row in rows if int(row["car_count"]) > 0]
        active = [row for row in rows if int(row["active_count"]) > 0]
        free_values = [float(row["remaining_m"]) for row in rows if row["remaining_m"] != ""]
        line_summary[line] = {
            "observations": len(rows),
            "occupied_cases": len(occupied),
            "active_cases": len(active),
            "overfull_cases": sum(1 for value in free_values if value < -0.5),
            "remaining_m": numeric_summary(free_values),
            "car_count": numeric_summary([int(row["car_count"]) for row in rows]),
            "active_count": numeric_summary([int(row["active_count"]) for row in rows]),
            "remaining_buckets": dict(Counter(row["remaining_bucket"] for row in rows)),
            "top_active_targets": top_counter_from_text(row["active_targets"] for row in rows),
            "top_all_targets": top_counter_from_text(row["targets"] for row in rows),
        }

    current_target = Counter()
    current_target_cases: defaultdict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in flow_rows:
        key = (row["current_line"], row["target_line"], row["role"])
        current_target[key] += int(row["count"])
        current_target_cases[key].add(row["case_id"])

    target_state_summary: dict[str, Any] = {}
    for target in sorted({row["target_line"] for row in target_rows}):
        rows = [row for row in target_rows if row["target_line"] == target]
        target_state_summary[target] = {
            "states": dict(Counter(row["state"] for row in rows)),
            "active_debt_to_target": numeric_summary([int(row["active_debt_to_target_count"]) for row in rows]),
            "remaining_m": numeric_summary([float(row["remaining_m"]) for row in rows]),
            "top_sources_for_active_debt": top_counter_from_text(row["active_debt_to_target_sources"] for row in rows),
        }

    pressure_by_line_target: dict[str, Any] = {}
    grouped_pressure: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in active_vehicle_rows:
        grouped_pressure[(row["line"], row["target_line"])].append(row)
    for (line, target), rows in sorted(grouped_pressure.items()):
        front_counts = [int(row["front_count"]) for row in rows]
        front_lengths = [float(row["front_length_m"]) for row in rows]
        pressure_by_line_target[f"{line}->{target}"] = {
            "cars": len(rows),
            "cases": len({row["case_id"] for row in rows}),
            "front_count": numeric_summary(front_counts),
            "front_length_m": numeric_summary(front_lengths),
            "front_roles": top_counter_from_text(row["front_roles"] for row in rows),
            "front_targets": top_counter_from_text(row["front_targets"] for row in rows),
        }

    segment_patterns = Counter()
    for row in segment_rows:
        if row["role"] == "active_debt":
            segment_patterns[(row["line"], row["target_line"], int(row["size"]))] += 1

    return {
        "cases_requested": len(case_rows) + len(skipped_rows),
        "cases_analyzed": len(case_rows),
        "cases_skipped": len(skipped_rows),
        "skipped_reasons": dict(Counter(row["reason"].split(":", 1)[0] for row in skipped_rows)),
        "total_cars": sum(int(row["total_cars"]) for row in case_rows),
        "active_debt_cars": len(active_vehicle_rows),
        "active_debt_by_current_line": dict(Counter(row["line"] for row in active_vehicle_rows).most_common()),
        "active_debt_by_target_line": dict(Counter(row["target_line"] for row in active_vehicle_rows).most_common()),
        "active_debt_front_count": numeric_summary([int(row["front_count"]) for row in active_vehicle_rows]),
        "front_accessible_active_cars": sum(1 for row in active_vehicle_rows if int(row["front_count"]) == 0),
        "blocked_active_cars": sum(1 for row in active_vehicle_rows if int(row["front_count"]) > 0),
        "line_summary": line_summary,
        "target_state_summary": target_state_summary,
        "top_current_line_target_role_flows": [
            {
                "current_line": line,
                "target_line": target,
                "role": role,
                "cars": count,
                "cases": len(current_target_cases[(line, target, role)]),
            }
            for (line, target, role), count in current_target.most_common(80)
        ],
        "pressure_by_line_target": pressure_by_line_target,
        "top_active_segments": [
            {"line": line, "target_line": target, "segment_size": size, "segments": count}
            for (line, target, size), count in segment_patterns.most_common(50)
        ],
    }


def summary_for_console(summary: dict[str, Any]) -> dict[str, Any]:
    interesting_lines = [
        "存5线北",
        "存5线南",
        "存3线",
        "存2线",
        "存1线",
        "预修线",
        "调梁棚",
        "调梁线北",
        "洗罐站",
        "油漆线",
        "抛丸线",
        "机库线",
        "机走北",
        "机走棚",
        "洗油北",
        "机南",
        "卸轮线",
    ]
    return {
        "cases_requested": summary["cases_requested"],
        "cases_analyzed": summary["cases_analyzed"],
        "cases_skipped": summary["cases_skipped"],
        "skipped_reasons": summary["skipped_reasons"],
        "active_debt_cars": summary["active_debt_cars"],
        "active_debt_by_current_line_top": dict(list(summary["active_debt_by_current_line"].items())[:15]),
        "active_debt_by_target_line_top": dict(list(summary["active_debt_by_target_line"].items())[:15]),
        "active_debt_front_count": summary["active_debt_front_count"],
        "front_accessible_active_cars": summary["front_accessible_active_cars"],
        "blocked_active_cars": summary["blocked_active_cars"],
        "line_remaining_focus": {
            line: {
                "occupied_cases": summary["line_summary"].get(line, {}).get("occupied_cases", 0),
                "active_cases": summary["line_summary"].get(line, {}).get("active_cases", 0),
                "remaining_m": summary["line_summary"].get(line, {}).get("remaining_m", {}),
                "top_active_targets": summary["line_summary"].get(line, {}).get("top_active_targets", {}),
            }
            for line in interesting_lines
        },
        "target_state_focus": {
            line: summary["target_state_summary"].get(line, {})
            for line in ("预修线", "调梁棚", "油漆线", "洗罐站", "抛丸线", "机库线", "存5线南", "存3线")
        },
        "top_current_line_target_role_flows": summary["top_current_line_target_role_flows"][:25],
        "top_active_segments": summary["top_active_segments"][:20],
    }


def numeric_summary(values: Iterable[float | int]) -> dict[str, Any]:
    xs = sorted(float(value) for value in values)
    if not xs:
        return {"count": 0}
    return {
        "count": len(xs),
        "min": round(xs[0], 3),
        "p10": round(percentile(xs, 0.10), 3),
        "p25": round(percentile(xs, 0.25), 3),
        "p50": round(median(xs), 3),
        "p75": round(percentile(xs, 0.75), 3),
        "p90": round(percentile(xs, 0.90), 3),
        "max": round(xs[-1], 3),
        "avg": round(sum(xs) / len(xs), 3),
    }


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    index = (len(xs) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return xs[lower]
    return xs[lower] + (xs[upper] - xs[lower]) * (index - lower)


def median(values: Iterable[float | int]) -> float:
    xs = list(values)
    if not xs:
        return 0.0
    return float(statistics.median(xs))


def remaining_bucket(remaining_m: float, has_spec: bool) -> str:
    if not has_spec:
        return "no_spec"
    if remaining_m < -0.5:
        return "overfull"
    if remaining_m < 15:
        return "0_15"
    if remaining_m < 30:
        return "15_30"
    if remaining_m < 60:
        return "30_60"
    if remaining_m < 100:
        return "60_100"
    return "100_plus"


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common() if key)


def top_counter_from_text(values: Iterable[str], limit: int = 20) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for text in values:
        for part in str(text or "").split("|"):
            if not part or ":" not in part:
                continue
            key, value = part.rsplit(":", 1)
            try:
                counter[key] += int(value)
            except ValueError:
                continue
    return dict(counter.most_common(limit))


def sequence_text(nos: list[str], lookup: dict[str, str]) -> str:
    return "|".join(lookup.get(no, "") or "(none)" for no in nos)


def fmt(value: float) -> str:
    return f"{value:.3f}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
