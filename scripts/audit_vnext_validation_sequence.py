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


REMOTE_LINES = set(physical.REMOTE_INTERACTION_LINES)
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
    parser = argparse.ArgumentParser(description="Audit vNext structures in the agreed 1-7 validation order.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--manual-root", default="data/人工调车数据")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "validation_sequence_audit"
    output_dir.mkdir(parents=True, exist_ok=True)

    case_rows = _read_csv(artifact_dir / "case_summary.csv")
    step_rows = _read_csv(artifact_dir / "step_trace.csv")
    operation_rows = _read_csv(artifact_dir / "operation_trace.csv")
    phase_rows = _read_csv(artifact_dir / "phase_gate_records.csv")
    structure_rows = _read_csv(artifact_dir / "structure_node_metrics.csv")
    resource_rows = _read_csv(artifact_dir / "resource_structure_records.csv")
    manual = read_manual_plans(Path(args.manual_root))

    step_by_candidate = {row["candidate_id"]: row for row in step_rows if row.get("candidate_id")}
    selected_steps = [row for row in step_rows if _truthy(row.get("selected"))]
    selected_by_case = _rows_by_case(selected_steps)
    operations_by_case = _rows_by_case(operation_rows)
    phase_by_case = _rows_by_case(phase_rows)

    transition_rows = _transition_rows(operations_by_case, step_by_candidate)
    case_validation_rows = _case_validation_rows(
        case_rows=case_rows,
        selected_by_case=selected_by_case,
        phase_by_case=phase_by_case,
        operations_by_case=operations_by_case,
        transition_rows=transition_rows,
        manual=manual,
    )
    template_rows = _template_rows(selected_steps, transition_rows)
    sequence_rows = _sequence_rows(
        case_rows=case_rows,
        selected_steps=selected_steps,
        transition_rows=transition_rows,
        template_rows=template_rows,
        case_validation_rows=case_validation_rows,
        phase_rows=phase_rows,
        structure_rows=structure_rows,
        resource_rows=resource_rows,
        manual=manual,
    )
    summary = {
        "case_count": len(case_rows),
        "completed": sum(1 for row in case_rows if row.get("status") == "completed"),
        "blocked": sum(1 for row in case_rows if row.get("status") == "blocked"),
        "sequence_status_counts": dict(Counter(row["status"] for row in sequence_rows)),
        "top_transition_templates": [
            row for row in template_rows if _int(row["internal_remote_transition_count"])
        ][:10],
    }

    physical.write_csv(output_dir / "validation_sequence_summary.csv", sequence_rows)
    physical.write_csv(output_dir / "case_validation_sequence.csv", case_validation_rows)
    physical.write_csv(output_dir / "operation_transition_attribution.csv", transition_rows)
    physical.write_csv(output_dir / "template_validation_metrics.csv", template_rows)
    physical.write_json(output_dir / "validation_sequence_summary.json", summary)
    _write_markdown(output_dir / "validation_sequence_summary.md", sequence_rows, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def _sequence_rows(
    *,
    case_rows: list[dict[str, str]],
    selected_steps: list[dict[str, str]],
    transition_rows: list[dict[str, Any]],
    template_rows: list[dict[str, Any]],
    case_validation_rows: list[dict[str, Any]],
    phase_rows: list[dict[str, str]],
    structure_rows: list[dict[str, str]],
    resource_rows: list[dict[str, str]],
    manual: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    case_count = len(case_rows)
    completed = sum(1 for row in case_rows if row.get("status") == "completed")
    hard_physical = sum(_int(row.get("hard_physical_violation_accepted_count")) for row in case_rows)
    final_length_warnings = sum(_int(row.get("final_length_warning_count")) for row in case_rows)
    physical_rejects = sum(_int(row.get("physical_reject_count")) for row in structure_rows)
    resource_rejects = sum(_int(row.get("resource_violation_count")) for row in structure_rows)
    top_blocked = _counter_text(Counter(row.get("blocked_reason", "") for row in case_rows if row.get("blocked_reason")), 8)

    manual_remote = []
    solver_remote = []
    manual_hooks = []
    solver_hooks = []
    for row in case_rows:
        case_id = row.get("case_id", "")
        if case_id not in manual:
            continue
        manual_remote.append(_int(manual[case_id].get("manual_remote_business_transition_count")))
        solver_remote.append(_int(row.get("remote_business_transition_count")))
        manual_hooks.append(_int(manual[case_id].get("manual_hook_count")))
        solver_hooks.append(_int(row.get("hook_count")))

    internal_transitions = [row for row in transition_rows if row["boundary"] == "internal_candidate"]
    between_transitions = [row for row in transition_rows if row["boundary"] == "between_candidates"]
    internal_by_template = Counter(row["current_template"] for row in internal_transitions)

    phase_counts = Counter(_phase_code(row.get("phase", "")) for row in selected_steps)
    target_phase_counts = Counter(_predicate(row).get("target_phase", "") for row in phase_rows)
    h4_steps = [row for row in selected_steps if _phase_code(row.get("phase", "")) == "H4"]
    h4_front_steps = [row for row in h4_steps if not _touches_remote(row)]
    early_remote_cases = sum(1 for row in case_validation_rows if row["remote_touched_too_early"])
    h4_fragment_cases = sum(1 for row in case_validation_rows if row["h4_fragmented"])

    template_by_name = {row["template_name"]: row for row in template_rows}
    depot_outbound = template_by_name.get("depot_outbound_session", {})
    depot_outbound_direct = [
        row
        for row in selected_steps
        if row.get("template_name") == "remote_depot_direct_accessible_prefix"
        and row.get("family") == "DEPOT_OUTBOUND"
    ]
    depot_outbound_direct_transitions = sum(
        1
        for row in internal_transitions
        if row.get("current_template") == "remote_depot_direct_accessible_prefix"
        and row.get("current_family") == "DEPOT_OUTBOUND"
    )

    remote_middle = template_by_name.get("remote_prefix_middle_digest_restore", {})
    remote_direct = template_by_name.get("remote_depot_direct_accessible_prefix", {})
    h4_low_effect = sum(
        1
        for row in selected_steps
        if _phase_code(row.get("phase", "")) == "H4" and _int(row.get("contract_reduction")) <= 1
    )

    cun4_group = template_by_name.get("cun4_release_group_assembly", {})
    cun4_accept = template_by_name.get("cun4_release_accept_digest", {})
    cun4_resource_fail = sum(
        1 for row in resource_rows if row.get("structure") == "CUN4_NORTH_BUFFER" and row.get("status") == "fail"
    )
    target_h3 = target_phase_counts.get("H3", 0)
    actual_h3 = phase_counts.get("H3", 0)

    serial = template_by_name.get("serial_gate_clear_support", {})
    serial_resource_fail = sum(
        1
        for row in resource_rows
        if row.get("structure", "").startswith("SERIAL_GATE") and row.get("status") == "fail"
    )
    serial_selected = _int(serial.get("selected_count"))
    serial_zero = _int(serial.get("zero_effect_step_count"))
    serial_support_gain = sum(
        _int(row.get("support_gain"))
        for row in selected_steps
        if row.get("template_name") == "serial_gate_clear_support"
    )

    return [
        _row(
            1,
            "PhysicalBoundary",
            "physical.py/frontier.py/resources.py",
            "fail" if hard_physical else ("warn" if completed < case_count else "pass"),
            f"hard_physical={hard_physical};completed={completed}/{case_count};physical_rejects={physical_rejects};resource_rejects={resource_rejects};final_length_warnings={final_length_warnings}",
            top_blocked,
            "先确认没有误拒人工计划；当前硬物理通过，但 blocked 仍需要按拒绝主因抽样复盘。",
        ),
        _row(
            2,
            "OperationTransitionAttribution",
            "operation_trace.csv + step_trace.csv",
            "fail" if _p(solver_remote, 0.5) > _p(manual_remote, 0.5) + 2 else "pass",
            (
                f"manual_remote_p50={_p(manual_remote, 0.5)};solver_remote_p50={_p(solver_remote, 0.5)};"
                f"internal_transitions={len(internal_transitions)};between_transitions={len(between_transitions)}"
            ),
            f"internal_by_template={_counter_text(internal_by_template, 8)}",
            "先分清候选内部切换和候选之间切换；内部切换高说明要改 episode，不是只调 policy。",
        ),
        _row(
            3,
            "HumanPhaseGate",
            "phase.py/policy.py",
            "fail" if early_remote_cases or target_h3 > actual_h3 * 3 or h4_front_steps else "pass",
            (
                f"phase_counts={_counter_text(phase_counts, 8)};target_H3={target_h3};actual_H3={actual_h3};"
                f"early_remote_cases={early_remote_cases};h4_front_steps={len(h4_front_steps)}"
            ),
            f"hook_p50_manual={_p(manual_hooks, 0.5)};hook_p50_solver={_p(solver_hooks, 0.5)}",
            "验证阶段门是否真的后置大库、压缩 H4、保护尾部收束。",
        ),
        _row(
            4,
            "DepotOutboundSession",
            "DepotOutboundSessionEpisode",
            "fail" if depot_outbound_direct and depot_outbound_direct_transitions else ("warn" if depot_outbound_direct else "pass"),
            (
                f"session_selected={_int(depot_outbound.get('selected_count'))};"
                f"session_reduction_mean={depot_outbound.get('contract_reduction_mean', 0)};"
                f"direct_depot_outbound_selected={len(depot_outbound_direct)};"
                f"direct_depot_outbound_internal_transitions={depot_outbound_direct_transitions}"
            ),
            f"session_low_effect={depot_outbound.get('low_effect_step_count', 0)};direct_examples={_selected_examples(depot_outbound_direct, 5)}",
            "验证出库是否成块；direct 出库多说明 session all-or-nothing 或物理子批次边界失效。",
        ),
        _row(
            5,
            "H4RemoteDigest",
            "RemoteSession/RemoteDepot episodes",
            "fail" if _int(remote_middle.get("selected_count")) and _int(remote_middle.get("low_effect_step_count")) * 2 > _int(remote_middle.get("selected_count")) else ("warn" if h4_fragment_cases else "pass"),
            (
                f"remote_prefix_middle_selected={remote_middle.get('selected_count', 0)};"
                f"remote_prefix_middle_low_effect={remote_middle.get('low_effect_step_count', 0)};"
                f"remote_direct_selected={remote_direct.get('selected_count', 0)};"
                f"h4_low_effect_steps={h4_low_effect};h4_fragment_cases={h4_fragment_cases}"
            ),
            f"remote_template_transitions={_counter_text(Counter(row['current_template'] for row in internal_transitions if row['current_template'] in REMOTE_TEMPLATES), 8)}",
            "验证 H4 是否块状消化 inbound/outbound/internal；低效 middle digest 需要被更强聚合替代或删除。",
        ),
        _row(
            6,
            "Cun4H2H3Release",
            "release.py/Cun4 episodes/phase.py",
            "fail" if target_h3 > actual_h3 * 3 or cun4_resource_fail else ("warn" if _int(cun4_group.get("zero_effect_step_count")) else "pass"),
            (
                f"cun4_group_selected={cun4_group.get('selected_count', 0)};"
                f"cun4_group_zero_effect={cun4_group.get('zero_effect_step_count', 0)};"
                f"cun4_accept_selected={cun4_accept.get('selected_count', 0)};"
                f"target_H3={target_h3};actual_H3={actual_h3};cun4_resource_fail={cun4_resource_fail}"
            ),
            "CUN4 assembly 的 contract reduction 天然可能为 0，但必须证明它减少后续 H4 碎勾。",
            "验证存4释放口是否形成 H3 边界，而不是被 H4 吞掉。",
        ),
        _row(
            7,
            "SerialGateSupport",
            "serial.py/SerialGateClearEpisode/resource_structures.py",
            "fail" if serial_resource_fail else ("warn" if serial_selected and serial_zero == serial_selected else "pass"),
            (
                f"serial_selected={serial_selected};serial_zero_contract_reduction={serial_zero};"
                f"serial_support_gain={serial_support_gain};serial_resource_fail={serial_resource_fail}"
            ),
            f"serial_phase_counts={serial.get('phase_counts', '')}",
            "验证清障是否绑定后续下游债务；如果只是零收益搬车，应收进 owner episode 或删除。",
        ),
    ]


def _case_validation_rows(
    *,
    case_rows: list[dict[str, str]],
    selected_by_case: dict[str, list[dict[str, str]]],
    phase_by_case: dict[str, list[dict[str, str]]],
    operations_by_case: dict[str, list[dict[str, str]]],
    transition_rows: list[dict[str, Any]],
    manual: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    transitions_by_case = _rows_by_case(transition_rows)
    rows: list[dict[str, Any]] = []
    for case in case_rows:
        case_id = case.get("case_id", "")
        steps = selected_by_case.get(case_id, [])
        phases = phase_by_case.get(case_id, [])
        ops = [row for row in operations_by_case.get(case_id, []) if row.get("action") in {"Get", "Put"}]
        remote_flags = [_normal_line(row.get("line")) in REMOTE_LINES for row in ops]
        first_remote = _first_true(remote_flags)
        manual_plan = manual.get(case_id)
        manual_first_depot = _int(manual_plan.get("manual_first_depot_digest_hook")) if manual_plan else 0
        manual_hook = _int(manual_plan.get("manual_hook_count")) if manual_plan else 0
        first_remote_ratio = _ratio(first_remote, max(1, len(ops)))
        manual_first_depot_ratio = _ratio(manual_first_depot, manual_hook)
        phase_counts = Counter(_phase_code(row.get("phase", "")) for row in steps)
        target_counts = Counter(_predicate(row).get("target_phase", "") for row in phases)
        h4_steps = [row for row in steps if _phase_code(row.get("phase", "")) == "H4"]
        h4_fragments = [row for row in h4_steps if _int(row.get("contract_reduction")) <= 1 and _move_count(row) <= 2]
        internal = sum(1 for row in transitions_by_case.get(case_id, []) if row["boundary"] == "internal_candidate")
        between = sum(1 for row in transitions_by_case.get(case_id, []) if row["boundary"] == "between_candidates")
        early = bool(manual_first_depot_ratio and first_remote_ratio and first_remote_ratio + 0.2 < manual_first_depot_ratio)
        rows.append(
            {
                "case_id": case_id,
                "status": case.get("status", ""),
                "blocked_reason": case.get("blocked_reason", ""),
                "solver_hook_count": _int(case.get("hook_count")),
                "manual_hook_count": manual_hook,
                "solver_remote_transition_count": _int(case.get("remote_business_transition_count")),
                "manual_remote_transition_count": _int(manual_plan.get("manual_remote_business_transition_count")) if manual_plan else 0,
                "operation_internal_transition_count": internal,
                "operation_between_transition_count": between,
                "first_remote_operation_index": first_remote,
                "first_remote_operation_ratio": first_remote_ratio,
                "manual_first_depot_ratio": manual_first_depot_ratio,
                "remote_touched_too_early": early,
                "phase_counts": _counter_text(phase_counts, 8),
                "target_phase_counts": _counter_text(target_counts, 8),
                "h4_step_count": len(h4_steps),
                "h4_fragment_count": len(h4_fragments),
                "h4_fragmented": bool(h4_steps and len(h4_fragments) > max(2, len(h4_steps) // 3)),
            }
        )
    return rows


def _transition_rows(
    operations_by_case: dict[str, list[dict[str, str]]],
    step_by_candidate: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id, operations in sorted(operations_by_case.items()):
        business = [row for row in operations if row.get("action") in {"Get", "Put"}]
        business.sort(key=lambda row: (_int(row.get("hook_index")), _int(row.get("operation_index"))))
        previous: dict[str, str] | None = None
        previous_remote: bool | None = None
        for row in business:
            current_remote = _normal_line(row.get("line")) in REMOTE_LINES
            if previous is not None and previous_remote is not None and current_remote != previous_remote:
                current_step = step_by_candidate.get(row.get("candidate_id", ""), {})
                previous_step = step_by_candidate.get(previous.get("candidate_id", ""), {})
                same_candidate = row.get("candidate_id") == previous.get("candidate_id")
                rows.append(
                    {
                        "case_id": case_id,
                        "hook_index": _int(row.get("hook_index")),
                        "operation_index": _int(row.get("operation_index")),
                        "boundary": "internal_candidate" if same_candidate else "between_candidates",
                        "from_remote": previous_remote,
                        "to_remote": current_remote,
                        "previous_line": previous.get("line", ""),
                        "current_line": row.get("line", ""),
                        "previous_candidate_id": previous.get("candidate_id", ""),
                        "current_candidate_id": row.get("candidate_id", ""),
                        "previous_template": previous_step.get("template_name", ""),
                        "current_template": current_step.get("template_name", ""),
                        "previous_family": previous_step.get("family", ""),
                        "current_family": current_step.get("family", ""),
                        "current_intent": current_step.get("intent", ""),
                        "current_contract_reduction": _int(current_step.get("contract_reduction")),
                        "current_move_count": _move_count(current_step),
                    }
                )
            previous = row
            previous_remote = current_remote
    return rows


def _template_rows(
    selected_steps: list[dict[str, str]],
    transition_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected_steps:
        grouped[row.get("template_name", "")].append(row)
    internal_by_template = Counter(
        row["current_template"] for row in transition_rows if row["boundary"] == "internal_candidate"
    )
    between_by_template = Counter(
        row["current_template"] for row in transition_rows if row["boundary"] == "between_candidates"
    )
    rows: list[dict[str, Any]] = []
    for template, items in sorted(grouped.items()):
        if not template:
            continue
        reductions = [_int(row.get("contract_reduction")) for row in items]
        move_counts = [_move_count(row) for row in items]
        phases = Counter(_phase_code(row.get("phase", "")) for row in items)
        families = Counter(row.get("family", "") for row in items)
        rows.append(
            {
                "template_name": template,
                "selected_count": len(items),
                "family_counts": _counter_text(families, 8),
                "phase_counts": _counter_text(phases, 8),
                "contract_reduction_sum": sum(reductions),
                "contract_reduction_mean": round(statistics.mean(reductions), 4) if reductions else 0,
                "contract_reduction_p50": _p(reductions, 0.5),
                "move_count_mean": round(statistics.mean(move_counts), 4) if move_counts else 0,
                "low_effect_step_count": sum(1 for value in reductions if value <= 1),
                "zero_effect_step_count": sum(1 for value in reductions if value <= 0),
                "internal_remote_transition_count": internal_by_template[template],
                "between_remote_transition_count": between_by_template[template],
            }
        )
    return sorted(rows, key=lambda row: (-_int(row["internal_remote_transition_count"]), row["template_name"]))


def _write_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# vNext validation sequence",
        "",
        f"- cases: {summary['case_count']}",
        f"- completed: {summary['completed']}",
        f"- blocked: {summary['blocked']}",
        "",
        "| # | structure | status | key metric | next action |",
        "|---:|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['sequence']} | {row['structure']} | {row['status']} | "
            f"{_md(row['primary_metric'])} | {_md(row['next_action'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(
    sequence: int,
    structure: str,
    code_boundary: str,
    status: str,
    primary_metric: str,
    evidence: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "structure": structure,
        "code_boundary": code_boundary,
        "status": status,
        "primary_metric": primary_metric,
        "evidence": evidence,
        "next_action": next_action,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _rows_by_case(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row.get("case_id", ""))].append(row)
    return result


def _truthy(value: Any) -> bool:
    return str(value).lower() == "true"


def _touches_remote(row: dict[str, str]) -> bool:
    lines = set(filter(None, str(row.get("touched_lines", "")).split("|")))
    lines.add(row.get("source_line", ""))
    lines.add(row.get("target_line", ""))
    return bool({_normal_line(line) for line in lines} & REMOTE_LINES)


def _normal_line(value: Any) -> str:
    return physical.normalize_line(str(value or ""))


def _phase_code(phase: str) -> str:
    text = str(phase)
    for code in ("H1", "H2", "H3", "H4", "H5"):
        if text.startswith(code):
            return code
    return text


def _predicate(row: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(row.get("predicate_values", "")).split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def _move_count(row: dict[str, Any]) -> int:
    if row.get("selected_move_count"):
        return _int(row.get("selected_move_count"))
    return len([item for item in str(row.get("move_nos", "")).split("|") if item])


def _selected_examples(rows: list[dict[str, str]], limit: int) -> str:
    items = []
    for row in rows[:limit]:
        items.append(f"{row.get('case_id')}#{row.get('hook_index')}:{row.get('source_line')}->{row.get('target_line')}")
    return "|".join(items)


def _first_true(flags: list[bool]) -> int:
    for index, flag in enumerate(flags, start=1):
        if flag:
            return index
    return 0


def _ratio(numerator: int, denominator: int) -> float | str:
    if not numerator or not denominator:
        return ""
    return round(numerator / denominator, 4)


def _p(values: list[int], quantile: float) -> int:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return 0
    index = int(round((len(clean) - 1) * quantile))
    return clean[index]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _counter_text(counter: Counter[Any], limit: int) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common(limit) if key)


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
