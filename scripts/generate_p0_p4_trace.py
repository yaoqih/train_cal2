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
    PHASES,
    PhaseGateContract,
    audit_truth_case,
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


CONTRACT_TARGET_BY_FAMILY = {
    "REPAIR_DEPOT": "REPAIR_INBOUND",
    "REPAIR_CONTEXT": "DEPOT_DIGEST",
    "PRE_REPAIR_STAGING": "PRE_REPAIR_STAGING",
    "DISPATCH_SHED_QUEUE": "DISPATCH_SHED_QUEUE",
    "FUNCTION_LINE_SERVICE": "FUNCTION_LINE_SERVICE",
    "YARD_REBALANCE": "YARD_REBALANCE",
}

EDGE_STATUS_BY_PHASE = {
    "H1": "FORMING",
    "H2": "PORT_READY",
    "H3": "ACCEPTED",
    "H4": "DIGESTING",
    "H5": "DONE",
}

DEFAULT_TARGET_BY_PHASE = {
    "H1": "YARD_REBALANCE",
    "H2": "CUN4_PORT_SHAPING",
    "H3": "STRICT_RELEASE",
    "H4": "DEPOT_DIGEST",
    "H5": "TAIL_CLOSEOUT",
}


@dataclass(frozen=True)
class PhaseTraceRecord:
    case_id: str
    step_index: int
    from_phase: str
    to_phase: str
    transition_type: str
    predicate_values: str
    consumed_contract_ids: str
    created_contract_ids: str
    carried_obligation_ids: str
    blocked_contract_ids: str
    evidence_ids: str
    reject_reason: str


@dataclass(frozen=True)
class ActionTraceRecord:
    case_id: str
    step_index: int
    phase: str
    edge_id: str
    edge_status: str
    contract_id: str
    contract_variant: str
    target_contract: str
    target_reason: str
    evidence_ids: str
    movable_vehicle_count: int
    primary_family: str
    phase_contract_expected: bool
    phase_contract_skip_allowed: bool


@dataclass(frozen=True)
class TraceSummary:
    truth_case_count: int
    traced_case_count: int
    skipped_without_manual_contract_count: int
    action_trace_record_count: int
    phase_trace_record_count: int
    target_contract_counts: dict[str, int]
    phase_counts: dict[str, int]


def load_manual_audits(manual_dir: Path, representative: set[str] | None) -> dict[str, Any]:
    audits_by_case: dict[str, Any] = {}
    for path in sorted(manual_dir.glob("*人工调车作业单/*.xlsx")):
        case_id = try_case_id_from_path(path)
        if not case_id or (representative is not None and case_id not in representative):
            continue
        audit = infer_manual_phase_audit(parse_manual_hooks(path))
        audit.source_path = str(path)
        audits_by_case.setdefault(audit.case_id, audit)
    return audits_by_case


def first_expected_contract(contracts: list[PhaseGateContract]) -> PhaseGateContract | None:
    for phase in PHASES:
        for contract in contracts:
            if contract.phase == phase and (contract.expected or contract.compressed_with):
                return contract
    return None


def count_unsatisfied_families(cars: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for car in cars:
        if is_satisfied(car):
            continue
        family = contract_family(car) or "RESIDUAL"
        counter[family] += 1
    return counter


def depot_initial_count(cars: list[dict[str, Any]]) -> int:
    depot_lines = {"修1库内", "修2库内", "修3库内", "修4库内"}
    return sum(1 for car in cars if current_line(car) in depot_lines)


def target_counter(cars: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for car in cars:
        for line in target_lines(car):
            counter[line] += 1
    return counter


def choose_target_contract(
    phase: str,
    family_counts: Counter[str],
    targets: Counter[str],
    depot_count: int,
) -> tuple[str, str, str]:
    if phase == "H2":
        if targets["存4线"]:
            return (
                "CUN4_PORT_SHAPING",
                "phase=H2; cun4_target_vehicle_count>0; protect release port before strict release",
                "YARD_REBALANCE",
            )
        return (
            "YARD_REBALANCE",
            "phase=H2 but no current 存4 target count; keep yard rebalance as conservative shaping intent",
            "YARD_REBALANCE",
        )
    if phase == "H3":
        return (
            "STRICT_RELEASE",
            "phase=H3; manual contract requires release/accept hard boundary before depot digest",
            "REPAIR_DEPOT",
        )
    if phase == "H4":
        if family_counts["REPAIR_CONTEXT"] or depot_count:
            return (
                "DEPOT_DIGEST",
                "phase=H4; existing depot context requires digest/slot/swap resolution",
                "REPAIR_CONTEXT",
            )
        return (
            "REPAIR_INBOUND",
            "phase=H4; repair depot target vehicles need inbound or slot contract resolution",
            "REPAIR_DEPOT",
        )
    if phase == "H5":
        return (
            "TAIL_CLOSEOUT",
            "phase=H5; only tail obligations may remain after primary contracts",
            "SUPPORT",
        )

    non_depot_priority = (
        "PRE_REPAIR_STAGING",
        "DISPATCH_SHED_QUEUE",
        "FUNCTION_LINE_SERVICE",
        "YARD_REBALANCE",
        "REPAIR_DEPOT",
        "REPAIR_CONTEXT",
    )
    for family in non_depot_priority:
        if family_counts[family]:
            target = CONTRACT_TARGET_BY_FAMILY.get(family, DEFAULT_TARGET_BY_PHASE[phase])
            return (
                target,
                f"phase=H1; primary unsatisfied family={family}; count={family_counts[family]}",
                family,
            )
    return (
        DEFAULT_TARGET_BY_PHASE[phase],
        "phase=H1; no unsatisfied non-depot family dominates, keep yard rebalance as seed intent",
        "YARD_REBALANCE",
    )


def build_evidence_ids(case_id: str, phase: str, family_counts: Counter[str], targets: Counter[str]) -> str:
    evidence = [
        f"case:{case_id}",
        f"phase:{phase}",
        f"movable:{sum(family_counts.values())}",
    ]
    for family, count in sorted(family_counts.items()):
        if count:
            evidence.append(f"family:{family}:{count}")
    if targets["存4线"]:
        evidence.append(f"target:存4线:{targets['存4线']}")
    depot_targets = sum(targets[line] for line in ("修1库内", "修2库内", "修3库内", "修4库内"))
    if depot_targets:
        evidence.append(f"target:depot:{depot_targets}")
    return "|".join(evidence)


def build_records_for_case(
    truth_path: Path,
    contracts: list[PhaseGateContract],
) -> tuple[PhaseTraceRecord, ActionTraceRecord] | None:
    case_id = case_id_from_path(truth_path)
    first_contract = first_expected_contract(contracts)
    if first_contract is None:
        return None

    payload = load_truth_case(truth_path)
    cars = payload.get("StartStatus") or []
    truth_audit = audit_truth_case(truth_path)
    families = count_unsatisfied_families(cars)
    targets = target_counter(cars)
    depot_count = depot_initial_count(cars)
    phase = first_contract.phase
    target_contract, target_reason, primary_family = choose_target_contract(
        phase,
        families,
        targets,
        depot_count,
    )
    evidence_ids = build_evidence_ids(case_id, phase, families, targets)
    edge_id = f"{case_id}:{phase}:{primary_family}:{target_contract}"
    contract_id = f"{case_id}:{phase}:{first_contract.variant}:{target_contract}"
    predicate_values = {
        "effective_contract_coverage": truth_audit.effective_contract_coverage,
        "residual_vehicle_ratio": truth_audit.residual_vehicle_ratio,
        "manual_variant": first_contract.variant,
        "phase_expected": first_contract.expected,
        "phase_skip_allowed": first_contract.skip_allowed,
        "entry_step": first_contract.entry_step,
        "exit_step": first_contract.exit_step,
    }
    phase_trace = PhaseTraceRecord(
        case_id=case_id,
        step_index=0,
        from_phase="",
        to_phase=phase,
        transition_type="enter",
        predicate_values=json.dumps(predicate_values, ensure_ascii=False, sort_keys=True),
        consumed_contract_ids="",
        created_contract_ids=contract_id,
        carried_obligation_ids="",
        blocked_contract_ids="",
        evidence_ids=evidence_ids,
        reject_reason="",
    )
    action_trace = ActionTraceRecord(
        case_id=case_id,
        step_index=0,
        phase=phase,
        edge_id=edge_id,
        edge_status=EDGE_STATUS_BY_PHASE[phase],
        contract_id=contract_id,
        contract_variant=first_contract.variant,
        target_contract=target_contract,
        target_reason=target_reason,
        evidence_ids=evidence_ids,
        movable_vehicle_count=truth_audit.movable_vehicle_count,
        primary_family=primary_family,
        phase_contract_expected=first_contract.expected,
        phase_contract_skip_allowed=first_contract.skip_allowed,
    )
    return phase_trace, action_trace


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    truth_dir = root / args.truth_dir
    manual_dir = root / args.manual_dir
    output_dir = root / args.output_dir
    representative = {case.upper() for case in args.representative_cases} if args.representative_only else None

    manual_audits_by_case = load_manual_audits(manual_dir, representative)
    contracts_by_case: dict[str, list[PhaseGateContract]] = {}
    for contract in build_phase_gate_contracts(list(manual_audits_by_case.values())):
        contracts_by_case.setdefault(contract.case_id, []).append(contract)

    truth_paths = [
        path
        for path in sorted(truth_dir.glob("validation_*.json"))
        if try_case_id_from_path(path)
        and (representative is None or try_case_id_from_path(path) in representative)
    ]

    phase_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    skipped_without_manual = 0
    for truth_path in truth_paths:
        case_id = case_id_from_path(truth_path)
        contracts = contracts_by_case.get(case_id, [])
        if not contracts:
            skipped_without_manual += 1
            continue
        records = build_records_for_case(truth_path, contracts)
        if records is None:
            skipped_without_manual += 1
            continue
        phase_record, action_record = records
        phase_rows.append(asdict(phase_record))
        action_rows.append(asdict(action_record))

    target_counts = Counter(row["target_contract"] for row in action_rows)
    phase_counts = Counter(row["phase"] for row in action_rows)
    summary = TraceSummary(
        truth_case_count=len(truth_paths),
        traced_case_count=len(action_rows),
        skipped_without_manual_contract_count=skipped_without_manual,
        action_trace_record_count=len(action_rows),
        phase_trace_record_count=len(phase_rows),
        target_contract_counts=dict(sorted(target_counts.items())),
        phase_counts=dict(sorted(phase_counts.items())),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "phase_gate_records.csv", phase_rows)
    write_csv(output_dir / "action_trace_records.csv", action_rows)
    write_json(output_dir / "p0_p4_trace_summary.json", asdict(summary))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir / 'phase_gate_records.csv'}")
    print(f"Wrote {output_dir / 'action_trace_records.csv'}")
    print(f"Wrote {output_dir / 'p0_p4_trace_summary.json'}")

    if args.check:
        errors: list[str] = []
        if not action_rows:
            errors.append("action trace is empty")
        if len(action_rows) + skipped_without_manual != len(truth_paths):
            errors.append("truth case accounting mismatch")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a lightweight P0-P4 structural trace from truth2 and manual H-phase contracts.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--manual-dir", default="data/人工调车数据")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="artifacts/p0_p4_trace")
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
