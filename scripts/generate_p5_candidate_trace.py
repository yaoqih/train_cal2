#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import (
    DEFAULT_REPRESENTATIVE_CASES,
    PHASE_RANK,
    PhaseGateContract,
    build_phase_gate_contracts,
    case_id_from_path,
    contract_family,
    current_line,
    infer_manual_phase_audit,
    is_satisfied,
    load_truth_case,
    parse_manual_hooks,
    target_lines,
    try_case_id_from_path,
    write_csv,
)


RESOURCE_REQUEST_BY_FAMILY = {
    "YARD_REBALANCE": "yard_track_access",
    "FUNCTION_LINE_SERVICE": "function_line_access",
    "PRE_REPAIR_STAGING": "pre_repair_line_access",
    "DISPATCH_SHED_QUEUE": "dispatch_shed_access",
    "LOCO_AREA_STAGING": "loco_area_access",
    "CUN4_PORT_SHAPING": "cun4_north_port_access",
    "STRICT_RELEASE": "cun4_release_gate_and_loco_end",
    "MACHINE_ACCEPT": "machine_accept_gate_and_receiver_capacity",
    "REPAIR_INBOUND": "depot_inbound_route_and_slot",
    "DEPOT_OUTBOUND": "depot_outbound_route",
    "DEPOT_SLOT": "depot_slot_capacity",
    "DEPOT_DIGEST": "depot_detach_order_and_slot",
    "TAIL_CLOSEOUT": "tail_route_and_loco_return",
    "SPECIAL_REPAIR_PROCESS": "special_process_resource",
}


@dataclass(frozen=True)
class CandidateTraceRecord:
    case_id: str
    step_index: int
    phase: str
    candidate_id: str
    source_contract: str
    action_family: str
    resource_request: str
    why_generated: str
    candidate_status: str
    candidate_level: str
    manual_signal_step: int | None
    manual_hook_count: int
    evidence_ids: str


@dataclass(frozen=True)
class CandidateTraceSummary:
    truth_case_count: int
    traced_case_count: int
    skipped_without_manual_baseline_count: int
    candidate_trace_record_count: int
    critical_candidate_count: int
    important_candidate_count: int
    structural_candidate_count: int
    phase_counts: dict[str, int]
    action_family_counts: dict[str, int]


def load_manual_audits(
    manual_dir: Path,
    representative: set[str] | None,
) -> dict[str, Any]:
    audits_by_case: dict[str, Any] = {}
    for path in sorted(manual_dir.glob("*人工调车作业单/*.xlsx")):
        case_id = try_case_id_from_path(path)
        if not case_id or (representative is not None and case_id not in representative):
            continue
        hooks = parse_manual_hooks(path)
        audit = infer_manual_phase_audit(hooks)
        audit.source_path = str(path)
        audits_by_case.setdefault(audit.case_id, audit)
    return audits_by_case


def target_counter(cars: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for car in cars:
        for line in target_lines(car):
            counter[line] += 1
    return counter


def family_counter(cars: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for car in cars:
        if is_satisfied(car):
            continue
        counter[contract_family(car) or "RESIDUAL"] += 1
    return counter


def depot_initial_count(cars: list[dict[str, Any]]) -> int:
    return sum(1 for car in cars if current_line(car) in {"修1库内", "修2库内", "修3库内", "修4库内"})


def depot_target_count(targets: Counter[str]) -> int:
    return sum(targets[line] for line in ("修1库内", "修2库内", "修3库内", "修4库内"))


def candidate_level(contract: PhaseGateContract, action_family: str) -> str:
    if contract.variant == "FULL_CHAIN_REPAIR":
        if contract.phase == "H3" and action_family in {"STRICT_RELEASE", "MACHINE_ACCEPT"}:
            return "critical"
        if contract.phase == "H4" and action_family == "DEPOT_DIGEST":
            return "critical"
        if contract.phase == "H2" and action_family == "CUN4_PORT_SHAPING":
            return "important"
    if contract.variant == "DEPOT_DIGEST_ONLY" and contract.phase == "H4" and action_family == "DEPOT_DIGEST":
        return "critical"
    if contract.variant == "MIXED_SIGNAL_REPAIR" and contract.phase == "H2" and action_family in {"STRICT_RELEASE", "CUN4_PORT_SHAPING"}:
        return "important"
    if contract.variant == "DIRECT_REPAIR_ENTRY" and contract.phase in {"H3", "H4"} and action_family in {"STRICT_RELEASE", "REPAIR_INBOUND", "DEPOT_DIGEST"}:
        return "critical"
    return "structural"


def candidate_families_for_contract(
    contract: PhaseGateContract,
    families: Counter[str],
    targets: Counter[str],
    depot_count: int,
) -> list[str]:
    phase = contract.phase
    variant = contract.variant
    candidates: set[str] = set()

    if phase == "H1":
        family_to_action = {
            "PRE_REPAIR_STAGING": "PRE_REPAIR_STAGING",
            "DISPATCH_SHED_QUEUE": "DISPATCH_SHED_QUEUE",
            "FUNCTION_LINE_SERVICE": "FUNCTION_LINE_SERVICE",
            "YARD_REBALANCE": "YARD_REBALANCE",
        }
        for family, action in family_to_action.items():
            if families[family]:
                candidates.add(action)
        if targets["存4线"]:
            candidates.add("CUN4_PORT_SHAPING")
    elif phase == "H2":
        if targets["存4线"] or variant in {"FULL_CHAIN_REPAIR", "MIXED_SIGNAL_REPAIR"}:
            candidates.add("CUN4_PORT_SHAPING")
        if variant == "MIXED_SIGNAL_REPAIR":
            candidates.add("STRICT_RELEASE")
        if families["YARD_REBALANCE"]:
            candidates.add("YARD_REBALANCE")
    elif phase == "H3":
        if variant in {"FULL_CHAIN_REPAIR", "DIRECT_REPAIR_ENTRY"}:
            candidates.add("STRICT_RELEASE")
        if variant == "FULL_CHAIN_REPAIR":
            candidates.add("MACHINE_ACCEPT")
        if families["REPAIR_DEPOT"]:
            candidates.add("REPAIR_INBOUND")
    elif phase == "H4":
        if variant in {"FULL_CHAIN_REPAIR", "DEPOT_DIGEST_ONLY"} or depot_count or families["REPAIR_CONTEXT"]:
            candidates.add("DEPOT_DIGEST")
        if families["REPAIR_DEPOT"] or depot_target_count(targets):
            candidates.add("REPAIR_INBOUND")
            candidates.add("DEPOT_SLOT")
        if variant == "DIRECT_REPAIR_ENTRY":
            candidates.add("REPAIR_INBOUND")
    elif phase == "H5":
        candidates.add("TAIL_CLOSEOUT")
        for family, action in (
            ("FUNCTION_LINE_SERVICE", "FUNCTION_LINE_SERVICE"),
            ("YARD_REBALANCE", "YARD_REBALANCE"),
            ("PRE_REPAIR_STAGING", "PRE_REPAIR_STAGING"),
            ("DISPATCH_SHED_QUEUE", "DISPATCH_SHED_QUEUE"),
        ):
            if families[family]:
                candidates.add(action)
    return sorted(candidates)


def build_evidence_ids(
    case_id: str,
    contract: PhaseGateContract,
    action_family: str,
    truth_path: Path,
    families: Counter[str],
    targets: Counter[str],
) -> str:
    payload = load_truth_case(truth_path)
    vehicle_count = len(payload.get("StartStatus") or [])
    return "|".join(
        [
            f"case:{case_id}",
            f"phase:{contract.phase}",
            f"action_family:{action_family}",
            f"variant:{contract.variant}",
            f"contract_expected:{contract.expected}",
            f"manual_entry_step:{contract.entry_step or ''}",
            f"manual_exit_step:{contract.exit_step or ''}",
            f"movable:{sum(families.values())}",
            f"target_depot:{depot_target_count(targets)}",
            f"target_cun4:{targets['存4线']}",
            f"vehicle_count:{vehicle_count}",
        ]
    )


def build_candidate_record(
    truth_path: Path,
    contract: PhaseGateContract,
    action_family: str,
    families: Counter[str],
    targets: Counter[str],
    index: int,
) -> CandidateTraceRecord:
    case_id = case_id_from_path(truth_path)
    source_contract = f"{case_id}:{contract.phase}:{contract.variant}:{action_family}"
    candidate_id = f"{source_contract}:candidate:{index}"
    resource_request = RESOURCE_REQUEST_BY_FAMILY[action_family]
    level = candidate_level(contract, action_family)
    why_generated = (
        f"candidate_level={level}; "
        f"phase={contract.phase}; variant={contract.variant}; "
        f"truth2 target/family evidence supports candidate before resource arbitration"
    )
    return CandidateTraceRecord(
        case_id=case_id,
        step_index=PHASE_RANK[contract.phase],
        phase=contract.phase,
        candidate_id=candidate_id,
        source_contract=source_contract,
        action_family=action_family,
        resource_request=resource_request,
        why_generated=why_generated,
        candidate_status="generated",
        candidate_level=level,
        manual_signal_step=contract.entry_step,
        manual_hook_count=contract.manual_phase_hook_count or 0,
        evidence_ids=build_evidence_ids(case_id, contract, action_family, truth_path, families, targets),
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    manual_dir = root / args.manual_dir
    truth_dir = root / args.truth_dir
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    manual_audits_by_case = load_manual_audits(manual_dir, representative)
    contracts_by_case: dict[str, list[PhaseGateContract]] = {}
    for contract in build_phase_gate_contracts(list(manual_audits_by_case.values())):
        if contract.expected or contract.compressed_with:
            contracts_by_case.setdefault(contract.case_id, []).append(contract)

    truth_paths = [
        path
        for path in sorted(truth_dir.glob("validation_*.json"))
        if try_case_id_from_path(path)
        and (representative is None or try_case_id_from_path(path) in representative)
    ]

    rows: list[dict[str, Any]] = []
    skipped_without_manual = 0
    traced_cases: set[str] = set()
    for truth_path in truth_paths:
        case_id = case_id_from_path(truth_path)
        contracts = contracts_by_case.get(case_id, [])
        if not contracts:
            skipped_without_manual += 1
            continue
        payload = load_truth_case(truth_path)
        cars = payload.get("StartStatus") or []
        families = family_counter(cars)
        targets = target_counter(cars)
        depot_count = depot_initial_count(cars)
        traced_cases.add(case_id)
        index = 1
        for contract in sorted(contracts, key=lambda item: PHASE_RANK[item.phase]):
            for action_family in candidate_families_for_contract(contract, families, targets, depot_count):
                rows.append(
                    asdict(
                        build_candidate_record(
                            truth_path,
                            contract,
                            action_family,
                            families,
                            targets,
                            index,
                        )
                    )
                )
                index += 1

    phase_counts = Counter(row["phase"] for row in rows)
    action_counts = Counter(row["action_family"] for row in rows)
    summary = CandidateTraceSummary(
        truth_case_count=len(truth_paths),
        traced_case_count=len(traced_cases),
        skipped_without_manual_baseline_count=skipped_without_manual,
        candidate_trace_record_count=len(rows),
        critical_candidate_count=sum(1 for row in rows if row["candidate_level"] == "critical"),
        important_candidate_count=sum(1 for row in rows if row["candidate_level"] == "important"),
        structural_candidate_count=sum(1 for row in rows if row["candidate_level"] == "structural"),
        phase_counts=dict(sorted(phase_counts.items())),
        action_family_counts=dict(sorted(action_counts.items())),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "candidate_trace_records.csv", rows)
    write_json(output_dir / "p5_candidate_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'candidate_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p5_candidate_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not rows:
            errors.append("candidate trace is empty")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate P5 candidate trace records from manual H-phase candidate baselines.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--manual-dir", default="data/人工调车数据")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="artifacts/p5_candidate_trace")
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
