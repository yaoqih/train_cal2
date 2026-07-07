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

import analyze_stage1_friendliness as s1f
from solver_vnext import physical


DIRECTABLE_TARGETS = ("油漆线", "抛丸线", "洗罐站", "预修线")
HIGH_PRESSURE_TARGETS = ("存4线", "预修线", "调梁棚", "油漆线", "洗罐站", "抛丸线")
CHOKE_LINES = {
    "机走棚",
    "机走北",
    "机北1",
    "机北2",
    "存4线",
    "存4南",
    "渡10",
    "联7",
    "渡4",
    "渡5",
    "渡6",
    "渡7",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Stage4 pressure from Stage1 outputs.")
    parser.add_argument("--stage1-dir", required=True, help="directory containing *_response.json from stage1_simple")
    parser.add_argument("--truth-dir", default="data/truth2", help="directory containing validation_*.json")
    parser.add_argument("--output-dir", default="", help="default: <stage1-dir>/stage4_pressure")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    stage1_dir = Path(args.stage1_dir)
    truth_dir = Path(args.truth_dir)
    output_dir = Path(args.output_dir) if args.output_dir else stage1_dir / "stage4_pressure"
    output_dir.mkdir(parents=True, exist_ok=True)

    truth_by_case = s1f.truth_case_map(truth_dir)
    response_paths = sorted(stage1_dir.glob("*_response.json"))
    if args.limit:
        response_paths = response_paths[: args.limit]

    graph = physical.TrackGraph()
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
        final_cars, replay_errors = s1f.final_cars_after_stage1(request, response)
        analysis = analyze_case(case_id, final_cars, summary, graph, len(replay_errors))
        case_rows.append(analysis["case"])
        line_rows.extend(analysis["lines"])
        vehicle_rows.extend(analysis["vehicles"])

    summary = summarize(case_rows, line_rows, vehicle_rows)
    write_csv(output_dir / "stage4_pressure_cases.csv", case_rows)
    write_csv(output_dir / "stage4_pressure_lines.csv", line_rows)
    write_csv(output_dir / "stage4_pressure_vehicles.csv", vehicle_rows)
    write_json(output_dir / "stage4_pressure_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")


def analyze_case(
    case_id: str,
    cars: list[dict[str, Any]],
    summary: dict[str, Any],
    graph: physical.TrackGraph,
    replay_error_count: int,
) -> dict[str, Any]:
    by_no = {physical.car_no(car): car for car in cars}
    debt_nos = {no for no, car in by_no.items() if s1f.is_stage4_debt(car)}
    debt_by_target = Counter(stage4_target_key(by_no[no]) for no in debt_nos)
    target_profiles = {target: target_line_profile(cars, target) for target in HIGH_PRESSURE_TARGETS}
    line_profiles = source_line_profiles(cars, debt_nos)

    vehicle_rows: list[dict[str, Any]] = []
    route_blockers = Counter()
    failure_reasons = Counter()
    for no in sorted(debt_nos):
        car = by_no[no]
        target_lines = stage4_target_lines(car)
        target_line = target_lines[0] if len(target_lines) == 1 else ""
        line_profile = line_profiles.get(car["Line"], {})
        prefix_nos = set(line_profile.get("prefix_nos") or [])
        prefix_target = str(line_profile.get("prefix_target") or "")
        prefix_ready = no in prefix_nos and bool(prefix_target)
        route = route_diagnosis(cars, graph, car, target_line) if target_line else empty_route_diag()
        for blocker in route["blockers"]:
            route_blockers[blocker] += 1
        target_profile = target_profiles.get(target_line, empty_target_profile(target_line))
        car_directable = car_directable_for_stage1(car, target_line)
        capacity_ok = (
            bool(target_line)
            and physical.line_has_length_capacity(target_line, cars, [car], {no})
        )
        reasons = direct_failure_reasons(
            car=car,
            target_line=target_line,
            car_directable=car_directable,
            prefix_ready=prefix_ready,
            target_ready=bool(target_profile["target_ready"]),
            route_open=bool(route["route_open"]),
            capacity_ok=capacity_ok,
        )
        failure_reasons.update(reasons)
        vehicle_rows.append({
            "case_id": case_id,
            "no": no,
            "source_line": car["Line"],
            "position": int(car.get("Position") or 0),
            "access_index": access_index(cars, car["Line"], no),
            "front_count": max(0, access_index(cars, car["Line"], no) - 1),
            "target_key": stage4_target_key(car),
            "target_line": target_line,
            "target_is_directable_class": int(target_line in DIRECTABLE_TARGETS),
            "has_force_position": int(bool(physical.force_positions(car))),
            "is_stage1_goal": int(bool(stage1_goal(car))),
            "target_ready": int(bool(target_profile["target_ready"])),
            "target_empty": int(bool(target_profile["target_empty"])),
            "target_dirty_count": int(target_profile["target_dirty_count"]),
            "target_forced_count": int(target_profile["target_forced_count"]),
            "prefix_ready": int(prefix_ready),
            "prefix_target": prefix_target,
            "prefix_size": int(line_profile.get("prefix_size") or 0),
            "route_open": int(bool(route["route_open"])),
            "static_route_len": int(route["static_route_len"]),
            "route_blockers": "|".join(route["blockers"]),
            "choke_blockers": "|".join(blocker for blocker in route["blockers"] if blocker in CHOKE_LINES),
            "capacity_ok": int(capacity_ok),
            "direct_opportunity": int(not reasons),
            "direct_failure_reasons": "|".join(reasons),
            "length": f"{physical.car_length(car):.3f}",
            "targets": "/".join(car.get("TargetLines") or []),
        })

    line_rows = []
    for line, profile in sorted(line_profiles.items()):
        line_debt_nos = [no for no in physical.line_access_order(cars, line) if no in debt_nos]
        if not line_debt_nos:
            continue
        line_rows.append({
            "case_id": case_id,
            "source_line": line,
            "debt_count": len(line_debt_nos),
            "first_debt_access_index": min(access_index(cars, line, no) for no in line_debt_nos),
            "prefix_target": profile.get("prefix_target", ""),
            "prefix_size": int(profile.get("prefix_size") or 0),
            "prefix_nos": "|".join(profile.get("prefix_nos") or []),
            "front_non_debt_count_before_first_debt": int(profile.get("front_non_debt_count") or 0),
            "targets": counter_text(Counter(stage4_target_key(by_no[no]) for no in line_debt_nos)),
        })

    direct_rows = [row for row in vehicle_rows if int(row["direct_opportunity"])]
    route_open_rows = [row for row in vehicle_rows if int(row["route_open"])]
    target_ready_rows = [row for row in vehicle_rows if int(row["target_ready"])]
    prefix_ready_rows = [row for row in vehicle_rows if int(row["prefix_ready"])]
    case_row = {
        "case_id": case_id,
        "stage1_status": summary.get("status", ""),
        "stage1_hooks": int(summary.get("hooks") or 0),
        "replay_error_count": replay_error_count,
        "stage4_debt_count": len(debt_nos),
        "direct_opportunity_count": len(direct_rows),
        "direct_opportunity_by_target": counter_text(Counter(row["target_line"] for row in direct_rows)),
        "prefix_ready_count": len(prefix_ready_rows),
        "target_ready_count": len(target_ready_rows),
        "route_open_count": len(route_open_rows),
        "forced_debt_count": sum(int(row["has_force_position"]) for row in vehicle_rows),
        "multi_target_debt_count": sum(1 for no in debt_nos if len(stage4_target_lines(by_no[no])) != 1),
        "debt_by_target": counter_text(debt_by_target),
        "route_blockers": counter_text(route_blockers),
        "direct_failure_reasons": counter_text(failure_reasons),
        "target_profiles": "|".join(
            f"{target}:ready={int(profile['target_ready'])},dirty={profile['target_dirty_count']},forced={profile['target_forced_count']}"
            for target, profile in target_profiles.items()
        ),
    }
    return {"case": case_row, "lines": line_rows, "vehicles": vehicle_rows}


def stage4_target_lines(car: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(set(car.get("TargetLines") or []) - s1f.STAGE4_IGNORED_TARGET_LINES))


def stage4_target_key(car: dict[str, Any]) -> str:
    targets = stage4_target_lines(car)
    if len(targets) == 1:
        return targets[0]
    return "/".join(targets)


def target_line_profile(cars: list[dict[str, Any]], target_line: str) -> dict[str, Any]:
    existing = [car for car in cars if car["Line"] == target_line]
    dirty = [car for car in existing if not s1f.target_satisfied(car)]
    forced = [car for car in existing if physical.force_positions(car)]
    return {
        "target_line": target_line,
        "target_empty": not existing,
        "target_ready": not dirty and not forced,
        "target_existing_count": len(existing),
        "target_dirty_count": len(dirty),
        "target_forced_count": len(forced),
    }


def empty_target_profile(target_line: str) -> dict[str, Any]:
    return {
        "target_line": target_line,
        "target_empty": False,
        "target_ready": False,
        "target_existing_count": 0,
        "target_dirty_count": 0,
        "target_forced_count": 0,
    }


def source_line_profiles(cars: list[dict[str, Any]], debt_nos: set[str]) -> dict[str, dict[str, Any]]:
    by_no = {physical.car_no(car): car for car in cars}
    out: dict[str, dict[str, Any]] = {}
    for line in sorted({car["Line"] for car in cars if car["Line"]}):
        ordered = physical.line_access_order(cars, line)
        prefix: list[str] = []
        prefix_target = ""
        front_non_debt_count = 0
        for no in ordered:
            if no not in debt_nos:
                if not prefix:
                    front_non_debt_count += 1
                break
            car = by_no[no]
            targets = stage4_target_lines(car)
            target = targets[0] if len(targets) == 1 else ""
            if not target or physical.force_positions(car):
                break
            if prefix_target and target != prefix_target:
                break
            trial = [by_no[item] for item in [*prefix, no]]
            if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix_target = target
            prefix.append(no)
        if ordered and ordered[0] not in debt_nos:
            first_debt = next((idx for idx, no in enumerate(ordered) if no in debt_nos), -1)
            front_non_debt_count = first_debt if first_debt > 0 else 0
        out[line] = {
            "prefix_nos": prefix,
            "prefix_target": prefix_target,
            "prefix_size": len(prefix),
            "front_non_debt_count": front_non_debt_count,
        }
    return out


def route_diagnosis(
    cars: list[dict[str, Any]],
    graph: physical.TrackGraph,
    car: dict[str, Any],
    target_line: str,
) -> dict[str, Any]:
    no = physical.car_no(car)
    if not target_line:
        return empty_route_diag()
    moving_nos = {no}
    source = car["Line"]
    occupied = physical.occupied_lines_for_route(cars, moving_nos)
    route = graph.route_avoiding_occupied(
        source,
        target_line,
        occupied,
        source_departure_lines=physical.route_departure_lines_for_source(source, cars, moving_nos),
        target_approach_lines=physical.route_approach_lines_for_put(target_line, cars, moving_nos),
        cars=cars,
        moving_nos=moving_nos,
        train_length_m=physical.train_length_for_nos(cars, moving_nos),
    )
    static = graph.route(source, target_line)
    blockers: list[str] = []
    if not route and static:
        endpoints = {source, target_line}
        blockers = [line for line in static if line in occupied and line not in endpoints]
    return {
        "route_open": bool(route),
        "static_route_len": len(static or []),
        "blockers": tuple(dict.fromkeys(blockers)),
    }


def empty_route_diag() -> dict[str, Any]:
    return {"route_open": False, "static_route_len": 0, "blockers": ()}


def car_directable_for_stage1(car: dict[str, Any], target_line: str) -> bool:
    return bool(
        target_line in DIRECTABLE_TARGETS
        and not stage1_goal(car)
        and not physical.force_positions(car)
        and not s1f.target_satisfied(car)
    )


def direct_failure_reasons(
    *,
    car: dict[str, Any],
    target_line: str,
    car_directable: bool,
    prefix_ready: bool,
    target_ready: bool,
    route_open: bool,
    capacity_ok: bool,
) -> list[str]:
    reasons: list[str] = []
    if not target_line:
        reasons.append("multi_or_missing_target")
    if target_line and target_line not in DIRECTABLE_TARGETS:
        reasons.append("target_class_not_stage1_directable")
    if stage1_goal(car):
        reasons.append("stage1_goal_car")
    if physical.force_positions(car):
        reasons.append("force_position")
    if not car_directable and not reasons:
        reasons.append("not_directable")
    if not prefix_ready:
        reasons.append("not_north_prefix_same_target")
    if not target_ready:
        reasons.append("target_line_not_clean")
    if not route_open:
        reasons.append("route_blocked_or_missing")
    if not capacity_ok:
        reasons.append("target_capacity_full")
    return reasons


def access_index(cars: list[dict[str, Any]], line: str, no: str) -> int:
    ordered = physical.line_access_order(cars, line)
    try:
        return ordered.index(no) + 1
    except ValueError:
        return 0


def stage1_goal(car: dict[str, Any]) -> str:
    return s1f.stage1_goal(car)


def summarize(
    case_rows: list[dict[str, Any]],
    line_rows: list[dict[str, Any]],
    vehicle_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    target_counts = Counter(row["target_key"] for row in vehicle_rows)
    source_counts = Counter(row["source_line"] for row in vehicle_rows)
    blocker_counts = Counter()
    choke_blocker_counts = Counter()
    failure_counts = Counter()
    opportunity_counts = Counter()
    target_funnel: dict[str, Counter[str]] = defaultdict(Counter)
    target_failures: dict[str, Counter[str]] = defaultdict(Counter)
    target_sources: dict[str, Counter[str]] = defaultdict(Counter)
    target_failure_combos: dict[str, Counter[str]] = defaultdict(Counter)
    for row in vehicle_rows:
        for item in str(row["route_blockers"]).split("|"):
            if item:
                blocker_counts[item] += 1
        for item in str(row["choke_blockers"]).split("|"):
            if item:
                choke_blocker_counts[item] += 1
        for item in str(row["direct_failure_reasons"]).split("|"):
            if item:
                failure_counts[item] += 1
        if int(row["direct_opportunity"]):
            opportunity_counts[row["target_line"]] += 1
        target = str(row["target_line"] or row["target_key"])
        funnel = target_funnel[target]
        funnel["total"] += 1
        funnel["prefix_ready"] += int(row["prefix_ready"])
        funnel["target_ready"] += int(row["target_ready"])
        funnel["route_open"] += int(row["route_open"])
        funnel["capacity_ok"] += int(row["capacity_ok"])
        funnel["direct_opportunity"] += int(row["direct_opportunity"])
        funnel["force_position"] += int(row["has_force_position"])
        target_sources[target][str(row["source_line"])] += 1
        reasons = [item for item in str(row["direct_failure_reasons"]).split("|") if item]
        for item in reasons:
            target_failures[target][item] += 1
        if reasons:
            target_failure_combos[target]["+".join(reasons)] += 1
    directable_total = sum(target_funnel[target]["total"] for target in DIRECTABLE_TARGETS)
    directable_opportunity = sum(target_funnel[target]["direct_opportunity"] for target in DIRECTABLE_TARGETS)
    return {
        "case_count": len(case_rows),
        "stage4_debt_count": len(vehicle_rows),
        "direct_opportunity_count": sum(opportunity_counts.values()),
        "directable_target_debt_count": directable_total,
        "directable_target_opportunity_count": directable_opportunity,
        "directable_target_opportunity_rate": round(
            directable_opportunity / directable_total,
            4,
        )
        if directable_total
        else 0.0,
        "target_counts": dict(target_counts.most_common()),
        "source_counts": dict(source_counts.most_common(20)),
        "route_blocker_counts": dict(blocker_counts.most_common(30)),
        "choke_blocker_counts": dict(choke_blocker_counts.most_common(30)),
        "direct_failure_reasons": dict(failure_counts.most_common()),
        "direct_opportunity_by_target": dict(opportunity_counts.most_common()),
        "target_funnel": {
            target: funnel_summary(counts)
            for target, counts in sorted(
                target_funnel.items(),
                key=lambda item: (-item[1]["total"], item[0]),
            )
        },
        "target_failure_reasons": {
            target: dict(counter.most_common())
            for target, counter in sorted(
                target_failures.items(),
                key=lambda item: (-target_funnel[item[0]]["total"], item[0]),
            )
        },
        "target_top_sources": {
            target: dict(counter.most_common(10))
            for target, counter in sorted(
                target_sources.items(),
                key=lambda item: (-target_funnel[item[0]]["total"], item[0]),
            )
        },
        "target_top_failure_combos": {
            target: dict(counter.most_common(8))
            for target, counter in sorted(
                target_failure_combos.items(),
                key=lambda item: (-target_funnel[item[0]]["total"], item[0]),
            )
        },
        "top_cases_by_debt": sorted(
            (
                {
                    "case_id": row["case_id"],
                    "debt": int(row["stage4_debt_count"]),
                    "direct_opportunity": int(row["direct_opportunity_count"]),
                    "route_open": int(row["route_open_count"]),
                    "target_ready": int(row["target_ready_count"]),
                    "prefix_ready": int(row["prefix_ready_count"]),
                }
                for row in case_rows
            ),
            key=lambda item: (-item["debt"], item["case_id"]),
        )[:15],
    }


def funnel_summary(counts: Counter[str]) -> dict[str, Any]:
    total = counts["total"]
    out: dict[str, Any] = {
        "total": total,
        "prefix_ready": counts["prefix_ready"],
        "target_ready": counts["target_ready"],
        "route_open": counts["route_open"],
        "capacity_ok": counts["capacity_ok"],
        "force_position": counts["force_position"],
        "direct_opportunity": counts["direct_opportunity"],
    }
    if total:
        out.update({
            "prefix_ready_rate": round(counts["prefix_ready"] / total, 4),
            "target_ready_rate": round(counts["target_ready"] / total, 4),
            "route_open_rate": round(counts["route_open"] / total, 4),
            "capacity_ok_rate": round(counts["capacity_ok"] / total, 4),
            "force_position_rate": round(counts["force_position"] / total, 4),
            "direct_opportunity_rate": round(counts["direct_opportunity"] / total, 4),
        })
    return out


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common() if key)


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
