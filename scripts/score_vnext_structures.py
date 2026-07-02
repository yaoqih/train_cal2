#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from compare_vnext_with_manual import read_manual_plans
from solver_vnext import physical


REMOTE_TEMPLATES = {
    "depot_outbound_session",
    "remote_session_prefix_batch_digest_restore",
    "depot_inbound_prefix_multidrop_session",
    "remote_session_directional_digest",
    "remote_prefix_middle_digest_restore",
    "remote_depot_direct_accessible_prefix",
    "owned_prefix_tail_digest_restore",
    "depot_locked_tail_slot_fill",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score vNext structure diagnostics from runtime artifacts.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--manual-root", default="data/人工调车数据")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "structure_scorecard"
    output_dir.mkdir(parents=True, exist_ok=True)

    case_rows = read_required_csv(artifact_dir / "case_summary.csv")
    structure_rows = read_required_csv(artifact_dir / "structure_node_metrics.csv")
    resource_rows = read_required_csv(artifact_dir / "resource_structure_records.csv")
    gap_rows = read_required_csv(artifact_dir / "generation_gap_records.csv")
    step_rows = read_required_csv(artifact_dir / "step_trace.csv")
    phase_rows = read_required_csv(artifact_dir / "phase_gate_records.csv")
    operation_rows = read_required_csv(artifact_dir / "operation_trace.csv")
    manual = read_manual_plans(Path(args.manual_root))
    if not manual:
        raise ValueError(f"no manual plans found under {args.manual_root}")

    selected_steps = [row for row in step_rows if truthy(row.get("selected"))]
    template_metrics = template_metric_rows(selected_steps, operation_rows)
    rows = build_scorecard_rows(
        case_rows=case_rows,
        structure_rows=structure_rows,
        resource_rows=resource_rows,
        gap_rows=gap_rows,
        selected_steps=selected_steps,
        phase_rows=phase_rows,
        operation_rows=operation_rows,
        manual=manual,
        template_metrics=template_metrics,
    )
    summary = {
        "artifact_dir": str(artifact_dir),
        "case_count": len(case_rows),
        "completed": sum(1 for row in case_rows if row.get("status") == "completed"),
        "blocked": sum(1 for row in case_rows if row.get("status") == "blocked"),
        "score_mean": round(statistics.mean(int(row["score"]) for row in rows), 4) if rows else 0,
        "status_counts": dict(Counter(row["status"] for row in rows)),
    }

    physical.write_csv(output_dir / "structure_scorecard.csv", rows)
    physical.write_json(output_dir / "structure_scorecard.json", {"summary": summary, "rows": rows})
    write_markdown(output_dir / "structure_scorecard.md", summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def build_scorecard_rows(
    *,
    case_rows: list[dict[str, str]],
    structure_rows: list[dict[str, str]],
    resource_rows: list[dict[str, str]],
    gap_rows: list[dict[str, str]],
    selected_steps: list[dict[str, str]],
    phase_rows: list[dict[str, str]],
    operation_rows: list[dict[str, str]],
    manual: dict[str, dict[str, Any]],
    template_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    case_count = len(case_rows)
    completed = sum(1 for row in case_rows if row.get("status") == "completed")
    blocked = sum(1 for row in case_rows if row.get("status") == "blocked")
    hard_physical = sum(to_int(row.get("hard_physical_violation_accepted_count")) for row in case_rows)
    final_length = sum(to_int(row.get("final_length_warning_count")) for row in case_rows)
    physical_rejects = sum(to_int(row.get("physical_reject_count")) for row in structure_rows)
    resource_rejects = sum(to_int(row.get("resource_violation_count")) for row in structure_rows)

    generated = sum(to_int(row.get("generated_candidate_count")) for row in structure_rows)
    accepted = sum(to_int(row.get("accepted_candidate_count")) for row in structure_rows)
    blocked_reasons = Counter(row.get("blocked_reason", "") for row in case_rows if row.get("blocked_reason"))
    gap_reasons = Counter(row.get("reason", "") for row in gap_rows if row.get("reason"))
    no_candidate_cases = sum(1 for row in case_rows if row.get("blocked_reason") == "no_episode_candidate_generated")
    all_rejected_cases = sum(1 for row in case_rows if row.get("blocked_reason", "").startswith("all_episode_candidates_rejected"))

    target_phase_counts = Counter(predicate_values(row).get("target_phase", "") for row in phase_rows)
    phase_counts = Counter(phase_code(row.get("phase", "")) for row in selected_steps)
    h4_front_steps = [row for row in selected_steps if phase_code(row.get("phase", "")) == "H4" and not touches_remote(row)]
    early_remote_cases = count_early_remote_cases(case_rows, operation_rows, manual)

    manual_remote_values, solver_remote_values, manual_hook_values, solver_hook_values = manual_solver_distributions(case_rows, manual)
    transition_rows = transition_attribution(operation_rows, selected_steps)
    internal_transitions = [row for row in transition_rows if row["boundary"] == "internal_candidate"]
    between_transitions = [row for row in transition_rows if row["boundary"] == "between_candidates"]

    depot_session = template_metrics.get("depot_outbound_session", {})
    direct_depot = [
        row
        for row in selected_steps
        if row.get("template_name") == "remote_depot_direct_accessible_prefix"
        and row.get("family") == "DEPOT_OUTBOUND"
    ]
    direct_depot_internal = sum(
        1
        for row in internal_transitions
        if row.get("current_template") == "remote_depot_direct_accessible_prefix"
        and row.get("current_family") == "DEPOT_OUTBOUND"
    )

    h4_steps = [row for row in selected_steps if phase_code(row.get("phase", "")) == "H4"]
    h4_low_effect = sum(1 for row in h4_steps if to_int(row.get("contract_reduction")) <= 1)
    remote_middle = template_metrics.get("remote_prefix_middle_digest_restore", {})
    remote_direct = template_metrics.get("remote_depot_direct_accessible_prefix", {})
    remote_internal = sum(1 for row in internal_transitions if row.get("current_template") in REMOTE_TEMPLATES)

    cun4_group = template_metrics.get("cun4_release_group_assembly", {})
    cun4_accept = template_metrics.get("cun4_release_accept_digest", {})
    cun4_resource_fail = sum(1 for row in resource_rows if row.get("structure") == "CUN4_NORTH_BUFFER" and row.get("status") == "fail")
    target_h3 = target_phase_counts.get("H3", 0)
    actual_h3 = phase_counts.get("H3", 0)

    serial_resource_fail = sum(
        1
        for row in resource_rows
        if row.get("structure", "").startswith("SERIAL_GATE") and row.get("status") == "fail"
    )
    serial_episode = template_metrics.get("serial_gate_clear_support", {})
    serial_selected = to_int(serial_episode.get("selected_count"))
    serial_zero = to_int(serial_episode.get("zero_effect_step_count"))
    serial_support_gain = sum(
        to_int(row.get("support_gain"))
        for row in selected_steps
        if row.get("template_name") == "serial_gate_clear_support"
    )

    spotting_required = gap_reasons.get("spotting_repack_required", 0)
    h5_steps = phase_counts.get("H5", 0)

    return [
        score_row(
            "PhysicalBoundary",
            100 if hard_physical == 0 and final_length == 0 else 0,
            f"hard_physical={hard_physical};final_length_warnings={final_length};physical_rejects={physical_rejects};resource_rejects={resource_rejects}",
            "hard_physical=0;final_length_warnings=0",
            "Do not relax physical rules to improve final completion.",
        ),
        score_row(
            "CandidateGenerationCoverage",
            100 if blocked == 0 else max(0, int(100 * completed / case_count)) if case_count else 0,
            (
                f"generated={generated};accepted={accepted};blocked_cases={blocked};"
                f"no_candidate_cases={no_candidate_cases};all_rejected_cases={all_rejected_cases};"
                f"gap_reasons={counter_text(gap_reasons, 8)};blocked_reasons={counter_text(blocked_reasons, 5)}"
            ),
            "standard repair chains should not stop with no candidates or all candidates rejected",
            "Generation coverage must be fixed before policy tuning.",
        ),
        score_row(
            "HumanPhaseGate",
            phase_gate_score(target_h3, actual_h3, early_remote_cases, len(h4_front_steps)),
            (
                f"target_H3={target_h3};actual_H3={actual_h3};early_remote_cases={early_remote_cases};"
                f"h4_front_steps={len(h4_front_steps)};phase_counts={counter_text(phase_counts, 8)}"
            ),
            "actual_H3/target_H3>=0.8;early_remote_cases<=20;h4_front_steps=0 or owner-bound",
            "Phase is a structure boundary, not a final score optimizer.",
        ),
        score_row(
            "RemoteContinuity",
            100 if percentile(solver_remote_values, 0.5) <= percentile(manual_remote_values, 0.5) + 1 else 80,
            (
                f"manual_remote_p50={percentile(manual_remote_values, 0.5)};"
                f"solver_remote_p50={percentile(solver_remote_values, 0.5)};"
                f"internal_transitions={len(internal_transitions)};between_transitions={len(between_transitions)};"
                f"manual_hook_p50={percentile(manual_hook_values, 0.5)};solver_hook_p50={percentile(solver_hook_values, 0.5)}"
            ),
            "remote_transition_p50<=manual_p50+1 before pursuing final score",
            "Internal transition count diagnoses episode shape, not policy order.",
        ),
        score_row(
            "DepotOutboundSession",
            100 if not direct_depot and not direct_depot_internal else 75,
            (
                f"session_selected={depot_session.get('selected_count', 0)};"
                f"session_reduction_mean={depot_session.get('contract_reduction_mean', 0)};"
                f"session_low_effect={depot_session.get('low_effect_step_count', 0)};"
                f"direct_outbound_selected={len(direct_depot)};"
                f"direct_outbound_internal_transitions={direct_depot_internal}"
            ),
            "direct outbound selected only when no valid session/subsession exists",
            "Outbound release should be block-shaped before H4 digest tuning.",
        ),
        score_row(
            "H4RemoteDigest",
            100 if h4_steps and h4_low_effect * 10 <= len(h4_steps) else 75 if h4_steps else 0,
            (
                f"h4_steps={len(h4_steps)};h4_low_effect={h4_low_effect};"
                f"remote_middle={remote_middle.get('selected_count', 0)}/{remote_middle.get('low_effect_step_count', 0)};"
                f"remote_direct={remote_direct.get('selected_count', 0)};"
                f"remote_internal_transitions={remote_internal}"
            ),
            "H4 low-effect steps<10%;single-car remote tail digest absent",
            "Keep high-yield session templates; missing tail debt must surface as session/slot-swap gaps.",
        ),
        score_row(
            "Cun4H2H3Release",
            100 if cun4_resource_fail == 0 and (target_h3 == 0 or actual_h3 / target_h3 >= 0.8) else 50,
            (
                f"cun4_group_selected={cun4_group.get('selected_count', 0)};"
                f"cun4_group_zero_effect={cun4_group.get('zero_effect_step_count', 0)};"
                f"cun4_accept_selected={cun4_accept.get('selected_count', 0)};"
                f"target_H3={target_h3};actual_H3={actual_h3};cun4_resource_fail={cun4_resource_fail}"
            ),
            "actual_H3/target_H3>=0.8;accept digest primarily records as H3",
            "CUN4 assembly can be zero contract reduction only if later H4 fragmentation falls.",
        ),
        score_row(
            "SerialGateSupport",
            100 if serial_resource_fail == 0 and serial_selected == 0 else 70 if serial_resource_fail == 0 else 0,
            (
                f"serial_episode_selected={serial_selected};serial_zero_effect={serial_zero};"
                f"serial_support_gain={serial_support_gain};serial_resource_fail={serial_resource_fail}"
            ),
            "independent serial clear episode absent;serial_resource_fail=0",
            "Any required serial clear must be owned by the episode that consumes the downstream debt.",
        ),
        score_row(
            "SpottingCloseout",
            100 if spotting_required == 0 and no_candidate_cases == 0 else 0,
            f"spotting_repack_required={spotting_required};no_candidate_cases={no_candidate_cases};h5_steps={h5_steps}",
            "H5 spotting gaps eliminated after H3/H4 are stable",
            "Do not optimize closeout before remote session shape is stable.",
        ),
    ]


def template_metric_rows(
    selected_steps: list[dict[str, str]],
    operation_rows: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    transitions = transition_attribution(operation_rows, selected_steps)
    internal_by_template = Counter(row["current_template"] for row in transitions if row["boundary"] == "internal_candidate")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected_steps:
        if row.get("template_name"):
            grouped[row["template_name"]].append(row)
    result: dict[str, dict[str, Any]] = {}
    for template, rows in sorted(grouped.items()):
        reductions = [to_int(row.get("contract_reduction")) for row in rows]
        result[template] = {
            "selected_count": len(rows),
            "contract_reduction_mean": round(statistics.mean(reductions), 4) if reductions else 0,
            "low_effect_step_count": sum(1 for value in reductions if value <= 1),
            "zero_effect_step_count": sum(1 for value in reductions if value <= 0),
            "internal_remote_transition_count": internal_by_template[template],
        }
    return result


def transition_attribution(
    operation_rows: list[dict[str, str]],
    step_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    step_by_candidate = {row.get("candidate_id", ""): row for row in step_rows if row.get("candidate_id")}
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in operation_rows:
        grouped[row.get("case_id", "")].append(row)
    rows: list[dict[str, Any]] = []
    for case_id, operations in sorted(grouped.items()):
        business = [row for row in operations if row.get("action") in {"Get", "Put"}]
        business.sort(key=lambda row: (to_int(row.get("hook_index")), to_int(row.get("operation_index"))))
        previous: dict[str, str] | None = None
        previous_remote: bool | None = None
        for row in business:
            current_remote = physical.normalize_line(row.get("line", "")) in physical.REMOTE_INTERACTION_LINES
            if previous is not None and previous_remote is not None and current_remote != previous_remote:
                current_step = step_by_candidate.get(row.get("candidate_id", ""), {})
                previous_step = step_by_candidate.get(previous.get("candidate_id", ""), {})
                rows.append(
                    {
                        "case_id": case_id,
                        "boundary": "internal_candidate" if row.get("candidate_id") == previous.get("candidate_id") else "between_candidates",
                        "previous_template": previous_step.get("template_name", ""),
                        "current_template": current_step.get("template_name", ""),
                        "previous_family": previous_step.get("family", ""),
                        "current_family": current_step.get("family", ""),
                    }
                )
            previous = row
            previous_remote = current_remote
    return rows


def count_early_remote_cases(
    case_rows: list[dict[str, str]],
    operation_rows: list[dict[str, str]],
    manual: dict[str, dict[str, Any]],
) -> int:
    operations_by_case: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in operation_rows:
        operations_by_case[row.get("case_id", "")].append(row)
    count = 0
    for case in case_rows:
        case_id = case.get("case_id", "")
        manual_plan = manual.get(case_id)
        if not manual_plan:
            continue
        operations = [row for row in operations_by_case.get(case_id, []) if row.get("action") in {"Get", "Put"}]
        operations.sort(key=lambda row: (to_int(row.get("hook_index")), to_int(row.get("operation_index"))))
        first_remote = 0
        for index, row in enumerate(operations, start=1):
            if physical.normalize_line(row.get("line", "")) in physical.REMOTE_INTERACTION_LINES:
                first_remote = index
                break
        manual_first = to_int(manual_plan.get("manual_first_depot_digest_hook"))
        manual_hook = to_int(manual_plan.get("manual_hook_count"))
        if first_remote and manual_first and manual_hook and first_remote / max(1, len(operations)) + 0.2 < manual_first / manual_hook:
            count += 1
    return count


def manual_solver_distributions(
    case_rows: list[dict[str, str]],
    manual: dict[str, dict[str, Any]],
) -> tuple[list[int], list[int], list[int], list[int]]:
    manual_remote: list[int] = []
    solver_remote: list[int] = []
    manual_hooks: list[int] = []
    solver_hooks: list[int] = []
    for row in case_rows:
        case_id = row.get("case_id", "")
        plan = manual.get(case_id)
        if not plan:
            continue
        manual_remote.append(to_int(plan.get("manual_remote_business_transition_count")))
        solver_remote.append(to_int(row.get("remote_business_transition_count")))
        manual_hooks.append(to_int(plan.get("manual_hook_count")))
        solver_hooks.append(to_int(row.get("hook_count")))
    return manual_remote, solver_remote, manual_hooks, solver_hooks


def phase_gate_score(target_h3: int, actual_h3: int, early_remote_cases: int, h4_front_steps: int) -> int:
    score = 100
    if target_h3 and actual_h3 / target_h3 < 0.8:
        score -= 35
    if early_remote_cases > 20:
        score -= min(30, early_remote_cases - 20)
    if h4_front_steps:
        score -= min(20, h4_front_steps * 2)
    return max(0, score)


def score_row(
    structure: str,
    score: int,
    evidence: str,
    repair_gate: str,
    integration_gate: str,
) -> dict[str, Any]:
    return {
        "structure": structure,
        "score": int(score),
        "status": "pass" if score >= 80 else "fail",
        "evidence": evidence,
        "repair_gate": repair_gate,
        "integration_gate": integration_gate,
    }


def read_required_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def predicate_values(row: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(row.get("predicate_values") or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key] = value
    return result


def touches_remote(row: dict[str, str]) -> bool:
    lines = set(str(row.get("touched_lines") or "").split("|"))
    lines.add(str(row.get("source_line") or ""))
    lines.add(str(row.get("target_line") or ""))
    return bool({physical.normalize_line(line) for line in lines if line} & physical.REMOTE_INTERACTION_LINES)


def phase_code(value: Any) -> str:
    text = str(value or "")
    for code in ("H1", "H2", "H3", "H4", "H5"):
        if text.startswith(code):
            return code
    return text


def truthy(value: Any) -> bool:
    return str(value).lower() == "true"


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def percentile(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = int(round((len(ordered) - 1) * quantile))
    return ordered[index]


def counter_text(counter: Counter[Any], limit: int) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common(limit) if key)


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def write_markdown(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# vNext structure scorecard",
        "",
        f"- artifact_dir: {summary['artifact_dir']}",
        f"- cases: {summary['case_count']}",
        f"- completed: {summary['completed']}",
        f"- blocked: {summary['blocked']}",
        "",
        "| structure | score | status | evidence |",
        "|---|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['structure']} | {row['score']} | {row['status']} | {markdown_cell(row['evidence'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
