#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from solver_vnext import physical


CHECK_FILES = (
    "case_summary.csv",
    "step_trace.csv",
    "operation_trace.csv",
    "phase_gate_records.csv",
    "structure_node_metrics.csv",
    "resource_structure_records.csv",
    "generation_gap_records.csv",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic vNext runtime replay checks.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--max-hooks", type=int, default=300)
    parser.add_argument("--case-id", nargs="*")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--skip-rerun", action="store_true", help="Only fingerprint the provided artifact directory.")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    artifact_dir = (root / args.artifact_dir).resolve() if not Path(args.artifact_dir).is_absolute() else Path(args.artifact_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "determinism_audit"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_rerun:
        records = [fingerprint_record("provided", artifact_dir, file_name) for file_name in CHECK_FILES]
        summary = {
            "mode": "skip_rerun",
            "file_count": len(records),
            "all_match": "",
        }
        physical.write_csv(output_dir / "determinism_check_records.csv", records)
        physical.write_json(output_dir / "determinism_check_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"wrote: {output_dir}")
        return 0

    run1 = output_dir / "run_1"
    run2 = output_dir / "run_2"
    for path in (run1, run2):
        if path.exists():
            shutil.rmtree(path)
    run_runtime(root, args.truth_dir, run1, args.max_hooks, args.case_id or [])
    run_runtime(root, args.truth_dir, run2, args.max_hooks, args.case_id or [])

    records = []
    for file_name in CHECK_FILES:
        left = fingerprint(run1 / file_name)
        right = fingerprint(run2 / file_name)
        records.append(
            {
                "file": file_name,
                "run1_hash": left["hash"],
                "run2_hash": right["hash"],
                "run1_rows": left["rows"],
                "run2_rows": right["rows"],
                "matches": int(left == right),
            }
        )
    summary = {
        "mode": "rerun",
        "file_count": len(records),
        "all_match": all(row["matches"] for row in records),
        "mismatch_files": [row["file"] for row in records if not row["matches"]],
    }
    physical.write_csv(output_dir / "determinism_check_records.csv", records)
    physical.write_json(output_dir / "determinism_check_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0 if summary["all_match"] else 1


def run_runtime(root: Path, truth_dir: str, output_dir: Path, max_hooks: int, case_ids: list[str]) -> None:
    command = [
        sys.executable,
        str(root / "scripts" / "generate_vnext_runtime_trace.py"),
        "--root",
        str(root),
        "--truth-dir",
        truth_dir,
        "--output-dir",
        str(output_dir),
        "--max-hooks",
        str(max_hooks),
        "--check",
    ]
    if case_ids:
        command.append("--case-id")
        command.extend(case_ids)
    subprocess.run(command, cwd=root, check=True)


def fingerprint_record(label: str, directory: Path, file_name: str) -> dict[str, Any]:
    item = fingerprint(directory / file_name)
    return {
        "file": file_name,
        "run": label,
        "hash": item["hash"],
        "rows": item["rows"],
    }


def fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = path.read_bytes()
    return {
        "hash": hashlib.sha256(payload).hexdigest(),
        "rows": count_csv_rows(path),
    }


def count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.reader(file)
        rows = list(reader)
    return max(0, len(rows) - 1) if rows else 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
