#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solver_vnext import physical


CAR_NO_PATTERN = re.compile(r"(?<!\d)\d{7}(?!\d)")
DEPOT_RAW_PATTERN = re.compile(r"^(?:注意)?修([1-4])$")
HARD_GAP_CODES = {
    "line_resolution_ambiguous",
    "ambiguous_line_alias",
    "count_omitted",
    "count_cell_contains_semantic_token",
    "method_missing",
    "truth_case_missing",
    "aggregate_line_count_negative",
    "line_not_exact_track_spec",
    "sequence_gap_possible_omitted_hook",
}


@dataclass(frozen=True)
class IdentityState:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    train: tuple[str, ...]


@dataclass(frozen=True)
class ReplayCandidate:
    state: IdentityState
    moved_nos: tuple[str, ...]
    expected_car_no: str
    line_scope: tuple[str, ...]
    endpoint: str
    validation: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay cleaned manual labels against truth2 car identities.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--label-dir", default="artifacts/manual_label_cleaning_20260702")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-states", type=int, default=512)
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    label_dir = root / args.label_dir
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    labels = read_csv(label_dir / "manual_action_labels.csv")
    truth_by_case = {
        physical.case_id_from_path(path): path
        for path in sorted((root / args.truth_dir).glob("*.json"))
        if path.name != "conversion_summary.json"
    }
    rows, summaries = replay_all(labels, truth_by_case, args.max_states)
    write_csv(output_dir / "manual_identity_replay_trace.csv", rows)
    write_csv(output_dir / "manual_identity_replay_case_summary.csv", summaries)
    summary = build_summary(rows, summaries)
    (output_dir / "manual_identity_replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def replay_all(
    labels: list[dict[str, str]],
    truth_by_case: dict[str, Path],
    max_states: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_case: dict[str, list[dict[str, str]]] = {}
    for row in labels:
        by_case.setdefault(row["case_id"], []).append(row)

    trace_rows: list[dict[str, Any]] = []
    case_summaries: list[dict[str, Any]] = []
    for case_id in sorted(by_case):
        case_rows = sorted(by_case[case_id], key=lambda item: int(item["manual_hook"]))
        truth_path = truth_by_case.get(case_id)
        if truth_path is None:
            for label in case_rows:
                trace_rows.append(trace_row(label, "truth_missing", 0, 0))
            case_summaries.append(case_summary(case_id, case_rows, [row for row in trace_rows if row["case_id"] == case_id]))
            continue
        initial_state = initial_identity_state(truth_path)
        states: set[IdentityState] = {initial_state}
        blocked_reason = ""
        for label in case_rows:
            before_count = len(states)
            if blocked_reason:
                trace_rows.append(
                    trace_row(
                        label,
                        "blocked_after_prior_gap",
                        before_count,
                        before_count,
                        detail=blocked_reason,
                        states_before=states,
                    )
                )
                continue
            noop_reason = identity_noop_reason(label, states)
            if noop_reason:
                trace_rows.append(
                    trace_row(
                        label,
                        "identity_noop",
                        before_count,
                        before_count,
                        detail=noop_reason,
                        states_before=states,
                        states_after=states,
                    )
                )
                continue
            blocking_codes = blocking_issue_codes(label)
            if blocking_codes & HARD_GAP_CODES or "merged_hook_possible" in blocking_codes:
                blocked_reason = structural_gap_reason(blocking_codes)
                trace_rows.append(
                    trace_row(
                        label,
                        "structural_gap",
                        before_count,
                        before_count,
                        detail=blocked_reason,
                        states_before=states,
                    )
                )
                continue
            if label.get("method") not in {"+", "-"}:
                blocked_reason = f"unsupported_method:{label.get('method')}"
                trace_rows.append(
                    trace_row(
                        label,
                        "unsupported_method_gap",
                        before_count,
                        before_count,
                        detail=blocked_reason,
                        states_before=states,
                    )
                )
                continue
            if not label.get("resolved_line") or label_count(label) is None:
                blocked_reason = "line_or_count_missing"
                trace_rows.append(
                    trace_row(
                        label,
                        "structural_gap",
                        before_count,
                        before_count,
                        detail=blocked_reason,
                        states_before=states,
                    )
                )
                continue

            candidates: list[ReplayCandidate] = []
            for state in sorted(states, key=state_sort_key):
                candidates.extend(expand_state(state, label))
            valid_candidates = [candidate for candidate in candidates if candidate.validation != "car_no_conflict"]
            if not valid_candidates:
                blocked_reason = replay_conflict_reason(candidates)
                trace_rows.append(
                    trace_row(
                        label,
                        "identity_conflict",
                        before_count,
                        0,
                        detail=blocked_reason,
                        candidates=candidates,
                        states_before=states,
                    )
                )
                continue
            next_states = {candidate.state for candidate in valid_candidates}
            if len(next_states) > max_states:
                blocked_reason = f"state_space_exceeded:{len(next_states)}>{max_states}"
                trace_rows.append(
                    trace_row(
                        label,
                        "state_space_exceeded",
                        before_count,
                        len(next_states),
                        detail=blocked_reason,
                        candidates=valid_candidates,
                        states_before=states,
                    )
                )
                continue
            status = "replayed_unique" if len(next_states) == 1 else "replayed_ambiguous"
            before_states = states
            states = next_states
            trace_rows.append(
                trace_row(
                    label,
                    status,
                    before_count,
                    len(states),
                    candidates=valid_candidates,
                    states_before=before_states,
                    states_after=states,
                )
            )
        case_trace = [row for row in trace_rows if row["case_id"] == case_id]
        case_summaries.append(case_summary(case_id, case_rows, case_trace))
    return trace_rows, case_summaries


def initial_identity_state(truth_path: Path) -> IdentityState:
    _case_id, _payload, cars, _depot_assignment, _loco = physical.read_case(truth_path)
    lines: dict[str, tuple[str, ...]] = {}
    for line in sorted({car["Line"] for car in cars if car.get("Line")}):
        lines[line] = tuple(physical.line_access_order(cars, line))
    return freeze_state(lines, tuple())


def expand_state(state: IdentityState, label: dict[str, str]) -> list[ReplayCandidate]:
    line = str(label["resolved_line"])
    method = str(label["method"])
    count = label_count(label)
    if count is None:
        return []
    note_car_nos = tuple(CAR_NO_PATTERN.findall(str(label.get("note") or "")))
    if method == "+":
        return expand_couple(state, line, count, note_car_nos, label)
    if method == "-":
        return expand_detach(state, detach_target_line(label, line), count, note_car_nos)
    return []


def expand_couple(
    state: IdentityState,
    line: str,
    count: int,
    note_car_nos: tuple[str, ...],
    label: dict[str, str],
) -> list[ReplayCandidate]:
    lines = thaw_lines(state)
    candidates: list[ReplayCandidate] = []
    for scope in replay_line_scopes(label):
        scoped_cars = scoped_line_cars(lines, scope)
        if count <= 0 or len(scoped_cars) < count:
            continue
        low_batch = tuple(scoped_cars[:count])
        high_batch = tuple(reversed(scoped_cars[-count:]))
        allowed = allowed_couple_endpoints(label)
        for endpoint, batch in (
            ("position_low", low_batch),
            ("position_high", high_batch),
        ):
            if endpoint not in allowed:
                continue
            if count == len(scoped_cars) and endpoint == "position_high":
                continue
            expected = batch[-1] if batch else ""
            validation = car_no_validation(expected, note_car_nos, batch)
            next_lines = remove_batch_from_lines(lines, batch)
            next_state = freeze_state(next_lines, (*state.train, *batch))
            candidates.append(ReplayCandidate(next_state, batch, expected, scope, endpoint, validation))
    return candidates


def allowed_couple_endpoints(label: dict[str, str]) -> tuple[str, ...]:
    note = str(label.get("note") or "")
    if label.get("resolved_line") == "存5线北" and "北头" in note:
        return ("position_low",)
    return ("position_low", "position_high")


def detach_target_line(label: dict[str, str], line: str) -> str:
    raw = str(label.get("line_raw") or "")
    note = str(label.get("note") or "")
    if raw == "存2" and "叉线" in note:
        return "预修线"
    if raw in {"机", "注意机"}:
        return "机南" if "南" in note else "机走棚"
    return line


def expand_detach(
    state: IdentityState,
    line: str,
    count: int,
    note_car_nos: tuple[str, ...],
) -> list[ReplayCandidate]:
    if count <= 0 or len(state.train) < count:
        return []
    lines = thaw_lines(state)
    line_cars = list(lines.get(line, tuple()))
    batch = tuple(state.train[-count:])
    remaining_train = tuple(state.train[:-count])
    expected = batch[0] if count >= 7 and batch else ""
    validation = car_no_validation(expected, note_car_nos, batch)
    candidates: list[ReplayCandidate] = []
    low_line = (*batch, *line_cars)
    high_line = (*line_cars, *reversed(batch))
    for endpoint, next_line in (
        ("position_low", low_line),
        ("position_high", high_line),
    ):
        if not line_cars and endpoint == "position_high":
            continue
        next_lines = dict(lines)
        next_lines[line] = tuple(next_line)
        next_state = freeze_state(next_lines, remaining_train)
        candidates.append(ReplayCandidate(next_state, batch, expected, (line,), endpoint, validation))
    return candidates


def replay_line_scopes(label: dict[str, str]) -> tuple[tuple[str, ...], ...]:
    raw = str(label.get("line_raw") or "")
    line = str(label.get("resolved_line") or "")
    note = str(label.get("note") or "")
    scopes: list[tuple[str, ...]] = []
    if raw == "存2" and label.get("method") == "+" and "叉线" in note:
        return dedupe_line_scopes([("预修线",)])
    if line and raw not in {"机", "注意机"}:
        scopes.append((line,))
    if raw in {"洗", "留道口洗", "洗南", "洗北"} or line in {"洗罐站", "洗罐线北"}:
        scopes.append(("洗罐站", "洗罐线北"))
        scopes.append(("洗罐站", "洗罐线北", "洗油北"))
    if raw in {"调", "调棚", "调北"} or line in {"调梁棚", "调梁线北"}:
        scopes.append(("调梁棚", "调梁线北"))
    if raw in {"存5", "注意存5", "存5线", "存5南", "存5北"} or line in {"存5线南", "存5线北"}:
        scopes.append(("存5线北", "存5线南"))
    if raw == "存2" and "叉线" in note:
        scopes.append(("预修线",))
    if raw in {"机", "注意机"}:
        scopes.append(("机南", "机走棚", "机走北"))
    elif raw in {"机南", "机棚", "机北3", "机走"} or line in {"机南", "机走棚", "机走北"}:
        scopes.append(("机南", "机走棚", "机走北"))
    depot_match = DEPOT_RAW_PATTERN.fullmatch(raw)
    if depot_match:
        index = depot_match.group(1)
        scopes.append((f"修{index}库外", f"修{index}库内"))
    elif line in {
        "修1库外", "修1库内",
        "修2库外", "修2库内",
        "修3库外", "修3库内",
        "修4库外", "修4库内",
    }:
        index = line[1]
        scopes.append((f"修{index}库外", f"修{index}库内"))
    return dedupe_line_scopes(scopes)


def dedupe_line_scopes(scopes: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    deduped: list[tuple[str, ...]] = []
    for scope in scopes:
        normalized = tuple(physical.normalize_line(line) for line in scope if physical.normalize_line(line))
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return tuple(deduped)


def scoped_line_cars(lines: dict[str, tuple[str, ...]], scope: tuple[str, ...]) -> tuple[str, ...]:
    cars: list[str] = []
    for line in scope:
        cars.extend(lines.get(line, tuple()))
    return tuple(cars)


def remove_batch_from_lines(
    lines: dict[str, tuple[str, ...]],
    batch: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    moving = set(batch)
    return {
        line: tuple(no for no in cars if no not in moving)
        for line, cars in lines.items()
    }


def car_no_validation(expected: str, note_car_nos: tuple[str, ...], moved_nos: tuple[str, ...]) -> str:
    if not note_car_nos:
        return "missing" if expected else "not_required"
    if expected and expected not in note_car_nos:
        return "car_no_conflict"
    if len(note_car_nos) >= 2 and not set(note_car_nos).issubset(set(moved_nos)):
        return "car_no_conflict"
    if len(note_car_nos) >= 2:
        return "matched_all_note_car_nos"
    if expected:
        return "matched_expected_car_no"
    if set(note_car_nos).issubset(set(moved_nos)):
        return "matched_note_car_no"
    return "car_no_conflict"


def trace_row(
    label: dict[str, str],
    status: str,
    state_count_before: int,
    state_count_after: int,
    *,
    detail: str = "",
    candidates: list[ReplayCandidate] | None = None,
    states_before: set[IdentityState] | None = None,
    states_after: set[IdentityState] | None = None,
) -> dict[str, Any]:
    candidates = candidates or []
    moved_sets = sorted({",".join(candidate.moved_nos) for candidate in candidates if candidate.moved_nos})
    expected_values = sorted({candidate.expected_car_no for candidate in candidates if candidate.expected_car_no})
    endpoints = Counter(candidate.endpoint for candidate in candidates)
    line_scopes = Counter("|".join(candidate.line_scope) for candidate in candidates)
    validations = Counter(candidate.validation for candidate in candidates)
    agreed_moved_nos = moved_sets[0] if len(moved_sets) == 1 else ""
    agreed_expected_car_no = expected_values[0] if len(expected_values) == 1 else ""
    return {
        "case_id": label.get("case_id", ""),
        "manual_hook": label.get("manual_hook", ""),
        "shift": label.get("shift", ""),
        "line_raw": label.get("line_raw", ""),
        "resolved_line": label.get("resolved_line", ""),
        "method": label.get("method", ""),
        "count": label.get("count", ""),
        "effective_count": label.get("effective_count", ""),
        "count_resolution_status": label.get("count_resolution_status", ""),
        "count_inference_reason": label.get("count_inference_reason", ""),
        "count_inference_confidence": label.get("count_inference_confidence", ""),
        "note": label.get("note", ""),
        "manual_reference_scope": label.get("manual_reference_scope", ""),
        "manual_reference_exclusion_reason": label.get("manual_reference_exclusion_reason", ""),
        "site_confirmed_alias": label.get("site_confirmed_alias", ""),
        "site_confirmed_alias_target": label.get("site_confirmed_alias_target", ""),
        "north_head_semantics": label.get("north_head_semantics", ""),
        "label_quality": label.get("label_quality", ""),
        "structural_label_quality": label.get("structural_label_quality", ""),
        "issue_codes": label.get("issue_codes", ""),
        "compound_hook_class": label.get("compound_hook_class", ""),
        "compound_identity_replay_mode": label.get("compound_identity_replay_mode", ""),
        "compound_algorithm_hint": label.get("compound_algorithm_hint", ""),
        "replay_status": status,
        "state_count_before": state_count_before,
        "state_count_after": state_count_after,
        "manual_state_before": unique_state_text(states_before),
        "manual_state_after": unique_state_text(states_after),
        "candidate_count": len(candidates),
        "moved_nos_if_unique": agreed_moved_nos,
        "expected_car_no_if_unique": agreed_expected_car_no,
        "line_scope_counts": counter_text(line_scopes),
        "endpoint_counts": counter_text(endpoints),
        "car_no_validation_counts": counter_text(validations),
        "detail": detail,
    }


def unique_state_text(states: set[IdentityState] | None) -> str:
    if not states or len(states) != 1:
        return ""
    return state_to_text(next(iter(states)))


def state_to_text(state: IdentityState) -> str:
    line_parts = []
    for line, cars in state.lines:
        if cars:
            line_parts.append(f"{line}:{','.join(cars)}")
    train = ",".join(state.train)
    return f"lines={'|'.join(line_parts)};train={train}"


def case_summary(
    case_id: str,
    labels: list[dict[str, str]],
    trace_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses = Counter(str(row["replay_status"]) for row in trace_rows)
    unique_moved = sum(1 for row in trace_rows if row.get("moved_nos_if_unique"))
    matched = sum(1 for row in trace_rows if "matched" in str(row.get("car_no_validation_counts") or ""))
    missing = sum(1 for row in trace_rows if "missing" in str(row.get("car_no_validation_counts") or ""))
    return {
        "case_id": case_id,
        "hook_count": len(labels),
        "status_counts": counter_text(statuses),
        "unique_moved_hook_count": unique_moved,
        "matched_car_no_hook_count": matched,
        "missing_car_no_hook_count": missing,
        "first_blocking_hook": first_blocking_hook(trace_rows),
        "first_blocking_status": first_blocking_status(trace_rows),
        "first_blocking_detail": first_blocking_detail(trace_rows),
    }


def build_summary(rows: list[dict[str, Any]], case_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row["replay_status"]) for row in rows)
    unique_moved = sum(1 for row in rows if row.get("moved_nos_if_unique"))
    matched = sum(1 for row in rows if "matched" in str(row.get("car_no_validation_counts") or ""))
    missing = sum(1 for row in rows if "missing" in str(row.get("car_no_validation_counts") or ""))
    replayed = statuses.get("replayed_unique", 0) + statuses.get("replayed_ambiguous", 0)
    completed_cases = sum(1 for row in case_summaries if not row["first_blocking_status"])
    reference_rows = [row for row in rows if row.get("manual_reference_scope") != "exclude_from_algorithm_learning"]
    reference_statuses = Counter(str(row["replay_status"]) for row in reference_rows)
    reference_replayed = reference_statuses.get("replayed_unique", 0) + reference_statuses.get("replayed_ambiguous", 0)
    return {
        "case_count": len(case_summaries),
        "hook_count": len(rows),
        "status_counts": dict(statuses),
        "replayed_hook_count": replayed,
        "replayed_hook_rate": round(replayed / len(rows), 4) if rows else 0,
        "completed_case_count": completed_cases,
        "unique_moved_hook_count": unique_moved,
        "unique_moved_hook_rate": round(unique_moved / len(rows), 4) if rows else 0,
        "matched_car_no_hook_count": matched,
        "missing_car_no_hook_count": missing,
        "reference_hook_count": len(reference_rows),
        "reference_replayed_hook_count": reference_replayed,
        "reference_replayed_hook_rate": round(reference_replayed / len(reference_rows), 4) if reference_rows else 0,
        "reference_status_counts": dict(reference_statuses),
        "manual_reference_scope_counts": dict(Counter(str(row.get("manual_reference_scope") or "") for row in rows)),
        "case_first_blocking_status_counts": dict(Counter(row["first_blocking_status"] for row in case_summaries)),
        "case_first_blocking_detail_counts": dict(Counter(row["first_blocking_detail"] for row in case_summaries).most_common(20)),
        "validated_scope_rules": {
            "洗": "洗罐站|洗罐线北|洗油北",
            "留道口洗": "洗罐站|洗罐线北|洗油北",
            "调": "调梁棚|调梁线北",
            "存2+叉线": "预修线/存2叉",
            "存2-叉线": "预修线/存2叉",
            "修N": "修N库外|修N库内",
            "注意修N": "修N库外|修N库内",
        },
        "validated_count_rules": {
            "库外N": "effective_count=N for '-' hooks, original count_omitted retained",
        },
        "validated_endpoint_rules": {
            "存5线北+北头": "position_low",
        },
        "validated_car_no_rules": {
            "multi_car_note": "all noted car numbers must belong to the moved batch",
        },
        "field_expert_confirmed_risk_rules": {
            "存5北头顶N": "compound storage5 internal reposition; do not learn as one ordinary + hook; south endpoint unresolved",
            "0103W": "holiday merged hook plan; exclude from algorithm-learning reference set",
            "机走线": "manual does not split north/south temporary segments; solver needs separate capacity model for temporary vs final parking",
        },
    }


def first_blocking_hook(rows: list[dict[str, Any]]) -> str:
    row = first_blocking_row(rows)
    return str(row.get("manual_hook") or "") if row else ""


def first_blocking_status(rows: list[dict[str, Any]]) -> str:
    row = first_blocking_row(rows)
    return str(row.get("replay_status") or "") if row else ""


def first_blocking_detail(rows: list[dict[str, Any]]) -> str:
    row = first_blocking_row(rows)
    return str(row.get("detail") or "") if row else ""


def first_blocking_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    passing = {"identity_noop", "replayed_unique", "replayed_ambiguous"}
    for row in rows:
        if row["replay_status"] not in passing:
            return row
    return None


def blocking_issue_codes(label: dict[str, str]) -> set[str]:
    codes = issue_set(label)
    raw = str(label.get("line_raw") or "")
    line = str(label.get("resolved_line") or "")
    if raw in {"库", "注意库"} and line == "机库线":
        codes.discard("line_resolution_ambiguous")
        codes.discard("ambiguous_line_alias")
    if raw in {"机", "注意机", "库", "注意库"} and line == "机库线":
        codes.discard("aggregate_line_count_negative")
    codes.discard("car_no_annotation_missing")
    if is_replayable_compound_identity_move(label):
        codes.discard("merged_hook_possible")
    if is_strict_count_inferred(label):
        codes.discard("count_omitted")
    return codes


def label_count(label: dict[str, str]) -> int | None:
    value = str(label.get("effective_count") or label.get("count") or "")
    if not value:
        return None
    return int(value)


def is_strict_count_inferred(label: dict[str, str]) -> bool:
    return (
        "count_omitted" in issue_set(label)
        and str(label.get("count_resolution_status") or "") == "inferred"
        and str(label.get("count_inference_confidence") or "") == "high"
        and str(label.get("count_inference_reason") or "").startswith("kuwai_note_count")
        and label_count(label) is not None
    )


def is_replayable_compound_identity_move(label: dict[str, str]) -> bool:
    if "merged_hook_possible" not in issue_set(label):
        return False
    if label.get("method") not in {"+", "-"} or label_count(label) is None:
        return False
    mode = str(label.get("compound_identity_replay_mode") or "")
    if not mode:
        mode = inferred_compound_identity_replay_mode(label)
    return mode in {
        "normal_identity_move_with_positioning_note",
        "normal_identity_move_with_push_note",
        "normal_identity_move_with_forkline_note",
    }


def inferred_compound_identity_replay_mode(label: dict[str, str]) -> str:
    note = str(label.get("note") or "")
    if re.search(r"对\d+", note):
        return "normal_identity_move_with_positioning_note"
    if re.search(r"顶\d+", note):
        return "structural_gap_push_reposition"
    if "叉线接" in note or re.search(r"叉线.*摘", note):
        return "normal_identity_move_with_forkline_note"
    return "structural_gap_compound_note"


def structural_gap_reason(codes: set[str]) -> str:
    codes = sorted(codes)
    return "|".join(codes) or "structural_quality_not_clean"


def identity_noop_reason(label: dict[str, str], states: set[IdentityState]) -> str:
    hook = int(label.get("manual_hook") or 0)
    raw = str(label.get("line_raw") or "")
    line = str(label.get("resolved_line") or "")
    method = str(label.get("method") or "")
    count = str(label.get("count") or "")
    count_raw = str(label.get("count_raw") or "")
    note = str(label.get("note") or "")
    car_numbers = tuple(CAR_NO_PATTERN.findall(note))
    count_value = label_count(label)
    if (
        method == "+"
        and count_value == 1
        and line == "机库线"
        and raw in {"库", "注意库"}
        and note in {"", "接"}
        and not car_numbers
    ):
        return "loco_only_yard_move_no_vehicle_delta"
    if line == "机库线" and ("称" in note or method == "称"):
        return "weighing_positioning_no_identity_delta"
    if (
        hook == 1
        and method == "-"
        and not count
        and line == "机库线"
        and raw in {"机", "库", "注意机", "注意库"}
    ):
        return "initial_loco_departure_no_vehicle_delta"
    if (
        not method
        and count_raw in {"回", "停"}
        and line == "机库线"
        and raw in {"机", "库", "注意机", "注意库"}
    ):
        return "loco_closeout_no_vehicle_delta"
    if method == "称" and count_raw == "停" and "称" in note:
        return "weigh_or_stop_positioning_no_identity_delta"
    if (
        method == "-"
        and not count
        and line == "存4线"
        and note == "北头"
    ):
        return "cun4_north_positioning_no_vehicle_delta"
    return ""


def replay_conflict_reason(candidates: list[ReplayCandidate]) -> str:
    if not candidates:
        return "no_candidate_from_current_identity_state"
    return f"candidate_validation:{counter_text(Counter(candidate.validation for candidate in candidates))}"


def freeze_state(lines: dict[str, tuple[str, ...]], train: tuple[str, ...]) -> IdentityState:
    frozen_lines = tuple(
        (line, tuple(cars))
        for line, cars in sorted(lines.items())
        if cars
    )
    return IdentityState(frozen_lines, tuple(train))


def thaw_lines(state: IdentityState) -> dict[str, tuple[str, ...]]:
    return {line: tuple(cars) for line, cars in state.lines}


def state_sort_key(state: IdentityState) -> tuple[Any, ...]:
    return (state.train, state.lines)


def issue_set(label: dict[str, str]) -> set[str]:
    return {code for code in str(label.get("issue_codes") or "").split("|") if code}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{count}" for key, count in sorted(counter.items()))


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
