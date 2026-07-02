#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import physical


LOW_CONFIDENCE_CLASSES = {"north_head_substitute", "substitute_note"}
STRUCTURE_ONLY_CLASSES = {"storage5_push_reposition"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build owner/blocker labels for replayed manual actions.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--label-dir", required=True)
    parser.add_argument("--replay-dir", required=True)
    parser.add_argument("--uncertainty-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    label_rows = read_csv(root / args.label_dir / "manual_action_labels.csv")
    replay_rows = read_csv(root / args.replay_dir / "manual_identity_replay_trace.csv")
    uncertainty_rows = read_csv(root / args.uncertainty_dir / "manual_uncertainty_class_assignments.csv")
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    truth_index = build_truth_index(root / args.truth_dir)
    replay_by_key = {(row.get("case_id", ""), row.get("manual_hook", "")): row for row in replay_rows}
    uncertainty_by_key = build_uncertainty_index(uncertainty_rows)

    owner_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    for label in sorted(label_rows, key=lambda row: (row.get("case_id", ""), to_int(row.get("manual_hook")))):
        key = (label.get("case_id", ""), label.get("manual_hook", ""))
        replay = replay_by_key.get(key, {})
        classes = uncertainty_by_key.get(key, ())
        row = classify_owner_blocker(label, replay, classes, truth_index.get(label.get("case_id", "")))
        owner_rows.append(row)
        if row["label_status"] == "gap":
            gap_rows.append(
                {
                    "case_id": row["case_id"],
                    "manual_hook": row["manual_hook"],
                    "gap_reason": row["gap_reason"],
                    "replay_status": row["replay_status"],
                    "uncertainty_classes": row["uncertainty_classes"],
                    "detail": row["detail"],
                }
            )

    case_rows = build_case_summary(owner_rows)
    summary = build_summary(owner_rows, gap_rows)
    physical.write_csv(output_dir / "manual_owner_blocker_labels.csv", owner_rows)
    physical.write_csv(output_dir / "manual_owner_blocker_case_summary.csv", case_rows)
    physical.write_csv(output_dir / "manual_owner_blocker_gap_records.csv", gap_rows)
    physical.write_json(output_dir / "manual_owner_blocker_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def classify_owner_blocker(
    label: dict[str, str],
    replay: dict[str, str],
    uncertainty_classes: tuple[str, ...],
    truth: dict[str, Any] | None,
) -> dict[str, Any]:
    base = {
        "case_id": label.get("case_id", ""),
        "manual_hook": label.get("manual_hook", ""),
        "phase_label": label.get("phase_label", ""),
        "family": label.get("family", ""),
        "intent": label.get("intent", ""),
        "line_raw": label.get("line_raw", ""),
        "resolved_line": label.get("resolved_line", ""),
        "method": label.get("method", ""),
        "effective_count": label.get("effective_count", ""),
        "manual_reference_scope": label.get("manual_reference_scope", ""),
        "structural_label_quality": label.get("structural_label_quality", ""),
        "replay_status": replay.get("replay_status", ""),
        "moved_nos": normalize_nos(replay.get("moved_nos_if_unique", "")),
        "owner_nos": "",
        "blocker_nos": "",
        "support_nos": "",
        "owner_source": "",
        "blocker_source": "",
        "label_status": "gap",
        "gap_reason": "",
        "uncertainty_classes": "|".join(uncertainty_classes),
        "detail": replay.get("detail", ""),
    }
    if label.get("manual_reference_scope") == "exclude_from_algorithm_learning":
        base["gap_reason"] = "excluded_reference"
        return base
    if any(item in LOW_CONFIDENCE_CLASSES for item in uncertainty_classes):
        base["gap_reason"] = "low_confidence_uncertainty_class"
        return base
    if any(item in STRUCTURE_ONLY_CLASSES for item in uncertainty_classes):
        base["gap_reason"] = "structure_only_uncertainty_class"
        base["label_status"] = "partial"
        return base
    moved = split_nos(base["moved_nos"])
    if replay.get("replay_status") != "replayed_unique" or not moved:
        base["gap_reason"] = replay_gap_reason(replay)
        return base
    if truth is None:
        base["gap_reason"] = "truth_case_missing"
        return base

    line = physical.normalize_line(label.get("resolved_line", ""))
    method = label.get("method", "")
    owner: list[str] = []
    support: list[str] = []
    for no in moved:
        fact = truth["cars"].get(no)
        if fact is None:
            support.append(no)
            continue
        if method == "-" and line and line in fact["target_lines"]:
            owner.append(no)
        elif method == "+" and line and fact["source_line"] == line and not fact["satisfied"]:
            owner.append(no)
        elif line and fact["target_line"] == line and not fact["satisfied"]:
            owner.append(no)
        else:
            support.append(no)

    if not owner:
        base["support_nos"] = "|".join(support)
        base["gap_reason"] = "no_contract_owner"
        return base

    blockers = [no for no in moved if no not in set(owner)]
    base["owner_nos"] = "|".join(owner)
    base["blocker_nos"] = "|".join(blockers)
    base["support_nos"] = "|".join(no for no in support if no not in set(blockers))
    base["owner_source"] = "truth2_target" if method == "-" else "truth2_unsatisfied_source"
    base["blocker_source"] = "same_move_non_owner" if blockers else ""
    base["label_status"] = "clean" if not blockers else "partial"
    base["gap_reason"] = ""
    return base


def build_truth_index(truth_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(truth_dir.glob("*.json")):
        if path.name == "conversion_summary.json":
            continue
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        loads = physical.line_loads(cars)
        facts: dict[str, dict[str, Any]] = {}
        for car in cars:
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            targets = tuple(dict.fromkeys([*physical.target_lines(car), target_line]))
            facts[no] = {
                "source_line": car["Line"],
                "target_line": target_line,
                "target_lines": targets,
                "satisfied": physical.car_is_satisfied(car, depot_assignment, cars),
            }
        result[case_id] = {"cars": facts}
    return result


def build_uncertainty_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], tuple[str, ...]]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        class_id = row.get("class_id", "")
        if class_id:
            grouped[(row.get("case_id", ""), row.get("manual_hook", ""))].add(class_id)
    return {key: tuple(sorted(values)) for key, values in grouped.items()}


def build_case_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["case_id"]].append(row)
    result: list[dict[str, Any]] = []
    for case_id in sorted(grouped):
        items = grouped[case_id]
        reference = [row for row in items if row["manual_reference_scope"] != "exclude_from_algorithm_learning"]
        result.append(
            {
                "case_id": case_id,
                "hook_count": len(items),
                "reference_hook_count": len(reference),
                "clean_count": sum(1 for row in reference if row["label_status"] == "clean"),
                "partial_count": sum(1 for row in reference if row["label_status"] == "partial"),
                "gap_count": sum(1 for row in reference if row["label_status"] == "gap"),
                "gap_reasons": counter_text(Counter(row["gap_reason"] for row in reference if row["gap_reason"]), 8),
            }
        )
    return result


def build_summary(rows: list[dict[str, Any]], gap_rows: list[dict[str, Any]]) -> dict[str, Any]:
    reference = [row for row in rows if row["manual_reference_scope"] != "exclude_from_algorithm_learning"]
    replay_unique = [row for row in reference if row["replay_status"] == "replayed_unique"]
    replay_unique_owner = [row for row in replay_unique if row["owner_nos"]]
    replay_unique_blocker_labeled = [
        row for row in replay_unique if row["label_status"] in {"clean", "partial"}
    ]
    replay_unique_nonempty_blocker = [row for row in replay_unique if row["blocker_nos"]]
    low_conf_forced = [
        row
        for row in rows
        if row["owner_nos"] and set(row["uncertainty_classes"].split("|")) & LOW_CONFIDENCE_CLASSES
    ]
    return {
        "hook_count": len(rows),
        "reference_hook_count": len(reference),
        "status_counts": dict(Counter(row["label_status"] for row in reference)),
        "gap_reason_counts": dict(Counter(row["gap_reason"] for row in gap_rows if row["gap_reason"]).most_common(20)),
        "replay_unique_count": len(replay_unique),
        "replay_unique_owner_coverage": ratio(len(replay_unique_owner), len(replay_unique)),
        "replay_unique_blocker_coverage": ratio(len(replay_unique_blocker_labeled), len(replay_unique)),
        "replay_unique_nonempty_blocker_rate": ratio(len(replay_unique_nonempty_blocker), len(replay_unique)),
        "owner_blocker_conflict_count": 0,
        "low_confidence_class_forced_label_count": len(low_conf_forced),
    }


def replay_gap_reason(replay: dict[str, str]) -> str:
    status = replay.get("replay_status", "")
    if status in {"identity_conflict", "state_space_exceeded", "structural_gap", "truth_missing"}:
        return status
    if status == "identity_noop":
        return "identity_noop"
    if status == "replayed_ambiguous":
        return "no_unique_replay"
    if not replay.get("moved_nos_if_unique"):
        return "no_unique_moved_nos"
    return status or "missing_replay_row"


def normalize_nos(value: str) -> str:
    return "|".join(split_nos(value))


def split_nos(value: str) -> list[str]:
    return [item for item in str(value or "").replace(",", "|").split("|") if item]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def counter_text(counter: Counter[Any], limit: int) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common(limit) if key)


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
