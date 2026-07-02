#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from solver_vnext import physical


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit vNext complexity budgets for diagnostics and candidate layers.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--manual-recall-dir", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--fail-on-violation", action="store_true")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    recall_dir = Path(args.manual_recall_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "complexity_budget_audit"
    output_dir.mkdir(parents=True, exist_ok=True)

    case_rows = read_csv(artifact_dir / "case_summary.csv")
    structure_rows = read_csv(artifact_dir / "structure_node_metrics.csv")
    gap_rows = read_csv(artifact_dir / "generation_gap_records.csv")
    step_rows = read_csv(artifact_dir / "step_trace.csv")
    recall_rows = read_csv(recall_dir / "manual_candidate_recall.csv")
    recall_gap_rows = read_csv(recall_dir / "manual_candidate_gap_records.csv")

    records = build_records(
        case_rows=case_rows,
        structure_rows=structure_rows,
        gap_rows=gap_rows,
        step_rows=step_rows,
        recall_rows=recall_rows,
        recall_gap_rows=recall_gap_rows,
    )
    summary = {
        "record_count": len(records),
        "status_counts": dict(Counter(row["status"] for row in records)),
        "fail_count": sum(1 for row in records if row["status"] == "fail"),
        "warn_count": sum(1 for row in records if row["status"] == "warn"),
    }
    physical.write_csv(output_dir / "complexity_budget_records.csv", records)
    physical.write_json(output_dir / "complexity_budget_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    if args.fail_on_violation and summary["fail_count"]:
        return 1
    return 0


def build_records(
    *,
    case_rows: list[dict[str, str]],
    structure_rows: list[dict[str, str]],
    gap_rows: list[dict[str, str]],
    step_rows: list[dict[str, str]],
    recall_rows: list[dict[str, str]],
    recall_gap_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    max_generated_per_hook = max([to_int(row.get("generated_candidate_count")) for row in structure_rows] or [0])
    max_accepted_per_hook = max([to_int(row.get("accepted_candidate_count")) for row in structure_rows] or [0])
    max_gap_records_per_hook = max_counter_count(gap_rows, ("case_id", "hook_index"))
    template_count = len({row.get("template_name", "") for row in step_rows if row.get("template_name")})
    max_recall_candidates = max([to_int(row.get("candidate_count")) for row in recall_rows] or [0])
    recall_gap_reasons = Counter(row.get("gap_reason", "") for row in recall_gap_rows if row.get("gap_reason"))
    blocked_reasons = Counter(row.get("blocked_reason", "") for row in case_rows if row.get("blocked_reason"))

    return [
        record(
            layer="runtime_main",
            inputs="truth2 cases",
            outputs="case_summary|step_trace|structure_node_metrics|generation_gap_records",
            state_owner="solver_state",
            enum_boundary="case_count",
            observed=len(case_rows),
            budget=200,
            evidence=f"blocked_reasons={counter_text(blocked_reasons, 8)}",
        ),
        record(
            layer="candidate_generation",
            inputs="contracts|episodes|manual-free solver state",
            outputs="generated/accepted candidate counts",
            state_owner="episode",
            enum_boundary="max_generated_candidate_count_per_hook",
            observed=max_generated_per_hook,
            budget=512,
            evidence=f"max_accepted_per_hook={max_accepted_per_hook};template_count={template_count}",
        ),
        record(
            layer="generation_gap",
            inputs="frontier|contracts|episode applicability",
            outputs="generation_gap_records",
            state_owner="diagnostics",
            enum_boundary="max_gap_records_per_hook",
            observed=max_gap_records_per_hook,
            budget=128,
            evidence=f"gap_reason_count={len({row.get('reason', '') for row in gap_rows if row.get('reason')})}",
        ),
        record(
            layer="manual_candidate_recall",
            inputs="manual labels|identity replay|owner blockers",
            outputs="manual_candidate_recall|manual_candidate_gap_records",
            state_owner="manual_replay",
            enum_boundary="max_generated_candidate_count_per_action",
            observed=max_recall_candidates,
            budget=512,
            evidence=f"gap_reasons={counter_text(recall_gap_reasons, 8)}",
        ),
        record(
            layer="policy",
            inputs="accepted candidates only",
            outputs="selected candidate",
            state_owner="policy",
            enum_boundary="selected_rows",
            observed=sum(1 for row in step_rows if str(row.get("selected")).lower() == "true"),
            budget=max(1, len(step_rows)),
            evidence="policy must not create candidates",
        ),
    ]


def record(
    *,
    layer: str,
    inputs: str,
    outputs: str,
    state_owner: str,
    enum_boundary: str,
    observed: int,
    budget: int,
    evidence: str,
) -> dict[str, Any]:
    status = "pass" if observed <= budget else "fail"
    return {
        "layer": layer,
        "inputs": inputs,
        "outputs": outputs,
        "state_owner": state_owner,
        "enum_boundary": enum_boundary,
        "observed_count": observed,
        "budget": budget,
        "status": status,
        "evidence": evidence,
        "rollback_condition": "observed_count exceeds budget or gap count grows without recall gain",
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def max_counter_count(rows: list[dict[str, str]], keys: tuple[str, ...]) -> int:
    counter: Counter[tuple[str, ...]] = Counter()
    for row in rows:
        counter[tuple(row.get(key, "") for key in keys)] += 1
    return counter.most_common(1)[0][1] if counter else 0


def counter_text(counter: Counter[Any], limit: int) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common(limit) if key)


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
