#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import physical
from analyze_depot_inbound_stage import (
    _analyze_case as _analyze_inbound_case,
    _initial_depot_inbound_debt,
    _read_operations,
)
from analyze_depot_stage2 import (
    _analyze_case as _analyze_stage2_case,
    _read_selected_steps,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze third-stage depot inbound release after outbound gathering.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "depot_stage3"
    output_dir.mkdir(parents=True, exist_ok=True)

    operations = _read_operations(artifact_dir / "operation_trace.csv")
    selected_steps = _read_selected_steps(artifact_dir / "step_trace.csv")
    inbound_initial = _initial_depot_inbound_debt(truth_dir)
    rows: list[dict[str, Any]] = []
    for path in sorted(truth_dir.glob("*.json")):
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        inbound_case = inbound_initial.get(case_id)
        if not inbound_case:
            continue
        inbound_result = _analyze_inbound_case(
            case_id=case_id,
            case=inbound_case,
            operations=operations.get(case_id, {}),
        )
        stage2_result = _analyze_stage2_case(
            case_id=case_id,
            cars=cars,
            depot_assignment=depot_assignment,
            operations=operations.get(case_id, {}),
            selected_steps=selected_steps.get(case_id, {}),
            inbound_result=inbound_result,
        )
        rows.append(
            _analyze_stage3_case(
                case_id=case_id,
                inbound_case=inbound_case,
                inbound_result=inbound_result,
                stage2_result=stage2_result,
                operations=operations.get(case_id, {}),
                selected_steps=selected_steps.get(case_id, {}),
            )
        )

    summary = _summary(rows)
    physical.write_csv(output_dir / "depot_stage3_cases.csv", rows)
    physical.write_json(output_dir / "depot_stage3_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def _analyze_stage3_case(
    *,
    case_id: str,
    inbound_case: dict[str, Any],
    inbound_result: dict[str, Any],
    stage2_result: dict[str, Any],
    operations: dict[int, list[dict[str, Any]]],
    selected_steps: dict[int, dict[str, str]],
) -> dict[str, Any]:
    debt: dict[str, dict[str, Any]] = inbound_case["debt"]
    debt_nos = set(debt)
    target_by_no = inbound_case["target_by_no"]
    line_by_no = dict(inbound_case["line_by_no"])
    start_hook = _stage3_start_hook(inbound_result=inbound_result, stage2_result=stage2_result)
    completion_hook = 0
    hook_indexes: set[int] = set()
    template_counts: Counter[str] = Counter()

    for hook_index in sorted(operations):
        if start_hook and hook_index >= start_hook:
            hook_indexes.add(hook_index)
            selected = selected_steps.get(hook_index)
            if selected:
                template_counts[selected.get("template_name", "")] += 1
        for op in operations[hook_index]:
            if op["action"] != "Put":
                continue
            for no in op["move_nos"]:
                line_by_no[no] = op["line"]
        if start_hook and hook_index >= start_hook and _all_depot_inbound_reached(
            line_by_no=line_by_no,
            target_by_no=target_by_no,
            debt_nos=debt_nos,
        ):
            completion_hook = hook_index
            break

    reached_nos = {
        no
        for no in debt_nos
        if line_by_no.get(no) == target_by_no.get(no)
    }
    if completion_hook:
        reached_nos = set(debt_nos)
    remaining_nos = tuple(sorted(debt_nos - reached_nos))
    return {
        "case_id": case_id,
        "stage1_complete": int(inbound_result["stage_complete"]),
        "stage1_completion_hook": int(inbound_result["completion_hook"]),
        "stage2_complete": int(stage2_result["stage2_complete"]),
        "stage2_completion_hook": int(stage2_result["outbound_completion_hook"]),
        "outbound_count": int(stage2_result["outbound_count"]),
        "stage3_start_hook": start_hook,
        "depot_inbound_count": len(debt_nos),
        "depot_inbound_reached_target_count": len(reached_nos),
        "stage3_complete": int(bool(debt_nos) and bool(completion_hook)),
        "stage3_completion_hook": completion_hook,
        "stage3_attempt_hook_count": len(hook_indexes),
        "stage3_hook_count": len(hook_indexes) if completion_hook else 0,
        "stage3_template_counts": _counter_text(template_counts),
        "remaining_depot_inbound_nos": "|".join(remaining_nos),
        "remaining_by_source_counts": _counter_text(Counter(debt[no]["source_line"] for no in remaining_nos)),
        "remaining_by_current_line_counts": _counter_text(Counter(line_by_no.get(no, "") for no in remaining_nos)),
        "remaining_by_target_counts": _counter_text(Counter(debt[no]["target_line"] for no in remaining_nos)),
    }


def _stage3_start_hook(*, inbound_result: dict[str, Any], stage2_result: dict[str, Any]) -> int:
    if not inbound_result["stage_complete"]:
        return 0
    outbound_count = int(stage2_result["outbound_count"])
    if outbound_count:
        if not stage2_result["stage2_complete"]:
            return 0
        return int(stage2_result["outbound_completion_hook"]) + 1
    return int(inbound_result["completion_hook"]) + 1


def _all_depot_inbound_reached(
    *,
    line_by_no: dict[str, str],
    target_by_no: dict[str, str],
    debt_nos: set[str],
) -> bool:
    return all(line_by_no.get(no) == target_by_no.get(no) for no in debt_nos)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [row for row in rows if row["stage3_start_hook"]]
    completed = [row for row in ready if row["stage3_complete"]]
    hook_counts = Counter(row["stage3_hook_count"] for row in completed)
    attempt_counts = Counter(row["stage3_attempt_hook_count"] for row in ready)
    failed_attempt_counts = Counter(row["stage3_attempt_hook_count"] for row in ready if not row["stage3_complete"])
    return {
        "case_count": len(rows),
        "stage3_ready_case_count": len(ready),
        "stage3_completed_case_count": len(completed),
        "stage3_completion_rate_on_ready_cases": _ratio(len(completed), len(ready)),
        "stage3_ready_depot_inbound_count": sum(row["depot_inbound_count"] for row in ready),
        "stage3_reached_target_count": sum(row["depot_inbound_reached_target_count"] for row in ready),
        "stage3_reached_target_rate_on_ready_debt": _ratio(
            sum(row["depot_inbound_reached_target_count"] for row in ready),
            sum(row["depot_inbound_count"] for row in ready),
        ),
        "stage3_hook_count_distribution": dict(sorted(hook_counts.items())),
        "stage3_attempt_hook_count_distribution_on_ready_cases": dict(sorted(attempt_counts.items())),
        "stage3_attempt_hook_count_distribution_on_failed_cases": dict(sorted(failed_attempt_counts.items())),
        "stage3_template_counts": dict(
            sum((Counter(_parse_counter_text(row["stage3_template_counts"])) for row in completed), Counter())
        ),
        "remaining_by_source_counts": dict(
            sum((Counter(_parse_counter_text(row["remaining_by_source_counts"])) for row in ready), Counter())
        ),
        "remaining_by_current_line_counts": dict(
            sum((Counter(_parse_counter_text(row["remaining_by_current_line_counts"])) for row in ready), Counter())
        ),
        "remaining_by_target_counts": dict(
            sum((Counter(_parse_counter_text(row["remaining_by_target_counts"])) for row in ready), Counter())
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
