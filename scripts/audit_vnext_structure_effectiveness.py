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
DEPOT_REMOTE_LINES = set(physical.DEPOT_TARGET_LINES) | {"卸轮线"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit whether vNext structures are effective, not only wired.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--manual-root", default="data/人工调车数据")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir) if args.output_dir else artifact_dir / "structure_effectiveness_audit"
    output_dir.mkdir(parents=True, exist_ok=True)

    manual = read_manual_plans(Path(args.manual_root))
    case_rows = _read_csv(artifact_dir / "case_summary.csv")
    step_rows = _read_csv(artifact_dir / "step_trace.csv")
    phase_rows = _read_csv(artifact_dir / "phase_gate_records.csv")
    structure_rows = _read_csv(artifact_dir / "structure_node_metrics.csv")

    selected_steps_by_case = _selected_steps_by_case(step_rows)
    phases_by_case = _rows_by_case(phase_rows)
    structure_by_case = _rows_by_case(structure_rows)

    case_audit_rows: list[dict[str, Any]] = []
    for case in case_rows:
        case_id = case.get("case_id", "")
        operations = _read_response_operations(artifact_dir, case_id)
        selected_steps = selected_steps_by_case.get(case_id, [])
        phase_records = phases_by_case.get(case_id, [])
        structure_records = structure_by_case.get(case_id, [])
        case_audit_rows.append(
            _case_audit_row(
                case=case,
                manual=manual.get(case_id),
                operations=operations,
                selected_steps=selected_steps,
                phase_records=phase_records,
                structure_records=structure_records,
            )
        )

    template_rows = _template_effectiveness_rows(step_rows)
    structure_summary_rows = _structure_summary_rows(case_audit_rows, template_rows, phase_rows, step_rows, structure_rows)
    summary = _summary(case_audit_rows, template_rows, structure_summary_rows)

    physical.write_csv(output_dir / "case_structure_effectiveness.csv", case_audit_rows)
    physical.write_csv(output_dir / "template_effectiveness.csv", template_rows)
    physical.write_csv(output_dir / "structure_effectiveness_summary.csv", structure_summary_rows)
    physical.write_json(output_dir / "structure_effectiveness_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def _case_audit_row(
    *,
    case: dict[str, str],
    manual: dict[str, Any] | None,
    operations: list[dict[str, Any]],
    selected_steps: list[dict[str, str]],
    phase_records: list[dict[str, str]],
    structure_records: list[dict[str, str]],
) -> dict[str, Any]:
    case_id = case.get("case_id", "")
    business_ops = [op for op in operations if op.get("Action") in {"Get", "Put"}]
    remote_flags = [_op_line(op) in DEPOT_REMOTE_LINES for op in business_ops]
    first_remote = _first_index(remote_flags)
    first_depot_put = _first_index(
        _op_line(op) in physical.DEPOT_TARGET_LINES and op.get("Action") == "Put"
        for op in business_ops
    )
    remote_segment_count, max_remote_segment_len = _segments(remote_flags)
    phase_counts = Counter(_phase_code(row.get("phase", "")) for row in selected_steps)
    target_phase_counts = Counter(_predicate(row).get("target_phase", "") for row in phase_records)
    template_counts = Counter(row.get("template_name", "") for row in selected_steps if row.get("template_name"))
    h4_steps = [row for row in selected_steps if _phase_code(row.get("phase", "")) == "H4"]
    h4_fragments = [
        row
        for row in h4_steps
        if _step_contract_reduction(row) <= 1 and _step_move_count(row) <= 2
    ]
    h4_front_touches = [
        row
        for row in h4_steps
        if not _touches_remote(row)
    ]
    h2_depot_outbound = [
        row
        for row in selected_steps
        if _phase_code(row.get("phase", "")) == "H2" and row.get("template_name") == "depot_outbound_session"
    ]
    manual_hook = _manual_int(manual, "manual_hook_count")
    manual_remote_transition = _manual_int(manual, "manual_remote_business_transition_count")
    manual_first_depot = _manual_int(manual, "manual_first_depot_digest_hook")
    solver_hook = _int(case.get("hook_count"))
    solver_remote_transition = _int(case.get("remote_business_transition_count"))
    first_remote_ratio = _ratio(first_remote, max(1, len(business_ops)))
    manual_first_depot_ratio = _ratio(manual_first_depot, manual_hook)
    issue_flags = _issue_flags(
        status=case.get("status", ""),
        final_unsatisfied=_int(case.get("final_unsatisfied")),
        solver_remote_transition=solver_remote_transition,
        manual_remote_transition=manual_remote_transition,
        solver_hook=solver_hook,
        manual_hook=manual_hook,
        first_remote_ratio=first_remote_ratio,
        manual_first_depot_ratio=manual_first_depot_ratio,
        h2_depot_outbound_count=len(h2_depot_outbound),
        h3_step_count=phase_counts.get("H3", 0),
        target_h3_count=target_phase_counts.get("H3", 0),
        h4_step_count=phase_counts.get("H4", 0),
        h4_fragment_count=len(h4_fragments),
        remote_segment_count=remote_segment_count,
        h4_front_touch_count=len(h4_front_touches),
    )
    generated = sum(_int(row.get("generated_candidate_count")) for row in structure_records)
    accepted = sum(_int(row.get("accepted_candidate_count")) for row in structure_records)
    phase_veto = sum(_int(row.get("phase_veto_count")) for row in structure_records)
    resource_reject = sum(_int(row.get("resource_violation_count")) for row in structure_records)
    return {
        "case_id": case_id,
        "status": case.get("status", ""),
        "blocked_reason": case.get("blocked_reason", ""),
        "manual_hook_count": manual_hook,
        "solver_hook_count": solver_hook,
        "hook_delta": solver_hook - manual_hook if manual_hook else "",
        "hook_ratio": round(solver_hook / manual_hook, 4) if manual_hook else "",
        "manual_remote_transition_count": manual_remote_transition,
        "solver_remote_transition_count": solver_remote_transition,
        "remote_transition_delta": solver_remote_transition - manual_remote_transition if manual_remote_transition else "",
        "manual_first_depot_digest_hook": manual_first_depot or "",
        "manual_first_depot_digest_ratio": manual_first_depot_ratio,
        "solver_first_remote_business_hook": first_remote or "",
        "solver_first_remote_business_ratio": first_remote_ratio,
        "solver_first_depot_put_hook": first_depot_put or "",
        "remote_segment_count": remote_segment_count,
        "max_remote_segment_len": max_remote_segment_len,
        "h1_selected_steps": phase_counts.get("H1", 0),
        "h2_selected_steps": phase_counts.get("H2", 0),
        "h3_selected_steps": phase_counts.get("H3", 0),
        "h4_selected_steps": phase_counts.get("H4", 0),
        "h5_selected_steps": phase_counts.get("H5", 0),
        "target_h3_count": target_phase_counts.get("H3", 0),
        "h2_depot_outbound_session_count": len(h2_depot_outbound),
        "h4_fragment_step_count": len(h4_fragments),
        "h4_front_touch_step_count": len(h4_front_touches),
        "selected_template_top": _counter_text(template_counts, 10),
        "candidate_generated_count": generated,
        "candidate_accepted_count": accepted,
        "phase_veto_count": phase_veto,
        "resource_reject_count": resource_reject,
        "issue_flags": "|".join(issue_flags),
    }


def _issue_flags(
    *,
    status: str,
    final_unsatisfied: int,
    solver_remote_transition: int,
    manual_remote_transition: int,
    solver_hook: int,
    manual_hook: int,
    first_remote_ratio: float,
    manual_first_depot_ratio: float,
    h2_depot_outbound_count: int,
    h3_step_count: int,
    target_h3_count: int,
    h4_step_count: int,
    h4_fragment_count: int,
    remote_segment_count: int,
    h4_front_touch_count: int,
) -> list[str]:
    flags: list[str] = []
    if status != "completed" or final_unsatisfied:
        flags.append("blocked_or_unsatisfied")
    if manual_hook and solver_hook > manual_hook * 1.25:
        flags.append("hook_count_far_above_manual")
    if manual_remote_transition and solver_remote_transition > manual_remote_transition + 2:
        flags.append("remote_transition_far_above_manual")
    if manual_first_depot_ratio and first_remote_ratio and first_remote_ratio + 0.2 < manual_first_depot_ratio:
        flags.append("remote_touched_too_early")
    if h2_depot_outbound_count:
        flags.append("h2_opens_depot_outbound")
    if target_h3_count and h3_step_count == 0:
        flags.append("h3_target_not_executed_as_phase")
    if h4_step_count > 6:
        flags.append("h4_not_compact")
    if h4_fragment_count > max(2, h4_step_count // 3):
        flags.append("h4_fragmented_single_car_work")
    if remote_segment_count > 3:
        flags.append("remote_session_not_continuous")
    if h4_front_touch_count > 0:
        flags.append("h4_contains_front_work")
    return flags


def _template_effectiveness_rows(step_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    selected = [row for row in step_rows if _is_selected(row)]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        grouped[row.get("template_name", "")].append(row)
    rows: list[dict[str, Any]] = []
    for template, items in sorted(grouped.items()):
        if not template:
            continue
        reductions = [_step_contract_reduction(row) for row in items]
        move_counts = [_step_move_count(row) for row in items]
        phases = Counter(_phase_code(row.get("phase", "")) for row in items)
        remote_touches = sum(1 for row in items if _touches_remote(row))
        rows.append(
            {
                "template_name": template,
                "selected_count": len(items),
                "phase_counts": _counter_text(phases, 8),
                "remote_touch_count": remote_touches,
                "contract_reduction_sum": sum(reductions),
                "contract_reduction_mean": round(statistics.mean(reductions), 4) if reductions else 0,
                "contract_reduction_p50": _percentile(reductions, 0.5),
                "move_count_mean": round(statistics.mean(move_counts), 4) if move_counts else 0,
                "low_effect_step_count": sum(1 for value in reductions if value <= 1),
                "zero_effect_step_count": sum(1 for value in reductions if value <= 0),
            }
        )
    return rows


def _structure_summary_rows(
    case_rows: list[dict[str, Any]],
    template_rows: list[dict[str, Any]],
    phase_rows: list[dict[str, str]],
    step_rows: list[dict[str, str]],
    structure_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    case_count = len(case_rows)
    completed = sum(1 for row in case_rows if row["status"] == "completed")
    issue_counter = Counter()
    for row in case_rows:
        issue_counter.update(flag for flag in str(row.get("issue_flags", "")).split("|") if flag)
    selected = [row for row in step_rows if _is_selected(row)]
    phase_counts = Counter(_phase_code(row.get("phase", "")) for row in selected)
    target_phase_counts = Counter(_predicate(row).get("target_phase", "") for row in phase_rows)
    h4_steps = [row for row in selected if _phase_code(row.get("phase", "")) == "H4"]
    h4_remote = [row for row in h4_steps if _touches_remote(row)]
    template_by_name = {row["template_name"]: row for row in template_rows}
    generated = sum(_int(row.get("generated_candidate_count")) for row in structure_rows)
    accepted = sum(_int(row.get("accepted_candidate_count")) for row in structure_rows)
    rows = [
        _summary_row(
            "FlowContractCoverage",
            "wired",
            "pass" if structure_rows and generated else "fail",
            generated,
            case_count,
            f"generated={generated};accepted={accepted}",
        ),
        _summary_row(
            "HumanPhaseGate",
            "phase_effect",
            "fail" if issue_counter["remote_touched_too_early"] or issue_counter["remote_transition_far_above_manual"] else "pass",
            len(phase_rows),
            case_count,
            f"phase_counts={_counter_text(phase_counts, 8)};target_phase_counts={_counter_text(target_phase_counts, 8)}",
        ),
        _summary_row(
            "H1FrontOrganization",
            "phase_effect",
            "weak" if issue_counter["remote_touched_too_early"] else "pass",
            phase_counts.get("H1", 0),
            case_count,
            f"remote_touched_too_early_cases={issue_counter['remote_touched_too_early']}",
        ),
        _summary_row(
            "H2Cun4PortShaping",
            "phase_effect",
            "fail" if issue_counter["h2_opens_depot_outbound"] else "weak",
            phase_counts.get("H2", 0),
            case_count,
            f"h2_opens_depot_outbound_cases={issue_counter['h2_opens_depot_outbound']};target_H2={target_phase_counts.get('H2', 0)}",
        ),
        _summary_row(
            "H3ReleaseAcceptBoundary",
            "phase_effect",
            "fail" if issue_counter["h3_target_not_executed_as_phase"] or phase_counts.get("H3", 0) == 0 else "weak",
            phase_counts.get("H3", 0),
            case_count,
            f"target_H3={target_phase_counts.get('H3', 0)};actual_H3={phase_counts.get('H3', 0)}",
        ),
        _summary_row(
            "H4RemoteDepotSession",
            "phase_effect",
            "fail" if issue_counter["remote_session_not_continuous"] or issue_counter["h4_not_compact"] else "pass",
            phase_counts.get("H4", 0),
            case_count,
            (
                f"h4_remote_steps={len(h4_remote)};h4_total_steps={len(h4_steps)};"
                f"remote_session_not_continuous_cases={issue_counter['remote_session_not_continuous']};"
                f"h4_not_compact_cases={issue_counter['h4_not_compact']};"
                f"h4_fragmented_cases={issue_counter['h4_fragmented_single_car_work']}"
            ),
        ),
        _summary_row(
            "H5Closeout",
            "phase_effect",
            "weak" if issue_counter["blocked_or_unsatisfied"] else "pass",
            phase_counts.get("H5", 0),
            case_count,
            f"blocked_or_unsatisfied_cases={issue_counter['blocked_or_unsatisfied']}",
        ),
    ]
    for template_name in (
        "depot_outbound_session",
        "remote_session_prefix_batch_digest_restore",
        "depot_inbound_prefix_multidrop_session",
        "remote_depot_direct_accessible_prefix",
        "depot_locked_tail_slot_fill",
    ):
        row = template_by_name.get(template_name)
        if not row:
            rows.append(_summary_row(template_name, "template_effect", "missing", 0, case_count, "not selected"))
            continue
        low = _int(row["low_effect_step_count"])
        selected_count = _int(row["selected_count"])
        status = "pass"
        if selected_count and low / selected_count > 0.5:
            status = "weak"
        rows.append(
            _summary_row(
                template_name,
                "template_effect",
                status,
                selected_count,
                case_count,
                (
                    f"phase_counts={row['phase_counts']};"
                    f"reduction_mean={row['contract_reduction_mean']};"
                    f"low_effect={low}/{selected_count}"
                ),
            )
        )
    return rows


def _summary(
    case_rows: list[dict[str, Any]],
    template_rows: list[dict[str, Any]],
    structure_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    completed = sum(1 for row in case_rows if row["status"] == "completed")
    issue_counter = Counter()
    for row in case_rows:
        issue_counter.update(flag for flag in str(row.get("issue_flags", "")).split("|") if flag)
    return {
        "case_count": len(case_rows),
        "completed": completed,
        "blocked": len(case_rows) - completed,
        "solver_hook_distribution": _distribution([_int(row["solver_hook_count"]) for row in case_rows]),
        "solver_remote_transition_distribution": _distribution([_int(row["solver_remote_transition_count"]) for row in case_rows]),
        "remote_transition_delta_distribution": _distribution(
            [_int(row["remote_transition_delta"]) for row in case_rows if row["remote_transition_delta"] != ""]
        ),
        "solver_first_remote_ratio_distribution": _distribution(
            [float(row["solver_first_remote_business_ratio"]) for row in case_rows if row["solver_first_remote_business_ratio"] != ""]
        ),
        "issue_counts": dict(issue_counter.most_common()),
        "structure_status_counts": dict(Counter(row["effectiveness_status"] for row in structure_rows)),
        "top_template_effectiveness": template_rows[:20],
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _read_response_operations(artifact_dir: Path, case_id: str) -> list[dict[str, Any]]:
    path = artifact_dir / "responses" / f"{case_id}.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("Data", {}).get("Operations", []) or [])


def _selected_steps_by_case(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if _is_selected(row):
            result[row["case_id"]].append(row)
    return result


def _rows_by_case(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        result[row["case_id"]].append(row)
    return result


def _is_selected(row: dict[str, str]) -> bool:
    return str(row.get("selected", "")).lower() == "true"


def _touches_remote(row: dict[str, str]) -> bool:
    touched = set(filter(None, str(row.get("touched_lines", "")).split("|")))
    touched.add(row.get("source_line", ""))
    touched.add(row.get("target_line", ""))
    return bool(touched & DEPOT_REMOTE_LINES)


def _step_contract_reduction(row: dict[str, str]) -> int:
    return _int(row.get("contract_reduction") or row.get("selected_contract_reduction"))


def _step_move_count(row: dict[str, str]) -> int:
    if row.get("selected_move_count"):
        return _int(row.get("selected_move_count"))
    return len([no for no in str(row.get("move_nos", "")).split("|") if no])


def _op_line(op: dict[str, Any]) -> str:
    return physical.normalize_line(str(op.get("Line", "")))


def _phase_code(phase: str) -> str:
    if phase.startswith("H1"):
        return "H1"
    if phase.startswith("H2"):
        return "H2"
    if phase.startswith("H3"):
        return "H3"
    if phase.startswith("H4"):
        return "H4"
    if phase.startswith("H5"):
        return "H5"
    return phase


def _predicate(row: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(row.get("predicate_values", "")).split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def _first_index(flags: Any) -> int:
    for index, flag in enumerate(flags, start=1):
        if flag:
            return index
    return 0


def _segments(flags: list[bool]) -> tuple[int, int]:
    count = 0
    current = 0
    maximum = 0
    previous = False
    for flag in flags:
        if flag:
            current += 1
            maximum = max(maximum, current)
            if not previous:
                count += 1
        else:
            current = 0
        previous = flag
    return count, maximum


def _manual_int(manual: dict[str, Any] | None, key: str) -> int:
    if not manual:
        return 0
    return _int(manual.get(key))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ratio(numerator: int, denominator: int) -> float | str:
    if not numerator or not denominator:
        return ""
    return round(numerator / denominator, 4)


def _counter_text(counter: Counter[Any], limit: int) -> str:
    return "|".join(f"{key}:{value}" for key, value in counter.most_common(limit) if key)


def _summary_row(
    structure: str,
    metric_group: str,
    status: str,
    observed: int,
    total: int,
    evidence: str,
) -> dict[str, Any]:
    return {
        "structure": structure,
        "metric_group": metric_group,
        "effectiveness_status": status,
        "observed": observed,
        "total": total,
        "evidence": evidence,
    }


def _distribution(values: list[int | float]) -> dict[str, Any]:
    values = [value for value in values if value != ""]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 4),
        "p50": _percentile(values, 0.5),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.9),
        "min": min(values),
        "max": max(values),
    }


def _percentile(values: list[int | float], percentile: float) -> int | float:
    if not values:
        return 0
    ordered = sorted(values)
    value = ordered[round((len(ordered) - 1) * percentile)]
    return round(value, 4) if isinstance(value, float) else value


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
