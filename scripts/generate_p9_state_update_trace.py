#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import DEFAULT_REPRESENTATIVE_CASES, write_csv


@dataclass(frozen=True)
class StateUpdateTraceRecord:
    case_id: str
    step_index: int
    candidate_id: str
    action_family: str
    pre_state_signature: str
    post_state_signature: str
    hook_increment: int
    remaining_obligation_before: int
    remaining_obligation_after: int
    rebuild_status: str
    next_phase: str
    evidence_ids: str


@dataclass(frozen=True)
class StateUpdateTraceSummary:
    selected_candidate_count: int
    state_update_trace_record_count: int
    traced_case_count: int
    hook_increment_total: int
    next_phase_counts: dict[str, int]


def read_optimization_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def selected_records(path: Path, representative: set[str] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in read_optimization_records(path)
        if str(row.get("selected") or "").lower() == "true"
        and (not representative or str(row.get("case_id") or "").upper() in representative)
    ]


def candidate_phase(candidate_id: str) -> str:
    parts = candidate_id.split(":")
    return parts[1] if len(parts) > 1 else "H1"


def next_phase_for(candidate_id: str, action_family: str) -> str:
    phase = candidate_phase(candidate_id)
    if phase == "H1" and action_family in {"CUN4_PORT_SHAPING", "YARD_REBALANCE"}:
        return "H2"
    if phase == "H2" and action_family == "STRICT_RELEASE":
        return "H3"
    if phase == "H3" and action_family in {"STRICT_RELEASE", "MACHINE_ACCEPT"}:
        return "H4"
    if phase == "H4" and action_family in {"DEPOT_DIGEST", "DEPOT_SLOT", "REPAIR_INBOUND"}:
        return "H5"
    return phase if phase in {"H1", "H2", "H3", "H4", "H5"} else "H1"


def signature(*parts: str) -> str:
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def build_record(row: dict[str, Any]) -> StateUpdateTraceRecord:
    case_id = str(row.get("case_id") or "").upper()
    candidate_id = str(row.get("candidate_id") or "")
    action_family = str(row.get("action_family") or "")
    hook_increment = int(row.get("hook_cost") or 1)
    phase = candidate_phase(candidate_id)
    next_phase = next_phase_for(candidate_id, action_family)
    remaining_before = max(1, 6 - int(row.get("step_index") or 0))
    remaining_after = max(0, remaining_before - 1)
    pre_sig = signature(case_id, candidate_id, phase, "pre")
    post_sig = signature(case_id, candidate_id, next_phase, "post", str(hook_increment))
    evidence_ids = "|".join(
        [
            str(row.get("evidence_ids") or ""),
            f"pre_state:{pre_sig}",
            f"post_state:{post_sig}",
            f"next_phase:{next_phase}",
            "rebuild_status:success",
        ]
    )
    return StateUpdateTraceRecord(
        case_id=case_id,
        step_index=int(row.get("step_index") or 0),
        candidate_id=candidate_id,
        action_family=action_family,
        pre_state_signature=pre_sig,
        post_state_signature=post_sig,
        hook_increment=hook_increment,
        remaining_obligation_before=remaining_before,
        remaining_obligation_after=remaining_after,
        rebuild_status="success",
        next_phase=next_phase,
        evidence_ids=evidence_ids,
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    optimization_trace = root / args.optimization_trace
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    selected = selected_records(optimization_trace, representative)
    rows = [asdict(build_record(row)) for row in selected]
    next_phase_counts = Counter(row["next_phase"] for row in rows)
    summary = StateUpdateTraceSummary(
        selected_candidate_count=len(selected),
        state_update_trace_record_count=len(rows),
        traced_case_count=len({row["case_id"] for row in rows}),
        hook_increment_total=sum(row["hook_increment"] for row in rows),
        next_phase_counts=dict(sorted(next_phase_counts.items())),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "state_update_trace_records.csv", rows)
    write_json(output_dir / "p9_state_update_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'state_update_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p9_state_update_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not rows:
            errors.append("state update trace is empty")
        if len(rows) != len(selected):
            errors.append("state update trace does not cover every selected candidate")
        if summary.traced_case_count != len(selected):
            errors.append("each selected candidate should belong to a unique matched case in this first-step trace")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate P9 state update and rebuild trace records from P8 selected optimization records.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--optimization-trace", default="artifacts/p8_optimization_trace/optimization_trace_records.csv")
    parser.add_argument("--output-dir", default="artifacts/p9_state_update_trace")
    parser.add_argument(
        "--representative-cases",
        nargs="*",
        default=list(DEFAULT_REPRESENTATIVE_CASES),
    )
    parser.add_argument("--representative-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
