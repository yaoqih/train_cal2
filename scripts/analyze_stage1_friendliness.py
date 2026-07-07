#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv
from solver_vnext import physical
from stage3_simple.solve import (
    ASSEMBLY_LINES as STAGE3_ASSEMBLY_LINES,
    TEMPLATE_A_FIRST_ORDER,
    TEMPLATE_A_SECOND_LINE,
    TEMPLATE_B_ORDER,
)


STAGE1_ASSEMBLY_LINES = ("机南", "洗油北", "机走棚", "机走北")
UNWHEEL_LINE = "卸轮线"
STAGE4_IGNORED_TARGET_LINES = set(physical.DEPOT_TARGET_LINES) | {UNWHEEL_LINE}
TEMPLATE_NAMES = ("B", "A")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Stage1 downstream friendliness for Stage3/Stage4.")
    parser.add_argument("--stage1-dir", required=True, help="directory containing *_response.json from stage1_simple")
    parser.add_argument("--truth-dir", default="data/truth2", help="directory containing validation_*.json")
    parser.add_argument("--output-dir", default="", help="default: <stage1-dir>/stage1_friendliness")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    stage1_dir = Path(args.stage1_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else stage1_dir / "stage1_friendliness"
    output_dir.mkdir(parents=True, exist_ok=True)

    truth_by_case = truth_case_map(truth_dir)
    response_paths = sorted(stage1_dir.glob("*_response.json"))
    if args.limit:
        response_paths = response_paths[: args.limit]

    case_rows: list[dict[str, Any]] = []
    line_rows: list[dict[str, Any]] = []
    vehicle_rows: list[dict[str, Any]] = []
    for response_path in response_paths:
        case_id = case_id_from_path(response_path)
        truth_path = truth_by_case.get(case_id)
        if not truth_path:
            continue
        request = read_json(truth_path)
        response = read_json(response_path)
        summary = read_summary(stage1_dir / f"{case_id}_summary.json")
        final_cars, replay_errors = final_cars_after_stage1(request, response)
        analysis = analyze_case(case_id, request, response, summary, final_cars, replay_errors)
        case_rows.append(analysis["case"])
        line_rows.extend(analysis["lines"])
        vehicle_rows.extend(analysis["vehicles"])

    summary = summarize_cases(case_rows)
    write_csv(output_dir / "stage1_friendliness_cases.csv", case_rows)
    write_csv(output_dir / "stage1_friendliness_lines.csv", line_rows)
    write_csv(output_dir / "stage1_friendliness_vehicles.csv", vehicle_rows)
    write_json(output_dir / "stage1_friendliness_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def analyze_case(
    case_id: str,
    request: dict[str, Any],
    response: dict[str, Any],
    summary: dict[str, Any],
    final_cars: list[dict[str, Any]],
    replay_errors: list[Any],
) -> dict[str, Any]:
    del request, response
    stage1 = stage1_boundary_metrics(final_cars)
    stage4 = stage4_metrics(final_cars)
    template_metrics = stage3_template_metrics(final_cars)
    best_template = min(
        TEMPLATE_NAMES,
        key=lambda name: (
            -float(template_metrics[name]["stage3_ready_score"]),
            int(template_metrics[name]["group_run_count"]),
            int(template_metrics[name]["extra_fragment_count"]),
            name,
        ),
    )
    best = template_metrics[best_template]
    group_by_no = best.get("group_by_no", {})
    line_rows = assembly_line_rows(case_id, final_cars, group_by_no, best_template)
    vehicle_rows = vehicle_detail_rows(case_id, final_cars, group_by_no, best_template)
    stage1_debt = summary.get("stage1_debt") or {}
    case_row = {
        "case_id": case_id,
        "stage1_status": summary.get("status", ""),
        "stage1_hooks": int(summary.get("hooks") or 0),
        "stage1_business_hooks": int(summary.get("business_hooks") or 0),
        "stage1_debt_count": int(stage1_debt.get("debt_count") or 0),
        "replay_error_count": len(replay_errors),
        "depot_assembly_count": stage1["depot_assembly_count"],
        "depot_unassembled_count": stage1["depot_unassembled_count"],
        "assembly_load_m": f"{stage1['assembly_load_m']:.3f}",
        "assembly_used_rate": f"{stage1['assembly_used_rate']:.6f}",
        "boundary_pollution_count": stage1["boundary_pollution_count"],
        "assembly_pollution_count": stage1["assembly_pollution_count"],
        "store4_pollution_count": stage1["store4_pollution_count"],
        "best_template": best_template,
        "stage3_ready_score": f"{best['stage3_ready_score']:.3f}",
        "stage3_grouped_rate": f"{best['grouped_rate']:.6f}",
        "stage3_cohesion_rate": f"{best['cohesion_rate']:.6f}",
        "stage3_active_count": best["active_count"],
        "stage3_ungrouped_count": best["ungrouped_count"],
        "stage3_group_count": best["distinct_group_count"],
        "stage3_group_run_count": best["group_run_count"],
        "stage3_group_switch_count": best["group_switch_count"],
        "stage3_extra_fragment_count": best["extra_fragment_count"],
        "stage3_prefix_blocked_count": best["prefix_blocked_count"],
        "template_a_score": f"{template_metrics['A']['stage3_ready_score']:.3f}",
        "template_a_runs": template_metrics["A"]["group_run_count"],
        "template_a_fragments": template_metrics["A"]["extra_fragment_count"],
        "template_b_score": f"{template_metrics['B']['stage3_ready_score']:.3f}",
        "template_b_runs": template_metrics["B"]["group_run_count"],
        "template_b_fragments": template_metrics["B"]["extra_fragment_count"],
        "stage4_tail_debt_count": stage4["tail_debt_count"],
        "stage4_access_blocked_debt_count": stage4["access_blocked_debt_count"],
        "stage4_done_prefix_count": stage4["done_prefix_count"],
        "stage4_done_prefix_rate": f"{stage4['done_prefix_rate']:.6f}",
        "store4_release_debt_count": stage4["store4_release_debt_count"],
        "stage4_debt_by_target": counter_text(stage4["debt_by_target"]),
        "stage3_group_sequence": best.get("group_sequence", ""),
        "blocking_reasons": "|".join(str(item) for item in summary.get("blocking_reasons") or []),
    }
    return {"case": case_row, "lines": line_rows, "vehicles": vehicle_rows}


def stage3_template_metrics(final_cars: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    active_nos = stage3_assembly_active_nos(final_cars)
    group_by_no = {
        physical.car_no(car): depot_stage3_group_key(car)
        for car in final_cars
        if physical.car_no(car) in active_nos
    }
    for template in TEMPLATE_NAMES:
        exposure = template_exposure_order(final_cars, template)
        metrics[template] = score_exposure(
            exposure=exposure,
            active_nos=active_nos,
            group_by_no=group_by_no,
            final_cars=final_cars,
        )
    return metrics


def score_exposure(
    *,
    exposure: tuple[str, ...],
    active_nos: set[str],
    group_by_no: dict[str, str],
    final_cars: list[dict[str, Any]],
) -> dict[str, Any]:
    active_count = len(active_nos)
    if not active_count:
        return {
            **empty_template_metric(active_count=0),
            "group_by_no": {},
            "exposure_order": "",
            "group_sequence": "",
        }

    grouped_in_exposure = [no for no in exposure if group_by_no.get(no)]
    ungrouped = sorted(no for no in active_nos if no not in group_by_no)
    keys = [group_by_no.get(no, "UNGROUPED") for no in exposure]
    run_keys = compressed(keys)
    group_run_count = len(run_keys)
    distinct_groups = {key for key in keys if key != "UNGROUPED"}
    fragments = Counter(run_keys)
    extra_fragment_count = sum(max(0, count - 1) for key, count in fragments.items() if key != "UNGROUPED")
    grouped_rate = len(grouped_in_exposure) / active_count if active_count else 1.0
    denominator = max(1, len(grouped_in_exposure) - len(distinct_groups))
    cohesion_rate = 1.0 - (extra_fragment_count / denominator)
    cohesion_rate = min(1.0, max(0.0, cohesion_rate))
    prefix_blocked_count = stage3_prefix_blocked_count(final_cars, active_nos)
    prefix_penalty_rate = prefix_blocked_count / active_count if active_count else 0.0
    stage3_ready_score = 100.0 * grouped_rate * cohesion_rate * (1.0 - min(1.0, prefix_penalty_rate))
    return {
        "stage3_ready_score": round(stage3_ready_score, 3),
        "grouped_rate": round(grouped_rate, 6),
        "cohesion_rate": round(cohesion_rate, 6),
        "active_count": active_count,
        "grouped_count": len(grouped_in_exposure),
        "ungrouped_count": len(ungrouped),
        "distinct_group_count": len(distinct_groups),
        "group_run_count": group_run_count,
        "group_switch_count": max(0, group_run_count - 1),
        "extra_fragment_count": extra_fragment_count,
        "prefix_blocked_count": prefix_blocked_count,
        "group_by_no": dict(group_by_no),
        "exposure_order": "|".join(exposure),
        "group_sequence": "|".join(run_keys),
    }


def empty_template_metric(active_count: int, reason: str = "") -> dict[str, Any]:
    del reason
    return {
        "stage3_ready_score": 100.0 if active_count == 0 else 0.0,
        "grouped_rate": 1.0 if active_count == 0 else 0.0,
        "cohesion_rate": 1.0 if active_count == 0 else 0.0,
        "active_count": active_count,
        "grouped_count": 0,
        "ungrouped_count": active_count,
        "distinct_group_count": 0,
        "group_run_count": 0,
        "group_switch_count": 0,
        "extra_fragment_count": 0,
        "prefix_blocked_count": 0,
        "group_by_no": {},
        "exposure_order": "",
        "group_sequence": "",
    }


def stage3_assembly_active_nos(final_cars: list[dict[str, Any]]) -> set[str]:
    return {
        physical.car_no(car)
        for car in final_cars
        if car["Line"] in set(STAGE3_ASSEMBLY_LINES)
        and set(car.get("TargetLines") or []) & set(physical.DEPOT_TARGET_LINES)
    }


def template_exposure_order(final_cars: list[dict[str, Any]], template: str) -> tuple[str, ...]:
    if template == "A":
        first: list[str] = []
        for line in TEMPLATE_A_FIRST_ORDER:
            first.extend(physical.line_access_order(final_cars, line))
        second = physical.line_access_order(final_cars, TEMPLATE_A_SECOND_LINE)
        return (*reversed(first), *reversed(second))
    all_nos: list[str] = []
    for line in TEMPLATE_B_ORDER:
        all_nos.extend(physical.line_access_order(final_cars, line))
    return tuple(reversed(all_nos))


def stage3_prefix_blocked_count(final_cars: list[dict[str, Any]], active_nos: set[str]) -> int:
    blocked = 0
    for line in STAGE3_ASSEMBLY_LINES:
        ordered = physical.line_access_order(final_cars, line)
        active_on_line = [no for no in ordered if no in active_nos]
        if not active_on_line:
            continue
        reachable_prefix = ordered[: len(active_on_line)]
        blocked += sum(1 for no in active_on_line if no not in reachable_prefix)
    return blocked


def stage1_boundary_metrics(final_cars: list[dict[str, Any]]) -> dict[str, Any]:
    assembly_capacity = sum(physical.TRACK_SPECS[line].length_m for line in STAGE1_ASSEMBLY_LINES)
    assembly_load = 0.0
    depot_assembly_count = 0
    depot_unassembled_count = 0
    assembly_pollution = 0
    store4_pollution = 0
    for car in final_cars:
        line = car["Line"]
        targets = set(car.get("TargetLines") or [])
        if line in STAGE1_ASSEMBLY_LINES:
            assembly_load += float(car.get("Length") or 14.3)
            if targets & set(physical.DEPOT_TARGET_LINES):
                depot_assembly_count += 1
            else:
                assembly_pollution += 1
        if line == "存4线" and UNWHEEL_LINE not in targets:
            store4_pollution += 1
        if stage1_goal(car) == "depot_assembly" and line not in STAGE1_ASSEMBLY_LINES:
            depot_unassembled_count += 1
    return {
        "depot_assembly_count": depot_assembly_count,
        "depot_unassembled_count": depot_unassembled_count,
        "assembly_load_m": assembly_load,
        "assembly_used_rate": assembly_load / assembly_capacity if assembly_capacity else 0.0,
        "assembly_pollution_count": assembly_pollution,
        "store4_pollution_count": store4_pollution,
        "boundary_pollution_count": assembly_pollution + store4_pollution,
    }


def stage4_metrics(final_cars: list[dict[str, Any]]) -> dict[str, Any]:
    by_no = {physical.car_no(car): car for car in final_cars}
    debt_nos = {physical.car_no(car) for car in final_cars if is_stage4_debt(car)}
    managed_nos = {
        physical.car_no(car)
        for car in final_cars
        if is_stage4_managed_target(car)
    }
    debt_by_target: Counter[str] = Counter()
    for no in debt_nos:
        car = by_no[no]
        debt_by_target[target_key(car)] += 1

    blocked_debt = 0
    done_prefix = 0
    managed_total = len(managed_nos)
    for line in sorted({car["Line"] for car in final_cars if car["Line"]}):
        ordered = physical.line_access_order(final_cars, line)
        seen_blocker = False
        for index, no in enumerate(ordered):
            car = by_no[no]
            if no in debt_nos:
                if seen_blocker:
                    blocked_debt += 1
                continue
            if no in managed_nos and target_satisfied(car):
                if not seen_blocker:
                    done_prefix += 1
                continue
            if any(later in debt_nos for later in ordered[index + 1 :]):
                seen_blocker = True

    store4_release = sum(
        1
        for car in final_cars
        if car["Line"] == "存4线"
        and "存4线" not in set(car.get("TargetLines") or [])
        and UNWHEEL_LINE not in set(car.get("TargetLines") or [])
    )
    return {
        "tail_debt_count": len(debt_nos),
        "access_blocked_debt_count": blocked_debt,
        "done_prefix_count": done_prefix,
        "done_prefix_rate": done_prefix / managed_total if managed_total else 1.0,
        "store4_release_debt_count": store4_release,
        "debt_by_target": debt_by_target,
    }


def assembly_line_rows(
    case_id: str,
    final_cars: list[dict[str, Any]],
    group_by_no: dict[str, str],
    template: str,
) -> list[dict[str, Any]]:
    by_no = {physical.car_no(car): car for car in final_cars}
    rows: list[dict[str, Any]] = []
    for line in STAGE1_ASSEMBLY_LINES:
        ordered = physical.line_access_order(final_cars, line)
        keys = [group_by_no.get(no) or line_bucket(by_no[no]) for no in ordered if no in by_no]
        run_keys = compressed(keys)
        counts = Counter(keys)
        dominant, dominant_count = counts.most_common(1)[0] if counts else ("", 0)
        load_m = sum(float(by_no[no].get("Length") or 14.3) for no in ordered if no in by_no)
        capacity = physical.TRACK_SPECS[line].length_m
        rows.append(
            {
                "case_id": case_id,
                "line": line,
                "best_template": template,
                "car_count": len(ordered),
                "load_m": f"{load_m:.3f}",
                "capacity_m": f"{capacity:.3f}",
                "used_rate": f"{load_m / capacity:.6f}" if capacity else "0.000000",
                "depot_count": sum(1 for no in ordered if no in group_by_no),
                "pollution_count": sum(1 for no in ordered if no not in group_by_no),
                "group_run_count": len(run_keys),
                "group_switch_count": max(0, len(run_keys) - 1),
                "extra_fragment_count": sum(max(0, count - 1) for count in Counter(run_keys).values()),
                "dominant_group": dominant,
                "purity_rate": f"{dominant_count / len(keys):.6f}" if keys else "1.000000",
                "long_count": sum(1 for no in ordered if no in by_no and float(by_no[no].get("Length") or 14.3) >= 17.6),
                "factory_count": sum(1 for no in ordered if no in by_no and str(by_no[no].get("RepairProcess") or "").startswith("厂")),
                "force_position_count": sum(1 for no in ordered if no in by_no and physical.force_positions(by_no[no])),
                "group_sequence": "|".join(keys),
                "car_sequence": "|".join(ordered),
            }
        )
    return rows


def vehicle_detail_rows(
    case_id: str,
    final_cars: list[dict[str, Any]],
    group_by_no: dict[str, str],
    template: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in STAGE1_ASSEMBLY_LINES:
        for access_index, no in enumerate(physical.line_access_order(final_cars, line), start=1):
            car = next((item for item in final_cars if physical.car_no(item) == no), None)
            if not car:
                continue
            rows.append(
                {
                    "case_id": case_id,
                    "line": line,
                    "access_index": access_index,
                    "position": int(car.get("Position") or 0),
                    "no": no,
                    "best_template": template,
                    "stage3_group": group_by_no.get(no, ""),
                    "target_key": group_by_no.get(no) or line_bucket(car),
                    "initial_line": car.get("_InitialLine", ""),
                    "repair_process": car.get("RepairProcess", ""),
                    "length": f"{float(car.get('Length') or 14.3):.3f}",
                    "is_long": int(float(car.get("Length") or 14.3) >= 17.6),
                    "is_factory": int(str(car.get("RepairProcess") or "").startswith("厂")),
                    "force_positions": "/".join(str(item) for item in physical.force_positions(car)),
                    "targets": "/".join(car.get("TargetLines") or []),
                }
            )
    return rows


def final_cars_after_stage1(
    request: dict[str, Any],
    response: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Any]]:
    initial = [physical.normalized_car(car) for car in request.get("StartStatus") or []]
    by_no = {physical.car_no(car): dict(car) for car in initial}
    generated = rv.generated(response)
    if generated:
        for row in generated:
            no = str(row.get("No") or "")
            if no not in by_no:
                continue
            initial_line = by_no[no].get("_InitialLine")
            by_no[no].update(row)
            by_no[no]["_InitialLine"] = initial_line
        return [physical.normalized_car(car) for car in by_no.values()], []
    replayed, bad = rv.replay(request, response)
    return [physical.normalized_car(car) for car in replayed], list(bad)


def stage1_goal(car: dict[str, Any]) -> str:
    if car.get("_InitialLine") in (set(physical.DEPOT_TARGET_LINES) | {UNWHEEL_LINE}):
        return ""
    if car["Line"] in physical.DEPOT_TARGET_LINES:
        return ""
    targets = set(car.get("TargetLines") or ())
    if UNWHEEL_LINE in targets:
        return "存4线"
    if targets & set(physical.DEPOT_TARGET_LINES):
        return "depot_assembly"
    return ""


def is_stage4_managed_target(car: dict[str, Any]) -> bool:
    targets = set(car.get("TargetLines") or [])
    return bool(targets) and not bool(targets & STAGE4_IGNORED_TARGET_LINES)


def is_stage4_debt(car: dict[str, Any]) -> bool:
    return is_stage4_managed_target(car) and not target_satisfied(car)


def target_satisfied(car: dict[str, Any]) -> bool:
    targets = set(car.get("TargetLines") or [])
    if not targets:
        return True
    line = car["Line"]
    if line not in targets:
        return False
    forced = physical.force_positions(car)
    if forced and int(car.get("Position") or 0) not in forced:
        return False
    if line == "存4线" and car.get("IsClosedDoor") and int(car.get("Position") or 0) <= 3:
        return False
    return True


def depot_stage3_group_key(car: dict[str, Any]) -> str:
    targets = set(car.get("TargetLines") or [])
    inner = sorted(targets & set(physical.DEPOT_LINES))
    outer = sorted(targets & set(physical.DEPOT_OUTSIDE_LINES))
    if inner and outer:
        target_part = "IO:" + "/".join((*inner, *outer))
    elif inner:
        target_part = "I:修1-4" if set(inner) == set(physical.DEPOT_LINES) else "I:" + "/".join(inner)
    elif outer:
        target_part = "O:" + "/".join(outer)
    else:
        target_part = "D:UNKNOWN"

    repair = str(car.get("RepairProcess") or "")
    length = float(car.get("Length") or 14.3)
    if repair.startswith("厂"):
        class_part = "厂修"
    elif length >= 17.6:
        class_part = "长车"
    else:
        class_part = "段修短"

    forced = physical.force_positions(car)
    force_part = f":F{min(forced)}-{max(forced)}" if forced else ""
    return f"{target_part}:{class_part}{force_part}"


def line_bucket(car: dict[str, Any]) -> str:
    targets = set(car.get("TargetLines") or [])
    if targets & set(physical.DEPOT_TARGET_LINES):
        return "DEPOT_UNASSIGNED"
    if UNWHEEL_LINE in targets:
        return "UNWHEEL_TO_STORE4"
    if "存4线" in targets:
        return "STORE4"
    return target_key(car) or "NON_DEPOT"


def target_key(car: dict[str, Any]) -> str:
    targets = tuple(sorted(car.get("TargetLines") or []))
    if not targets:
        return ""
    if len(targets) == 1:
        return targets[0]
    return "/".join(targets)


def compressed(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def summarize_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"case_count": 0}
    scores = [float(row["stage3_ready_score"]) for row in rows]
    debts = [int(row["stage4_tail_debt_count"]) for row in rows]
    blocked = [int(row["stage4_access_blocked_debt_count"]) for row in rows]
    pollution = [int(row["boundary_pollution_count"]) for row in rows]
    return {
        "case_count": len(rows),
        "stage1_complete_count": sum(1 for row in rows if row["stage1_status"] == "complete"),
        "avg_stage3_ready_score": round(sum(scores) / len(scores), 3),
        "min_stage3_ready_score": round(min(scores), 3),
        "p10_stage3_ready_score": percentile(scores, 0.10),
        "avg_stage4_tail_debt_count": round(sum(debts) / len(debts), 3),
        "avg_stage4_access_blocked_debt_count": round(sum(blocked) / len(blocked), 3),
        "boundary_pollution_case_count": sum(1 for value in pollution if value > 0),
        "best_template_counts": dict(Counter(str(row["best_template"]) for row in rows)),
        "worst_stage3_cases": [
            {
                "case_id": row["case_id"],
                "score": float(row["stage3_ready_score"]),
                "runs": int(row["stage3_group_run_count"]),
                "fragments": int(row["stage3_extra_fragment_count"]),
                "stage1_status": row["stage1_status"],
            }
            for row in sorted(rows, key=lambda item: (float(item["stage3_ready_score"]), item["case_id"]))[:10]
        ],
        "worst_stage4_cases": [
            {
                "case_id": row["case_id"],
                "tail_debt": int(row["stage4_tail_debt_count"]),
                "blocked_debt": int(row["stage4_access_blocked_debt_count"]),
                "debt_by_target": row["stage4_debt_by_target"],
            }
            for row in sorted(
                rows,
                key=lambda item: (
                    -int(item["stage4_tail_debt_count"]),
                    -int(item["stage4_access_blocked_debt_count"]),
                    item["case_id"],
                ),
            )[:10]
        ],
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[index], 3)


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in sorted(counter.items()) if key)


def truth_case_map(truth_dir: Path) -> dict[str, Path]:
    return {
        physical.case_id_from_path(path): path
        for path in sorted(truth_dir.glob("validation_*.json"))
    }


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"cannot infer case id from {path}")
    return match.group(1).upper()


def read_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return read_json(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
