#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from validate_phase_gates import write_csv


STRUCTURE_SPECS = {
    "DepotSlotGraph + SpotSwapDelta": {
        "gap_buckets": {
            "TARGET_POSITION_OCCUPIED_NEEDS_SLOT_SWAP",
            "DEPOT_SLOT_ASSIGNMENT_INCOMPLETE",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "depot_slot_failure_count": 0,
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "target spot occupied and depot slot assignment gaps must be fully resolved.",
    },
    "CapacityAwareCandidateGenerator + ReleaseMoveSearch": {
        "gap_buckets": {
            "TARGET_CAPACITY_NEEDS_RELEASE_OR_SPLIT",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "target line capacity gaps must be solved by release moves or legal split batching.",
    },
    "StagingSearch + CarryOrderPlanner": {
        "gap_buckets": {
            "SAME_LINE_REPOSITION_NEEDS_STAGING",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "same-line reorder must become explicit staging moves with legal carry order.",
    },
    "SourceBlockerRelocationSearch": {
        "gap_buckets": {
            "SOURCE_FRONT_BLOCKER_NEEDS_RELOCATION",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "front blockers must be relocated or co-carried before target extraction.",
    },
    "OrderedSpotAllocator": {
        "gap_buckets": {
            "BATCH_POSITION_ASSIGNMENT_NEEDS_ORDERED_SPOTS",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "target_position_collision_inside_batch": 0,
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "forced spot batches must allocate one legal ordered spot per vehicle.",
    },
    "P10 PhysicalValidator Runtime Gate": {
        "gap_buckets": set(),
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
            "unknown_route_count": 0,
            "state_loop_count": 0,
        },
        "required_metrics": {
            "hard_physical_violation_accepted_count": 0,
            "unknown_route_count": 0,
            "state_loop_count": 0,
            "physically_feasible_final_unsatisfied_vehicle_count": 0,
            "feasible_completed_case_count": "physically_feasible_case_count",
        },
        "evidence": "runtime must emit only physically valid moves and complete all interface cases.",
    },
    "InputPhysicalConsistencyGate": {
        "gap_buckets": {
            "TARGET_FINAL_CAPACITY_INFEASIBLE",
        },
        "acceptance_record_max": 0,
        "acceptance_case_max": 0,
        "progress_floor_fields": {
            "hard_physical_violation_accepted_count": 0,
        },
        "required_metrics": {
            "hard_physical_violation_accepted_count": 0,
        },
        "evidence": "interface targets must be physically consistent with documented track lengths before runtime can prove completion.",
    },
}


@dataclass(frozen=True)
class StructureAcceptanceRow:
    structure: str
    status: str
    gap_record_count: int
    gap_case_count: int
    acceptance_record_max: int
    acceptance_case_max: int
    gap_record_clearance_ratio: float
    gap_case_clearance_ratio: float
    truth_case_count: int
    completed_case_count: int
    final_unsatisfied_vehicle_count: int
    hard_physical_violation_accepted_count: int
    unknown_route_count: int
    state_loop_count: int
    physically_feasible_case_count: int
    feasible_completed_case_count: int
    failed_required_metrics: str
    failed_progress_floor_metrics: str
    evidence: str
    next_required_action: str


@dataclass(frozen=True)
class AcceptanceSummary:
    structure_count: int
    passed_structure_count: int
    failed_structure_count: int
    truth_case_count: int
    completed_case_count: int
    blocked_case_count: int
    total_initial_unsatisfied_vehicle_count: int
    total_final_unsatisfied_vehicle_count: int
    hard_physical_violation_accepted_count: int
    unknown_route_count: int
    state_loop_count: int
    physically_feasible_case_count: int
    feasible_completed_case_count: int
    acceptance_ready: bool
    failed_structures: list[str]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def int_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def metric_value(summary: dict[str, Any], reason_counts: Counter[str], metric: str) -> int:
    if metric == "final_unsatisfied_vehicle_count":
        return int_value(summary.get("total_final_unsatisfied_vehicle_count"))
    if metric == "target_position_collision_inside_batch":
        return reason_counts[metric]
    if metric == "physically_feasible_final_unsatisfied_vehicle_count":
        return int_value(summary.get("physically_feasible_final_unsatisfied_vehicle_count"))
    if metric == "feasible_completed_case_count":
        return int_value(summary.get("feasible_completed_case_count"))
    if metric == "physically_feasible_case_count":
        return int_value(summary.get("physically_feasible_case_count"))
    return int_value(summary.get(metric))


def requirement_failure(
    summary: dict[str, Any],
    reason_counts: Counter[str],
    metric: str,
    expected: Any,
) -> str:
    actual = metric_value(summary, reason_counts, metric)
    if isinstance(expected, str):
        expected_value = int_value(summary.get(expected))
        expected_label = expected
    else:
        expected_value = int_value(expected)
        expected_label = str(expected_value)
    if actual == expected_value:
        return ""
    return f"{metric}={actual}!={expected_label}"


def build_gap_lookup(gap_rows: list[dict[str, str]]) -> dict[str, dict[str, int]]:
    lookup: dict[str, dict[str, int]] = {}
    for row in gap_rows:
        bucket = str(row.get("gap_bucket") or "")
        lookup[bucket] = {
            "record_count": int_value(row.get("record_count")),
            "case_count": int_value(row.get("case_count")),
        }
    return lookup


def gap_bucket_for_reason(reason: str) -> str:
    if reason == "same_line_reposition_requires_staging_search":
        return "SAME_LINE_REPOSITION_NEEDS_STAGING"
    if reason.startswith("target_line_length_violation:"):
        return "TARGET_CAPACITY_NEEDS_RELEASE_OR_SPLIT"
    if reason.startswith("target_final_capacity_infeasible:"):
        return "TARGET_FINAL_CAPACITY_INFEASIBLE"
    if reason.startswith("target_position_occupied:"):
        return "TARGET_POSITION_OCCUPIED_NEEDS_SLOT_SWAP"
    if reason == "target_position_collision_inside_batch":
        return "BATCH_POSITION_ASSIGNMENT_NEEDS_ORDERED_SPOTS"
    if reason == "source_front_blocked_by_satisfied_or_lower_position_cars":
        return "SOURCE_FRONT_BLOCKER_NEEDS_RELOCATION"
    if reason == "no_feasible_depot_slot":
        return "DEPOT_SLOT_ASSIGNMENT_INCOMPLETE"
    if "route_missing" in reason:
        return "TRACK_GRAPH_ROUTE_GAP"
    return "OTHER_PHYSICAL_REJECT"


def count_structure_gaps_from_candidates(
    buckets: set[str],
    candidate_rows: list[dict[str, str]],
    gap_rows: list[dict[str, str]],
    case_rows: list[dict[str, str]],
) -> tuple[int, int]:
    record_count = 0
    case_ids: set[str] = set()
    for row in case_rows:
        reasons = str(row.get("blocked_reason") or "")
        if not reasons:
            continue
        for reason in reasons.split("|"):
            if gap_bucket_for_reason(reason) not in buckets:
                continue
            record_count += 1
            case_id = str(row.get("case_id") or "")
            if case_id:
                case_ids.add(case_id)
    return record_count, len(case_ids)


def gap_clearance_ratio(current: int, total_scope: int) -> float:
    if total_scope <= 0:
        return 1.0 if current == 0 else 0.0
    return round(max(0.0, 1.0 - (current / total_scope)), 6)


def next_required_action(structure: str, failed_required: list[str], failed_floor: list[str]) -> str:
    if not failed_required and not failed_floor:
        return "none"
    if structure == "DepotSlotGraph + SpotSwapDelta":
        return "implement slot occupancy graph, outbound release slots, and swap deltas before inbound acceptance"
    if structure == "CapacityAwareCandidateGenerator + ReleaseMoveSearch":
        return "add target-capacity lookahead, release candidates, and legal split batching"
    if structure == "StagingSearch + CarryOrderPlanner":
        return "generate temporary staging paths and ordered carry/drop segments for same-line reorder"
    if structure == "SourceBlockerRelocationSearch":
        return "generate blocker relocation or co-carry moves before extracting blocked vehicles"
    if structure == "OrderedSpotAllocator":
        return "allocate forced spots per vehicle with ordered detach feasibility"
    if structure == "P10 PhysicalValidator Runtime Gate":
        return "wire all remaining generators through validator until every case completes with zero hard violations"
    if structure == "InputPhysicalConsistencyGate":
        return "fix target/track-length input mouth, split final targets, or provide an explicit business override for impossible target capacity"
    return "close failed required metrics"


def build_acceptance_rows(
    summary: dict[str, Any],
    gap_rows: list[dict[str, str]],
    candidate_rows: list[dict[str, str]],
    case_rows: list[dict[str, str]],
) -> list[StructureAcceptanceRow]:
    _gap_lookup = build_gap_lookup(gap_rows)
    reason_counts = Counter(
        {key: int_value(value) for key, value in (summary.get("rejection_reason_counts") or {}).items()}
    )
    total_reject_or_block = int_value(summary.get("rejected_candidate_count")) + int_value(summary.get("blocked_candidate_count"))
    truth_case_count = int_value(summary.get("truth_case_count"))
    physically_infeasible_cases = {
        str(row.get("case_id") or "")
        for row in case_rows
        if str(row.get("blocked_reason") or "").startswith("target_final_capacity_infeasible:")
    }
    physically_feasible_case_count = max(0, truth_case_count - len(physically_infeasible_cases))
    feasible_completed_case_count = sum(
        1
        for row in case_rows
        if str(row.get("case_id") or "") not in physically_infeasible_cases
        and str(row.get("status") or "") == "completed"
    )
    physically_feasible_final_unsatisfied = sum(
        int_value(row.get("final_unsatisfied_vehicle_count"))
        for row in case_rows
        if str(row.get("case_id") or "") not in physically_infeasible_cases
    )
    enriched_summary = dict(summary)
    enriched_summary["physically_feasible_case_count"] = physically_feasible_case_count
    enriched_summary["feasible_completed_case_count"] = feasible_completed_case_count
    enriched_summary["physically_feasible_final_unsatisfied_vehicle_count"] = physically_feasible_final_unsatisfied
    rows: list[StructureAcceptanceRow] = []

    for structure, spec in STRUCTURE_SPECS.items():
        gap_records, gap_cases = count_structure_gaps_from_candidates(
            spec["gap_buckets"],
            candidate_rows,
            gap_rows,
            case_rows,
        )
        required_metrics = dict(spec["required_metrics"])
        failed_required = []
        if gap_records > int_value(spec["acceptance_record_max"]):
            failed_required.append(f"gap_record_count={gap_records}>{int_value(spec['acceptance_record_max'])}")
        if gap_cases > int_value(spec["acceptance_case_max"]):
            failed_required.append(f"gap_case_count={gap_cases}>{int_value(spec['acceptance_case_max'])}")
        failed_required.extend(
            failure
            for metric, expected in required_metrics.items()
            if (failure := requirement_failure(enriched_summary, reason_counts, metric, expected))
        )
        failed_floor = [
            failure
            for metric, expected in spec["progress_floor_fields"].items()
            if (failure := requirement_failure(enriched_summary, reason_counts, metric, expected))
        ]
        accepted = (
            gap_records <= int_value(spec["acceptance_record_max"])
            and gap_cases <= int_value(spec["acceptance_case_max"])
            and not failed_required
            and not failed_floor
        )
        rows.append(
            StructureAcceptanceRow(
                structure=structure,
                status="passed" if accepted else "failed",
                gap_record_count=gap_records,
                gap_case_count=gap_cases,
                acceptance_record_max=int_value(spec["acceptance_record_max"]),
                acceptance_case_max=int_value(spec["acceptance_case_max"]),
                gap_record_clearance_ratio=gap_clearance_ratio(gap_records, total_reject_or_block),
                gap_case_clearance_ratio=gap_clearance_ratio(gap_cases, truth_case_count),
                truth_case_count=truth_case_count,
                completed_case_count=int_value(summary.get("completed_case_count")),
                final_unsatisfied_vehicle_count=int_value(summary.get("total_final_unsatisfied_vehicle_count")),
                hard_physical_violation_accepted_count=int_value(summary.get("hard_physical_violation_accepted_count")),
                unknown_route_count=int_value(summary.get("unknown_route_count")),
                state_loop_count=int_value(summary.get("state_loop_count")),
                physically_feasible_case_count=physically_feasible_case_count,
                feasible_completed_case_count=feasible_completed_case_count,
                failed_required_metrics="|".join(failed_required),
                failed_progress_floor_metrics="|".join(failed_floor),
                evidence=str(spec["evidence"]),
                next_required_action=next_required_action(structure, failed_required, failed_floor),
            )
        )
    return rows


def build_summary(
    summary: dict[str, Any],
    rows: list[StructureAcceptanceRow],
    case_rows: list[dict[str, str]],
) -> AcceptanceSummary:
    failed = [row.structure for row in rows if row.status != "passed"]
    physically_infeasible_cases = {
        str(row.get("case_id") or "")
        for row in case_rows
        if str(row.get("blocked_reason") or "").startswith("target_final_capacity_infeasible:")
    }
    physically_feasible_case_count = max(0, int_value(summary.get("truth_case_count")) - len(physically_infeasible_cases))
    feasible_completed_case_count = sum(
        1
        for row in case_rows
        if str(row.get("case_id") or "") not in physically_infeasible_cases
        and str(row.get("status") or "") == "completed"
    )
    return AcceptanceSummary(
        structure_count=len(rows),
        passed_structure_count=len(rows) - len(failed),
        failed_structure_count=len(failed),
        truth_case_count=int_value(summary.get("truth_case_count")),
        completed_case_count=int_value(summary.get("completed_case_count")),
        blocked_case_count=int_value(summary.get("blocked_case_count")),
        total_initial_unsatisfied_vehicle_count=int_value(summary.get("total_initial_unsatisfied_vehicle_count")),
        total_final_unsatisfied_vehicle_count=int_value(summary.get("total_final_unsatisfied_vehicle_count")),
        hard_physical_violation_accepted_count=int_value(summary.get("hard_physical_violation_accepted_count")),
        unknown_route_count=int_value(summary.get("unknown_route_count")),
        state_loop_count=int_value(summary.get("state_loop_count")),
        physically_feasible_case_count=physically_feasible_case_count,
        feasible_completed_case_count=feasible_completed_case_count,
        acceptance_ready=not failed,
        failed_structures=failed,
    )


def write_readme(output_dir: Path, summary: AcceptanceSummary) -> None:
    text = f"""# Remaining Structure Acceptance

This audit converts P10 physical runtime gaps into quantitative acceptance gates for the remaining solver structures.

## Result

| metric | value |
|---|---:|
| structure_count | {summary.structure_count} |
| passed_structure_count | {summary.passed_structure_count} |
| failed_structure_count | {summary.failed_structure_count} |
| truth_case_count | {summary.truth_case_count} |
| completed_case_count | {summary.completed_case_count} |
| blocked_case_count | {summary.blocked_case_count} |
| total_initial_unsatisfied_vehicle_count | {summary.total_initial_unsatisfied_vehicle_count} |
| total_final_unsatisfied_vehicle_count | {summary.total_final_unsatisfied_vehicle_count} |
| hard_physical_violation_accepted_count | {summary.hard_physical_violation_accepted_count} |
| unknown_route_count | {summary.unknown_route_count} |
| state_loop_count | {summary.state_loop_count} |
| physically_feasible_case_count | {summary.physically_feasible_case_count} |
| feasible_completed_case_count | {summary.feasible_completed_case_count} |
| acceptance_ready | {str(summary.acceptance_ready).lower()} |

Acceptance requires every remaining structure to pass with zero assigned gap records/cases and global P10 completion.
"""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "README.md").write_text(text, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    physical_dir = root / args.physical_runtime_dir
    output_dir = root / args.output_dir
    summary = read_json(physical_dir / "physical_runtime_summary.json")
    gap_rows = read_csv(physical_dir / "physical_gap_summary.csv")
    candidate_rows = read_csv(physical_dir / "candidate_physical_audit.csv")
    case_rows = read_csv(physical_dir / "case_summary.csv")
    rows = build_acceptance_rows(summary, gap_rows, candidate_rows, case_rows)
    acceptance_summary = build_summary(summary, rows, case_rows)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "remaining_structure_acceptance.csv", [asdict(row) for row in rows])
    write_json(output_dir / "remaining_structure_acceptance_summary.json", asdict(acceptance_summary))
    write_readme(output_dir, acceptance_summary)

    print(json.dumps(asdict(acceptance_summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir}")
    if args.check and not acceptance_summary.acceptance_ready:
        print("CHECK_FAILED: remaining structures have not reached quantitative acceptance")
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate quantitative acceptance of remaining P10 runtime structures.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--physical-runtime-dir", default="artifacts/physical_runtime_trace")
    parser.add_argument("--output-dir", default="artifacts/remaining_structure_acceptance")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
