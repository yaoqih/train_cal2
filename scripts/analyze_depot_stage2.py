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
from analyze_depot_inbound_stage import (
    _analyze_case as _analyze_inbound_case,
    _initial_depot_inbound_debt,
    _read_operations,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze depot outbound second-stage gathering to CUN4.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "depot_stage2"
    output_dir.mkdir(parents=True, exist_ok=True)

    operations = _read_operations(artifact_dir / "operation_trace.csv")
    selected_steps = _read_selected_steps(artifact_dir / "step_trace.csv")
    inbound_initial = _initial_depot_inbound_debt(truth_dir)
    rows: list[dict[str, Any]] = []
    for path in sorted(truth_dir.glob("*.json")):
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        inbound_case = inbound_initial.get(case_id)
        inbound_result = (
            _analyze_inbound_case(
                case_id=case_id,
                case=inbound_case,
                operations=operations.get(case_id, {}),
            )
            if inbound_case
            else None
        )
        rows.append(
            _analyze_case(
                case_id=case_id,
                cars=cars,
                depot_assignment=depot_assignment,
                operations=operations.get(case_id, {}),
                selected_steps=selected_steps.get(case_id, {}),
                inbound_result=inbound_result,
            )
        )

    summary = _summary(rows)
    physical.write_csv(output_dir / "depot_stage2_cases.csv", rows)
    physical.write_json(output_dir / "depot_stage2_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def _read_selected_steps(path: Path) -> dict[str, dict[int, dict[str, str]]]:
    by_case: dict[str, dict[int, dict[str, str]]] = defaultdict(dict)
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("selected") != "True":
                continue
            by_case[row["case_id"]][int(row["hook_index"] or 0)] = row
    return {case_id: dict(hooks) for case_id, hooks in by_case.items()}


def _analyze_case(
    *,
    case_id: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    operations: dict[int, list[dict[str, Any]]],
    selected_steps: dict[int, dict[str, str]],
    inbound_result: dict[str, Any] | None,
) -> dict[str, Any]:
    outbound_plan = depot_outbound_plan.build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=depot_assignment,
    )
    outbound_nos = set(outbound_plan.outbound_nos)
    line_by_no = {physical.car_no(car): car["Line"] for car in cars}
    stage1_complete = bool(inbound_result and inbound_result["stage_complete"])
    stage1_completion_hook = int(inbound_result["completion_hook"] if inbound_result else 0)
    cun4_release_hook = int(inbound_result["cun4_release_hook"] if inbound_result else 0)
    release_hook = int(inbound_result["release_hook"] if inbound_result else 0)
    cun4_grouped_count = int((inbound_result["grouped_by_line_counts"].get("存4线", 0) if inbound_result else 0))
    start_hook = _stage2_start_hook(
        stage1_complete=stage1_complete,
        stage1_completion_hook=stage1_completion_hook,
        cun4_release_hook=cun4_release_hook,
        cun4_grouped_count=cun4_grouped_count,
        outbound_nos=outbound_nos,
    )
    completion_hook = 0
    one_pull_hook = 0
    stage2_templates: Counter[str] = Counter()
    stage2_hook_indexes: set[int] = set()
    for hook_index in sorted(operations):
        hook_ops = operations[hook_index]
        for op in hook_ops:
            if op["action"] != "Put":
                continue
            for no in op["move_nos"]:
                line_by_no[no] = op["line"]
        if not start_hook or hook_index < start_hook or not outbound_nos:
            continue
        selected = selected_steps.get(hook_index)
        if selected:
            stage2_templates[selected.get("template_name", "")] += 1
        stage2_hook_indexes.add(hook_index)
        if _selected_hook_is_one_pull(selected=selected, outbound_nos=outbound_nos):
            one_pull_hook = hook_index
        if all(line_by_no.get(no) == "存4线" for no in outbound_nos):
            completion_hook = hook_index
            break

    reached_count = sum(1 for no in outbound_nos if line_by_no.get(no) == "存4线")
    if completion_hook:
        reached_count = len(outbound_nos)
    stage2_hook_count = len(stage2_hook_indexes) if completion_hook else 0
    return {
        "case_id": case_id,
        "stage1_complete": int(stage1_complete),
        "stage1_completion_hook": stage1_completion_hook,
        "stage1_release_hook": release_hook,
        "cun4_release_hook": cun4_release_hook,
        "cun4_grouped_count": cun4_grouped_count,
        "stage2_start_hook": start_hook,
        "outbound_count": len(outbound_nos),
        "outbound_plan_status": outbound_plan.status,
        "outbound_plan_reason": outbound_plan.reason,
        "outbound_pull_equivalent": outbound_plan.pull_equivalent,
        "outbound_reached_cun4_count": reached_count,
        "outbound_completion_hook": completion_hook,
        "stage2_complete": int(bool(outbound_nos) and bool(completion_hook)),
        "stage2_hook_count": stage2_hook_count,
        "stage2_one_pull": int(bool(completion_hook and one_pull_hook == completion_hook)),
        "stage2_template_counts": _counter_text(stage2_templates),
        "remaining_outbound_nos": "|".join(sorted(no for no in outbound_nos if line_by_no.get(no) != "存4线")),
        "outbound_nos": "|".join(outbound_plan.outbound_nos),
        "route_blocker_nos": "|".join(outbound_plan.route_blocker_nos),
        "outbound_non_cun4_target_nos": "|".join(outbound_plan.non_cun4_nos),
        "outbound_final_cun4_target_nos": "|".join(outbound_plan.cun4_target_nos),
        "outbound_stage2_cun4_hold_nos": "|".join(outbound_plan.cun4_nos),
        "outbound_pull_order_nos": "|".join(outbound_plan.pull_order_nos),
    }


def _stage2_start_hook(
    *,
    stage1_complete: bool,
    stage1_completion_hook: int,
    cun4_release_hook: int,
    cun4_grouped_count: int,
    outbound_nos: set[str],
) -> int:
    if not outbound_nos or not stage1_complete:
        return 0
    if cun4_grouped_count:
        return cun4_release_hook
    return stage1_completion_hook + 1


def _selected_hook_is_one_pull(
    *,
    selected: dict[str, str] | None,
    outbound_nos: set[str],
) -> bool:
    if not selected:
        return False
    if selected.get("template_name") != "depot_outbound_session":
        return False
    move_nos = set(no for no in selected.get("move_nos", "").split("|") if no)
    put_lines = set(line for line in selected.get("put_lines", "").split("|") if line)
    return outbound_nos <= move_nos and put_lines == {"存4线"}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = [row for row in rows if row["outbound_count"]]
    stage1_ready = [row for row in relevant if row["stage1_complete"]]
    stage2_started = [row for row in stage1_ready if row["stage2_start_hook"]]
    completed = [row for row in stage2_started if row["stage2_complete"]]
    hook_counts = Counter(row["stage2_hook_count"] for row in completed)
    return {
        "case_count": len(rows),
        "outbound_case_count": len(relevant),
        "stage1_ready_outbound_case_count": len(stage1_ready),
        "stage2_started_case_count": len(stage2_started),
        "outbound_debt_count": sum(row["outbound_count"] for row in relevant),
        "stage1_ready_outbound_debt_count": sum(row["outbound_count"] for row in stage1_ready),
        "stage2_started_outbound_debt_count": sum(row["outbound_count"] for row in stage2_started),
        "stage2_completed_case_count": len(completed),
        "stage2_completion_rate_on_stage1_ready_cases": _ratio(len(completed), len(stage1_ready)),
        "stage2_completion_rate_on_started_cases": _ratio(len(completed), len(stage2_started)),
        "stage2_reached_cun4_count": sum(row["outbound_reached_cun4_count"] for row in stage2_started),
        "stage2_reached_cun4_rate_on_started_debt": _ratio(
            sum(row["outbound_reached_cun4_count"] for row in stage2_started),
            sum(row["outbound_count"] for row in stage2_started),
        ),
        "stage2_one_pull_case_count": sum(row["stage2_one_pull"] for row in completed),
        "stage2_hook_count_distribution": dict(sorted(hook_counts.items())),
        "outbound_plan_reason_counts": dict(Counter(row["outbound_plan_reason"] for row in relevant)),
        "stage2_template_counts": dict(
            sum((Counter(_parse_counter_text(row["stage2_template_counts"])) for row in completed), Counter())
        ),
    }


def _ratio(numerator: int, denominator: int) -> float:
    if not denominator:
        return 1.0
    return round(numerator / denominator, 6)


def _counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in sorted(counter.items()) if key)


def _parse_counter_text(text: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for item in text.split("|"):
        if not item:
            continue
        key, value = item.rsplit(":", 1)
        output[key] = int(value)
    return output


if __name__ == "__main__":
    main()
