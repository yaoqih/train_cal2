#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import replay_validator as rv

DEFAULT_MANUAL_DIR = ROOT / "artifacts" / "manual_restored_interface" / "bundles"
DEFAULT_ALGORITHM_DIR = ROOT / "artifacts" / "stage4_refactor_single_full_final"
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "multi_get_capability_diagnosis"

DEPOT_INNER_LINES = {f"修{index}库内" for index in range(1, 5)}
STORAGE_LINES = {"存1线", "存2线", "存3线", "存5线北", "存5线南"}
TOPOLOGY_SENSITIVE_LINES = {
    "机南",
    "洗油北",
    "机走棚",
    "机走北",
    "洗罐线北",
    "调梁线北",
    "存4南",
    "存5线北",
    "机北1",
    "机北2",
    "存1线",
    "预修线",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("Data") or {}
    return sorted(data.get("Operations") or [], key=lambda row: int(row.get("Index") or 0))


def car_no(car: dict[str, Any]) -> str:
    return str(car.get("No") or car.get("CarNo") or "")


def target_map(request: dict[str, Any]) -> dict[str, set[str]]:
    return {
        car_no(car): {str(line) for line in car.get("TargetLines") or [] if line}
        for car in request.get("StartStatus") or []
        if car_no(car)
    }


def pull_weights(request: dict[str, Any]) -> dict[str, int]:
    return {
        car_no(car): 4 if car.get("IsHeavy") else 1
        for car in request.get("StartStatus") or []
        if car_no(car)
    }


def op_action(row: dict[str, Any]) -> str:
    return str(row.get("Action") or "")


def op_line(row: dict[str, Any]) -> str:
    return str(row.get("Line") or "")


def op_move(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(no) for no in row.get("MoveCars") or [])


def op_train(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(no) for no in row.get("TrainCars") or [])


def op_path(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(line) for line in row.get("PassbyPath") or [])


def percentile(values: Iterable[float], quantile: float) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        return 0.0
    if len(rows) == 1:
        return rows[0]
    offset = (len(rows) - 1) * quantile
    low = int(offset)
    high = min(low + 1, len(rows) - 1)
    weight = offset - low
    return rows[low] * (1.0 - weight) + rows[high] * weight


def ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def phase_labels(rows: list[dict[str, Any]]) -> list[str]:
    """Apply the event boundaries documented in 人工Response调车全流程阶段统计.md."""
    first_depot_get = next(
        (index for index, row in enumerate(rows) if op_action(row) == "Get" and op_line(row) in DEPOT_INNER_LINES),
        None,
    )
    first_storage_get = next(
        (
            index
            for index, row in enumerate(rows)
            if op_action(row) == "Get"
            and op_line(row) in STORAGE_LINES
            and (first_depot_get is None or index < first_depot_get)
        ),
        None,
    )
    first_store4_put = next(
        (
            index
            for index, row in enumerate(rows)
            if op_action(row) == "Put"
            and op_line(row) == "存4线"
            and (first_depot_get is None or index >= first_depot_get)
        ),
        None,
    )
    depot_puts_after_store4 = [
        index
        for index, row in enumerate(rows)
        if op_action(row) == "Put"
        and op_line(row) in DEPOT_INNER_LINES
        and (first_store4_put is None or index > first_store4_put)
    ]
    last_depot_put = depot_puts_after_store4[-1] if depot_puts_after_store4 else None

    labels: list[str] = []
    for index, _row in enumerate(rows):
        if last_depot_put is not None and index > last_depot_put:
            labels.append("P5")
        elif first_store4_put is not None and index > first_store4_put:
            labels.append("P4")
        elif first_depot_get is not None and index >= first_depot_get:
            labels.append("P3")
        elif first_storage_get is not None and index >= first_storage_get:
            labels.append("P2")
        else:
            labels.append("P1")
    return labels


@dataclass(frozen=True)
class OpContext:
    row: dict[str, Any]
    before: tuple[str, ...]
    after: tuple[str, ...]
    phase: str


def operation_contexts(rows: list[dict[str, Any]], *, fixed_phase: str = "") -> tuple[list[OpContext], Counter[str]]:
    phases = [fixed_phase] * len(rows) if fixed_phase else phase_labels(rows)
    contexts: list[OpContext] = []
    diagnostics: Counter[str] = Counter()
    before: tuple[str, ...] = ()
    for row, phase in zip(rows, phases):
        action = op_action(row)
        move = op_move(row)
        after = op_train(row)
        if action == "Get":
            expected = (*before, *move)
            if after != expected:
                diagnostics["get_traincars_mismatch"] += 1
        elif action == "Put":
            if len(move) > len(before) or (move and before[-len(move) :] != move):
                diagnostics["put_not_tail"] += 1
            expected = before[: len(before) - len(move)] if move and len(move) <= len(before) else before
            if after != expected:
                diagnostics["put_traincars_mismatch"] += 1
        contexts.append(OpContext(row=row, before=before, after=after, phase=phase))
        before = after
    if before:
        diagnostics["response_ends_with_carry"] += 1
    return contexts, diagnostics


def carry_sessions(contexts: list[OpContext]) -> list[list[OpContext]]:
    sessions: list[list[OpContext]] = []
    current: list[OpContext] = []
    for context in contexts:
        action = op_action(context.row)
        if not current and action not in {"Get", "Weigh"} and not context.before:
            continue
        current.append(context)
        if not context.after:
            sessions.append(current)
            current = []
    if current:
        sessions.append(current)
    return sessions


def phase_span(contexts: list[OpContext]) -> str:
    order = ("P1", "P2", "P3", "P4", "P5", "S4")
    present = {context.phase for context in contexts}
    return ">".join(phase for phase in order if phase in present)


def analyze_group(
    contexts: list[OpContext],
    targets: dict[str, set[str]],
    weights: dict[str, int],
) -> dict[str, Any]:
    seen: set[str] = set()
    first_origin: dict[str, str] = {}
    final_line: dict[str, str] = {}
    get_lines: list[str] = []
    fresh_get_lines: list[str] = []
    fresh_flags: list[bool] = []
    get_hooks = 0
    put_hooks = 0
    weigh_hooks = 0
    partial_put_hooks = 0
    retained_get_hooks = 0
    fresh_get_hooks = 0
    reget_hooks = 0
    move_car_events = 0
    route_nodes = 0
    consecutive_gets = 0
    max_consecutive_gets = 0
    initial_get_run = 0
    still_initial_get_run = True
    active_spans: Counter[str] = Counter()
    longest_carried_span = 0

    for context in contexts:
        row = context.row
        action = op_action(row)
        line = op_line(row)
        moved = op_move(row)
        move_car_events += len(moved)
        route_nodes += len(op_path(row))
        present = set(context.before) | set(context.after)
        for no in set(active_spans) - present:
            active_spans.pop(no, None)
        for no in present:
            active_spans[no] += 1
            longest_carried_span = max(longest_carried_span, active_spans[no])
        if action == "Get":
            get_hooks += 1
            get_lines.append(line)
            fresh = any(no not in seen for no in moved)
            fresh_flags.append(fresh)
            if fresh:
                fresh_get_hooks += 1
                fresh_get_lines.append(line)
            if all(no in seen for no in moved):
                reget_hooks += 1
            if context.before:
                retained_get_hooks += 1
            for no in moved:
                if no not in seen:
                    first_origin[no] = line
                seen.add(no)
            consecutive_gets += 1
            max_consecutive_gets = max(max_consecutive_gets, consecutive_gets)
            if still_initial_get_run:
                initial_get_run += 1
        elif action == "Put":
            put_hooks += 1
            if context.after:
                partial_put_hooks += 1
            for no in moved:
                final_line[no] = line
            consecutive_gets = 0
            still_initial_get_run = False
        elif action == "Weigh":
            weigh_hooks += 1
            consecutive_gets = 0
        else:
            consecutive_gets = 0

    partial_put_then_get = False
    for index, context in enumerate(contexts):
        if op_action(context.row) != "Put" or not context.after:
            continue
        seen_before_later_get = set(seen_no for prior in contexts[: index + 1] for seen_no in op_move(prior.row) if op_action(prior.row) == "Get")
        for later in contexts[index + 1 :]:
            # A macro may contain several independently closed carry sessions.
            # Once the retained consist is empty, a later Get is not a
            # partial-Put continuation of this session.
            if not later.before:
                break
            if op_action(later.row) != "Get":
                if not later.after:
                    break
                continue
            if any(no not in seen_before_later_get for no in op_move(later.row)):
                partial_put_then_get = True
                break
            if not later.after:
                break
        if partial_put_then_get:
            break

    productive_nos = {
        no
        for no, origin in first_origin.items()
        if final_line.get(no) and final_line.get(no) != origin
    }
    restored_nos = {
        no
        for no, origin in first_origin.items()
        if final_line.get(no) == origin
    }
    delivered_nos = {
        no
        for no in first_origin
        if final_line.get(no) and final_line[no] in targets.get(no, set())
    }
    temporary_relocated_nos = productive_nos - delivered_nos
    productive_source_lines = {first_origin[no] for no in productive_nos}
    productive_final_lines = {final_line[no] for no in productive_nos}
    fresh_source_lines = set(fresh_get_lines)
    put_lines = {op_line(context.row) for context in contexts if op_action(context.row) == "Put"}
    flow_edges = {(first_origin[no], final_line[no]) for no in productive_nos}
    loaded_hooks = get_hooks + put_hooks + weigh_hooks
    actual_transfer_hooks = get_hooks + put_hooks
    consolidation_gain = max(0, 2 * len(flow_edges) - actual_transfer_hooks)
    sequence = ">".join(
        f"{op_action(context.row)[0]}:{op_line(context.row)}({len(op_move(context.row))})"
        for context in contexts
        if op_action(context.row) in {"Get", "Put", "Weigh"}
    )

    return {
        "hooks": loaded_hooks,
        "get_hooks": get_hooks,
        "put_hooks": put_hooks,
        "weigh_hooks": weigh_hooks,
        "fresh_get_hooks": fresh_get_hooks,
        "reget_hooks": reget_hooks,
        "retained_get_hooks": retained_get_hooks,
        "partial_put_hooks": partial_put_hooks,
        "unique_cars": len(first_origin),
        "peak_carry": max((len(context.after) for context in contexts), default=0),
        "peak_pull_equivalent": max(
            (sum(weights.get(no, 1) for no in context.after) for context in contexts),
            default=0,
        ),
        "minimum_nonzero_carry": min(
            (len(context.after) for context in contexts if context.after),
            default=0,
        ),
        "longest_carried_span": longest_carried_span,
        "long_anchor_session": int(loaded_hooks >= 10 and longest_carried_span >= loaded_hooks - 1),
        "move_car_events": move_car_events,
        "cars_per_hook": round(move_car_events / loaded_hooks, 3) if loaded_hooks else 0.0,
        "route_nodes": route_nodes,
        "max_consecutive_gets": max_consecutive_gets,
        "initial_get_run": initial_get_run,
        "distinct_get_lines": len(set(get_lines)),
        "distinct_fresh_source_lines": len(fresh_source_lines),
        "distinct_put_lines": len(put_lines),
        "productive_source_lines": len(productive_source_lines),
        "productive_final_lines": len(productive_final_lines),
        "productive_cars": len(productive_nos),
        "restored_cars": len(restored_nos),
        "target_delivered_cars": len(delivered_nos),
        "temporary_relocated_cars": len(temporary_relocated_nos),
        "flow_edges": len(flow_edges),
        "hook_consolidation_gain": consolidation_gain,
        "one_get_one_put": int(get_hooks == 1 and put_hooks == 1),
        "multi_get": int(get_hooks >= 2),
        "multi_source": int(len(fresh_source_lines) >= 2),
        "productive_multi_get": int(fresh_get_hooks >= 2 and len(productive_nos) >= 2),
        "strategic_multi_source": int(len(productive_source_lines) >= 2),
        "structural_multi_source": int(len(fresh_source_lines) >= 2 and len(productive_source_lines) < 2),
        "multi_put": int(put_hooks >= 2),
        "partial_put_then_get": int(partial_put_then_get),
        "source_convergence": int(len(productive_source_lines) >= 2 and len(productive_final_lines) == 1),
        "target_fanout": int(len(productive_final_lines) >= 2),
        "cross_flow": int(len(productive_source_lines) >= 2 and len(productive_final_lines) >= 2),
        "get_lines": "|".join(sorted(set(get_lines))),
        "fresh_source_lines": "|".join(sorted(fresh_source_lines)),
        "put_lines": "|".join(sorted(put_lines)),
        "productive_sources": "|".join(sorted(productive_source_lines)),
        "productive_destinations": "|".join(sorted(productive_final_lines)),
        "sequence": sequence,
    }


def session_rows(
    *,
    dataset: str,
    case_id: str,
    rows: list[dict[str, Any]],
    targets: dict[str, set[str]],
    weights: dict[str, int],
    fixed_phase: str = "",
) -> tuple[list[dict[str, Any]], Counter[str], list[OpContext]]:
    contexts, diagnostics = operation_contexts(rows, fixed_phase=fixed_phase)
    output: list[dict[str, Any]] = []
    for session_index, group in enumerate(carry_sessions(contexts), start=1):
        metrics = analyze_group(group, targets, weights)
        output.append({
            "dataset": dataset,
            "case_id": case_id,
            "session": session_index,
            "start_index": int(group[0].row.get("Index") or 0),
            "end_index": int(group[-1].row.get("Index") or 0),
            "phase_start": group[0].phase,
            "phase_span": phase_span(group),
            "closed": int(not group[-1].after),
            **metrics,
        })
    return output, diagnostics, contexts


def response_metrics(
    dataset: str,
    case_id: str,
    rows: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any]:
    targets = target_map(request)
    get_counts: Counter[str] = Counter()
    put_counts: Counter[str] = Counter()
    move_events = 0
    for row in rows:
        moved = op_move(row)
        move_events += len(moved)
        if op_action(row) == "Get":
            get_counts.update(moved)
        elif op_action(row) == "Put":
            put_counts.update(moved)
    touched = set(get_counts) | set(put_counts)
    rehandled = {no for no, count in get_counts.items() if count > 1}
    target_put_then_get = 0
    temporary_put_then_get = 0
    for index, row in enumerate(rows):
        if op_action(row) != "Put":
            continue
        future_gets = {
            no
            for future in rows[index + 1 :]
            if op_action(future) == "Get"
            for no in op_move(future)
        }
        for no in op_move(row):
            if no not in future_gets:
                continue
            if op_line(row) in targets.get(no, set()):
                target_put_then_get += 1
            else:
                temporary_put_then_get += 1
    hooks = sum(op_action(row) in {"Get", "Put", "Weigh"} for row in rows)
    gets = sum(op_action(row) == "Get" for row in rows)
    final_line = {
        car_no(car): str(car.get("Line") or "")
        for car in request.get("StartStatus") or []
        if car_no(car)
    }
    for row in rows:
        if op_action(row) == "Put":
            for no in op_move(row):
                final_line[no] = op_line(row)
    final_line_unsatisfied = {
        no for no, lines in targets.items() if final_line.get(no, "") not in lines
    }
    return {
        "dataset": dataset,
        "case_id": case_id,
        "hooks": hooks,
        "get_hooks": gets,
        "put_hooks": sum(op_action(row) == "Put" for row in rows),
        "sessions": len(sessions),
        "one_get_one_put_sessions": sum(int(row["one_get_one_put"]) for row in sessions),
        "multi_get_sessions": sum(int(row["multi_get"]) for row in sessions),
        "multi_source_sessions": sum(int(row["multi_source"]) for row in sessions),
        "strategic_multi_source_sessions": sum(int(row["strategic_multi_source"]) for row in sessions),
        "structural_multi_source_sessions": sum(int(row["structural_multi_source"]) for row in sessions),
        "multi_put_sessions": sum(int(row["multi_put"]) for row in sessions),
        "partial_put_then_get_sessions": sum(int(row["partial_put_then_get"]) for row in sessions),
        "source_convergence_sessions": sum(int(row["source_convergence"]) for row in sessions),
        "target_fanout_sessions": sum(int(row["target_fanout"]) for row in sessions),
        "cross_flow_sessions": sum(int(row["cross_flow"]) for row in sessions),
        "hook_consolidation_gain": sum(int(row["hook_consolidation_gain"]) for row in sessions),
        "max_peak_carry": max((int(row["peak_carry"]) for row in sessions), default=0),
        "move_car_events": move_events,
        "cars_per_hook": round(move_events / hooks, 3) if hooks else 0.0,
        "touched_cars": len(touched),
        "rehandled_cars": len(rehandled),
        "rehandled_car_rate": ratio(len(rehandled), len(touched)),
        "temporary_put_then_get_car_events": temporary_put_then_get,
        "target_put_then_get_car_events": target_put_then_get,
        "closures_per_get": ratio(len(sessions), gets),
        "final_line_unsatisfied": len(final_line_unsatisfied),
        "long_sessions_ge_10": sum(int(row["hooks"]) >= 10 for row in sessions),
        "long_anchor_sessions": sum(int(row["long_anchor_session"]) for row in sessions),
        "max_pull_equivalent": max((int(row["peak_pull_equivalent"]) for row in sessions), default=0),
    }


def placement_rows(
    *, dataset: str, case_id: str, rows: list[dict[str, Any]], targets: dict[str, set[str]]
) -> list[dict[str, Any]]:
    future_get_indices: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if op_action(row) == "Get":
            for no in op_move(row):
                future_get_indices[no].append(index)
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if op_action(row) != "Put":
            continue
        for no in op_move(row):
            next_get = next((item for item in future_get_indices.get(no, []) if item > index), None)
            target_compatible = op_line(row) in targets.get(no, set())
            output.append({
                "dataset": dataset,
                "case_id": case_id,
                "operation_index": int(row.get("Index") or index + 1),
                "line": op_line(row),
                "car_no": no,
                "target_compatible": int(target_compatible),
                "temporary": int(not target_compatible),
                "recovered_later": int(next_get is not None),
                "dwell_operations": (next_get - index) if next_get is not None else -1,
                "topology_sensitive": int(op_line(row) in TOPOLOGY_SENSITIVE_LINES),
            })
    return output


def staging_recovery_rows(
    *, dataset: str, case_id: str, rows: list[dict[str, Any]], targets: dict[str, set[str]]
) -> list[dict[str, Any]]:
    latest_temporary_put: dict[str, tuple[int, str]] = {}
    output: list[dict[str, Any]] = []
    carry_before: tuple[str, ...] = ()
    for index, row in enumerate(rows):
        action = op_action(row)
        moved = op_move(row)
        if action == "Put":
            for no in moved:
                if op_line(row) not in targets.get(no, set()):
                    latest_temporary_put[no] = (index, op_line(row))
        elif action == "Get":
            recovered = [no for no in moved if no in latest_temporary_put and latest_temporary_put[no][1] == op_line(row)]
            if recovered:
                source_puts = {latest_temporary_put[no][0] for no in recovered}
                dwells = [index - latest_temporary_put[no][0] for no in recovered]
                output.append({
                    "dataset": dataset,
                    "case_id": case_id,
                    "get_index": int(row.get("Index") or index + 1),
                    "staging_line": op_line(row),
                    "recovered_cars": len(recovered),
                    "accumulation_put_hooks": len(source_puts),
                    "multi_put_accumulation": int(len(source_puts) >= 2),
                    "get_while_carrying": int(bool(carry_before)),
                    "median_dwell_operations": round(statistics.median(dwells), 2),
                })
                for no in recovered:
                    latest_temporary_put.pop(no, None)
        carry_before = op_train(row)
    return output


def load_manual(manual_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    successful_bundles: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for path in sorted(manual_dir.glob("*.json")):
        bundle = read_json(path)
        summary = bundle.get("Summary") or {}
        if not summary.get("success"):
            continue
        case_id = str(summary.get("case_id") or path.name.split("_", 1)[0])
        response_rows = operations(bundle.get("Response") or {})
        signature = json.dumps(response_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record = {
            "case_id": case_id,
            "bundle_id": str(summary.get("manual_file_id") or path.stem),
            "path": path,
            "request": bundle.get("Request") or {},
            "response": bundle.get("Response") or {},
            "rows": response_rows,
            "manual_hook_count": int(summary.get("manual_hook_count") or 0),
            "operation_count": len(response_rows),
            "signature": signature,
        }
        successful_bundles.append(record)
        key = (case_id, signature)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return successful_bundles, unique


def load_algorithm_cases(algorithm_dir: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in sorted(algorithm_dir.glob("*_response.json")):
        if path.name.endswith("_combined_response.json"):
            continue
        case_id = path.name.removesuffix("_response.json")
        request_path = algorithm_dir / f"{case_id}_stage4_request.json"
        combined_path = algorithm_dir / f"{case_id}_combined_response.json"
        summary_path = algorithm_dir / f"{case_id}_summary.json"
        request = read_json(request_path) if request_path.exists() else {}
        summary = read_json(summary_path) if summary_path.exists() else {}
        stage4_payload = read_json(path)
        combined_payload = read_json(combined_path) if combined_path.exists() else {}
        output.append({
            "case_id": case_id,
            "request": request,
            "stage4_payload": stage4_payload,
            "combined_payload": combined_payload,
            "stage4_rows": operations(stage4_payload),
            "combined_rows": operations(combined_payload),
            "summary": summary,
        })
    return output


def load_stage4_macros(
    algorithm_dir: Path,
    case: dict[str, Any],
    targets: dict[str, set[str]],
    weights: dict[str, int],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    trace_path = algorithm_dir / f"{case['case_id']}_trace.json"
    if not trace_path.exists():
        return [], Counter()
    accepted = [row for row in read_json(trace_path) if row.get("accepted")]
    contexts, diagnostics = operation_contexts(case["stage4_rows"], fixed_phase="S4")
    output: list[dict[str, Any]] = []
    offset = 0
    for macro_index, trace_row in enumerate(accepted, start=1):
        count = int(trace_row.get("operations") or 0)
        group = contexts[offset : offset + count]
        if not group:
            diagnostics["empty_accepted_macro"] += 1
            continue
        metrics = analyze_group(group, targets, weights)
        output.append({
            "dataset": "algorithm_stage4_macro",
            "case_id": case["case_id"],
            "macro": int(trace_row.get("macro") or macro_index),
            "start_index": int(group[0].row.get("Index") or offset + 1),
            "end_index": int(group[-1].row.get("Index") or offset + count),
            "reason": str(trace_row.get("reason") or ""),
            "source": str(trace_row.get("source") or ""),
            "target": str(trace_row.get("target") or ""),
            "closed": int(not group[-1].after),
            **metrics,
        })
        offset += count
    if offset != len(contexts):
        diagnostics["macro_operation_count_mismatch"] += 1
    return output, diagnostics


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def aggregate_sessions(
    rows: list[dict[str, Any]],
    dataset: str,
    *,
    phase: str = "",
    phase_start_only: bool = False,
    case_ids: set[str] | None = None,
    label: str = "",
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if row["dataset"] == dataset
        and (case_ids is None or str(row["case_id"]) in case_ids)
        and (
            not phase
            or (str(row["phase_start"]) == phase if phase_start_only else phase in str(row["phase_span"]).split(">"))
        )
    ]
    cases = {str(row["case_id"]) for row in selected}
    hooks = sum(int(row["hooks"]) for row in selected)
    return {
        "dataset": label or dataset + (f"_{phase}" if phase else ""),
        "cases": len(cases),
        "sessions": len(selected),
        "hooks": hooks,
        "one_get_one_put": sum(int(row["one_get_one_put"]) for row in selected),
        "multi_get": sum(int(row["multi_get"]) for row in selected),
        "multi_source": sum(int(row["multi_source"]) for row in selected),
        "strategic_multi_source": sum(int(row["strategic_multi_source"]) for row in selected),
        "structural_multi_source": sum(int(row["structural_multi_source"]) for row in selected),
        "multi_put": sum(int(row["multi_put"]) for row in selected),
        "partial_put_then_get": sum(int(row["partial_put_then_get"]) for row in selected),
        "source_convergence": sum(int(row["source_convergence"]) for row in selected),
        "target_fanout": sum(int(row["target_fanout"]) for row in selected),
        "cross_flow": sum(int(row["cross_flow"]) for row in selected),
        "reget_hooks": sum(int(row["reget_hooks"]) for row in selected),
        "restored_cars": sum(int(row["restored_cars"]) for row in selected),
        "temporary_relocated_cars": sum(int(row["temporary_relocated_cars"]) for row in selected),
        "hook_consolidation_gain": sum(int(row["hook_consolidation_gain"]) for row in selected),
        "one_get_one_put_rate": ratio(sum(int(row["one_get_one_put"]) for row in selected), len(selected)),
        "strategic_multi_source_rate": ratio(sum(int(row["strategic_multi_source"]) for row in selected), len(selected)),
        "multi_put_rate": ratio(sum(int(row["multi_put"]) for row in selected), len(selected)),
        "partial_put_then_get_rate": ratio(sum(int(row["partial_put_then_get"]) for row in selected), len(selected)),
        "cars_per_hook": round(
            sum(int(row["move_car_events"]) for row in selected) / hooks, 3
        ) if hooks else 0.0,
        "peak_carry_p50": round(percentile((int(row["peak_carry"]) for row in selected), 0.5), 2),
        "peak_carry_p95": round(percentile((int(row["peak_carry"]) for row in selected), 0.95), 2),
        "long_sessions_ge_10": sum(int(row["hooks"]) >= 10 for row in selected),
        "long_anchor_sessions": sum(int(row["long_anchor_session"]) for row in selected),
        "pull_equivalent_over_20": sum(int(row["peak_pull_equivalent"]) > 20 for row in selected),
    }


def replay_audit(
    *, dataset: str, case_id: str, request: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    replayed, replay_bad = rv.replay(request, response)
    business_bad = rv.business_errors(request, rv.final_cars(response, replayed))
    physical = [item for item in replay_bad if item.kind == "physical"]
    state = [item for item in replay_bad if item.kind == "state"]
    schema = [item for item in replay_bad if item.kind == "schema"]
    return {
        "dataset": dataset,
        "case_id": case_id,
        "schema_violations": len(schema),
        "physical_violations": len(physical),
        "state_violations": len(state),
        "business_violations": len(business_bad),
        "operation_hard_clean": int(not schema and not physical),
        "full_clean": int(not schema and not physical and not state and not business_bad),
        "physical_codes": "|".join(
            f"{code}:{count}" for code, count in Counter(item.code for item in physical).most_common()
        ),
        "state_codes": "|".join(
            f"{code}:{count}" for code, count in Counter(item.code for item in state).most_common()
        ),
        "business_codes": "|".join(
            f"{code}:{count}" for code, count in Counter(item.code for item in business_bad).most_common()
        ),
    }


def aggregate_line_placements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["dataset"]), str(row["line"]))].append(row)
    output: list[dict[str, Any]] = []
    for (dataset, line), items in grouped.items():
        hook_ids = {(row["case_id"], row["operation_index"]) for row in items}
        temporary = [row for row in items if row["temporary"]]
        recovered = [row for row in temporary if row["recovered_later"]]
        dwells = [int(row["dwell_operations"]) for row in recovered]
        output.append({
            "dataset": dataset,
            "line": line,
            "put_hooks": len(hook_ids),
            "car_placements": len(items),
            "temporary_car_placements": len(temporary),
            "temporary_rate": ratio(len(temporary), len(items)),
            "temporary_recovered": len(recovered),
            "temporary_left": len(temporary) - len(recovered),
            "median_recovery_dwell": round(statistics.median(dwells), 2) if dwells else 0.0,
            "topology_sensitive": int(line in TOPOLOGY_SENSITIVE_LINES),
        })
    return sorted(output, key=lambda row: (row["dataset"], -int(row["temporary_car_placements"]), row["line"]))


def paired_rows(
    manual_metrics: dict[str, dict[str, Any]],
    algorithm_metrics: dict[str, dict[str, Any]],
    status_by_case: dict[str, str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for case_id in sorted(set(manual_metrics) & set(algorithm_metrics)):
        manual = manual_metrics[case_id]
        algorithm = algorithm_metrics[case_id]
        output.append({
            "case_id": case_id,
            "algorithm_status": status_by_case.get(case_id, ""),
            "manual_hooks": manual["hooks"],
            "algorithm_hooks": algorithm["hooks"],
            "hook_delta_algorithm_minus_manual": int(algorithm["hooks"]) - int(manual["hooks"]),
            "manual_sessions": manual["sessions"],
            "algorithm_sessions": algorithm["sessions"],
            "session_delta": int(algorithm["sessions"]) - int(manual["sessions"]),
            "manual_strategic_multi_source": manual["strategic_multi_source_sessions"],
            "algorithm_strategic_multi_source": algorithm["strategic_multi_source_sessions"],
            "strategic_gap_manual_minus_algorithm": int(manual["strategic_multi_source_sessions"]) - int(algorithm["strategic_multi_source_sessions"]),
            "manual_partial_put_then_get": manual["partial_put_then_get_sessions"],
            "algorithm_partial_put_then_get": algorithm["partial_put_then_get_sessions"],
            "manual_multi_put": manual["multi_put_sessions"],
            "algorithm_multi_put": algorithm["multi_put_sessions"],
            "manual_one_get_one_put": manual["one_get_one_put_sessions"],
            "algorithm_one_get_one_put": algorithm["one_get_one_put_sessions"],
            "manual_rehandled_cars": manual["rehandled_cars"],
            "algorithm_rehandled_cars": algorithm["rehandled_cars"],
            "rehandle_gap_algorithm_minus_manual": int(algorithm["rehandled_cars"]) - int(manual["rehandled_cars"]),
            "manual_cars_per_hook": manual["cars_per_hook"],
            "algorithm_cars_per_hook": algorithm["cars_per_hook"],
        })
    return output


def markdown_table(rows: list[list[Any]], headers: list[str]) -> str:
    if not rows:
        return "(none)"
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(str(item) for item in row) + " |" for row in rows)
    return "\n".join(output)


def write_report(
    path: Path,
    *,
    successful_bundle_count: int,
    unique_manual_count: int,
    algorithm_count: int,
    session_aggregates: list[dict[str, Any]],
    paired: list[dict[str, Any]],
    session_rows_all: list[dict[str, Any]],
    line_summary: list[dict[str, Any]],
    macro_rows: list[dict[str, Any]],
    recovery_rows: list[dict[str, Any]],
    replay_audits: list[dict[str, Any]],
    diagnostics: Counter[str],
) -> None:
    aggregate_rows = [
        [
            row["dataset"], row["cases"], row["sessions"], row["hooks"],
            f"{100 * row['one_get_one_put_rate']:.1f}%",
            row["strategic_multi_source"], f"{100 * row['strategic_multi_source_rate']:.1f}%",
            row["multi_put"], row["partial_put_then_get"], row["cars_per_hook"],
            row["long_sessions_ge_10"], row["pull_equivalent_over_20"],
        ]
        for row in session_aggregates
    ]
    top_paired = sorted(
        paired,
        key=lambda row: (
            -int(row["strategic_gap_manual_minus_algorithm"]),
            -int(row["hook_delta_algorithm_minus_manual"]),
            row["case_id"],
        ),
    )[:20]
    paired_table = [
        [
            row["case_id"], row["algorithm_status"], row["manual_hooks"], row["algorithm_hooks"],
            row["manual_strategic_multi_source"], row["algorithm_strategic_multi_source"],
            row["manual_partial_put_then_get"], row["algorithm_partial_put_then_get"],
            row["manual_rehandled_cars"], row["algorithm_rehandled_cars"],
        ]
        for row in top_paired
    ]
    examples = sorted(
        [row for row in session_rows_all if row["dataset"] == "manual" and row["strategic_multi_source"]],
        key=lambda row: (-int(row["productive_cars"]), -int(row["hooks"]), row["case_id"], row["session"]),
    )[:15]
    example_table = [
        [
            row["case_id"], row["session"], row["phase_span"], row["hooks"], row["peak_carry"],
            row["productive_sources"], row["productive_destinations"], row["sequence"],
        ]
        for row in examples
    ]
    sensitive = [row for row in line_summary if row["topology_sensitive"]]
    sensitive_table = [
        [
            row["dataset"], row["line"], row["put_hooks"], row["temporary_car_placements"],
            row["temporary_recovered"], row["temporary_left"], row["median_recovery_dwell"],
        ]
        for row in sensitive
    ]
    macro_counter: dict[str, Counter[str]] = defaultdict(Counter)
    for row in macro_rows:
        reason = str(row["reason"])
        macro_counter[reason]["macros"] += 1
        macro_counter[reason]["hooks"] += int(row["hooks"])
        macro_counter[reason]["multi_source"] += int(row["multi_source"])
        macro_counter[reason]["strategic"] += int(row["strategic_multi_source"])
        macro_counter[reason]["structural"] += int(row["structural_multi_source"])
        macro_counter[reason]["partial_continue"] += int(row["partial_put_then_get"])
    macro_table = [
        [reason, values["macros"], values["hooks"], values["multi_source"], values["strategic"], values["structural"], values["partial_continue"]]
        for reason, values in sorted(macro_counter.items(), key=lambda item: -item[1]["hooks"])
    ]
    recovery_counter: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in recovery_rows:
        key = (str(row["dataset"]), str(row["staging_line"]))
        recovery_counter[key]["gets"] += 1
        recovery_counter[key]["cars"] += int(row["recovered_cars"])
        recovery_counter[key]["multi_put"] += int(row["multi_put_accumulation"])
        recovery_counter[key]["while_carrying"] += int(row["get_while_carrying"])
    recovery_table = [
        [dataset, line, values["gets"], values["cars"], values["multi_put"], values["while_carrying"]]
        for (dataset, line), values in sorted(recovery_counter.items(), key=lambda item: (item[0][0], -item[1]["cars"]))
        if values["cars"] >= 5
    ]
    diagnostics_table = [[name, count] for name, count in diagnostics.most_common()]
    audit_table = []
    for dataset in ("manual", "algorithm_combined_matched", "algorithm_stage4"):
        items = [row for row in replay_audits if row["dataset"] == dataset]
        if not items:
            continue
        audit_table.append([
            dataset,
            len(items),
            sum(int(row["operation_hard_clean"]) for row in items),
            sum(int(row["full_clean"]) for row in items),
            sum(int(row["physical_violations"]) for row in items),
            sum(int(row["state_violations"]) for row in items),
            sum(int(row["business_violations"]) for row in items),
        ])

    lines = [
        "# 多摘多挂能力会话级统计",
        "",
        "## 数据口径",
        "",
        f"- 人工成功 bundle：{successful_bundle_count}；去除完全重复作业单后：{unique_manual_count}。",
        f"- 当前算法 Stage4 artifact 案例：{algorithm_count}。",
        f"- 人工与算法 combined 可配对的唯一成功案例：{len(paired)}。",
        "- carry session：从空挂首次 Get 到 TrainCars 再次为空；这是物理持车边界，不等同于算法宏边界。",
        "- strategic multi-source：同一 carry session 中，至少两个不同来源的车辆最终没有恢复原线。仅取出并恢复 blocker 不计入战略多源编组。",
        "- partial-put-then-get：已有挂车，部分 Put 后仍保留车列，并在清空前继续取得新车。",
        "- carry session 在缓存回取处会把缓存线视为新来源；判断算法是否真正汇聚多个业务原始来源，必须同时查看后面的宏级 strategic/structural 分类。",
        "",
        "## 核心会话统计",
        "",
        markdown_table(
            aggregate_rows,
            ["cohort", "cases", "sessions", "hooks", "1G1P rate", "strategic multi-source", "rate", "multi-put", "partial-put+get", "car events/hook", "sessions >=10", "pull >20"],
        ),
        "",
        "`manual_start_P5` 与 `algorithm_stage4` 只是流程位置近似比较；两者起始状态并不相同。人工全流程与 algorithm combined 的配对比较也不能直接当成最优钩数竞赛。",
        "人工 phase 行按 session 起始阶段统计，不重复计算跨阶段 session；跨阶段持续持车本身是重要能力证据。",
        "表中的 carry strategic multi-source 不是算法业务多源宏数：layout 宏先放空再从多条缓存线回取时，新的 carry session 已丢失原始来源信息。算法业务来源能力以宏级 strategic 列为准。",
        "",
        "## 独立重放审计",
        "",
        markdown_table(
            audit_table,
            ["cohort", "cases", "operation hard-clean", "full clean", "physical violations", "state violations", "business violations"],
        ),
        "",
        "人工还原器只恢复车辆身份、摘挂顺序与简单图路径，不验证占线、操作端、牵引上限和最终业务目标。因此人工 Response 只能证明作业意图模式，不能原样作为物理可执行基准。",
        "",
        "## Stage4 宏内能力构成",
        "",
        markdown_table(
            macro_table,
            ["reason", "macros", "hooks", "raw multi-source", "strategic", "structural", "partial-put+get"],
        ),
        "",
        "## 人工典型战略多源会话",
        "",
        markdown_table(
            example_table,
            ["case", "session", "phase", "hooks", "peak", "sources", "destinations", "sequence"],
        ),
        "",
        "## 配对差异较大的案例",
        "",
        markdown_table(
            paired_table,
            ["case", "status", "manual hooks", "algo hooks", "manual strategic", "algo strategic", "manual partial+get", "algo partial+get", "manual rehandled", "algo rehandled"],
        ),
        "",
        "## 拓扑敏感线路的非目标落车",
        "",
        markdown_table(
            sensitive_table,
            ["cohort", "line", "put hooks", "temporary cars", "recovered", "left", "median dwell ops"],
        ),
        "",
        "## 临编后回取",
        "",
        markdown_table(
            recovery_table,
            ["cohort", "line", "recovery Gets", "cars", "accumulated from >=2 Puts", "Get while carrying"],
        ),
        "",
        "## 状态一致性诊断",
        "",
        markdown_table(diagnostics_table, ["diagnostic", "count"]),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare manual and algorithm multi-Get/multi-Put sessions.")
    parser.add_argument("--manual-dir", type=Path, default=DEFAULT_MANUAL_DIR)
    parser.add_argument("--algorithm-dir", type=Path, default=DEFAULT_ALGORITHM_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    successful_bundles, manual_cases = load_manual(args.manual_dir)
    algorithm_cases = load_algorithm_cases(args.algorithm_dir)
    algorithm_by_id = {case["case_id"]: case for case in algorithm_cases}
    manual_by_id = {case["case_id"]: case for case in manual_cases}
    diagnostics: Counter[str] = Counter()
    all_sessions: list[dict[str, Any]] = []
    all_metrics: list[dict[str, Any]] = []
    all_placements: list[dict[str, Any]] = []
    all_recoveries: list[dict[str, Any]] = []
    all_macros: list[dict[str, Any]] = []
    all_replay_audits: list[dict[str, Any]] = []

    for case in manual_cases:
        targets = target_map(case["request"])
        weights = pull_weights(case["request"])
        sessions, issues, _contexts = session_rows(
            dataset="manual",
            case_id=case["case_id"],
            rows=case["rows"],
            targets=targets,
            weights=weights,
        )
        diagnostics.update({f"manual:{key}": value for key, value in issues.items()})
        all_sessions.extend(sessions)
        all_metrics.append(response_metrics("manual", case["case_id"], case["rows"], sessions, case["request"]))
        all_placements.extend(placement_rows(dataset="manual", case_id=case["case_id"], rows=case["rows"], targets=targets))
        all_recoveries.extend(staging_recovery_rows(dataset="manual", case_id=case["case_id"], rows=case["rows"], targets=targets))
        all_replay_audits.append(replay_audit(
            dataset="manual",
            case_id=case["case_id"],
            request=case["request"],
            response=case["response"],
        ))

    for case in algorithm_cases:
        targets = target_map(case["request"])
        weights = pull_weights(case["request"])
        if case["combined_rows"]:
            sessions, issues, _contexts = session_rows(
                dataset="algorithm_combined",
                case_id=case["case_id"],
                rows=case["combined_rows"],
                targets=targets,
                weights=weights,
            )
            diagnostics.update({f"algorithm_combined:{key}": value for key, value in issues.items()})
            all_sessions.extend(sessions)
            all_metrics.append(response_metrics("algorithm_combined", case["case_id"], case["combined_rows"], sessions, case["request"]))
            all_placements.extend(placement_rows(dataset="algorithm_combined", case_id=case["case_id"], rows=case["combined_rows"], targets=targets))
            all_recoveries.extend(staging_recovery_rows(dataset="algorithm_combined", case_id=case["case_id"], rows=case["combined_rows"], targets=targets))
            manual_case = manual_by_id.get(case["case_id"])
            if manual_case:
                all_replay_audits.append(replay_audit(
                    dataset="algorithm_combined_matched",
                    case_id=case["case_id"],
                    request=manual_case["request"],
                    response=case["combined_payload"],
                ))

        sessions, issues, _contexts = session_rows(
            dataset="algorithm_stage4",
            case_id=case["case_id"],
            rows=case["stage4_rows"],
            targets=targets,
            weights=weights,
            fixed_phase="S4",
        )
        diagnostics.update({f"algorithm_stage4:{key}": value for key, value in issues.items()})
        all_sessions.extend(sessions)
        all_metrics.append(response_metrics("algorithm_stage4", case["case_id"], case["stage4_rows"], sessions, case["request"]))
        all_placements.extend(placement_rows(dataset="algorithm_stage4", case_id=case["case_id"], rows=case["stage4_rows"], targets=targets))
        all_recoveries.extend(staging_recovery_rows(dataset="algorithm_stage4", case_id=case["case_id"], rows=case["stage4_rows"], targets=targets))
        all_replay_audits.append(replay_audit(
            dataset="algorithm_stage4",
            case_id=case["case_id"],
            request=case["request"],
            response=case["stage4_payload"],
        ))
        macros, issues = load_stage4_macros(args.algorithm_dir, case, targets, weights)
        diagnostics.update({f"algorithm_stage4_macro:{key}": value for key, value in issues.items()})
        all_macros.extend(macros)

    metrics_by_dataset: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in all_metrics:
        metrics_by_dataset[str(row["dataset"])][str(row["case_id"])] = row
    status_by_case = {case["case_id"]: str((case["summary"] or {}).get("status") or "") for case in algorithm_cases}
    paired = paired_rows(metrics_by_dataset["manual"], metrics_by_dataset["algorithm_combined"], status_by_case)
    paired_ids = {str(row["case_id"]) for row in paired}

    aggregates = [
        aggregate_sessions(all_sessions, "manual", case_ids=paired_ids, label="manual_matched"),
        aggregate_sessions(all_sessions, "algorithm_combined", case_ids=paired_ids, label="algorithm_combined_matched"),
        aggregate_sessions(all_sessions, "algorithm_stage4", case_ids=paired_ids, label="algorithm_stage4_matched"),
        aggregate_sessions(all_sessions, "algorithm_stage4", label="algorithm_stage4_all"),
        aggregate_sessions(all_sessions, "manual", phase="P1", phase_start_only=True, label="manual_start_P1"),
        aggregate_sessions(all_sessions, "manual", phase="P2", phase_start_only=True, label="manual_start_P2"),
        aggregate_sessions(all_sessions, "manual", phase="P3", phase_start_only=True, label="manual_start_P3"),
        aggregate_sessions(all_sessions, "manual", phase="P4", phase_start_only=True, label="manual_start_P4"),
        aggregate_sessions(all_sessions, "manual", phase="P5", phase_start_only=True, label="manual_start_P5"),
    ]
    line_summary = aggregate_line_placements(all_placements)

    write_csv(args.output_dir / "carry_sessions.csv", all_sessions)
    write_csv(args.output_dir / "response_metrics.csv", all_metrics)
    write_csv(args.output_dir / "paired_case_comparison.csv", paired)
    write_csv(args.output_dir / "car_placement_events.csv", all_placements)
    write_csv(args.output_dir / "line_placement_summary.csv", line_summary)
    write_csv(args.output_dir / "staging_recovery_events.csv", all_recoveries)
    write_csv(args.output_dir / "stage4_macro_sessions.csv", all_macros)
    write_csv(args.output_dir / "session_aggregates.csv", aggregates)
    write_csv(args.output_dir / "replay_audit.csv", all_replay_audits)
    write_report(
        args.output_dir / "report.md",
        successful_bundle_count=len(successful_bundles),
        unique_manual_count=len(manual_cases),
        algorithm_count=len(algorithm_cases),
        session_aggregates=aggregates,
        paired=paired,
        session_rows_all=all_sessions,
        line_summary=line_summary,
        macro_rows=all_macros,
        recovery_rows=all_recoveries,
        replay_audits=all_replay_audits,
        diagnostics=diagnostics,
    )
    metadata = {
        "manual_successful_bundles": len(successful_bundles),
        "manual_unique_successful_responses": len(manual_cases),
        "manual_duplicate_bundles": len(successful_bundles) - len(manual_cases),
        "algorithm_cases": len(algorithm_cases),
        "paired_unique_successful_cases": len(paired),
        "manual_success_without_combined_response": sorted(set(manual_by_id) - {row["case_id"] for row in paired}),
        "algorithm_without_successful_manual": sorted(set(algorithm_by_id) - set(manual_by_id)),
        "diagnostics": dict(diagnostics),
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output_dir / "report.md")


if __name__ == "__main__":
    main()
