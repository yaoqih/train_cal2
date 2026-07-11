#!/usr/bin/env python3
"""Summarize the strict Stage1-4 truth3 solver chain.

The input and output location is intentionally fixed to:

    /root/train_cal2/artifacts/stage1-4_simple/truth3

Only per-case files named ``NNNN[WZ]_summary.json`` are consumed.  Solver
responses, traces, aggregate summaries, and source requests are never read or
modified.  The script writes ``stage_statistics.json`` and
``case_statistics.csv`` beside the stage directories.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


RESULT_ROOT = Path("/root/train_cal2/artifacts/stage1-4_simple/truth3")
STAGES = ("stage1", "stage2", "stage3", "stage4")
CASE_SUMMARY_RE = re.compile(r"^(?P<case_id>[0-9]{4}[WZ])_summary\.json$")
TIME_FILES = {
    # Stage3 was deliberately split after 16 cases so each remaining case ran
    # in a fresh process and released its high-water search cache.  The two
    # outer GNU-time measurements are additive and together cover all work.
    "stage3": ("stage3_time.txt", "stage3_remaining_time.txt"),
}

# These summaries are emitted when a stage is deliberately skipped because
# the preceding stage (or one of its required artifacts) is unavailable.
UPSTREAM_REASON_CODES = {
    "stage2": {"stage1_response_missing", "stage1_not_complete"},
    "stage3": {"stage2_combined_response_missing", "stage2_not_complete"},
    "stage4": {
        "stage3_summary_missing",
        "stage3_not_complete",
        "stage3_artifact_missing",
    },
}


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_reason(value: Any) -> str:
    """Turn heterogeneous reason values into stable, one-line text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.splitlines()).strip()
    if isinstance(value, dict):
        kind = value.get("kind")
        code = value.get("code")
        detail = value.get("detail")
        if kind is not None or code is not None or detail is not None:
            return ":".join(str(part) for part in (kind, code, detail) if part is not None)
        return compact_json(value)
    if isinstance(value, (list, tuple)):
        return compact_json(value)
    return str(value)


def collect_reasons(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return []
    values: list[Any] = []
    blocking = data.get("blocking_reasons")
    if isinstance(blocking, list):
        values.extend(blocking)
    elif blocking is not None:
        values.append(blocking)
    for key in ("error", "message", "reason", "exception"):
        if data.get(key) not in (None, ""):
            values.append(data[key])

    reasons: list[str] = []
    seen: set[str] = set()
    for value in values:
        reason = normalize_reason(value)
        if reason and reason not in seen:
            reasons.append(reason)
            seen.add(reason)
    return reasons


def reason_code(reason: str) -> str:
    if reason.startswith("{") or reason.startswith("["):
        return "structured_reason"
    return reason.split(":", 1)[0] if reason else "unspecified"


def finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(float(number)):
        return None
    if float(number).is_integer():
        return int(number)
    return float(number)


def hook_count(data: dict[str, Any] | None) -> int | None:
    if not data:
        return None
    value = finite_number(data.get("business_hooks"))
    if value is None or value < 0 or not float(value).is_integer():
        return None
    return int(value)


def elapsed_seconds(data: dict[str, Any] | None) -> float | None:
    if not data:
        return None
    value = finite_number(data.get("elapsed_seconds"))
    if value is None or value < 0:
        return None
    return float(value)


def stage4_portfolio_elapsed_seconds(data: dict[str, Any] | None) -> float | None:
    """Return total Stage4 portfolio time when evaluations are available."""

    if not data:
        return None
    evaluations = data.get("stage4_portfolio_evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        return None
    values: list[float] = []
    for item in evaluations:
        if not isinstance(item, dict):
            continue
        value = finite_number(item.get("elapsed_seconds"))
        if value is not None and value >= 0:
            values.append(float(value))
    return round(sum(values), 6) if values else None


def load_stage_summaries(stage: str) -> dict[str, dict[str, Any]]:
    """Load matching summaries; malformed files become diagnosable records."""

    stage_dir = RESULT_ROOT / stage
    records: dict[str, dict[str, Any]] = {}
    if not stage_dir.is_dir():
        return records

    for path in sorted(stage_dir.iterdir()):
        match = CASE_SUMMARY_RE.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        case_id = match.group("case_id")
        record: dict[str, Any] = {
            "path": path,
            "data": None,
            "load_error": None,
        }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level JSON value is not an object")
            payload_case_id = payload.get("case_id")
            if payload_case_id not in (None, case_id):
                raise ValueError(
                    f"case_id mismatch: filename={case_id}, payload={payload_case_id}"
                )
            record["data"] = payload
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            record["load_error"] = f"summary_load_error:{type(exc).__name__}:{exc}"
        records[case_id] = record
    return records


def strict_complete_issues(stage: str, data: dict[str, Any]) -> list[str]:
    """Apply replay checks that are stricter than a raw status comparison."""

    if data.get("status") != "complete":
        return []
    issues: list[str] = []
    if hook_count(data) is None:
        issues.append("business_hooks_missing_or_invalid")
    if stage == "stage3" and data.get("combined_replay_physical_ok") is not True:
        issues.append("strict_combined_replay_physical_ok:false_or_missing")
    if stage == "stage4":
        if data.get("replay_physical_ok") is not True:
            issues.append("strict_replay_physical_ok:false_or_missing")
        if data.get("combined_replay_physical_ok") is not True:
            issues.append("strict_combined_replay_physical_ok:false_or_missing")
        final_unsatisfied = finite_number(data.get("final_unsatisfied_count"))
        if "final_unsatisfied_count" in data and final_unsatisfied is None:
            issues.append("strict_final_unsatisfied_count:invalid")
        elif final_unsatisfied is not None and final_unsatisfied != 0:
            issues.append(f"strict_final_unsatisfied_count:{final_unsatisfied}")
    return issues


def has_upstream_reason(stage: str, reasons: Iterable[str]) -> bool:
    expected = UPSTREAM_REASON_CODES.get(stage, set())
    return any(reason_code(reason) in expected for reason in reasons)


def has_solver_exception(reasons: Iterable[str]) -> bool:
    return any(reason_code(reason) == "solver_exception" for reason in reasons)


def classify_case_stage(
    stage: str,
    case_id: str,
    loaded: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    data = loaded.get("data") if loaded else None
    load_error = loaded.get("load_error") if loaded else None
    raw_status = data.get("status") if data else None
    reasons = collect_reasons(data)

    if stage != "stage1" and previous and previous["classification"] != "complete":
        classification = "upstream_unavailable"
        reasons.insert(
            0,
            f"upstream_{previous['stage']}:{previous['classification']}",
        )
    elif has_upstream_reason(stage, reasons):
        classification = "upstream_unavailable"
    elif loaded is None:
        classification = "error"
        reasons.append("summary_missing")
    elif load_error:
        classification = "error"
        reasons.append(load_error)
    elif raw_status == "error" or has_solver_exception(reasons):
        classification = "error"
    elif raw_status == "complete":
        strict_issues = strict_complete_issues(stage, data)
        if strict_issues:
            classification = "partial"
            reasons.extend(strict_issues)
        else:
            classification = "complete"
    elif raw_status == "partial":
        classification = "partial"
    else:
        classification = "error"
        reasons.append(f"unknown_or_missing_status:{raw_status}")

    if classification != "complete" and not reasons:
        reasons.append(f"status:{raw_status or classification}")

    # Preserve order while removing duplicate synthetic/raw reasons.
    unique_reasons = list(dict.fromkeys(reasons))
    portfolio_elapsed = (
        stage4_portfolio_elapsed_seconds(data) if stage == "stage4" else None
    )
    return {
        "case_id": case_id,
        "stage": stage,
        "classification": classification,
        "solvable": classification == "complete",
        "raw_status": raw_status,
        "business_hooks": hook_count(data),
        "elapsed_seconds": elapsed_seconds(data),
        "portfolio_elapsed_seconds": portfolio_elapsed,
        "replay_physical_ok": data.get("replay_physical_ok") if data else None,
        "combined_replay_physical_ok": (
            data.get("combined_replay_physical_ok") if data else None
        ),
        "reasons": unique_reasons,
        "summary_file": str(loaded["path"]) if loaded else None,
    }


def parse_wall_clock(value: str) -> float | None:
    try:
        fields = value.strip().split(":")
        if len(fields) == 3:
            hours, minutes, seconds = fields
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(fields) == 2:
            minutes, seconds = fields
            return int(minutes) * 60 + float(seconds)
        if len(fields) == 1:
            return float(fields[0])
    except (TypeError, ValueError):
        return None
    return None


def format_wall_clock(seconds: float) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"
    return f"{int(minutes)}:{secs:05.2f}"


def parse_single_time_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file": str(path),
        "exists": path.is_file(),
        "command": None,
        "user_seconds": None,
        "system_seconds": None,
        "wall_clock": None,
        "wall_clock_seconds": None,
        "exit_status": None,
        "terminated_by_signal": None,
        "measurement_complete": False,
    }
    if not path.is_file():
        return result
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["read_error"] = f"{type(exc).__name__}:{exc}"
        return result

    patterns = {
        "command": re.compile(r'^\s*Command being timed:\s*"?(.*?)"?\s*$'),
        "user_seconds": re.compile(r"^\s*User time \(seconds\):\s*(\S+)\s*$"),
        "system_seconds": re.compile(r"^\s*System time \(seconds\):\s*(\S+)\s*$"),
        "wall_clock": re.compile(
            r"^\s*Elapsed \(wall clock\) time \(h:mm:ss or m:ss\):\s*(\S+)\s*$"
        ),
        "exit_status": re.compile(r"^\s*Exit status:\s*(-?[0-9]+)\s*$"),
        "terminated_by_signal": re.compile(
            r"^\s*Command terminated by signal\s+([0-9]+)\s*$"
        ),
    }
    for line in text.splitlines():
        for key, pattern in patterns.items():
            match = pattern.match(line)
            if not match:
                continue
            raw = match.group(1)
            if key in {"user_seconds", "system_seconds"}:
                result[key] = finite_number(raw)
            elif key in {"exit_status", "terminated_by_signal"}:
                result[key] = int(raw)
            else:
                result[key] = raw
            break
    if result["wall_clock"] is not None:
        result["wall_clock_seconds"] = parse_wall_clock(result["wall_clock"])
    result["measurement_complete"] = (
        result["wall_clock_seconds"] is not None and result["exit_status"] is not None
    )
    return result


def parse_time_file(stage: str) -> dict[str, Any]:
    names = TIME_FILES.get(stage, (f"{stage}_time.txt",))
    segments = [parse_single_time_file(RESULT_ROOT / name) for name in names]
    if len(segments) == 1:
        result = dict(segments[0])
        result["files"] = [segments[0]["file"]]
        result["segments"] = segments
        return result

    wall_values = [segment["wall_clock_seconds"] for segment in segments]
    user_values = [segment["user_seconds"] for segment in segments]
    system_values = [segment["system_seconds"] for segment in segments]
    wall_total = (
        sum(float(value) for value in wall_values)
        if all(value is not None for value in wall_values)
        else None
    )
    return {
        "file": None,
        "files": [segment["file"] for segment in segments],
        "exists": all(segment["exists"] for segment in segments),
        "command": "segmented_stage_run",
        "user_seconds": (
            rounded(sum(float(value) for value in user_values))
            if all(value is not None for value in user_values)
            else None
        ),
        "system_seconds": (
            rounded(sum(float(value) for value in system_values))
            if all(value is not None for value in system_values)
            else None
        ),
        "wall_clock": format_wall_clock(wall_total) if wall_total is not None else None,
        "wall_clock_seconds": rounded(wall_total) if wall_total is not None else None,
        "exit_status": 0 if all(segment["exit_status"] == 0 for segment in segments) else None,
        "terminated_by_signal": [
            segment["terminated_by_signal"]
            for segment in segments
            if segment["terminated_by_signal"] is not None
        ],
        "measurement_complete": all(
            segment["measurement_complete"] for segment in segments
        ),
        "segments": segments,
        "note": "sum of sequential outer GNU-time segments; first segment was intentionally stopped after a case boundary to release Stage3 cache memory",
    }


def rounded(value: float) -> float:
    return round(value, 6)


def numeric_statistics(values: Iterable[int | float | None], population: int) -> dict[str, Any]:
    observed = [float(value) for value in values if value is not None]
    if not observed:
        return {
            "value_count": 0,
            "missing_count": population,
            "total": 0,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
        }
    total = sum(observed)
    return {
        "value_count": len(observed),
        "missing_count": population - len(observed),
        "total": rounded(total),
        "mean": rounded(statistics.fmean(observed)),
        "median": rounded(statistics.median(observed)),
        "minimum": rounded(min(observed)),
        "maximum": rounded(max(observed)),
    }


def stage_statistics(stage: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(record["classification"] for record in records)
    for name in ("complete", "partial", "error", "upstream_unavailable"):
        counts.setdefault(name, 0)
    raw_status_counts = Counter(str(record["raw_status"] or "missing") for record in records)
    eligible = len(records) - counts["upstream_unavailable"]

    hook_stats: dict[str, Any] = {
        "definition": (
            "this_stage_operation_rows_get_put_weigh"
            if stage == "stage4"
            else "this_stage_operation_rows_get_put"
        ),
        "all_cases": numeric_statistics(
            (record["business_hooks"] for record in records), len(records)
        ),
    }
    elapsed_stats: dict[str, Any] = {
        "all_cases": numeric_statistics(
            (record["elapsed_seconds"] for record in records), len(records)
        )
    }
    for classification in ("complete", "partial", "error", "upstream_unavailable"):
        subset = [
            record for record in records if record["classification"] == classification
        ]
        hook_stats[classification] = numeric_statistics(
            (record["business_hooks"] for record in subset), len(subset)
        )
        elapsed_stats[classification] = numeric_statistics(
            (record["elapsed_seconds"] for record in subset), len(subset)
        )

    result: dict[str, Any] = {
        "case_count": len(records),
        "summary_file_count": sum(record["summary_file"] is not None for record in records),
        "counts": dict(counts),
        "raw_status_counts": dict(sorted(raw_status_counts.items())),
        "eligible_case_count": eligible,
        "strict_solvable_count": counts["complete"],
        "strict_solvability_rate_all_cases": (
            rounded(counts["complete"] / len(records)) if records else None
        ),
        "strict_solvability_rate_eligible_cases": (
            rounded(counts["complete"] / eligible) if eligible else None
        ),
        "business_hooks": hook_stats,
        "reported_elapsed_seconds": elapsed_stats,
        "outer_process_timing": parse_time_file(stage),
        "complete_case_ids": [
            record["case_id"]
            for record in records
            if record["classification"] == "complete"
        ],
        "non_complete_cases": [
            {
                "case_id": record["case_id"],
                "classification": record["classification"],
                "raw_status": record["raw_status"],
                "business_hooks": record["business_hooks"],
                "elapsed_seconds": record["elapsed_seconds"],
                "reasons": record["reasons"],
            }
            for record in records
            if record["classification"] != "complete"
        ],
        "non_complete_reason_codes": dict(
            Counter(
                reason_code(reason)
                for record in records
                if record["classification"] != "complete"
                for reason in record["reasons"]
            ).most_common()
        ),
    }
    if stage == "stage4":
        portfolio_stats: dict[str, Any] = {
            "all_cases": numeric_statistics(
                (record["portfolio_elapsed_seconds"] for record in records),
                len(records),
            )
        }
        for classification in (
            "complete",
            "partial",
            "error",
            "upstream_unavailable",
        ):
            subset = [
                record
                for record in records
                if record["classification"] == classification
            ]
            portfolio_stats[classification] = numeric_statistics(
                (record["portfolio_elapsed_seconds"] for record in subset),
                len(subset),
            )
        result["portfolio_elapsed_seconds"] = portfolio_stats
    return result


def build_case_rows(
    case_ids: list[str],
    loaded_by_stage: dict[str, dict[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows: list[dict[str, Any]] = []
    stage_records: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}

    for case_id in case_ids:
        row: dict[str, Any] = {"case_id": case_id}
        previous: dict[str, Any] | None = None
        executed_hook_total = 0
        observed_hook_stage_count = 0
        all_hook_values_present = True
        consecutive_complete = 0

        for stage in STAGES:
            record = classify_case_stage(
                stage,
                case_id,
                loaded_by_stage[stage].get(case_id),
                previous,
            )
            stage_records[stage].append(record)
            prefix = stage
            row[f"{prefix}_classification"] = record["classification"]
            row[f"{prefix}_solvable"] = record["solvable"]
            row[f"{prefix}_raw_status"] = record["raw_status"]
            row[f"{prefix}_business_hooks"] = record["business_hooks"]
            row[f"{prefix}_elapsed_seconds"] = record["elapsed_seconds"]
            row[f"{prefix}_portfolio_elapsed_seconds"] = record[
                "portfolio_elapsed_seconds"
            ]
            row[f"{prefix}_replay_physical_ok"] = record["replay_physical_ok"]
            row[f"{prefix}_combined_replay_physical_ok"] = record[
                "combined_replay_physical_ok"
            ]
            row[f"{prefix}_reason"] = " | ".join(record["reasons"])

            hooks = record["business_hooks"]
            if hooks is None:
                all_hook_values_present = False
            else:
                executed_hook_total += hooks
                observed_hook_stage_count += 1
            if (
                record["classification"] == "complete"
                and consecutive_complete == STAGES.index(stage)
            ):
                consecutive_complete += 1
            previous = record

        all_complete = consecutive_complete == len(STAGES)
        row["highest_strict_complete_stage"] = (
            STAGES[consecutive_complete - 1] if consecutive_complete else "none"
        )
        row["all_4_stages_solvable"] = all_complete
        row["business_hooks_observed_stage_count"] = observed_hook_stage_count
        row["executed_business_hooks_total"] = executed_hook_total
        row["complete_chain_business_hooks_total"] = (
            executed_hook_total if all_complete and all_hook_values_present else None
        )
        rows.append(row)
    return rows, stage_records


def csv_fieldnames() -> list[str]:
    fields = ["case_id"]
    for stage in STAGES:
        fields.extend(
            [
                f"{stage}_classification",
                f"{stage}_solvable",
                f"{stage}_raw_status",
                f"{stage}_business_hooks",
                f"{stage}_elapsed_seconds",
                f"{stage}_portfolio_elapsed_seconds",
                f"{stage}_replay_physical_ok",
                f"{stage}_combined_replay_physical_ok",
                f"{stage}_reason",
            ]
        )
    fields.extend(
        [
            "highest_strict_complete_stage",
            "all_4_stages_solvable",
            "business_hooks_observed_stage_count",
            "executed_business_hooks_total",
            "complete_chain_business_hooks_total",
        ]
    )
    return fields


def atomic_write_text(path: Path, text: str, encoding: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding=encoding)
    temporary.replace(path)


def write_outputs(rows: list[dict[str, Any]], stages: dict[str, Any]) -> None:
    json_path = RESULT_ROOT / "stage_statistics.json"
    csv_path = RESULT_ROOT / "case_statistics.csv"
    final_complete = sum(bool(row["all_4_stages_solvable"]) for row in rows)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "result_root": str(RESULT_ROOT),
        "case_count": len(rows),
        "strict_chain_complete_count": final_complete,
        "strict_chain_solvability_rate": (
            rounded(final_complete / len(rows)) if rows else None
        ),
        "classification_definition": {
            "complete": "raw stage completion plus the stage-specific strict replay checks",
            "partial": "stage ran but did not satisfy strict completion",
            "error": "solver/load/missing-summary error while its upstream chain was available",
            "upstream_unavailable": "the preceding strict stage or its required artifact was unavailable",
        },
        "timing_definition": {
            "outer_process_timing": "authoritative GNU time -v wall clock; sequential process-isolation segments are summed",
            "reported_elapsed_seconds": "per-case internal elapsed_seconds copied from each summary when present",
            "stage4_portfolio_elapsed_seconds": "sum of Stage4 portfolio evaluation elapsed_seconds when present",
        },
        "stages": stages,
    }
    atomic_write_text(
        json_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=csv_fieldnames(), extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    # UTF-8 with BOM is intentional so the Chinese reason text opens cleanly in Excel.
    atomic_write_text(csv_path, stream.getvalue(), "utf-8-sig")


def write_recomputed_solver_aggregates(
    loaded_by_stage: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Restore batch-style aggregates after memory-isolated per-case runs."""

    stage3_summaries = [
        record["data"]
        for _, record in sorted(loaded_by_stage["stage3"].items())
        if isinstance(record.get("data"), dict)
    ]
    stage3_complete = [
        item for item in stage3_summaries if item.get("status") == "complete"
    ]
    stage3_ops = [int(item.get("operations") or 0) for item in stage3_complete]
    stage3_aggregate = {
        "cases": len(stage3_summaries),
        "complete": len(stage3_complete),
        "partial": len(stage3_summaries) - len(stage3_complete),
        "avg_operations_complete": (
            round(sum(stage3_ops) / len(stage3_ops), 3) if stage3_ops else 0
        ),
        "max_operations_complete": max(stage3_ops) if stage3_ops else 0,
        "templates_complete": dict(
            sorted(Counter(item.get("template") for item in stage3_complete).items())
        ),
        "partial_reasons": dict(
            Counter(
                str(reason).split(":", 1)[0]
                for item in stage3_summaries
                if item.get("status") != "complete"
                for reason in item.get("blocking_reasons") or []
            ).most_common()
        ),
        "summaries": stage3_summaries,
    }

    stage4_summaries = [
        record["data"]
        for _, record in sorted(loaded_by_stage["stage4"].items())
        if isinstance(record.get("data"), dict)
    ]
    stage4_complete = [
        item for item in stage4_summaries if item.get("status") == "complete"
    ]
    stage4_aggregate = {
        "cases": len(stage4_summaries),
        "complete": len(stage4_complete),
        "feasible_unproved": sum(
            1
            for item in stage4_summaries
            if item.get("optimality") == "feasible_unproved"
        ),
        "partial": sum(
            1 for item in stage4_summaries if item.get("status") == "partial"
        ),
        "avg_operations_complete": round(
            sum(int(item.get("operations") or 0) for item in stage4_complete)
            / max(1, len(stage4_complete)),
            3,
        ),
        "avg_business_hooks_complete": round(
            sum(int(item.get("business_hooks") or 0) for item in stage4_complete)
            / max(1, len(stage4_complete)),
            3,
        ),
        "partial_reasons": dict(
            Counter(
                str(reason).split(":", 1)[0]
                for item in stage4_summaries
                if item.get("status") != "complete"
                for reason in (item.get("blocking_reasons") or [])[:3]
            )
        ),
        "summaries": stage4_summaries,
    }

    for stage, aggregate in (
        ("stage3", stage3_aggregate),
        ("stage4", stage4_aggregate),
    ):
        atomic_write_text(
            RESULT_ROOT / stage / "aggregate_summary.json",
            json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n",
            "utf-8",
        )


def main() -> int:
    if not RESULT_ROOT.is_dir():
        raise SystemExit(f"result root does not exist: {RESULT_ROOT}")
    loaded_by_stage = {stage: load_stage_summaries(stage) for stage in STAGES}
    case_ids = sorted(
        {
            case_id
            for summaries in loaded_by_stage.values()
            for case_id in summaries
        }
    )
    if not case_ids:
        raise SystemExit(
            f"no per-case summaries matching {CASE_SUMMARY_RE.pattern!r} under {RESULT_ROOT}"
        )

    rows, records_by_stage = build_case_rows(case_ids, loaded_by_stage)
    stages = {
        stage: stage_statistics(stage, records_by_stage[stage]) for stage in STAGES
    }
    write_outputs(rows, stages)
    write_recomputed_solver_aggregates(loaded_by_stage)

    for stage in STAGES:
        counts = stages[stage]["counts"]
        timing = stages[stage]["outer_process_timing"]
        print(
            f"{stage}: complete={counts['complete']} partial={counts['partial']} "
            f"error={counts['error']} upstream_unavailable={counts['upstream_unavailable']} "
            f"wall={timing['wall_clock'] or 'unavailable'}"
        )
    print(f"wrote {RESULT_ROOT / 'stage_statistics.json'}")
    print(f"wrote {RESULT_ROOT / 'case_statistics.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
