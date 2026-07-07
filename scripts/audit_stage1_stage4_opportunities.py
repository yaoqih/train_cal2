#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
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
from stage1_simple.solve import (
    ASSEMBLY_ALL,
    CACHE_LINES,
    STAGE4_IGNORED_TARGET_LINES,
    Stage1Solver,
)


STORAGE_CACHE_LINES = {"存1线", "存2线", "存3线", "存5线北", "存5线南"}
DIRECT_FUNCTION_TARGETS = {"机库线", "预修线", "油漆线", "洗罐站", "抛丸线"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Stage1 opportunities that can reduce Stage4 pressure.")
    parser.add_argument("--stage1-dir", required=True, help="directory containing *_response.json from stage1_simple")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="", help="default: <stage1-dir>/stage1_stage4_opportunities")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    stage1_dir = Path(args.stage1_dir)
    truth_by_case = truth_case_map(Path(args.truth_dir))
    output_dir = Path(args.output_dir) if args.output_dir else stage1_dir / "stage1_stage4_opportunities"
    output_dir.mkdir(parents=True, exist_ok=True)

    response_paths = sorted(stage1_dir.glob("*_response.json"))
    if args.limit:
        response_paths = response_paths[: args.limit]

    missed_rows: list[dict[str, Any]] = []
    moved_satisfied_rows: list[dict[str, Any]] = []
    final_debt_rows: list[dict[str, Any]] = []
    final_run_rows: list[dict[str, Any]] = []
    for response_path in response_paths:
        case_id = case_id_from_path(response_path)
        truth_path = truth_by_case.get(case_id)
        if not truth_path:
            continue
        request = read_json(truth_path)
        response = read_json(response_path)
        operations = response.get("Data", {}).get("Operations") or []
        missed, moved = audit_operations(case_id, truth_path, request, operations)
        missed_rows.extend(missed)
        moved_satisfied_rows.extend(moved)
        final_debt_rows.extend(final_stage4_debt_rows(case_id, response))
        final_run_rows.extend(final_stage4_debt_run_rows(case_id, response))

    summary = {
        "missed_direct_put": missed_direct_summary(missed_rows),
        "moved_satisfied_stage4": moved_satisfied_summary(moved_satisfied_rows),
        "final_stage4_debt_location": final_debt_summary(final_debt_rows),
        "target_fragmentation": target_fragmentation_summary(final_debt_rows),
        "debt_run_fragmentation": debt_run_fragmentation_summary(final_run_rows),
    }
    write_csv(output_dir / "missed_direct_put_rows.csv", missed_rows)
    write_csv(output_dir / "moved_satisfied_stage4_rows.csv", moved_satisfied_rows)
    write_csv(output_dir / "final_stage4_debt_location_rows.csv", final_debt_rows)
    write_csv(output_dir / "stage4_debt_run_rows.csv", final_run_rows)
    write_json(output_dir / "stage1_stage4_opportunity_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def audit_operations(
    case_id: str,
    truth_path: Path,
    request: dict[str, Any],
    operations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    missed_rows: list[dict[str, Any]] = []
    moved_satisfied_rows: list[dict[str, Any]] = []
    index = 0
    while index < len(operations):
        get_op = operations[index]
        if get_op.get("Action") != "Get":
            index += 1
            continue
        put_op = operations[index + 1] if index + 1 < len(operations) else {}
        if put_op.get("Action") != "Put":
            index += 1
            continue

        get_index = int(get_op.get("Index") or 0)
        source = physical.normalize_line(get_op.get("Line"))
        actual_put = physical.normalize_line(put_op.get("Line"))
        move_nos = [str(no) for no in get_op.get("MoveCars") or []]
        cars, loco = state_before(request, operations, get_index)
        solver = Stage1Solver(truth_path, profile="baseline")
        solver.cars = cars
        solver.loco = loco
        by_no = solver.by_no()
        batch = [by_no[no] for no in move_nos if no in by_no]
        missed_rows.append(missed_direct_row(
            case_id=case_id,
            get_index=get_index,
            put_index=int(put_op.get("Index") or 0),
            source=source,
            actual_put=actual_put,
            move_nos=move_nos,
            batch=batch,
            solver=solver,
        ))
        moved_satisfied_rows.extend(moved_satisfied_rows_for_batch(
            case_id=case_id,
            get_index=get_index,
            source=source,
            actual_put=actual_put,
            move_nos=move_nos,
            batch=batch,
            solver=solver,
        ))
        index += 2
    return missed_rows, moved_satisfied_rows


def missed_direct_row(
    *,
    case_id: str,
    get_index: int,
    put_index: int,
    source: str,
    actual_put: str,
    move_nos: list[str],
    batch: list[dict[str, Any]],
    solver: Stage1Solver,
) -> dict[str, Any]:
    target = common_stage4_target(batch)
    stage1_count = sum(1 for car in batch if solver.stage1_goal(car))
    row: dict[str, Any] = {
        "case_id": case_id,
        "get_index": get_index,
        "put_index": put_index,
        "source": source,
        "actual_put": actual_put,
        "move_count": len(move_nos),
        "move_nos": "|".join(move_nos),
        "common_target": target,
        "stage1_count": stage1_count,
        "already_satisfied_count": sum(1 for car in batch if solver.target_satisfied(car)),
        "target_clean": 0,
        "target_capacity": 0,
        "physical_ok": 0,
        "missed_direct": 0,
        "opportunity_class": "",
        "reason": "",
    }
    if not target:
        row["reason"] = "no_single_stage4_target"
        return row
    if target == actual_put:
        row["reason"] = "already_put_to_target"
        return row
    if target == source:
        row["reason"] = "source_is_target_moved_out"
        return row
    if stage1_count:
        row["reason"] = "contains_stage1_car"
        return row
    row["target_clean"] = int(target_clean(solver, target, move_nos))
    if not row["target_clean"]:
        row["reason"] = "target_not_clean"
        return row
    row["target_capacity"] = int(physical.line_has_length_capacity(target, solver.cars, batch, set(move_nos)))
    if not row["target_capacity"]:
        row["reason"] = "target_capacity_full"
        return row
    candidate = solver.make_candidate(
        source=source,
        target=target,
        batch=batch,
        kind="audit_direct_target",
        reason="audit_direct_target",
    )
    validation = solver.validate_candidate(candidate) if candidate else None
    row["physical_ok"] = int(bool(validation and validation.accepted))
    if validation and validation.accepted:
        row["missed_direct"] = 1
        row["opportunity_class"] = opportunity_class(source, target, len(move_nos))
        row["reason"] = "missed_direct_target_put"
    else:
        row["reason"] = "physical_blocked:" + "|".join(validation.reasons if validation else ("candidate_none",))
    return row


def moved_satisfied_rows_for_batch(
    *,
    case_id: str,
    get_index: int,
    source: str,
    actual_put: str,
    move_nos: list[str],
    batch: list[dict[str, Any]],
    solver: Stage1Solver,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_no = {physical.car_no(car): car for car in batch}
    for no in move_nos:
        car = by_no.get(no)
        if not car:
            continue
        if not is_stage4_managed(car) or not solver.target_satisfied(car):
            continue
        rows.append({
            "case_id": case_id,
            "get_index": get_index,
            "source": source,
            "actual_put": actual_put,
            "no": no,
            "target_key": stage4_target_key(car),
            "from_target_line": int(source in (car.get("TargetLines") or [])),
            "to_target_line": int(actual_put in (car.get("TargetLines") or [])),
            "target_lines": "|".join(car.get("TargetLines") or []),
            "stage1_goal": solver.stage1_goal(car),
            "source_is_assembly": int(source in ASSEMBLY_ALL),
            "actual_is_assembly": int(actual_put in ASSEMBLY_ALL),
        })
    return rows


def final_stage4_debt_rows(case_id: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    generated = response.get("Data", {}).get("GeneratedEndStatus") or []
    for car in [physical.normalized_car(item) for item in generated]:
        targets = set(car.get("TargetLines") or []) - STAGE4_IGNORED_TARGET_LINES
        if not targets or not is_stage4_debt_car(car):
            continue
        line = car["Line"]
        rows.append({
            "case_id": case_id,
            "no": physical.car_no(car),
            "line": line,
            "target_key": stage4_target_key(car),
            "targets": "|".join(sorted(targets)),
            "line_is_cache": int(line in CACHE_LINES),
            "line_is_assembly": int(line in ASSEMBLY_ALL),
            "source_initial": car.get("_InitialLine", ""),
        })
    return rows


def final_stage4_debt_run_rows(case_id: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    generated = response.get("Data", {}).get("GeneratedEndStatus") or []
    cars = [physical.normalized_car(item) for item in generated]
    by_no = {physical.car_no(car): car for car in cars}
    for line in sorted({car["Line"] for car in cars if car["Line"]}):
        run_index = 0
        current_target = ""
        current_nos: list[str] = []

        def flush() -> None:
            nonlocal current_target, current_nos
            if not current_target or not current_nos:
                return
            rows.append({
                "case_id": case_id,
                "line": line,
                "run_index": run_index,
                "target_key": current_target,
                "vehicle_count": len(current_nos),
                "nos": "|".join(current_nos),
                "line_is_cache": int(line in CACHE_LINES),
                "line_is_assembly": int(line in ASSEMBLY_ALL),
            })
            current_target = ""
            current_nos = []

        for no in physical.line_access_order(cars, line):
            car = by_no.get(no)
            target = stage4_target_key(car) if car and is_stage4_debt_car(car) else ""
            if target and target == current_target:
                current_nos.append(no)
                continue
            flush()
            if target:
                run_index += 1
                current_target = target
                current_nos = [no]
        flush()
    return rows


def state_before(
    request: dict[str, Any],
    operations: list[dict[str, Any]],
    before_index: int,
) -> tuple[list[dict[str, Any]], physical.LocoLocation]:
    cars = [rv.ncar(car) for car in request.get("StartStatus") or []]
    by_no = {rv.car_no(car): car for car in cars}
    loco = physical.initial_loco_location(request.get("locoNode") or {})
    for row in sorted(
        [op for op in operations if int(op.get("Index") or 0) < before_index],
        key=lambda item: int(item.get("Index") or 0),
    ):
        action = row.get("Action")
        line = physical.normalize_line(row.get("Line"))
        move_nos = [str(no) for no in row.get("MoveCars") or []]
        if action == "Get":
            rv.apply_get(cars, line, move_nos)
            loco = physical.operation_stand_location(row.get("PassbyPath") or [], line)
        elif action == "Put":
            rv.apply_put(cars, line, move_nos)
            loco = physical.post_put_loco_location(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            for no in move_nos:
                if no in by_no:
                    by_no[no]["_Weighed"] = True
            loco = physical.operation_stand_location(row.get("PassbyPath") or [], physical.WEIGH_LINE)
    return [physical.normalized_car(car) for car in cars], loco


def missed_direct_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missed = [row for row in rows if int(row["missed_direct"])]
    by_target = Counter()
    by_source = Counter()
    by_actual = Counter()
    by_case = Counter()
    by_class = Counter()
    reason_counts = Counter()
    reason_targets: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        count = int(row["move_count"])
        reason = str(row["reason"])
        reason_counts[reason] += count
        target = str(row.get("common_target") or "")
        if target:
            reason_targets[reason][target] += count
    for row in missed:
        count = int(row["move_count"])
        by_target[str(row["common_target"])] += count
        by_source[str(row["source"])] += count
        by_actual[str(row["actual_put"])] += count
        by_case[str(row["case_id"])] += count
        by_class[str(row["opportunity_class"])] += count
    return {
        "operation_batch_count": len(rows),
        "moved_vehicle_count": sum(int(row["move_count"]) for row in rows),
        "missed_direct_batch_count": len(missed),
        "missed_direct_vehicle_count": sum(int(row["move_count"]) for row in missed),
        "missed_direct_by_target": dict(by_target.most_common()),
        "missed_direct_by_source": dict(by_source.most_common(20)),
        "missed_direct_actual_put": dict(by_actual.most_common(20)),
        "missed_direct_by_class": dict(by_class.most_common()),
        "top_cases": dict(by_case.most_common(20)),
        "reason_vehicle_counts": dict(reason_counts.most_common()),
        "reason_by_target_vehicle_counts": {
            reason: dict(counter.most_common(20))
            for reason, counter in sorted(
                reason_targets.items(),
                key=lambda item: (-sum(item[1].values()), item[0]),
            )
        },
    }


def moved_satisfied_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_target = Counter()
    by_source = Counter()
    by_actual = Counter()
    by_case = Counter()
    for row in rows:
        by_target[str(row["target_key"])] += 1
        by_source[str(row["source"])] += 1
        by_actual[str(row["actual_put"])] += 1
        by_case[str(row["case_id"])] += 1
    return {
        "moved_satisfied_stage4_vehicle_count": len(rows),
        "from_target_line_count": sum(int(row["from_target_line"]) for row in rows),
        "not_put_back_to_target_count": sum(1 - int(row["to_target_line"]) for row in rows),
        "by_target": dict(by_target.most_common()),
        "by_source": dict(by_source.most_common(20)),
        "actual_put": dict(by_actual.most_common(20)),
        "top_cases": dict(by_case.most_common(20)),
    }


def final_debt_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_line = Counter()
    by_target = Counter()
    by_case = Counter()
    pair = Counter()
    for row in rows:
        by_line[str(row["line"])] += 1
        by_target[str(row["target_key"])] += 1
        by_case[str(row["case_id"])] += 1
        pair[(str(row["line"]), str(row["target_key"]))] += 1
    return {
        "stage4_debt_count": len(rows),
        "debt_on_cache_lines": sum(int(row["line_is_cache"]) for row in rows),
        "debt_on_assembly_lines": sum(int(row["line_is_assembly"]) for row in rows),
        "by_line": dict(by_line.most_common(30)),
        "by_target": dict(by_target.most_common(30)),
        "top_line_target_pairs": {f"{line}->{target}": count for (line, target), count in pair.most_common(30)},
        "top_cases": dict(by_case.most_common(20)),
    }


def target_fragmentation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_target: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "lines": Counter(),
        "cache_lines": Counter(),
        "cases": Counter(),
    })
    for row in rows:
        target = str(row["target_key"])
        item = per_target[target]
        item["count"] += 1
        item["lines"][str(row["line"])] += 1
        item["cases"][str(row["case_id"])] += 1
        if int(row["line_is_cache"]):
            item["cache_lines"][str(row["line"])] += 1
    return {
        target: {
            "count": item["count"],
            "source_line_count": len(item["lines"]),
            "cache_line_count": len(item["cache_lines"]),
            "top_lines": dict(item["lines"].most_common(10)),
            "top_cache_lines": dict(item["cache_lines"].most_common(10)),
            "case_count": len(item["cases"]),
        }
        for target, item in sorted(per_target.items(), key=lambda pair: (-pair[1]["count"], pair[0]))
    }


def debt_run_fragmentation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_line: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "run_count": 0,
        "vehicle_count": 0,
        "targets": Counter(),
        "cases": Counter(),
    })
    by_target: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "run_count": 0,
        "vehicle_count": 0,
        "lines": Counter(),
        "cases": Counter(),
    })
    case_line_target_runs = Counter()
    for row in rows:
        line = str(row["line"])
        target = str(row["target_key"])
        case_id = str(row["case_id"])
        count = int(row["vehicle_count"])
        by_line[line]["run_count"] += 1
        by_line[line]["vehicle_count"] += count
        by_line[line]["targets"][target] += count
        by_line[line]["cases"][case_id] += 1
        by_target[target]["run_count"] += 1
        by_target[target]["vehicle_count"] += count
        by_target[target]["lines"][line] += count
        by_target[target]["cases"][case_id] += 1
        case_line_target_runs[(case_id, line, target)] += 1
    return {
        "stage4_debt_run_count": len(rows),
        "stage4_debt_vehicle_count": sum(int(row["vehicle_count"]) for row in rows),
        "cache_run_count": sum(1 for row in rows if int(row["line_is_cache"])),
        "assembly_run_count": sum(1 for row in rows if int(row["line_is_assembly"])),
        "extra_same_line_target_run_count": sum(
            max(0, count - 1) for count in case_line_target_runs.values()
        ),
        "by_line": {
            line: {
                "run_count": item["run_count"],
                "vehicle_count": item["vehicle_count"],
                "target_type_count": len(item["targets"]),
                "top_targets": dict(item["targets"].most_common(10)),
                "case_count": len(item["cases"]),
            }
            for line, item in sorted(
                by_line.items(),
                key=lambda pair: (-pair[1]["run_count"], pair[0]),
            )[:30]
        },
        "by_target": {
            target: {
                "run_count": item["run_count"],
                "vehicle_count": item["vehicle_count"],
                "source_line_count": len(item["lines"]),
                "top_lines": dict(item["lines"].most_common(10)),
                "case_count": len(item["cases"]),
            }
            for target, item in sorted(
                by_target.items(),
                key=lambda pair: (-pair[1]["run_count"], pair[0]),
            )
        },
    }


def opportunity_class(source: str, target: str, move_count: int) -> str:
    if target in DIRECT_FUNCTION_TARGETS and move_count >= 2:
        return "P0_safe_segment_direct"
    if target in DIRECT_FUNCTION_TARGETS and source not in STORAGE_CACHE_LINES:
        return "P1_nonstorage_single_direct"
    if target == "存4线":
        return "P2_cun4_release_or_isolation"
    return "P3_not_stage1_primary"


def common_stage4_target(cars: list[dict[str, Any]]) -> str:
    targets: list[str] = []
    for car in cars:
        item = stage4_target_lines(car)
        if len(item) != 1:
            return ""
        targets.append(item[0])
    return targets[0] if targets and all(target == targets[0] for target in targets) else ""


def stage4_target_lines(car: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(set(car.get("TargetLines") or []) - STAGE4_IGNORED_TARGET_LINES))


def stage4_target_key(car: dict[str, Any]) -> str:
    targets = stage4_target_lines(car)
    return targets[0] if len(targets) == 1 else "/".join(targets)


def is_stage4_managed(car: dict[str, Any]) -> bool:
    return bool(stage4_target_lines(car))


def is_stage4_debt_car(car: dict[str, Any]) -> bool:
    return is_stage4_managed(car) and not local_target_satisfied(car)


def target_clean(solver: Stage1Solver, target: str, moving_nos: list[str]) -> bool:
    moving = set(moving_nos)
    for car in solver.cars:
        if car["Line"] != target or physical.car_no(car) in moving:
            continue
        if not solver.target_satisfied(car) or physical.force_positions(car):
            return False
    return True


def local_target_satisfied(car: dict[str, Any]) -> bool:
    targets = set(car.get("TargetLines") or [])
    if not targets:
        return True
    if car["Line"] not in targets:
        return False
    forced = physical.force_positions(car)
    if forced and int(car.get("Position") or 0) not in forced:
        return False
    if car["Line"] == "存4线" and car.get("IsClosedDoor") and int(car.get("Position") or 0) <= 3:
        return False
    return True


def truth_case_map(truth_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in truth_dir.glob("validation_*.json"):
        out[case_id_from_path(path)] = path
    return out


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4})(\d{2})(\d{2})([ZWzw])", path.name)
    if match:
        return f"{match.group(2)}{match.group(3)}{match.group(4).upper()}"
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"cannot infer case id from {path}")
    return match.group(1).upper()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
