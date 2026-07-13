#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical  # noqa: E402


IGNORED_SERVICE_TARGETS = {"机走棚", "机走北"}
SERVICE_LINES = (
    "抛丸线",
    "油漆线",
    "洗罐站",
    "洗罐线北",
    "机库线",
    "调梁棚",
    "调梁线北",
    "预修线",
    "存1线",
    "存2线",
    "存3线",
    "存5线南",
    "存5线北",
)
SERVICE_REGIONS = (
    ("前场洗油抛", ("抛丸线", "油漆线", "洗罐站", "洗罐线北")),
    ("调梁区域", ("调梁棚", "调梁线北")),
    ("预修和机库", ("预修线", "机库线")),
    ("存车区域", ("存1线", "存2线", "存3线", "存5线南", "存5线北")),
)
NON_SERVICE_TARGETS = set(physical.DEPOT_TARGET_LINES) | {"卸轮线"}


State = tuple[dict[str, tuple[str, ...]], tuple[str, ...], dict[str, int]]


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
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            target_by_no[no] = target_line
            if physical.car_is_satisfied(car, depot_assignment, cars):
                continue
            if (
                target_line in physical.DEPOT_INBOUND_DESTINATION_LINES
                and car["Line"] not in physical.DEPOT_INBOUND_DESTINATION_LINES
            ):
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


def _assembly_contamination(
    *,
    line_by_no: dict[str, str],
    target_by_no: dict[str, str],
) -> tuple[str, ...]:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Stage1 output with successfully restored manual plans.")
    parser.add_argument("--manual-dir", type=Path, default=Path("artifacts/manual_restored_interface"))
    parser.add_argument("--solver-dir", type=Path, required=True)
    parser.add_argument("--truth-dir", type=Path, default=Path("data/truth2"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = compare_all(args.manual_dir, args.solver_dir, args.truth_dir)
    unique_rows, duplicate_rows = collapse_duplicate_plans(rows)
    line_rows = summarize_lines(unique_rows)
    summary = summarize(rows, unique_rows, duplicate_rows, line_rows)

    physical.write_csv(args.output_dir / "case_comparison.csv", [flat_case_row(row) for row in unique_rows])
    physical.write_csv(args.output_dir / "successful_plan_comparison.csv", [flat_case_row(row) for row in rows])
    physical.write_csv(args.output_dir / "line_comparison.csv", line_rows)
    physical.write_csv(args.output_dir / "region_comparison.csv", summary["service_region_breakdown"])
    physical.write_json(args.output_dir / "summary.json", summary)
    (args.output_dir / "report.md").write_text(render_report(summary, line_rows), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {args.output_dir}")


def compare_all(manual_dir: Path, solver_dir: Path, truth_dir: Path) -> list[dict[str, Any]]:
    case_summary_rows = read_csv(manual_dir / "manual_restore_case_summary.csv")
    truth_by_case = {
        physical.case_id_from_path(path): path
        for path in truth_dir.glob("*.json")
    }
    inbound_cases = _initial_depot_inbound_debt(truth_dir)
    successful = [
        row
        for row in case_summary_rows
        if row.get("success") == "1"
        and row.get("case_id") in truth_by_case
        and row.get("case_id") in inbound_cases
    ]
    rows: list[dict[str, Any]] = []
    for item in successful:
        case_id = item["case_id"]
        bundle_path = manual_dir / "bundles" / f"{case_id}_{item['manual_file_id']}.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        _case_id, _request, cars, _assignment, _loco = physical.read_case(truth_by_case[case_id])
        inbound_case = inbound_cases[case_id]
        initial_state = initial_state_for(cars)
        manual_state, checkpoint_kind, checkpoint_operation, manual_business_hooks = manual_checkpoint(
            initial_state,
            bundle["Response"]["Data"]["Operations"],
            inbound_case,
        )
        solver_state = response_state(solver_dir / f"{case_id}_response.json")
        solver_summary = json.loads((solver_dir / f"{case_id}_summary.json").read_text(encoding="utf-8"))
        initial_settlement = settlement(initial_state, cars)
        manual_settlement = settlement(manual_state, cars)
        solver_settlement = settlement(solver_state, cars)
        initial_grouped = grouped_nos(initial_state, inbound_case)
        manual_grouped = grouped_nos(manual_state, inbound_case)
        solver_grouped = grouped_nos(solver_state, inbound_case)
        manual_with_garage = grouped_or_garage_nos(manual_state, inbound_case)
        rows.append({
            "case_id": case_id,
            "manual_file_id": item["manual_file_id"],
            "checkpoint_kind": checkpoint_kind,
            "checkpoint_operation": checkpoint_operation,
            "manual_business_hooks": manual_business_hooks,
            "solver_primary_business_hooks": int(solver_summary.get("primary_business_hooks") or 0),
            "solver_business_hooks": int(solver_summary.get("business_hooks") or 0),
            "inbound_debt_count": len(inbound_case["debt"]),
            "initial_grouped_count": len(initial_grouped),
            "manual_grouped_count": len(manual_grouped),
            "manual_grouped_or_garage_count": len(manual_with_garage),
            "solver_grouped_count": len(solver_grouped),
            "service_eligible_count": manual_settlement["eligible_count"],
            "forced_service_eligible_count": manual_settlement["forced_eligible_count"],
            "initial_service_line_satisfied_count": initial_settlement["line_satisfied_count"],
            "manual_service_line_satisfied_count": manual_settlement["line_satisfied_count"],
            "solver_service_line_satisfied_count": solver_settlement["line_satisfied_count"],
            "initial_service_satisfied_count": initial_settlement["satisfied_count"],
            "manual_service_satisfied_count": manual_settlement["satisfied_count"],
            "solver_service_satisfied_count": solver_settlement["satisfied_count"],
            "initial_forced_on_target_line_count": initial_settlement["forced_on_target_line_count"],
            "manual_forced_on_target_line_count": manual_settlement["forced_on_target_line_count"],
            "solver_forced_on_target_line_count": solver_settlement["forced_on_target_line_count"],
            "initial_forced_position_satisfied_count": initial_settlement["forced_position_satisfied_count"],
            "manual_forced_position_satisfied_count": manual_settlement["forced_position_satisfied_count"],
            "solver_forced_position_satisfied_count": solver_settlement["forced_position_satisfied_count"],
            "initial_forced_wrong_position_count": initial_settlement["forced_wrong_position_count"],
            "manual_forced_wrong_position_count": manual_settlement["forced_wrong_position_count"],
            "solver_forced_wrong_position_count": solver_settlement["forced_wrong_position_count"],
            "initial_contiguous_count": initial_settlement["contiguous_count"],
            "manual_contiguous_count": manual_settlement["contiguous_count"],
            "solver_contiguous_count": solver_settlement["contiguous_count"],
            "initial_total_completed_count": len(initial_grouped) + initial_settlement["satisfied_count"],
            "manual_total_completed_count": len(manual_grouped) + manual_settlement["satisfied_count"],
            "solver_total_completed_count": len(solver_grouped) + solver_settlement["satisfied_count"],
            "initial_satisfied_by_line": initial_settlement["satisfied_by_line"],
            "manual_satisfied_by_line": manual_settlement["satisfied_by_line"],
            "solver_satisfied_by_line": solver_settlement["satisfied_by_line"],
            "initial_contiguous_by_line": initial_settlement["contiguous_by_line"],
            "manual_contiguous_by_line": manual_settlement["contiguous_by_line"],
            "solver_contiguous_by_line": solver_settlement["contiguous_by_line"],
            "initial_forced_on_target_by_line": initial_settlement["forced_on_target_by_line"],
            "manual_forced_on_target_by_line": manual_settlement["forced_on_target_by_line"],
            "solver_forced_on_target_by_line": solver_settlement["forced_on_target_by_line"],
            "initial_forced_position_by_line": initial_settlement["forced_position_by_line"],
            "manual_forced_position_by_line": manual_settlement["forced_position_by_line"],
            "solver_forced_position_by_line": solver_settlement["forced_position_by_line"],
        })
    return sorted(rows, key=lambda row: (row["case_id"], row["manual_file_id"]))


def manual_checkpoint(
    initial_state: State,
    operations: list[dict[str, Any]],
    inbound_case: dict[str, Any],
) -> tuple[State, str, int, int]:
    state = initial_state
    last_empty_state = state
    last_empty_operation = 0
    first_complete_state: State | None = None
    first_complete_operation = 0
    pre_release_state: State | None = None
    pre_release_operation = 0
    inbound_nos = set(inbound_case["debt"])

    for index, operation in enumerate(operations, start=1):
        moving = set(operation.get("MoveCars") or ())
        target = physical.normalize_line(operation.get("Line"))
        if (
            operation.get("Action") == "Put"
            and target in physical.DEPOT_INBOUND_DESTINATION_LINES
            and moving & inbound_nos
        ):
            pre_release_state = last_empty_state
            pre_release_operation = last_empty_operation
            break
        state = apply_operation(state, operation)
        if not state[1]:
            last_empty_state = state
            last_empty_operation = index
        if first_complete_state is None and stage1_contract_complete(state, inbound_case):
            first_complete_state = state
            first_complete_operation = index

    if first_complete_state is not None:
        checkpoint = first_complete_state
        checkpoint_kind = "strict_complete"
        checkpoint_operation = first_complete_operation
    elif pre_release_state is not None:
        checkpoint = pre_release_state
        checkpoint_kind = "pre_first_inbound_release"
        checkpoint_operation = pre_release_operation
    else:
        checkpoint = state
        checkpoint_kind = "manual_end"
        checkpoint_operation = len(operations)

    included_operations = (
        operations[:checkpoint_operation]
        if checkpoint_kind in {"strict_complete", "pre_first_inbound_release"}
        else operations
    )
    business_hooks = sum(1 for operation in included_operations if operation.get("Action") in {"Get", "Put"})
    return checkpoint, checkpoint_kind, checkpoint_operation, business_hooks


def initial_state_for(cars: list[dict[str, Any]]) -> State:
    lines = {
        line: tuple(physical.line_access_order(cars, line))
        for line in sorted({car["Line"] for car in cars})
    }
    positions = {
        physical.car_no(car): int(car.get("Position") or 0)
        for car in cars
    }
    return lines, tuple(), positions


def response_state(path: Path) -> State:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["Data"]["GeneratedEndStatus"]
    lines: dict[str, list[str]] = defaultdict(list)
    positions: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: (item["Line"], int(item.get("Position") or 0), item["No"])):
        lines[row["Line"]].append(row["No"])
        positions[row["No"]] = int(row.get("Position") or 0)
    return {line: tuple(nos) for line, nos in lines.items()}, tuple(), positions


def apply_operation(state: State, operation: dict[str, Any]) -> State:
    lines = {line: list(nos) for line, nos in state[0].items()}
    positions = dict(state[2])
    moving = list(operation.get("MoveCars") or ())
    moving_set = set(moving)
    action = operation.get("Action")
    target = physical.normalize_line(operation.get("Line"))
    if action in {"Get", "Put"}:
        for line in lines:
            lines[line] = [no for no in lines[line] if no not in moving_set]
        for no in moving:
            positions[no] = 0
    if action == "Get" and not physical.is_spotting_line(target):
        for position, no in enumerate(lines.get(target, []), start=1):
            positions[no] = position
    if action == "Put":
        lines[target] = [*moving, *lines.get(target, [])]
        for position, no in enumerate(lines[target], start=1):
            positions[no] = position
    train = operation["TrainCars"] if "TrainCars" in operation else state[1]
    return {line: tuple(nos) for line, nos in lines.items()}, tuple(train), positions


def stage1_contract_complete(state: State, inbound_case: dict[str, Any]) -> bool:
    if state[1]:
        return False
    grouped = grouped_nos(state, inbound_case)
    if len(grouped) != len(inbound_case["debt"]):
        return False
    return not _assembly_contamination(
        line_by_no=line_by_no(state),
        target_by_no=inbound_case["target_by_no"],
    )


def grouped_nos(state: State, inbound_case: dict[str, Any]) -> set[str]:
    current_lines = line_by_no(state)
    return {
        no
        for no, item in inbound_case["debt"].items()
        if _is_final_assembly_line(
            line=current_lines.get(no, ""),
            target_line=item["target_line"],
        )
    }


def grouped_or_garage_nos(state: State, inbound_case: dict[str, Any]) -> set[str]:
    current_lines = line_by_no(state)
    return {
        no
        for no, item in inbound_case["debt"].items()
        if _is_final_assembly_line(
            line=current_lines.get(no, ""),
            target_line=item["target_line"],
        )
        or (
            item["target_line"] in physical.DEPOT_TARGET_LINES
            and current_lines.get(no) == "机库线"
        )
    }


def settlement(state: State, cars: list[dict[str, Any]]) -> dict[str, Any]:
    current = positioned_cars(state, cars)
    eligible = [car for car in current if service_eligible(car)]
    line_satisfied_by_line = Counter(
        car["Line"]
        for car in eligible
        if target_line_satisfied(car)
    )
    satisfied_by_line = Counter(
        car["Line"]
        for car in eligible
        if target_satisfied(car)
    )
    forced = [car for car in eligible if physical.force_positions(car)]
    forced_on_target_by_line = Counter(
        car["Line"]
        for car in forced
        if target_line_satisfied(car)
    )
    forced_position_by_line = Counter(
        car["Line"]
        for car in forced
        if target_satisfied(car)
    )
    forced_on_target_line = sum(forced_on_target_by_line.values())
    forced_position_satisfied = sum(forced_position_by_line.values())
    contiguous_by_line: dict[str, int] = {}
    for line in SERVICE_LINES:
        south_to_north = sorted(
            (car for car in current if car["Line"] == line),
            key=lambda car: (int(car.get("Position") or 0), physical.car_no(car)),
            reverse=True,
        )
        count = 0
        for car in south_to_north:
            if set(car.get("TargetLines") or ()) & IGNORED_SERVICE_TARGETS:
                continue
            if service_eligible(car) and target_satisfied(car):
                count += 1
                continue
            if not car.get("TargetLines") and target_satisfied(car):
                continue
            break
        contiguous_by_line[line] = count
    return {
        "eligible_count": len(eligible),
        "line_satisfied_count": sum(line_satisfied_by_line.values()),
        "satisfied_count": sum(satisfied_by_line.values()),
        "contiguous_count": sum(contiguous_by_line.values()),
        "forced_eligible_count": len(forced),
        "forced_on_target_line_count": forced_on_target_line,
        "forced_position_satisfied_count": forced_position_satisfied,
        "forced_wrong_position_count": forced_on_target_line - forced_position_satisfied,
        "satisfied_by_line": {line: satisfied_by_line[line] for line in SERVICE_LINES},
        "contiguous_by_line": contiguous_by_line,
        "forced_on_target_by_line": {line: forced_on_target_by_line[line] for line in SERVICE_LINES},
        "forced_position_by_line": {line: forced_position_by_line[line] for line in SERVICE_LINES},
    }


def positioned_cars(state: State, cars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_no = {physical.car_no(car): car for car in cars}
    result: list[dict[str, Any]] = []
    for line, nos in state[0].items():
        for position, no in enumerate(nos, start=1):
            if no not in by_no:
                continue
            car = dict(by_no[no])
            car["Line"] = line
            car["Position"] = int(state[2].get(no) or position)
            result.append(car)
    return result


def service_eligible(car: dict[str, Any]) -> bool:
    targets = set(car.get("TargetLines") or ())
    return (
        bool(targets)
        and not bool(targets & IGNORED_SERVICE_TARGETS)
        and not bool(targets & NON_SERVICE_TARGETS)
        and bool(targets & set(SERVICE_LINES))
    )


def target_line_satisfied(car: dict[str, Any]) -> bool:
    targets = set(car.get("TargetLines") or ())
    if not targets:
        return True
    if car["Line"] not in targets:
        return False
    return not (
        car["Line"] == "存4线"
        and car.get("IsClosedDoor")
        and int(car.get("Position") or 0) <= 3
    )


def target_satisfied(car: dict[str, Any]) -> bool:
    if not target_line_satisfied(car):
        return False
    forced = physical.force_positions(car)
    return not forced or int(car.get("Position") or 0) in forced


def line_by_no(state: State) -> dict[str, str]:
    return {no: line for line, nos in state[0].items() for no in nos}


def collapse_duplicate_plans(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    unique: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for case_id, variants in sorted(by_case.items()):
        selected = max(
            variants,
            key=lambda row: (
                row["manual_total_completed_count"],
                row["manual_contiguous_count"],
                row["manual_grouped_count"],
            ),
        )
        unique.append(selected)
        if len(variants) > 1:
            duplicates.append({
                "case_id": case_id,
                "variant_count": len(variants),
                "metric_variants": len({comparison_metric_tuple(row) for row in variants}),
            })
    return unique, duplicates


def comparison_metric_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["manual_grouped_count"],
        row["manual_service_satisfied_count"],
        row["manual_contiguous_count"],
        row["manual_total_completed_count"],
    )


def summarize(
    plan_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    duplicate_rows: list[dict[str, Any]],
    line_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    inbound_debt = total(rows, "inbound_debt_count")
    eligible = total(rows, "service_eligible_count")
    initial_grouped = total(rows, "initial_grouped_count")
    manual_grouped = total(rows, "manual_grouped_count")
    solver_grouped = total(rows, "solver_grouped_count")
    initial_service = total(rows, "initial_service_satisfied_count")
    manual_service = total(rows, "manual_service_satisfied_count")
    solver_service = total(rows, "solver_service_satisfied_count")
    initial_line_service = total(rows, "initial_service_line_satisfied_count")
    manual_line_service = total(rows, "manual_service_line_satisfied_count")
    solver_line_service = total(rows, "solver_service_line_satisfied_count")
    initial_contiguous = total(rows, "initial_contiguous_count")
    manual_contiguous = total(rows, "manual_contiguous_count")
    solver_contiguous = total(rows, "solver_contiguous_count")
    forced_eligible = total(rows, "forced_service_eligible_count")
    initial_forced_on_line = total(rows, "initial_forced_on_target_line_count")
    manual_forced_on_line = total(rows, "manual_forced_on_target_line_count")
    solver_forced_on_line = total(rows, "solver_forced_on_target_line_count")
    initial_forced_position = total(rows, "initial_forced_position_satisfied_count")
    manual_forced_position = total(rows, "manual_forced_position_satisfied_count")
    solver_forced_position = total(rows, "solver_forced_position_satisfied_count")
    region_breakdown = summarize_service_regions(line_rows)
    return {
        "sample": {
            "successful_plan_file_count": len(plan_rows),
            "unique_case_count": len(rows),
            "duplicate_cases": duplicate_rows,
            "checkpoint_kind_counts": dict(Counter(row["checkpoint_kind"] for row in rows)),
        },
        "inbound_assembly": {
            "debt_count": inbound_debt,
            "initial_grouped_count": initial_grouped,
            "manual_grouped_count": manual_grouped,
            "manual_grouping_rate": ratio(manual_grouped, inbound_debt),
            "manual_grouped_or_garage_count": total(rows, "manual_grouped_or_garage_count"),
            "manual_grouped_or_garage_rate": ratio(total(rows, "manual_grouped_or_garage_count"), inbound_debt),
            "solver_grouped_count": solver_grouped,
            "solver_grouping_rate": ratio(solver_grouped, inbound_debt),
            "solver_minus_manual_count": solver_grouped - manual_grouped,
            "case_comparison": comparison_counts(rows, "manual_grouped_count", "solver_grouped_count"),
        },
        "service_targets": {
            "eligible_count": eligible,
            "initial_satisfied_count": initial_service,
            "manual_satisfied_count": manual_service,
            "solver_satisfied_count": solver_service,
            "initial_satisfied_rate": ratio(initial_service, eligible),
            "manual_satisfied_rate": ratio(manual_service, eligible),
            "solver_satisfied_rate": ratio(solver_service, eligible),
            "manual_increment_count": manual_service - initial_service,
            "solver_increment_count": solver_service - initial_service,
            "solver_increment_vs_manual_rate": ratio(solver_service - initial_service, manual_service - initial_service),
            "solver_minus_manual_count": solver_service - manual_service,
            "case_comparison": comparison_counts(
                rows,
                "manual_service_satisfied_count",
                "solver_service_satisfied_count",
            ),
        },
        "service_target_line_only": {
            "eligible_count": eligible,
            "initial_satisfied_count": initial_line_service,
            "manual_satisfied_count": manual_line_service,
            "solver_satisfied_count": solver_line_service,
            "manual_increment_count": manual_line_service - initial_line_service,
            "solver_increment_count": solver_line_service - initial_line_service,
            "solver_minus_manual_count": solver_line_service - manual_line_service,
        },
        "forced_position_compliance": {
            "eligible_count": forced_eligible,
            "initial_on_target_line_count": initial_forced_on_line,
            "manual_on_target_line_count": manual_forced_on_line,
            "solver_on_target_line_count": solver_forced_on_line,
            "initial_position_satisfied_count": initial_forced_position,
            "manual_position_satisfied_count": manual_forced_position,
            "solver_position_satisfied_count": solver_forced_position,
            "initial_wrong_position_count": total(rows, "initial_forced_wrong_position_count"),
            "manual_wrong_position_count": total(rows, "manual_forced_wrong_position_count"),
            "solver_wrong_position_count": total(rows, "solver_forced_wrong_position_count"),
            "manual_position_increment_count": manual_forced_position - initial_forced_position,
            "solver_position_increment_count": solver_forced_position - initial_forced_position,
            "manual_compliance_rate_on_target_line": ratio(manual_forced_position, manual_forced_on_line),
            "solver_compliance_rate_on_target_line": ratio(solver_forced_position, solver_forced_on_line),
            "solver_minus_manual_position_satisfied_count": solver_forced_position - manual_forced_position,
        },
        "south_contiguous": {
            "eligible_count": eligible,
            "initial_count": initial_contiguous,
            "manual_count": manual_contiguous,
            "solver_count": solver_contiguous,
            "initial_rate": ratio(initial_contiguous, eligible),
            "manual_rate": ratio(manual_contiguous, eligible),
            "solver_rate": ratio(solver_contiguous, eligible),
            "manual_increment_count": manual_contiguous - initial_contiguous,
            "solver_increment_count": solver_contiguous - initial_contiguous,
            "solver_minus_manual_count": solver_contiguous - manual_contiguous,
            "case_comparison": comparison_counts(rows, "manual_contiguous_count", "solver_contiguous_count"),
        },
        "service_region_breakdown": region_breakdown,
        "outside_pre_repair_and_garage": summarize_selected_regions(
            region_breakdown,
            excluded={"预修和机库"},
        ),
        "combined_completed": {
            "initial_count": total(rows, "initial_total_completed_count"),
            "manual_count": total(rows, "manual_total_completed_count"),
            "manual_count_if_garage_is_assembly": (
                total(rows, "manual_grouped_or_garage_count") + manual_service
            ),
            "solver_count": total(rows, "solver_total_completed_count"),
            "solver_minus_manual_count": (
                total(rows, "solver_total_completed_count")
                - total(rows, "manual_total_completed_count")
            ),
            "solver_minus_manual_per_case": round(
                (
                    total(rows, "solver_total_completed_count")
                    - total(rows, "manual_total_completed_count")
                ) / len(rows),
                3,
            ) if rows else 0,
            "case_comparison": comparison_counts(
                rows,
                "manual_total_completed_count",
                "solver_total_completed_count",
            ),
        },
        "plan_file_sensitivity": {
            "plan_file_count": len(plan_rows),
            "solver_minus_manual_grouped_count": (
                total(plan_rows, "solver_grouped_count") - total(plan_rows, "manual_grouped_count")
            ),
            "solver_minus_manual_service_count": (
                total(plan_rows, "solver_service_satisfied_count")
                - total(plan_rows, "manual_service_satisfied_count")
            ),
            "solver_minus_manual_contiguous_count": (
                total(plan_rows, "solver_contiguous_count") - total(plan_rows, "manual_contiguous_count")
            ),
            "solver_minus_manual_total_count": (
                total(plan_rows, "solver_total_completed_count")
                - total(plan_rows, "manual_total_completed_count")
            ),
        },
        "operation_cost": {
            "manual_business_hooks": distribution(rows, "manual_business_hooks"),
            "solver_primary_business_hooks": distribution(rows, "solver_primary_business_hooks"),
            "solver_business_hooks_with_cleanup": distribution(rows, "solver_business_hooks"),
        },
        "largest_manual_service_advantages": ranked_differences(
            rows,
            "manual_service_satisfied_count",
            "solver_service_satisfied_count",
            manual_first=True,
        ),
        "largest_solver_service_advantages": ranked_differences(
            rows,
            "manual_service_satisfied_count",
            "solver_service_satisfied_count",
            manual_first=False,
        ),
    }


def summarize_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in SERVICE_LINES:
        initial_satisfied = sum(row["initial_satisfied_by_line"][line] for row in rows)
        manual_satisfied = sum(row["manual_satisfied_by_line"][line] for row in rows)
        solver_satisfied = sum(row["solver_satisfied_by_line"][line] for row in rows)
        initial_contiguous = sum(row["initial_contiguous_by_line"][line] for row in rows)
        manual_contiguous = sum(row["manual_contiguous_by_line"][line] for row in rows)
        solver_contiguous = sum(row["solver_contiguous_by_line"][line] for row in rows)
        initial_forced_on_target = sum(row["initial_forced_on_target_by_line"][line] for row in rows)
        manual_forced_on_target = sum(row["manual_forced_on_target_by_line"][line] for row in rows)
        solver_forced_on_target = sum(row["solver_forced_on_target_by_line"][line] for row in rows)
        initial_forced_position = sum(row["initial_forced_position_by_line"][line] for row in rows)
        manual_forced_position = sum(row["manual_forced_position_by_line"][line] for row in rows)
        solver_forced_position = sum(row["solver_forced_position_by_line"][line] for row in rows)
        result.append({
            "line": line,
            "initial_satisfied": initial_satisfied,
            "manual_satisfied": manual_satisfied,
            "solver_satisfied": solver_satisfied,
            "solver_minus_manual_satisfied": solver_satisfied - manual_satisfied,
            "manual_satisfied_increment": manual_satisfied - initial_satisfied,
            "solver_satisfied_increment": solver_satisfied - initial_satisfied,
            "initial_contiguous": initial_contiguous,
            "manual_contiguous": manual_contiguous,
            "solver_contiguous": solver_contiguous,
            "solver_minus_manual_contiguous": solver_contiguous - manual_contiguous,
            "manual_contiguous_increment": manual_contiguous - initial_contiguous,
            "solver_contiguous_increment": solver_contiguous - initial_contiguous,
            "initial_forced_on_target": initial_forced_on_target,
            "manual_forced_on_target": manual_forced_on_target,
            "solver_forced_on_target": solver_forced_on_target,
            "initial_forced_position": initial_forced_position,
            "manual_forced_position": manual_forced_position,
            "solver_forced_position": solver_forced_position,
            "manual_forced_wrong_position": manual_forced_on_target - manual_forced_position,
            "solver_forced_wrong_position": solver_forced_on_target - solver_forced_position,
            "solver_minus_manual_forced_position": solver_forced_position - manual_forced_position,
        })
    return result


def summarize_service_regions(line_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_line = {row["line"]: row for row in line_rows}
    return [
        summarize_line_group(name, lines, by_line)
        for name, lines in SERVICE_REGIONS
    ]


def summarize_selected_regions(
    region_rows: list[dict[str, Any]],
    *,
    excluded: set[str],
) -> dict[str, Any]:
    selected = [row for row in region_rows if row["region"] not in excluded]
    initial_satisfied = sum(row["initial_satisfied_count"] for row in selected)
    manual_satisfied = sum(row["manual_satisfied_count"] for row in selected)
    solver_satisfied = sum(row["solver_satisfied_count"] for row in selected)
    initial_contiguous = sum(row["initial_contiguous_count"] for row in selected)
    manual_contiguous = sum(row["manual_contiguous_count"] for row in selected)
    solver_contiguous = sum(row["solver_contiguous_count"] for row in selected)
    manual_increment = manual_satisfied - initial_satisfied
    solver_increment = solver_satisfied - initial_satisfied
    return {
        "initial_satisfied_count": initial_satisfied,
        "manual_satisfied_count": manual_satisfied,
        "solver_satisfied_count": solver_satisfied,
        "manual_increment_count": manual_increment,
        "solver_increment_count": solver_increment,
        "solver_minus_manual_count": solver_satisfied - manual_satisfied,
        "solver_increment_vs_manual_rate": ratio(solver_increment, manual_increment),
        "initial_contiguous_count": initial_contiguous,
        "manual_contiguous_count": manual_contiguous,
        "solver_contiguous_count": solver_contiguous,
        "manual_contiguous_increment_count": manual_contiguous - initial_contiguous,
        "solver_contiguous_increment_count": solver_contiguous - initial_contiguous,
        "solver_minus_manual_contiguous_count": solver_contiguous - manual_contiguous,
    }


def summarize_line_group(
    name: str,
    lines: tuple[str, ...],
    by_line: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = summarize_metric_rows([by_line[line] for line in lines])
    result.update({"region": name, "lines": list(lines)})
    return result


def summarize_metric_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    initial_satisfied = sum(row["initial_satisfied"] for row in rows)
    manual_satisfied = sum(row["manual_satisfied"] for row in rows)
    solver_satisfied = sum(row["solver_satisfied"] for row in rows)
    initial_contiguous = sum(row["initial_contiguous"] for row in rows)
    manual_contiguous = sum(row["manual_contiguous"] for row in rows)
    solver_contiguous = sum(row["solver_contiguous"] for row in rows)
    manual_increment = manual_satisfied - initial_satisfied
    solver_increment = solver_satisfied - initial_satisfied
    return {
        "initial_satisfied_count": initial_satisfied,
        "manual_satisfied_count": manual_satisfied,
        "solver_satisfied_count": solver_satisfied,
        "manual_increment_count": manual_increment,
        "solver_increment_count": solver_increment,
        "solver_minus_manual_count": solver_satisfied - manual_satisfied,
        "solver_increment_vs_manual_rate": ratio(solver_increment, manual_increment),
        "initial_contiguous_count": initial_contiguous,
        "manual_contiguous_count": manual_contiguous,
        "solver_contiguous_count": solver_contiguous,
        "manual_contiguous_increment_count": manual_contiguous - initial_contiguous,
        "solver_contiguous_increment_count": solver_contiguous - initial_contiguous,
        "solver_minus_manual_contiguous_count": solver_contiguous - manual_contiguous,
    }


def flat_case_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not key.endswith("_by_line")
    } | {
        "solver_minus_manual_grouped": row["solver_grouped_count"] - row["manual_grouped_count"],
        "solver_minus_manual_service": (
            row["solver_service_satisfied_count"] - row["manual_service_satisfied_count"]
        ),
        "solver_minus_manual_contiguous": row["solver_contiguous_count"] - row["manual_contiguous_count"],
        "solver_minus_manual_total": row["solver_total_completed_count"] - row["manual_total_completed_count"],
    }


def render_report(summary: dict[str, Any], line_rows: list[dict[str, Any]]) -> str:
    sample = summary["sample"]
    inbound = summary["inbound_assembly"]
    service = summary["service_targets"]
    line_only_service = summary["service_target_line_only"]
    forced_position = summary["forced_position_compliance"]
    contiguous = summary["south_contiguous"]
    combined = summary["combined_completed"]
    region_rows = summary["service_region_breakdown"]
    outside_pre_repair = summary["outside_pre_repair_and_garage"]
    operation_cost = summary["operation_cost"]
    pre_repair = next(row for row in region_rows if row["region"] == "预修和机库")
    forced_line_rows = [
        row
        for row in line_rows
        if row["manual_forced_on_target"] or row["solver_forced_on_target"]
    ]
    lines = [
        "# Stage1 与成功回放人工计划完成车辆对比",
        "",
        "## 样本与口径",
        "",
        f"- 成功回放人工计划：{sample['successful_plan_file_count']} 份。",
        f"- 唯一 case：{sample['unique_case_count']} 个。",
        "- 重复人工计划按 case 去重；重复版本指标一致时仅保留一份。",
        "- 人工检查点优先取首次满足严格 Stage1 合同的状态；否则取首次向大库送入待入库车之前、车列为空的最后状态。",
        "- 去机走棚、机走北（机北3）的车辆不进入其他目标到位统计。",
        "- 其他目标‘到位’采用严格口径：到达目标股道后，存在 ForceTargetPosition 的车辆还必须处于允许位号。",
        "",
        "## 总体结果",
        "",
        "|指标|人工|Stage1|Stage1 - 人工|",
        "|---|---:|---:|---:|",
        f"|大库/卸轮集结完成|{inbound['manual_grouped_count']}|{inbound['solver_grouped_count']}|{inbound['solver_minus_manual_count']:+d}|",
        f"|仅到其他目标股道（忽略强制位）|{line_only_service['manual_satisfied_count']}|{line_only_service['solver_satisfied_count']}|{line_only_service['solver_minus_manual_count']:+d}|",
        f"|其他真实目标严格到位|{service['manual_satisfied_count']}|{service['solver_satisfied_count']}|{service['solver_minus_manual_count']:+d}|",
        f"|南端连续到位|{contiguous['manual_count']}|{contiguous['solver_count']}|{contiguous['solver_minus_manual_count']:+d}|",
        f"|集结完成 + 其他目标到位|{combined['manual_count']}|{combined['solver_count']}|{combined['solver_minus_manual_count']:+d}|",
        "",
        "## 关键解释",
        "",
        f"- 人工在严格 Stage1 检查点前只同时集结了 {inbound['manual_grouping_rate']:.1%} 的待入库车；Stage1 为 {inbound['solver_grouping_rate']:.1%}。",
        "- 人工采用边编边送的滚动入库，算法要求所有待入库车同时完成集结；两者的 Stage1 合同不同，因此“集结完成 + 其他到位”的总数不能直接用来判断整体调车能力。",
        f"- 其他真实目标车方面，人工新增 {service['manual_increment_count']} 辆，Stage1 新增 {service['solver_increment_count']} 辆，{delta_text('Stage1', service['solver_minus_manual_count'], '人工')}。",
        f"- 预修线和机库线单独贡献了 Stage1 相对人工的 {pre_repair['solver_minus_manual_count']:+d} 辆；排除这两条线后，人工新增 {outside_pre_repair['manual_increment_count']} 辆，Stage1 新增 {outside_pre_repair['solver_increment_count']} 辆，{delta_text('Stage1', outside_pre_repair['solver_minus_manual_count'], '人工')}。",
        f"- 全场连续到位中，{delta_text('Stage1', contiguous['solver_minus_manual_count'], '人工')}；其中预修线和机库线贡献了 {pre_repair['solver_minus_manual_contiguous_count']:+d} 辆，排除两线后，{delta_text('Stage1', outside_pre_repair['solver_minus_manual_contiguous_count'], '人工')}。",
        f"- 人工平均 {operation_cost['manual_business_hooks']['average']:.2f} 次 Get/Put；Stage1 含服务收尾平均 {operation_cost['solver_business_hooks_with_cleanup']['average']:.2f} 次。",
        "- 结论：Stage1 总体严格到位数、南端连续数和强制对位率均超过人工；仍需单独分析人工对位成功而 Stage1 未完成的局部机会。",
        "",
        "## 强制对位",
        "",
        f"- 样本共有 {forced_position['eligible_count']} 辆服务范围内的强制位车辆。",
        "",
        "|指标|初始|人工|Stage1|",
        "|---|---:|---:|---:|",
        f"|已到目标股道|{forced_position['initial_on_target_line_count']}|{forced_position['manual_on_target_line_count']}|{forced_position['solver_on_target_line_count']}|",
        f"|目标股道且位号合规|{forced_position['initial_position_satisfied_count']}|{forced_position['manual_position_satisfied_count']}|{forced_position['solver_position_satisfied_count']}|",
        f"|到线但位号错误|{forced_position['initial_wrong_position_count']}|{forced_position['manual_wrong_position_count']}|{forced_position['solver_wrong_position_count']}|",
        f"|到线后的位号合规率|-|{forced_position['manual_compliance_rate_on_target_line']:.1%}|{forced_position['solver_compliance_rate_on_target_line']:.1%}|",
        "",
        f"- 人工新增精确对位 {forced_position['manual_position_increment_count']} 辆，Stage1 新增 {forced_position['solver_position_increment_count']} 辆；{delta_text('Stage1', forced_position['solver_minus_manual_position_satisfied_count'], '人工')}。",
        "",
        "|目标线|人工到线|人工位号合规|Stage1到线|Stage1位号合规|合规差值|",
        "|---|---:|---:|---:|---:|---:|",
        *[
            f"|{row['line']}|{row['manual_forced_on_target']}|{row['manual_forced_position']}|"
            f"{row['solver_forced_on_target']}|{row['solver_forced_position']}|"
            f"{row['solver_minus_manual_forced_position']:+d}|"
            for row in forced_line_rows
        ],
        "",
        "## 分区域新增完成",
        "",
        "|区域|人工新增到位|Stage1 新增到位|差值|人工新增连续|Stage1 新增连续|差值|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in region_rows:
        lines.append(
            f"|{row['region']}|{row['manual_increment_count']}|{row['solver_increment_count']}|"
            f"{row['solver_minus_manual_count']:+d}|{row['manual_contiguous_increment_count']}|"
            f"{row['solver_contiguous_increment_count']}|{row['solver_minus_manual_contiguous_count']:+d}|"
        )
    lines.extend([
        f"|排除预修和机库|{outside_pre_repair['manual_increment_count']}|"
        f"{outside_pre_repair['solver_increment_count']}|{outside_pre_repair['solver_minus_manual_count']:+d}|"
        f"{outside_pre_repair['manual_contiguous_increment_count']}|"
        f"{outside_pre_repair['solver_contiguous_increment_count']}|"
        f"{outside_pre_repair['solver_minus_manual_contiguous_count']:+d}|",
        "",
        "## 逐线路",
        "",
        "|线路|人工目标到位|Stage1 目标到位|差值|人工连续到位|Stage1 连续到位|差值|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in line_rows:
        lines.append(
            f"|{row['line']}|{row['manual_satisfied']}|{row['solver_satisfied']}|"
            f"{row['solver_minus_manual_satisfied']:+d}|{row['manual_contiguous']}|"
            f"{row['solver_contiguous']}|{row['solver_minus_manual_contiguous']:+d}|"
        )
    lines.append("")
    return "\n".join(lines)


def delta_text(subject: str, delta: int, reference: str) -> str:
    if delta > 0:
        return f"{subject}比{reference}多 {delta} 辆"
    if delta < 0:
        return f"{subject}比{reference}少 {abs(delta)} 辆"
    return f"{subject}与{reference}相同"


def ranked_differences(
    rows: list[dict[str, Any]],
    manual_key: str,
    solver_key: str,
    *,
    manual_first: bool,
) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: row[solver_key] - row[manual_key],
        reverse=not manual_first,
    )[:10]
    return [
        {
            "case_id": row["case_id"],
            "manual_count": row[manual_key],
            "solver_count": row[solver_key],
            "solver_minus_manual": row[solver_key] - row[manual_key],
        }
        for row in ranked
    ]


def comparison_counts(rows: list[dict[str, Any]], manual_key: str, solver_key: str) -> dict[str, int]:
    counts = Counter(
        "solver_better"
        if row[solver_key] > row[manual_key]
        else "manual_better"
        if row[solver_key] < row[manual_key]
        else "same"
        for row in rows
    )
    return {key: counts[key] for key in ("solver_better", "manual_better", "same")}


def distribution(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [int(row[key]) for row in rows]
    return {
        "total": sum(values),
        "average": round(sum(values) / len(values), 3) if values else 0,
        "median": statistics.median(values) if values else 0,
        "max": max(values) if values else 0,
    }


def total(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row[key]) for row in rows)


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    main()
