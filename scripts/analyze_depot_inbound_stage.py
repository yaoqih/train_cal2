#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import physical


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze first-stage depot inbound assembly from a vNext artifact.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "depot_inbound_stage"
    output_dir.mkdir(parents=True, exist_ok=True)

    initial = _initial_depot_inbound_debt(truth_dir)
    operations = _read_operations(artifact_dir / "operation_trace.csv")
    case_rows: list[dict[str, Any]] = []
    line_distribution: Counter[str] = Counter()
    source_distribution: Counter[str] = Counter()
    target_distribution: Counter[str] = Counter()
    ungrouped_source_distribution: Counter[str] = Counter()
    ungrouped_target_distribution: Counter[str] = Counter()
    hook_size_distribution: Counter[int] = Counter()
    effective_hook_size_distribution: Counter[int] = Counter()
    contamination_line_distribution: Counter[str] = Counter()

    for case_id, case in sorted(initial.items()):
        result = _analyze_case(case_id=case_id, case=case, operations=operations.get(case_id, {}))
        case_rows.append(result)
        line_distribution.update(result["grouped_by_line_counts"])
        source_distribution.update(result["grouped_by_source_counts"])
        target_distribution.update(result["grouped_by_target_counts"])
        ungrouped_source_distribution.update(result["ungrouped_by_source_counts"])
        ungrouped_target_distribution.update(result["ungrouped_by_target_counts"])
        hook_size_distribution.update(result["assembly_hook_size_counts"])
        effective_hook_size_distribution.update(result["effective_assembly_hook_size_counts"])
        contamination_line_distribution.update(result["contamination_by_line_counts"])

    summary = _summary(
        case_rows=case_rows,
        line_distribution=line_distribution,
        source_distribution=source_distribution,
        target_distribution=target_distribution,
        ungrouped_source_distribution=ungrouped_source_distribution,
        ungrouped_target_distribution=ungrouped_target_distribution,
        hook_size_distribution=hook_size_distribution,
        effective_hook_size_distribution=effective_hook_size_distribution,
        contamination_line_distribution=contamination_line_distribution,
    )
    physical.write_csv(output_dir / "depot_inbound_stage_cases.csv", [_flat_case_row(row) for row in case_rows])
    physical.write_json(output_dir / "depot_inbound_stage_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def _initial_depot_inbound_debt(truth_dir: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for path in sorted(truth_dir.glob("*.json")):
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        loads = physical.line_loads(cars)
        debt: dict[str, dict[str, Any]] = {}
        line_by_no: dict[str, str] = {}
        target_by_no: dict[str, str] = {}
        for car in cars:
            no = physical.car_no(car)
            line_by_no[no] = car["Line"]
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            target_by_no[no] = target_line
            if not physical.car_is_satisfied(car, depot_assignment, cars):
                if target_line in physical.DEPOT_INBOUND_DESTINATION_LINES and car["Line"] not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                    debt[no] = {
                        "source_line": car["Line"],
                        "target_line": target_line,
                        "initial_grouped": _is_final_assembly_line(
                            line=car["Line"],
                            target_line=target_line,
                        ),
                    }
        if debt:
            cases[case_id] = {
                "debt": debt,
                "line_by_no": line_by_no,
                "target_by_no": target_by_no,
            }
    return cases


def _read_operations(path: Path) -> dict[str, dict[int, list[dict[str, Any]]]]:
    by_case: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            row["hook_index"] = int(row["hook_index"])
            row["move_nos"] = tuple(no for no in row["move_cars"].split("|") if no)
            by_case[row["case_id"]][row["hook_index"]].append(row)
    return {case_id: dict(hooks) for case_id, hooks in by_case.items()}


def _analyze_case(
    *,
    case_id: str,
    case: dict[str, Any],
    operations: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    debt: dict[str, dict[str, Any]] = case["debt"]
    line_by_no = dict(case["line_by_no"])
    target_by_no = case["target_by_no"]
    debt_nos = set(debt)
    stage_hook_count = 0
    stage_operation_count = 0
    assembly_hook_count = 0
    effective_assembly_hook_count = 0
    assembly_car_put_count = 0
    effective_assembly_car_put_count = 0
    polluting_assembly_hook_count = 0
    polluting_assembly_put_count = 0
    assembly_hook_size_counts: Counter[int] = Counter()
    effective_assembly_hook_size_counts: Counter[int] = Counter()
    assembly_hook_nos_by_hook: dict[int, set[str]] = {}
    release_hook = 0
    cun4_release_hook = 0
    first_completion_hook = 0
    accepted_line_by_no: dict[str, str] | None = None

    for hook_index in sorted(operations):
        hook_ops = operations[hook_index]
        if _is_depot_inbound_release_hook(hook_ops):
            release_hook = hook_index
            if _is_cun4_depot_release_hook(hook_ops):
                cun4_release_hook = hook_index
            break
        if first_completion_hook:
            continue
        stage_hook_count += 1
        stage_operation_count += sum(1 for op in hook_ops if op["action"] in {"Get", "Put"})
        put_grouped_nos: set[str] = set()
        put_contamination_nos: set[str] = set()
        for op in hook_ops:
            if op["action"] != "Put":
                continue
            for no in op["move_nos"]:
                line_by_no[no] = op["line"]
                if no in debt_nos and _is_final_assembly_line(
                    line=op["line"],
                    target_line=target_by_no.get(no, ""),
                ):
                    put_grouped_nos.add(no)
                elif (
                    op["line"] in physical.DEPOT_INBOUND_ASSEMBLY_LINES
                    and _is_assembly_contamination_line(
                        line=op["line"],
                        target_line=target_by_no.get(no, ""),
                    )
                ):
                    put_contamination_nos.add(no)
        if put_grouped_nos:
            assembly_hook_count += 1
            assembly_car_put_count += len(put_grouped_nos)
            assembly_hook_size_counts[len(put_grouped_nos)] += 1
            assembly_hook_nos_by_hook[hook_index] = set(put_grouped_nos)
        if put_contamination_nos:
            polluting_assembly_hook_count += 1
            polluting_assembly_put_count += len(put_contamination_nos)
        if not first_completion_hook:
            grouped_now = {
                no
                for no in debt_nos
                if _is_final_assembly_line(
                    line=line_by_no.get(no, ""),
                    target_line=target_by_no.get(no, ""),
                )
            }
            contamination_now = _assembly_contamination(line_by_no=line_by_no, target_by_no=target_by_no)
            if grouped_now == debt_nos and not contamination_now:
                first_completion_hook = hook_index
                accepted_line_by_no = dict(line_by_no)

    checkpoint_line_by_no = accepted_line_by_no if accepted_line_by_no is not None else line_by_no
    grouped_at_checkpoint = {
        no
        for no in debt_nos
        if _is_final_assembly_line(
            line=checkpoint_line_by_no.get(no, ""),
            target_line=target_by_no.get(no, ""),
        )
    }
    contamination_at_checkpoint = _assembly_contamination(line_by_no=checkpoint_line_by_no, target_by_no=target_by_no)
    stage_complete_at_checkpoint = bool(first_completion_hook) or (
        grouped_at_checkpoint == debt_nos and not contamination_at_checkpoint
    )
    for hook_index, put_nos in assembly_hook_nos_by_hook.items():
        effective_nos = put_nos & grouped_at_checkpoint
        if not effective_nos:
            continue
        effective_assembly_hook_count += 1
        effective_assembly_car_put_count += len(effective_nos)
        effective_assembly_hook_size_counts[len(effective_nos)] += 1
    grouped_basis = grouped_at_checkpoint
    grouped_by_line = Counter(checkpoint_line_by_no[no] for no in grouped_basis)
    grouped_by_source = Counter(debt[no]["source_line"] for no in grouped_basis)
    grouped_by_target = Counter(debt[no]["target_line"] for no in grouped_basis)
    ungrouped_nos = tuple(sorted(debt_nos - grouped_at_checkpoint))
    ungrouped_by_source = Counter(debt[no]["source_line"] for no in ungrouped_nos)
    ungrouped_by_target = Counter(debt[no]["target_line"] for no in ungrouped_nos)
    contamination_by_line = Counter(checkpoint_line_by_no[no] for no in contamination_at_checkpoint)
    initial_grouped_count = sum(1 for item in debt.values() if item["initial_grouped"])
    grouped_count = len(grouped_at_checkpoint)
    debt_count = len(debt_nos)
    return {
        "case_id": case_id,
        "debt_count": debt_count,
        "initial_grouped_count": initial_grouped_count,
        "grouped_at_checkpoint_count": grouped_count,
        "ungrouped_at_checkpoint_count": debt_count - grouped_count,
        "grouping_rate": grouped_count / debt_count if debt_count else 1.0,
        "stage_complete": int(stage_complete_at_checkpoint),
        "first_completion_hook": first_completion_hook,
        "completion_hook": first_completion_hook if stage_complete_at_checkpoint else 0,
        "complete_then_contaminated": int(bool(first_completion_hook and not stage_complete_at_checkpoint)),
        "stage_hook_count": stage_hook_count,
        "stage_operation_count": stage_operation_count,
        "assembly_hook_count": assembly_hook_count,
        "effective_assembly_hook_count": effective_assembly_hook_count,
        "assembly_car_put_count": assembly_car_put_count,
        "effective_assembly_car_put_count": effective_assembly_car_put_count,
        "polluting_assembly_hook_count": polluting_assembly_hook_count,
        "polluting_assembly_put_count": polluting_assembly_put_count,
        "release_hook": release_hook,
        "cun4_release_hook": cun4_release_hook,
        "release_before_complete": int(bool(release_hook and not stage_complete_at_checkpoint)),
        "contamination_count": len(contamination_at_checkpoint),
        "contamination_nos": contamination_at_checkpoint,
        "ungrouped_nos": ungrouped_nos,
        "grouped_by_line_counts": grouped_by_line,
        "grouped_by_source_counts": grouped_by_source,
        "grouped_by_target_counts": grouped_by_target,
        "ungrouped_by_source_counts": ungrouped_by_source,
        "ungrouped_by_target_counts": ungrouped_by_target,
        "contamination_by_line_counts": contamination_by_line,
        "assembly_hook_size_counts": assembly_hook_size_counts,
        "effective_assembly_hook_size_counts": effective_assembly_hook_size_counts,
    }


def _is_depot_inbound_release_hook(hook_ops: list[dict[str, Any]]) -> bool:
    has_assembly_get = any(
        op["action"] == "Get" and op["line"] in physical.DEPOT_INBOUND_ASSEMBLY_LINES
        for op in hook_ops
    )
    has_destination_put = any(
        op["action"] == "Put" and op["line"] in physical.DEPOT_INBOUND_DESTINATION_LINES
        for op in hook_ops
    )
    return has_assembly_get and has_destination_put


def _is_cun4_depot_release_hook(hook_ops: list[dict[str, Any]]) -> bool:
    has_cun4_get = any(op["action"] == "Get" and op["line"] == "存4线" for op in hook_ops)
    has_destination_put = any(
        op["action"] == "Put" and op["line"] in physical.DEPOT_INBOUND_DESTINATION_LINES
        for op in hook_ops
    )
    return has_cun4_get and has_destination_put


def _assembly_contamination(*, line_by_no: dict[str, str], target_by_no: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            no
            for no, line in line_by_no.items()
            if line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
            and _is_assembly_contamination_line(
                line=line,
                target_line=target_by_no.get(no, ""),
            )
        )
    )


def _is_final_assembly_line(*, line: str, target_line: str) -> bool:
    if target_line == "卸轮线":
        return line == "存4线"
    if target_line in physical.DEPOT_TARGET_LINES:
        return line in physical.DEPOT_INBOUND_ASSEMBLY_LINES and line != "存4线"
    return False


def _is_assembly_contamination_line(*, line: str, target_line: str) -> bool:
    if line == "存4线":
        return target_line != "卸轮线"
    return target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES


def _summary(
    *,
    case_rows: list[dict[str, Any]],
    line_distribution: Counter[str],
    source_distribution: Counter[str],
    target_distribution: Counter[str],
    ungrouped_source_distribution: Counter[str],
    ungrouped_target_distribution: Counter[str],
    hook_size_distribution: Counter[int],
    effective_hook_size_distribution: Counter[int],
    contamination_line_distribution: Counter[str],
) -> dict[str, Any]:
    total_debt = sum(row["debt_count"] for row in case_rows)
    total_initial_grouped = sum(row["initial_grouped_count"] for row in case_rows)
    total_grouped = sum(row["grouped_at_checkpoint_count"] for row in case_rows)
    total_assembly_hooks = sum(row["assembly_hook_count"] for row in case_rows)
    total_effective_assembly_hooks = sum(row["effective_assembly_hook_count"] for row in case_rows)
    total_new_grouped = max(0, total_grouped - total_initial_grouped)
    return {
        "case_count": len(case_rows),
        "debt_count": total_debt,
        "initial_grouped_count": total_initial_grouped,
        "initial_grouping_rate": round(total_initial_grouped / total_debt, 6) if total_debt else 1.0,
        "grouped_at_checkpoint_count": total_grouped,
        "ungrouped_at_checkpoint_count": total_debt - total_grouped,
        "grouping_rate": round(total_grouped / total_debt, 6) if total_debt else 1.0,
        "stage_complete_case_count": sum(row["stage_complete"] for row in case_rows),
        "stage_complete_rate": round(sum(row["stage_complete"] for row in case_rows) / len(case_rows), 6) if case_rows else 1.0,
        "release_before_complete_case_count": sum(row["release_before_complete"] for row in case_rows),
        "complete_then_contaminated_case_count": sum(row["complete_then_contaminated"] for row in case_rows),
        "stage_hook_count": sum(row["stage_hook_count"] for row in case_rows),
        "stage_operation_count": sum(row["stage_operation_count"] for row in case_rows),
        "assembly_hook_count": total_assembly_hooks,
        "effective_assembly_hook_count": total_effective_assembly_hooks,
        "assembly_car_put_count": sum(row["assembly_car_put_count"] for row in case_rows),
        "effective_assembly_car_put_count": sum(row["effective_assembly_car_put_count"] for row in case_rows),
        "new_grouped_per_assembly_hook": round(total_new_grouped / total_assembly_hooks, 6) if total_assembly_hooks else 0.0,
        "new_grouped_per_effective_assembly_hook": round(total_new_grouped / total_effective_assembly_hooks, 6) if total_effective_assembly_hooks else 0.0,
        "polluting_assembly_hook_count": sum(row["polluting_assembly_hook_count"] for row in case_rows),
        "polluting_assembly_put_count": sum(row["polluting_assembly_put_count"] for row in case_rows),
        "contaminated_case_count": sum(1 for row in case_rows if row["contamination_count"]),
        "contamination_count": sum(row["contamination_count"] for row in case_rows),
        "grouped_by_line_counts": dict(line_distribution),
        "grouped_by_source_counts": dict(source_distribution),
        "grouped_by_target_counts": dict(target_distribution),
        "ungrouped_by_source_counts": dict(ungrouped_source_distribution),
        "ungrouped_by_target_counts": dict(ungrouped_target_distribution),
        "contamination_by_line_counts": dict(contamination_line_distribution),
        "assembly_hook_size_counts": {str(size): count for size, count in sorted(hook_size_distribution.items())},
        "effective_assembly_hook_size_counts": {str(size): count for size, count in sorted(effective_hook_size_distribution.items())},
    }


def _flat_case_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "debt_count": row["debt_count"],
        "initial_grouped_count": row["initial_grouped_count"],
        "grouped_at_checkpoint_count": row["grouped_at_checkpoint_count"],
        "ungrouped_at_checkpoint_count": row["ungrouped_at_checkpoint_count"],
        "grouping_rate": f"{row['grouping_rate']:.6f}",
        "stage_complete": row["stage_complete"],
        "first_completion_hook": row["first_completion_hook"],
        "completion_hook": row["completion_hook"],
        "complete_then_contaminated": row["complete_then_contaminated"],
        "stage_hook_count": row["stage_hook_count"],
        "stage_operation_count": row["stage_operation_count"],
        "assembly_hook_count": row["assembly_hook_count"],
        "effective_assembly_hook_count": row["effective_assembly_hook_count"],
        "assembly_car_put_count": row["assembly_car_put_count"],
        "effective_assembly_car_put_count": row["effective_assembly_car_put_count"],
        "polluting_assembly_hook_count": row["polluting_assembly_hook_count"],
        "polluting_assembly_put_count": row["polluting_assembly_put_count"],
        "release_hook": row["release_hook"],
        "cun4_release_hook": row["cun4_release_hook"],
        "release_before_complete": row["release_before_complete"],
        "contamination_count": row["contamination_count"],
        "ungrouped_nos": ",".join(row["ungrouped_nos"]),
        "contamination_nos": ",".join(row["contamination_nos"]),
        "grouped_by_line_counts": _counter_text(row["grouped_by_line_counts"]),
        "grouped_by_source_counts": _counter_text(row["grouped_by_source_counts"]),
        "grouped_by_target_counts": _counter_text(row["grouped_by_target_counts"]),
        "ungrouped_by_source_counts": _counter_text(row["ungrouped_by_source_counts"]),
        "ungrouped_by_target_counts": _counter_text(row["ungrouped_by_target_counts"]),
        "contamination_by_line_counts": _counter_text(row["contamination_by_line_counts"]),
        "assembly_hook_size_counts": _counter_text(row["assembly_hook_size_counts"]),
        "effective_assembly_hook_size_counts": _counter_text(row["effective_assembly_hook_size_counts"]),
    }


def _counter_text(counter: Counter[Any]) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common())


if __name__ == "__main__":
    main()
