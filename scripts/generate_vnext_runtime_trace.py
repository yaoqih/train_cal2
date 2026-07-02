#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from solver_vnext.engine import VNextSolver, write_artifacts
from solver_vnext import physical


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run vNext contract/resource/delta/episode solver.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="artifacts/vnext_runtime_trace")
    parser.add_argument("--case-id", nargs="*")
    parser.add_argument("--max-hooks", type=int, default=300)
    parser.add_argument(
        "--trace-all-candidates",
        action="store_true",
        help="Write rejected/non-selected candidate rows. Default writes selected steps only for faster full runs.",
    )
    parser.add_argument(
        "--trace-frontier",
        action="store_true",
        help="Write per-hook AccessFrontier records. Useful for blocking diagnostics, slower on full runs.",
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    truth_dir = root / args.truth_dir
    output_dir = root / args.output_dir
    truth_paths = sorted(path for path in truth_dir.glob("*.json") if path.name != "conversion_summary.json")
    if args.case_id:
        wanted = {item.upper() for item in args.case_id}
        truth_paths = [path for path in truth_paths if physical.case_id_from_path(path) in wanted]

    solver = VNextSolver(
        max_hooks=args.max_hooks,
        trace_all_candidates=args.trace_all_candidates,
        trace_frontier=args.trace_frontier,
    )
    results = []
    traces = []
    phase_records = []
    frontier_records = []
    staging_records = []
    flow_edge_records = []
    connection_records = []
    structure_node_records = []
    resource_structure_records = []
    generation_gap_records = []
    operations = []
    for truth_path in truth_paths:
        (
            result,
            case_traces,
            case_operations,
            case_phase_records,
            case_frontier_records,
            case_staging_records,
            case_flow_edge_records,
            case_connection_records,
            case_structure_node_records,
            case_resource_structure_records,
            case_generation_gap_records,
        ) = solver.solve_case(truth_path, output_dir)
        results.append(result)
        traces.extend(case_traces)
        operations.extend(case_operations)
        phase_records.extend(case_phase_records)
        frontier_records.extend(case_frontier_records)
        staging_records.extend(case_staging_records)
        flow_edge_records.extend(case_flow_edge_records)
        connection_records.extend(case_connection_records)
        structure_node_records.extend(case_structure_node_records)
        resource_structure_records.extend(case_resource_structure_records)
        generation_gap_records.extend(case_generation_gap_records)

    write_artifacts(
        output_dir,
        results,
        traces,
        operations,
        phase_records,
        frontier_records,
        staging_records,
        flow_edge_records,
        connection_records,
        structure_node_records,
        resource_structure_records,
        generation_gap_records,
    )
    completed = sum(1 for row in results if row.status == "completed")
    blocked = sum(1 for row in results if row.status == "blocked")
    print(
        "vNext Runtime Report\n"
        f"- cases: {len(results)}\n"
        f"- completed: {completed}\n"
        f"- blocked: {blocked}\n"
        f"- hooks: {sum(row.hook_count for row in results)}\n"
        f"- wrote: {output_dir}"
    )
    if args.check:
        if not results:
            print("CHECK_FAILED: no case processed")
            return 1
        if any(row.hard_physical_violation_accepted_count for row in results):
            print("CHECK_FAILED: accepted hard physical violation")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
