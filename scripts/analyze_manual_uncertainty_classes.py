#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


CAR_NO_PATTERN = re.compile(r"(?<!\d)\d{7}(?!\d)")


CLASS_RULES: dict[str, dict[str, str]] = {
    "forkline_plus": {
        "description": "存2 + 叉线",
        "conclusion": "field_confirmed_source_is_prepair_line",
        "confidence": "high",
        "algorithm_instruction": "use source scope 预修线/存2叉; do not include ordinary 存2线 for this source",
        "must_not_assume": "do not treat 叉线 as switch text or as a separate route marker",
        "next_validation": "use later identity replay only to validate car order, not to rediscover the line alias",
    },
    "forkline_minus": {
        "description": "存2 - 叉线",
        "conclusion": "field_confirmed_target_is_prepair_line_but_identity_order_not_fully_closed",
        "confidence": "medium_high",
        "algorithm_instruction": "use target scope 预修线/存2叉; keep endpoint/order ambiguous until later car-number evidence",
        "must_not_assume": "do not infer ordinary 存2线 destination; do not infer exact endpoint without car evidence",
        "next_validation": "pair with subsequent 存2+叉线 hooks and car numbers to reduce endpoint ambiguity",
    },
    "storage5_push_reposition": {
        "description": "存5 北头顶N",
        "conclusion": "field_confirmed_compound_internal_reposition",
        "confidence": "high_for_structure_low_for_exact_endpoint",
        "algorithm_instruction": "model as storage5 internal reposition, e.g. 存5+M > 存5-M > 存5-N; target_south_endpoint remains unresolved",
        "must_not_assume": "do not learn it as one ordinary + or - hook; do not force 存5线南 as exact target",
        "next_validation": "build a bounded storage5 reposition episode and validate by final line loads rather than exact manual endpoint",
    },
    "storage5_north_endpoint": {
        "description": "存5 北头 without 顶",
        "conclusion": "endpoint_hint_only",
        "confidence": "high",
        "algorithm_instruction": "for 存5线北 + 北头 use position_low; for other methods keep normal validation",
        "must_not_assume": "do not map 北头 to H2 or 存4 semantics",
        "next_validation": "continue checking matched car-number hooks for endpoint consistency",
    },
    "north_head_substitute": {
        "description": "北头代N",
        "conclusion": "compound_substitute_unresolved",
        "confidence": "low",
        "algorithm_instruction": "keep as structural gap; N is not a safe vehicle count",
        "must_not_assume": "do not convert 北头代N to count=N or a simple detach",
        "next_validation": "requires field dictionary or paired before/after state evidence",
    },
    "substitute_note": {
        "description": "代N / 南代N / 机代N",
        "conclusion": "substitute_operation_unresolved",
        "confidence": "low",
        "algorithm_instruction": "keep as structural gap unless a narrower field-confirmed template exists",
        "must_not_assume": "do not treat 代N as count, endpoint, or normal +/− movement",
        "next_validation": "collect field definitions per phrase: 南代, 机代, 叉线代, 接代",
    },
    "machine_corridor_south": {
        "description": "机/库 with 南 or 南头",
        "conclusion": "machine_corridor_aggregate_not_precise_segment",
        "confidence": "medium",
        "algorithm_instruction": "model manual 机 as aggregate machine corridor; distinguish temporary capacity from final parking in solver",
        "must_not_assume": "do not infer exact 机南 cars from manual hook alone",
        "next_validation": "compare initial 机走 loads and later car numbers before adding 机南 to exact replay scope",
    },
    "machine_loco_noop": {
        "description": "库/机 +1, 称, 回/停 no freight delta",
        "conclusion": "field_confirmed_loco_or_weighing_noop",
        "confidence": "high",
        "algorithm_instruction": "exclude from freight identity delta and candidate recall denominator",
        "must_not_assume": "do not create freight +1 movement from empty 机库 just because manual says 库+1",
        "next_validation": "keep as no-op unless note contains car numbers or non-loco count evidence",
    },
    "storage4_north_blank": {
        "description": "存4 - 北头 with omitted count",
        "conclusion": "field_confirmed_reminder_noop",
        "confidence": "high",
        "algorithm_instruction": "exclude from freight identity delta; keep as phase/resource signal only",
        "must_not_assume": "do not infer a detach count from blank count",
        "next_validation": "distinguish from 北头摘 and 北头代N",
    },
    "kuwai_count": {
        "description": "库外N / 库外N摘",
        "conclusion": "count_can_be_inferred",
        "confidence": "high",
        "algorithm_instruction": "use effective_count=N while preserving original count_omitted for audit",
        "must_not_assume": "do not apply this to 北头代N or arbitrary numeric notes",
        "next_validation": "continue replay and car-number checks where available",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize structured uncertainty classes in cleaned manual labels.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--label-dir", default="artifacts/manual_label_cleaning_20260702")
    parser.add_argument("--replay-dir", default="artifacts/manual_identity_replay_20260702")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    label_rows = read_csv(root / args.label_dir / "manual_action_labels.csv")
    replay_rows = read_csv(root / args.replay_dir / "manual_identity_replay_trace.csv")
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = replay_rows if replay_rows else label_rows
    class_rows = build_class_rows(rows)
    summary_rows = build_summary_rows(class_rows)
    examples = build_examples(class_rows)
    write_csv(output_dir / "manual_uncertainty_class_assignments.csv", class_rows)
    write_csv(output_dir / "manual_uncertainty_class_summary.csv", summary_rows)
    write_csv(output_dir / "manual_uncertainty_evidence_examples.csv", examples)
    summary = build_json_summary(summary_rows)
    (output_dir / "manual_uncertainty_validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def build_class_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    class_rows: list[dict[str, Any]] = []
    for row in rows:
        for class_id in classify(row):
            info = CLASS_RULES[class_id]
            class_rows.append(
                {
                    "class_id": class_id,
                    "description": info["description"],
                    "case_id": row.get("case_id", ""),
                    "manual_hook": row.get("manual_hook", ""),
                    "manual_reference_scope": row.get("manual_reference_scope", ""),
                    "line_raw": row.get("line_raw", ""),
                    "resolved_line": row.get("resolved_line", ""),
                    "method": row.get("method", ""),
                    "count": row.get("count", ""),
                    "effective_count": row.get("effective_count", ""),
                    "note": row.get("note", ""),
                    "replay_status": row.get("replay_status", ""),
                    "state_count_before": row.get("state_count_before", ""),
                    "state_count_after": row.get("state_count_after", ""),
                    "candidate_count": row.get("candidate_count", ""),
                    "line_scope_counts": row.get("line_scope_counts", ""),
                    "endpoint_counts": row.get("endpoint_counts", ""),
                    "car_no_validation_counts": row.get("car_no_validation_counts", ""),
                    "moved_nos_if_unique": row.get("moved_nos_if_unique", ""),
                    "detail": row.get("detail", ""),
                    "compound_identity_replay_mode": row.get("compound_identity_replay_mode", ""),
                    "compound_algorithm_hint": row.get("compound_algorithm_hint", ""),
                    "car_no_values": "|".join(CAR_NO_PATTERN.findall(row.get("note", ""))),
                    "conclusion": info["conclusion"],
                    "confidence": info["confidence"],
                    "algorithm_instruction": info["algorithm_instruction"],
                    "must_not_assume": info["must_not_assume"],
                    "next_validation": info["next_validation"],
                }
            )
    return sorted(class_rows, key=lambda item: (item["class_id"], item["case_id"], int(item["manual_hook"] or 0)))


def classify(row: dict[str, str]) -> list[str]:
    note = row.get("note", "")
    classes: list[str] = []
    if row.get("line_raw") == "存2" and row.get("method") == "+" and "叉线" in note:
        classes.append("forkline_plus")
    if row.get("line_raw") == "存2" and row.get("method") == "-" and "叉线" in note:
        classes.append("forkline_minus")
    if row.get("compound_identity_replay_mode") == "structural_gap_storage5_push_reposition":
        classes.append("storage5_push_reposition")
    elif row.get("resolved_line") == "存5线北" and "北头" in note:
        classes.append("storage5_north_endpoint")
    if re.fullmatch(r"北头代\d+", note):
        classes.append("north_head_substitute")
    elif "代" in note or row.get("method") == "代":
        classes.append("substitute_note")
    if row.get("resolved_line") == "机库线" and ("南" in note or row.get("method") == "顶"):
        classes.append("machine_corridor_south")
    if row.get("replay_status") == "identity_noop" and row.get("detail") in {
        "loco_only_yard_move_no_vehicle_delta",
        "weighing_positioning_no_identity_delta",
        "loco_closeout_no_vehicle_delta",
        "initial_loco_departure_no_vehicle_delta",
    }:
        classes.append("machine_loco_noop")
    if row.get("replay_status") == "identity_noop" and row.get("detail") == "cun4_north_positioning_no_vehicle_delta":
        classes.append("storage4_north_blank")
    if row.get("count_resolution_status") == "inferred" and str(row.get("count_inference_reason", "")).startswith("kuwai_note_count"):
        classes.append("kuwai_count")
    return classes


def build_summary_rows(class_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in class_rows:
        grouped[row["class_id"]].append(row)
    summary_rows: list[dict[str, Any]] = []
    for class_id in sorted(grouped):
        rows = grouped[class_id]
        info = CLASS_RULES[class_id]
        statuses = Counter(str(row.get("replay_status") or "") for row in rows)
        validations = Counter()
        for row in rows:
            for item in str(row.get("car_no_validation_counts") or "").split("|"):
                if item:
                    key, _, value = item.partition(":")
                    validations[key] += int(value or 0)
        reference_rows = [row for row in rows if row.get("manual_reference_scope") != "exclude_from_algorithm_learning"]
        with_car_no = sum(1 for row in rows if row.get("car_no_values"))
        summary_rows.append(
            {
                "class_id": class_id,
                "description": info["description"],
                "hook_count": len(rows),
                "reference_hook_count": len(reference_rows),
                "with_car_no_note_count": with_car_no,
                "status_counts": counter_text(statuses),
                "car_no_validation_totals": counter_text(validations),
                "conclusion": info["conclusion"],
                "confidence": info["confidence"],
                "algorithm_instruction": info["algorithm_instruction"],
                "must_not_assume": info["must_not_assume"],
                "next_validation": info["next_validation"],
            }
        )
    return summary_rows


def build_examples(class_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in class_rows:
        grouped[row["class_id"]].append(row)
    examples: list[dict[str, Any]] = []
    preferred_statuses = [
        "replayed_unique",
        "replayed_ambiguous",
        "structural_gap",
        "identity_conflict",
        "state_space_exceeded",
        "identity_noop",
    ]
    for class_id in sorted(grouped):
        rows = grouped[class_id]
        used: set[tuple[str, str, str]] = set()
        for status in preferred_statuses:
            status_rows = [row for row in rows if row.get("replay_status") == status]
            for row in status_rows[:3]:
                key = (row["case_id"], row["manual_hook"], row["replay_status"])
                if key in used:
                    continue
                used.add(key)
                example = {
                    key_name: row.get(key_name, "")
                    for key_name in (
                        "class_id",
                        "case_id",
                        "manual_hook",
                        "line_raw",
                        "resolved_line",
                        "method",
                        "count",
                        "effective_count",
                        "note",
                        "replay_status",
                        "line_scope_counts",
                        "endpoint_counts",
                        "car_no_validation_counts",
                        "moved_nos_if_unique",
                        "detail",
                        "compound_algorithm_hint",
                        "algorithm_instruction",
                        "must_not_assume",
                    )
                }
                examples.append(example)
                if len([item for item in examples if item["class_id"] == class_id]) >= 8:
                    break
            if len([item for item in examples if item["class_id"] == class_id]) >= 8:
                break
    return examples


def build_json_summary(summary_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "class_count": len(summary_rows),
        "total_class_assignments": sum(int(row["hook_count"]) for row in summary_rows),
        "classes": summary_rows,
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


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


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{count}" for key, count in sorted(counter.items()))


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
