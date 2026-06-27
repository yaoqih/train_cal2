#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import (
    DEFAULT_REPRESENTATIVE_CASES,
    case_id_from_path,
    current_line,
    load_truth_case,
    try_case_id_from_path,
    write_csv,
)


RESOURCE_SCOPE_BY_REQUEST = {
    "yard_track_access": "yard_tracks",
    "function_line_access": "function_lines",
    "pre_repair_line_access": "pre_repair_line",
    "dispatch_shed_access": "dispatch_shed",
    "loco_area_access": "loco_area",
    "cun4_north_port_access": "cun4_north_port",
    "cun4_release_gate_and_loco_end": "cun4_release_gate",
    "machine_accept_gate_and_receiver_capacity": "machine_accept_gate",
    "depot_inbound_route_and_slot": "depot_inbound_route",
    "depot_outbound_route": "depot_outbound_route",
    "depot_slot_capacity": "depot_slot_capacity",
    "depot_detach_order_and_slot": "depot_digest_slot",
    "tail_route_and_loco_return": "tail_route",
    "special_process_resource": "special_process_area",
}


@dataclass(frozen=True)
class ResourceTraceRecord:
    case_id: str
    step_index: int
    candidate_id: str
    source_contract: str
    action_family: str
    resource_request: str
    resource_scope: str
    resource_status: str
    arbitration_reason: str
    blocker_ids: str
    evidence_ids: str


@dataclass(frozen=True)
class ResourceTraceSummary:
    candidate_trace_record_count: int
    resource_trace_record_count: int
    traced_case_count: int
    resource_status_counts: dict[str, int]
    resource_request_counts: dict[str, int]


def read_candidate_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def line_counts(truth_path: Path) -> Counter[str]:
    payload = load_truth_case(truth_path)
    return Counter(current_line(car) for car in payload.get("StartStatus") or [])


def status_for_candidate(candidate: dict[str, Any], counts: Counter[str]) -> tuple[str, str, str]:
    request = str(candidate.get("resource_request") or "")
    level = str(candidate.get("candidate_level") or "").lower()
    action_family = str(candidate.get("action_family") or "")

    if request == "cun4_north_port_access":
        status = "constrained" if counts["存4线"] else "available"
        return status, "cun4 port candidate requires explicit port health check before release", ""
    if request == "cun4_release_gate_and_loco_end":
        return "available", "release gate is structurally required and no initial blocker is encoded in truth2", ""
    if request == "machine_accept_gate_and_receiver_capacity":
        depot_inside = sum(counts[line] for line in ("修1库内", "修2库内", "修3库内", "修4库内"))
        if depot_inside >= 20:
            return "constrained", "machine accept must coordinate with depot capacity already occupied in initial state", ""
        return "available", "machine accept gate has no initial capacity blocker in truth2", ""
    if request in {"depot_inbound_route_and_slot", "depot_slot_capacity", "depot_detach_order_and_slot"}:
        depot_inside = sum(counts[line] for line in ("修1库内", "修2库内", "修3库内", "修4库内"))
        status = "constrained" if depot_inside else "available"
        return status, "depot resource requires slot/digest check before delta acceptance", ""
    if request == "tail_route_and_loco_return":
        return "available", "tail route is only considered after primary phase obligations", ""
    if level == "critical" and action_family in {"STRICT_RELEASE", "MACHINE_ACCEPT", "DEPOT_DIGEST"}:
        return "available", "critical manual-baseline candidate remains available for hard-gate evaluation", ""
    return "available", "no initial truth2 resource blocker detected for candidate family", ""


def build_record(candidate: dict[str, Any], counts: Counter[str]) -> ResourceTraceRecord:
    request = str(candidate.get("resource_request") or "")
    status, reason, blocker_ids = status_for_candidate(candidate, counts)
    scope = RESOURCE_SCOPE_BY_REQUEST.get(request, "unknown_resource_scope")
    case_id = str(candidate.get("case_id") or "").upper()
    evidence_ids = "|".join(
        [
            str(candidate.get("evidence_ids") or ""),
            f"resource_scope:{scope}",
            f"resource_status:{status}",
        ]
    )
    return ResourceTraceRecord(
        case_id=case_id,
        step_index=int(candidate.get("step_index") or 0),
        candidate_id=str(candidate.get("candidate_id") or ""),
        source_contract=str(candidate.get("source_contract") or ""),
        action_family=str(candidate.get("action_family") or ""),
        resource_request=request,
        resource_scope=scope,
        resource_status=status,
        arbitration_reason=reason,
        blocker_ids=blocker_ids,
        evidence_ids=evidence_ids,
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    truth_dir = root / args.truth_dir
    candidate_trace = root / args.candidate_trace
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    truth_by_case = {
        case_id_from_path(path): path
        for path in sorted(truth_dir.glob("validation_*.json"))
        if try_case_id_from_path(path)
        and (representative is None or try_case_id_from_path(path) in representative)
    }

    candidate_rows = [
        row
        for row in read_candidate_records(candidate_trace)
        if not representative or str(row.get("case_id") or "").upper() in representative
    ]
    rows: list[dict[str, Any]] = []
    for candidate in candidate_rows:
        case_id = str(candidate.get("case_id") or "").upper()
        truth_path = truth_by_case.get(case_id)
        if truth_path is None:
            continue
        rows.append(asdict(build_record(candidate, line_counts(truth_path))))

    status_counts = Counter(row["resource_status"] for row in rows)
    request_counts = Counter(row["resource_request"] for row in rows)
    summary = ResourceTraceSummary(
        candidate_trace_record_count=len(candidate_rows),
        resource_trace_record_count=len(rows),
        traced_case_count=len({row["case_id"] for row in rows}),
        resource_status_counts=dict(sorted(status_counts.items())),
        resource_request_counts=dict(sorted(request_counts.items())),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "resource_trace_records.csv", rows)
    write_json(output_dir / "p6_resource_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'resource_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p6_resource_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not rows:
            errors.append("resource trace is empty")
        if len(rows) != len(candidate_rows):
            errors.append("resource trace does not cover every candidate")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate P6 resource arbitration trace records from P5 candidate trace records.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--candidate-trace", default="artifacts/p5_candidate_trace/candidate_trace_records.csv")
    parser.add_argument("--output-dir", default="artifacts/p6_resource_trace")
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
