#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import DEFAULT_REPRESENTATIVE_CASES, write_csv


HOOK_COST_BY_ACTION = {
    "STRICT_RELEASE": 1,
    "MACHINE_ACCEPT": 1,
    "DEPOT_DIGEST": 1,
    "DEPOT_SLOT": 1,
    "REPAIR_INBOUND": 2,
    "CUN4_PORT_SHAPING": 2,
    "TAIL_CLOSEOUT": 2,
    "YARD_REBALANCE": 3,
    "PRE_REPAIR_STAGING": 3,
    "DISPATCH_SHED_QUEUE": 3,
    "FUNCTION_LINE_SERVICE": 3,
    "LOCO_AREA_STAGING": 3,
    "DEPOT_OUTBOUND": 3,
    "SPECIAL_REPAIR_PROCESS": 3,
}

PHASE_PRIORITY = {"H3": 0, "H4": 1, "H2": 2, "H1": 3, "H5": 4}


@dataclass(frozen=True)
class OptimizationTraceRecord:
    case_id: str
    step_index: int
    candidate_id: str
    source_contract: str
    action_family: str
    hook_cost: int
    rank: int
    selected: bool
    why_ranked: str
    evidence_ids: str


@dataclass(frozen=True)
class OptimizationTraceSummary:
    delta_trace_record_count: int
    optimization_trace_record_count: int
    traced_case_count: int
    selected_candidate_count: int
    selected_hook_cost_counts: dict[str, int]


def read_delta_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def candidate_phase(candidate_id: str) -> str:
    parts = candidate_id.split(":")
    return parts[1] if len(parts) > 1 else ""


def hook_cost(record: dict[str, Any]) -> int:
    action_family = str(record.get("action_family") or "")
    base = HOOK_COST_BY_ACTION.get(action_family, 4)
    resource_delta = str(record.get("resource_delta") or "")
    if resource_delta.startswith("reserve_with_constraint:"):
        return base + 1
    if resource_delta.startswith("wait_for:") or resource_delta.startswith("blocked:"):
        return base + 3
    return base


def sort_key(record: dict[str, Any]) -> tuple[int, int, str]:
    phase = candidate_phase(str(record.get("candidate_id") or ""))
    return (
        hook_cost(record),
        PHASE_PRIORITY.get(phase, 9),
        str(record.get("candidate_id") or ""),
    )


def build_record(record: dict[str, Any], rank: int, selected: bool) -> OptimizationTraceRecord:
    cost = hook_cost(record)
    phase = candidate_phase(str(record.get("candidate_id") or ""))
    why_ranked = (
        f"hook_cost={cost}; phase_priority={PHASE_PRIORITY.get(phase, 9)}; "
        "local sort by hook cost, phase priority, candidate id"
    )
    evidence_ids = "|".join(
        [
            str(record.get("evidence_ids") or ""),
            f"hook_cost:{cost}",
            f"rank:{rank}",
            f"selected:{str(selected).lower()}",
        ]
    )
    return OptimizationTraceRecord(
        case_id=str(record.get("case_id") or "").upper(),
        step_index=int(record.get("step_index") or 0),
        candidate_id=str(record.get("candidate_id") or ""),
        source_contract=str(record.get("source_contract") or ""),
        action_family=str(record.get("action_family") or ""),
        hook_cost=cost,
        rank=rank,
        selected=selected,
        why_ranked=why_ranked,
        evidence_ids=evidence_ids,
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    delta_trace = root / args.delta_trace
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    accepted_rows = [
        row
        for row in read_delta_records(delta_trace)
        if str(row.get("gate_decision") or "").lower() == "accept"
        and (not representative or str(row.get("case_id") or "").upper() in representative)
    ]
    rows_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted_rows:
        rows_by_case[str(row.get("case_id") or "").upper()].append(row)

    output_rows: list[dict[str, Any]] = []
    for _case_id, case_rows in sorted(rows_by_case.items()):
        for rank, row in enumerate(sorted(case_rows, key=sort_key), start=1):
            output_rows.append(asdict(build_record(row, rank, rank == 1)))

    selected_cost_counts = Counter(
        str(row["hook_cost"]) for row in output_rows if row["selected"]
    )
    summary = OptimizationTraceSummary(
        delta_trace_record_count=len(accepted_rows),
        optimization_trace_record_count=len(output_rows),
        traced_case_count=len(rows_by_case),
        selected_candidate_count=sum(1 for row in output_rows if row["selected"]),
        selected_hook_cost_counts=dict(sorted(selected_cost_counts.items())),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "optimization_trace_records.csv", output_rows)
    write_json(output_dir / "p8_optimization_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'optimization_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p8_optimization_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not output_rows:
            errors.append("optimization trace is empty")
        if len(output_rows) != len(accepted_rows):
            errors.append("optimization trace does not cover every accepted delta")
        if summary.selected_candidate_count != summary.traced_case_count:
            errors.append("each traced case must have exactly one selected candidate")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate P8 local hook optimization trace records from P7 accepted delta records.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--delta-trace", default="artifacts/p7_delta_trace/delta_trace_records.csv")
    parser.add_argument("--output-dir", default="artifacts/p8_optimization_trace")
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
