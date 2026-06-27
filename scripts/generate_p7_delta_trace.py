#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import DEFAULT_REPRESENTATIVE_CASES, write_csv


@dataclass(frozen=True)
class DeltaTraceRecord:
    case_id: str
    step_index: int
    candidate_id: str
    source_contract: str
    action_family: str
    contract_delta: str
    resource_delta: str
    hard_violation_count: int
    hard_gate_reason: str
    gate_decision: str
    evidence_ids: str


@dataclass(frozen=True)
class DeltaTraceSummary:
    resource_trace_record_count: int
    delta_trace_record_count: int
    traced_case_count: int
    gate_decision_counts: dict[str, int]
    hard_violation_count: int


def read_resource_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def build_contract_delta(record: dict[str, Any]) -> str:
    action_family = str(record.get("action_family") or "")
    if action_family == "STRICT_RELEASE":
        return "advance_release_boundary"
    if action_family == "MACHINE_ACCEPT":
        return "activate_machine_accept_protection"
    if action_family == "DEPOT_DIGEST":
        return "reduce_depot_digest_obligation"
    if action_family == "DEPOT_SLOT":
        return "reserve_or_validate_depot_slot"
    if action_family == "REPAIR_INBOUND":
        return "advance_repair_inbound_obligation"
    if action_family == "TAIL_CLOSEOUT":
        return "close_tail_obligation"
    return f"advance_{action_family.lower()}_contract"


def build_resource_delta(record: dict[str, Any]) -> str:
    status = str(record.get("resource_status") or "")
    request = str(record.get("resource_request") or "")
    if status == "available":
        return f"consume:{request}"
    if status == "constrained":
        return f"reserve_with_constraint:{request}"
    if status == "waiting":
        return f"wait_for:{request}"
    if status == "blocked":
        return f"blocked:{request}"
    return "unknown_resource_delta"


def gate_for_resource(record: dict[str, Any]) -> tuple[str, int, str]:
    status = str(record.get("resource_status") or "")
    if status in {"available", "constrained"}:
        return "accept", 0, "resource status allows delta hard-gate evaluation"
    if status == "waiting":
        return "defer", 0, "resource waiting requires later rebuild before acceptance"
    if status == "blocked":
        return "reject", 1, "resource blocker prevents hard-gate acceptance"
    return "reject", 1, "unknown resource status cannot pass hard gate"


def build_record(resource: dict[str, Any]) -> DeltaTraceRecord:
    decision, violation_count, reason = gate_for_resource(resource)
    evidence_ids = "|".join(
        [
            str(resource.get("evidence_ids") or ""),
            f"contract_delta:{build_contract_delta(resource)}",
            f"resource_delta:{build_resource_delta(resource)}",
            f"gate_decision:{decision}",
        ]
    )
    return DeltaTraceRecord(
        case_id=str(resource.get("case_id") or "").upper(),
        step_index=int(resource.get("step_index") or 0),
        candidate_id=str(resource.get("candidate_id") or ""),
        source_contract=str(resource.get("source_contract") or ""),
        action_family=str(resource.get("action_family") or ""),
        contract_delta=build_contract_delta(resource),
        resource_delta=build_resource_delta(resource),
        hard_violation_count=violation_count,
        hard_gate_reason=reason,
        gate_decision=decision,
        evidence_ids=evidence_ids,
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    resource_trace = root / args.resource_trace
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    resource_rows = [
        row
        for row in read_resource_records(resource_trace)
        if not representative or str(row.get("case_id") or "").upper() in representative
    ]
    rows = [asdict(build_record(row)) for row in resource_rows]
    decision_counts = Counter(row["gate_decision"] for row in rows)
    summary = DeltaTraceSummary(
        resource_trace_record_count=len(resource_rows),
        delta_trace_record_count=len(rows),
        traced_case_count=len({row["case_id"] for row in rows}),
        gate_decision_counts=dict(sorted(decision_counts.items())),
        hard_violation_count=sum(int(row["hard_violation_count"]) for row in rows),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "delta_trace_records.csv", rows)
    write_json(output_dir / "p7_delta_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'delta_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p7_delta_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not rows:
            errors.append("delta trace is empty")
        if len(rows) != len(resource_rows):
            errors.append("delta trace does not cover every resource record")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate P7 contract/resource delta and hard-gate trace records from P6 resource trace records.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--resource-trace", default="artifacts/p6_resource_trace/resource_trace_records.csv")
    parser.add_argument("--output-dir", default="artifacts/p7_delta_trace")
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
