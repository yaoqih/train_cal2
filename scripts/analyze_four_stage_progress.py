#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import depot_outbound_plan
from solver_vnext import physical
from solver_vnext import strategic_plan
from analyze_depot_inbound_stage import (
    _analyze_case as _analyze_inbound_case,
    _initial_depot_inbound_debt,
    _read_operations,
)
from analyze_depot_stage2 import (
    _analyze_case as _analyze_stage2_case,
    _read_selected_steps,
)
from analyze_depot_stage3 import _analyze_stage3_case


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze four-stage vNext progress and unfinished vehicles.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "four_stage_progress"
    output_dir.mkdir(parents=True, exist_ok=True)

    case_summary = _read_case_summary(artifact_dir / "case_summary.csv")
    operations = _read_operations(artifact_dir / "operation_trace.csv")
    selected_steps = _read_selected_steps(artifact_dir / "step_trace.csv")
    phase_records = _read_phase_records(artifact_dir / "phase_gate_records.csv")
    inbound_initial = _initial_depot_inbound_debt(truth_dir)
    wanted_case_ids = set(case_summary)

    case_rows: list[dict[str, Any]] = []
    unfinished_rows: list[dict[str, Any]] = []
    for path in sorted(truth_dir.glob("*.json")):
        if path.name == "conversion_summary.json":
            continue
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        if case_id not in wanted_case_ids:
            continue
        summary_row = case_summary.get(case_id, {})
        case_ops = operations.get(case_id, {})
        case_selected = selected_steps.get(case_id, {})
        inbound_case = inbound_initial.get(case_id)
        inbound_result = _inbound_result(
            case_id=case_id,
            inbound_case=inbound_case,
            operations=case_ops,
        )
        stage2_result = _analyze_stage2_case(
            case_id=case_id,
            cars=cars,
            depot_assignment=depot_assignment,
            operations=case_ops,
            selected_steps=case_selected,
            inbound_result=inbound_result if inbound_case else None,
        )
        stage3_result = (
            _analyze_stage3_case(
                case_id=case_id,
                inbound_case=inbound_case,
                inbound_result=inbound_result,
                stage2_result=stage2_result,
                operations=case_ops,
                selected_steps=case_selected,
            )
            if inbound_case
            else None
        )
        outbound_plan = depot_outbound_plan.build_depot_outbound_assembly_plan(
            cars=cars,
            depot_assignment=depot_assignment,
        )
        final_cars = _final_cars(
            artifact_dir=artifact_dir,
            case_id=case_id,
            initial_cars=cars,
            operations=case_ops,
        )
        front_business = _front_business_progress(
            initial_cars=cars,
            final_cars=final_cars,
            depot_assignment=depot_assignment,
        )
        phase_progress = _phase_progress(
            phase_records=phase_records.get(case_id, []),
            hook_count=_int(summary_row.get("hook_count")),
            status=str(summary_row.get("status") or ""),
            inbound_count=len(inbound_case["debt"]) if inbound_case else 0,
            outbound_count=len(outbound_plan.outbound_nos),
            stage2_result=stage2_result,
            stage3_result=stage3_result,
        )
        vehicle_progress = _vehicle_progress(
            case_id=case_id,
            initial_cars=cars,
            final_cars=final_cars,
            depot_assignment=depot_assignment,
            outbound_nos=set(outbound_plan.outbound_nos),
        )
        unfinished_rows.extend(vehicle_progress["unfinished_rows"])
        case_rows.append(
            {
                "case_id": case_id,
                "status": summary_row.get("status", ""),
                "hook_count": _int(summary_row.get("hook_count")),
                "initial_unsatisfied": vehicle_progress["initial_unsatisfied"],
                "final_unsatisfied": vehicle_progress["final_unsatisfied"],
                "vehicle_completion_rate": f"{vehicle_progress['completion_rate']:.6f}",
                "stage1_complete": int(phase_progress["stage1_complete"]),
                "stage1_completion_hook": phase_progress["stage1_hook"],
                "stage1_grouping_complete": int(inbound_result["stage_complete"]),
                "stage1_grouping_rate": f"{inbound_result['grouping_rate']:.6f}",
                "stage1_grouped_vehicle_count": inbound_result["grouped_at_checkpoint_count"],
                "stage1_ungrouped_vehicle_count": inbound_result["ungrouped_at_checkpoint_count"],
                "front_business_initial_pending_count": front_business["initial_pending"],
                "front_business_final_pending_count": front_business["final_pending"],
                "front_business_completion_rate": f"{front_business['completion_rate']:.6f}",
                "stage2_complete": int(phase_progress["stage2_complete"]),
                "stage2_completion_hook": phase_progress["stage2_hook"],
                "stage2_outbound_count": len(outbound_plan.outbound_nos),
                "stage2_reached_cun4_count": stage2_result["outbound_reached_cun4_count"],
                "stage3_complete": int(phase_progress["stage3_complete"]),
                "stage3_completion_hook": phase_progress["stage3_hook"],
                "stage3_inbound_count": len(inbound_case["debt"]) if inbound_case else 0,
                "stage3_reached_target_count": stage3_result["depot_inbound_reached_target_count"] if stage3_result else 0,
                "stage4_complete": int(str(summary_row.get("status") or "") == "completed"),
                "stage1_hook_count": phase_progress["stage1_hook_count"],
                "stage2_hook_count": phase_progress["stage2_hook_count"],
                "stage3_hook_count": phase_progress["stage3_hook_count"],
                "stage4_hook_count": phase_progress["stage4_hook_count"],
                "blocked_reason": summary_row.get("blocked_reason", ""),
            }
        )

    summary = _summary(case_rows=case_rows, unfinished_rows=unfinished_rows)
    physical.write_csv(output_dir / "four_stage_case_progress.csv", case_rows)
    physical.write_csv(output_dir / "unfinished_vehicle_analysis.csv", unfinished_rows)
    physical.write_json(output_dir / "four_stage_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def _read_case_summary(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["case_id"]: row for row in csv.DictReader(handle)}


def _read_phase_records(path: Path) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            row["step_index"] = _int(row.get("step_index"))
            row["predicates"] = _parse_predicates(row.get("predicate_values", ""))
            records[row["case_id"]].append(row)
    return {case_id: sorted(rows, key=lambda row: row["step_index"]) for case_id, rows in records.items()}


def _parse_predicates(text: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        output[key] = value
    return output


def _inbound_result(
    *,
    case_id: str,
    inbound_case: dict[str, Any] | None,
    operations: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    if inbound_case:
        return _analyze_inbound_case(case_id=case_id, case=inbound_case, operations=operations)
    return {
        "stage_complete": 1,
        "completion_hook": 0,
        "cun4_release_hook": 0,
        "release_hook": 0,
        "grouping_rate": 1.0,
        "grouped_at_checkpoint_count": 0,
        "ungrouped_at_checkpoint_count": 0,
        "grouped_by_line_counts": Counter(),
    }


def _front_business_progress(
    *,
    initial_cars: list[dict[str, Any]],
    final_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> dict[str, Any]:
    initial_pending = len(
        strategic_plan.build_front_topology_plan(
            cars=initial_cars,
            depot_assignment=depot_assignment,
            remote_debt=0,
        ).priority_nos
    )
    final_pending = len(
        strategic_plan.build_front_topology_plan(
            cars=final_cars,
            depot_assignment=depot_assignment,
            remote_debt=0,
        ).priority_nos
    )
    if initial_pending:
        completion_rate = 1.0 - final_pending / initial_pending
    else:
        completion_rate = 1.0
    return {
        "initial_pending": initial_pending,
        "final_pending": final_pending,
        "completion_rate": max(0.0, min(1.0, completion_rate)),
    }


def _phase_progress(
    *,
    phase_records: list[dict[str, Any]],
    hook_count: int,
    status: str,
    inbound_count: int,
    outbound_count: int,
    stage2_result: dict[str, Any],
    stage3_result: dict[str, Any] | None,
) -> dict[str, Any]:
    stage1_hook: int | None = None
    stage2_hook: int | None = None
    stage3_hook: int | None = None
    for row in phase_records:
        preds = row["predicates"]
        inbound_grouped = preds.get("depot_inbound_assembly_complete") == "True"
        outbound_done = preds.get("depot_outbound_assembly_complete") == "True"
        if stage1_hook is None and inbound_grouped:
            stage1_hook = max(0, _int(row["step_index"]) - 1)
        if stage1_hook is not None and stage2_hook is None and outbound_done:
            stage2_hook = max(stage1_hook, _int(row["step_index"]) - 1)
        phase_reason = preds.get("phase_reason", "")
        if stage2_hook is not None and stage3_hook is None and (
            row.get("to_phase") == "H5"
            or row.get("from_phase") == "H5"
            or phase_reason in {"stage4_residual_closeout", "primary_debt_closed"}
        ):
            stage3_hook = max(stage2_hook, _int(row["step_index"]) - 1)

    if not outbound_count and stage1_hook is not None and stage2_hook is None:
        stage2_hook = stage1_hook
    if not inbound_count and stage2_hook is not None and stage3_hook is None:
        stage3_hook = stage2_hook
    if stage2_result.get("stage2_complete"):
        stage2_hook = stage2_hook if stage2_hook is not None else _int(stage2_result.get("outbound_completion_hook"))
    if stage3_result and stage3_result.get("stage3_complete"):
        stage3_hook = stage3_hook if stage3_hook is not None else _int(stage3_result.get("stage3_completion_hook"))
    if status == "completed":
        stage1_hook = stage1_hook if stage1_hook is not None else hook_count
        stage2_hook = stage2_hook if stage2_hook is not None else hook_count
        stage3_hook = stage3_hook if stage3_hook is not None else hook_count

    stage1_complete = stage1_hook is not None or (not phase_records and status == "completed")
    stage2_complete = stage2_hook is not None or (stage1_complete and not outbound_count)
    stage3_complete = stage3_hook is not None or (stage2_complete and not inbound_count)
    stage1_value = stage1_hook if stage1_hook is not None else 0
    stage2_value = stage2_hook if stage2_hook is not None else 0
    stage3_value = stage3_hook if stage3_hook is not None else 0
    stage1_end = stage1_value if stage1_complete else hook_count
    stage2_end = stage2_value if stage2_complete else stage1_end
    stage3_end = stage3_value if stage3_complete else stage2_end
    return {
        "stage1_complete": stage1_complete,
        "stage2_complete": stage2_complete,
        "stage3_complete": stage3_complete,
        "stage1_hook": stage1_value,
        "stage2_hook": stage2_value,
        "stage3_hook": stage3_value,
        "stage1_hook_count": stage1_end,
        "stage2_hook_count": max(0, stage2_end - stage1_end),
        "stage3_hook_count": max(0, stage3_end - stage2_end),
        "stage4_hook_count": max(0, hook_count - stage3_end) if status == "completed" else 0,
    }


def _final_cars(
    *,
    artifact_dir: Path,
    case_id: str,
    initial_cars: list[dict[str, Any]],
    operations: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    final = [dict(car) for car in initial_cars]
    by_no = {physical.car_no(car): car for car in final}
    response_path = artifact_dir / "responses" / f"{case_id}.json"
    if response_path.exists():
        payload = json.loads(response_path.read_text(encoding="utf-8"))
        for row in payload.get("Data", {}).get("GeneratedEndStatus", []):
            no = str(row.get("No") or "")
            if no not in by_no:
                continue
            by_no[no]["Line"] = physical.normalize_line(row.get("Line"))
            by_no[no]["Position"] = _int(row.get("Position"))
    weighed: set[str] = set()
    for hook_ops in operations.values():
        for op in hook_ops:
            if op.get("action") != "Weigh":
                continue
            weighed.update(op.get("move_nos", ()))
    for no in weighed:
        if no in by_no:
            by_no[no]["_Weighed"] = True
    return final


def _vehicle_progress(
    *,
    case_id: str,
    initial_cars: list[dict[str, Any]],
    final_cars: list[dict[str, Any]],
    depot_assignment: Any,
    outbound_nos: set[str],
) -> dict[str, Any]:
    initial_unsatisfied = {physical.car_no(car) for car in physical.unsatisfied_cars(initial_cars, depot_assignment)}
    final_unsatisfied_cars = physical.unsatisfied_cars(final_cars, depot_assignment)
    loads = physical.line_loads(final_cars)
    initial_by_no = {physical.car_no(car): car for car in initial_cars}
    rows: list[dict[str, Any]] = []
    for car in final_unsatisfied_cars:
        no = physical.car_no(car)
        target_line, target_position, reason = physical.planned_target_for_car(
            car,
            final_cars,
            depot_assignment,
            loads,
        )
        initial_line = initial_by_no.get(no, {}).get("Line", "")
        category = _unfinished_category(
            no=no,
            initial_line=initial_line,
            final_line=car["Line"],
            target_line=target_line,
            outbound_nos=outbound_nos,
        )
        rows.append(
            {
                "case_id": case_id,
                "no": no,
                "category": category,
                "initial_line": initial_line,
                "final_line": car["Line"],
                "final_position": int(car.get("Position") or 0),
                "target_line": target_line,
                "target_position": target_position or "",
                "target_reason": reason,
                "is_weigh": int(bool(car.get("IsWeigh"))),
                "weighed": int(bool(car.get("_Weighed"))),
            }
        )
    denominator = len(initial_unsatisfied) or len(final_cars)
    completion_rate = 1.0 - (len(final_unsatisfied_cars) / denominator if denominator else 0.0)
    return {
        "initial_unsatisfied": len(initial_unsatisfied),
        "final_unsatisfied": len(final_unsatisfied_cars),
        "completion_rate": max(0.0, completion_rate),
        "unfinished_rows": rows,
    }


def _unfinished_category(
    *,
    no: str,
    initial_line: str,
    final_line: str,
    target_line: str,
    outbound_nos: set[str],
) -> str:
    if no in outbound_nos and final_line != "存4线":
        return "stage2_depot_outbound_to_cun4"
    if (
        target_line in physical.DEPOT_INBOUND_DESTINATION_LINES
        and initial_line not in physical.DEPOT_INBOUND_DESTINATION_LINES
    ):
        if final_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return "stage3_depot_inbound_release"
        if final_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
            return "stage3_depot_position_or_order"
        return "stage1_depot_inbound_grouping"
    if final_line == "存4线" and target_line != "存4线":
        return "stage4_cun4_front_release"
    return "stage1_or_stage4_front_position"


def _summary(*, case_rows: list[dict[str, Any]], unfinished_rows: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(case_rows)
    completed_cases = [row for row in case_rows if row["status"] == "completed"]
    bins = Counter(_rate_bin(float(row["vehicle_completion_rate"])) for row in case_rows)
    category_counts = Counter(row["category"] for row in unfinished_rows)
    line_target_counts = Counter(f"{row['final_line']}->{row['target_line']}" for row in unfinished_rows)
    return {
        "case_count": case_count,
        "completed_case_count": len(completed_cases),
        "completed_case_rate": _ratio(len(completed_cases), case_count),
        "stage1_complete_case_count": sum(row["stage1_complete"] for row in case_rows),
        "stage1_complete_case_rate": _ratio(sum(row["stage1_complete"] for row in case_rows), case_count),
        "stage1_grouping_complete_case_count": sum(row["stage1_grouping_complete"] for row in case_rows),
        "stage1_grouping_complete_case_rate": _ratio(sum(row["stage1_grouping_complete"] for row in case_rows), case_count),
        "front_business_initial_pending_vehicle_count": sum(row["front_business_initial_pending_count"] for row in case_rows),
        "front_business_final_pending_vehicle_count": sum(row["front_business_final_pending_count"] for row in case_rows),
        "front_business_completion_rate": _front_business_summary_rate(case_rows),
        "stage2_complete_case_count": sum(row["stage2_complete"] for row in case_rows),
        "stage2_complete_case_rate": _ratio(sum(row["stage2_complete"] for row in case_rows), case_count),
        "stage3_complete_case_count": sum(row["stage3_complete"] for row in case_rows),
        "stage3_complete_case_rate": _ratio(sum(row["stage3_complete"] for row in case_rows), case_count),
        "stage4_complete_case_count": sum(row["stage4_complete"] for row in case_rows),
        "stage4_complete_case_rate": _ratio(sum(row["stage4_complete"] for row in case_rows), case_count),
        "completed_hook_distribution": dict(sorted(Counter(row["hook_count"] for row in completed_cases).items())),
        "stage1_hook_distribution_completed_stage": dict(
            sorted(Counter(row["stage1_hook_count"] for row in case_rows if row["stage1_complete"]).items())
        ),
        "stage2_hook_distribution_completed_stage": dict(
            sorted(Counter(row["stage2_hook_count"] for row in case_rows if row["stage2_complete"]).items())
        ),
        "stage3_hook_distribution_completed_stage": dict(
            sorted(Counter(row["stage3_hook_count"] for row in case_rows if row["stage3_complete"]).items())
        ),
        "stage4_hook_distribution_completed_cases": dict(
            sorted(Counter(row["stage4_hook_count"] for row in completed_cases).items())
        ),
        "vehicle_completion_rate_bins": dict(sorted(bins.items())),
        "initial_unsatisfied_vehicle_count": sum(row["initial_unsatisfied"] for row in case_rows),
        "final_unsatisfied_vehicle_count": sum(row["final_unsatisfied"] for row in case_rows),
        "unfinished_vehicle_category_counts": dict(category_counts.most_common()),
        "unfinished_vehicle_line_target_top20": dict(line_target_counts.most_common(20)),
    }


def _rate_bin(rate: float) -> str:
    if rate >= 1.0:
        return "100%"
    floor = int(max(0.0, min(rate, 0.999999)) * 10) * 10
    return f"{floor}-{floor + 9}%"


def _front_business_summary_rate(case_rows: list[dict[str, Any]]) -> float:
    initial = sum(row["front_business_initial_pending_count"] for row in case_rows)
    final = sum(row["front_business_final_pending_count"] for row in case_rows)
    if not initial:
        return 1.0
    return round(max(0.0, 1.0 - final / initial), 6)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    main()
