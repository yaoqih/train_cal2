#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_manual_action_labels as manual_labels  # noqa: E402
import replay_manual_identity_labels as manual_replay  # noqa: E402
from solver_vnext import physical  # noqa: E402


PASSING_RESTORE_STATUSES = {"restored", "noop", "weigh_restored"}
VALIDATION_SCORE = {
    "matched_all_note_car_nos": 0,
    "matched_expected_car_no": 1,
    "matched_note_car_no": 2,
    "not_required": 3,
    "missing": 4,
    "car_no_conflict": 9,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restore manual shunting sheets to API response JSON.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--manual-root", default="data/人工调车数据")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--label-dir", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--case-id", action="append", default=[])
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    label_dir = ensure_label_dir(args, root, output_dir)
    labels = read_csv(label_dir / "manual_action_labels.csv")
    if args.case_id:
        wanted = {case_id.upper() for case_id in args.case_id}
        labels = [row for row in labels if str(row.get("case_id") or "").upper() in wanted]

    truth_by_case = {
        physical.case_id_from_path(path): path
        for path in sorted((root / args.truth_dir).glob("*.json"))
        if path.name != "conversion_summary.json"
    }
    rows_by_file: dict[str, list[dict[str, str]]] = {}
    for row in labels:
        rows_by_file.setdefault(str(row["manual_file_id"]), []).append(row)

    summaries: list[dict[str, Any]] = []
    all_trace_rows: list[dict[str, Any]] = []
    for manual_file_id in sorted(rows_by_file):
        file_rows = sorted(rows_by_file[manual_file_id], key=lambda item: int(item.get("manual_hook") or 0))
        if not file_rows:
            continue
        case_id = str(file_rows[0].get("case_id") or "")
        truth_path = truth_by_case.get(case_id)
        if truth_path is None:
            summary = missing_truth_summary(file_rows)
            summaries.append(summary)
            all_trace_rows.extend(summary.pop("_trace_rows"))
            continue

        bundle, trace_rows, summary = restore_case(file_rows, truth_path)
        summaries.append(summary)
        all_trace_rows.extend(trace_rows)
        stem = f"{case_id}_{manual_file_id}"
        physical.write_json(output_dir / "responses" / f"{stem}.json", bundle["Response"])
        physical.write_json(output_dir / "bundles" / f"{stem}.json", bundle)
        write_csv(output_dir / "traces" / f"{stem}.csv", trace_rows)

    write_csv(output_dir / "manual_restore_operation_trace.csv", all_trace_rows)
    write_csv(output_dir / "manual_restore_case_summary.csv", summaries)
    summary = build_summary(summaries, all_trace_rows)
    physical.write_json(output_dir / "manual_restore_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def ensure_label_dir(args: argparse.Namespace, root: Path, output_dir: Path) -> Path:
    if args.label_dir:
        label_dir = root / args.label_dir
        if not (label_dir / "manual_action_labels.csv").exists():
            raise FileNotFoundError(str(label_dir / "manual_action_labels.csv"))
        return label_dir

    label_dir = output_dir / "_labels"
    if (label_dir / "manual_action_labels.csv").exists():
        return label_dir
    namespace = argparse.Namespace(
        root=str(root),
        manual_root=args.manual_root,
        truth_dir=args.truth_dir,
        output_dir=str(label_dir),
    )
    result = manual_labels.run(namespace)
    if result not in (0, None):
        raise RuntimeError(f"manual label generation failed: exit={result}")
    return label_dir


def restore_case(
    labels: list[dict[str, str]],
    truth_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    case_id, payload, cars, _depot_assignment, loco = physical.read_case(truth_path)
    state = initial_identity_state(cars)
    graph = physical.TrackGraph()
    loco_line = loco.line
    operations: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    weighed: set[str] = set()
    helper_loco_count = 0
    blocked_reason = ""

    for label in labels:
        if blocked_reason:
            trace_rows.append(trace_row(label, "blocked_after_prior_gap", blocked_reason))
            continue

        before_state = state
        candidate: manual_replay.ReplayCandidate | None = None
        operation: dict[str, Any] | None = None
        status = ""
        detail = ""

        if is_helper_loco_attach(label):
            helper_loco_count += 1
            status = "noop"
            detail = "helper_loco_attach_at_engine_house"
        elif is_helper_loco_detach(label) and helper_loco_count > 0:
            helper_loco_count -= 1
            status = "noop"
            detail = "helper_loco_detach_at_engine_house"
        elif is_train_closeout_label(label) and state.train:
            operation, state, loco_line = build_closeout_operation(
                label=label,
                state=state,
                graph=graph,
                loco_line=loco_line,
                operation_index=len(operations) + 1,
            )
            operations.append(operation)
            status = "restored"
            detail = "closeout_put_remaining_train"
        else:
            noop_reason = manual_replay.identity_noop_reason(label, {state})
            if noop_reason:
                status = "noop"
                detail = noop_reason
            elif is_weigh_label(label):
                operation, loco_line, detail = build_weigh_operation(
                    label=label,
                    state=state,
                    cars=cars,
                    weighed=weighed,
                    graph=graph,
                    loco_line=loco_line,
                    operation_index=len(operations) + 1,
                )
                status = "weigh_restored" if operation else "noop"
                if operation:
                    operations.append(operation)
            elif label.get("method") in {"+", "-"} and manual_replay.label_count(label) is not None:
                candidates = manual_replay.expand_state(state, label)
                candidate = choose_candidate(label, candidates)
                if candidate is None:
                    status = "blocked"
                    detail = "no_candidate_from_current_identity_state"
                    blocked_reason = f"{label.get('manual_hook')}:{detail}"
                else:
                    operation, loco_line = build_move_operation(
                        label=label,
                        candidate=candidate,
                        before_state=before_state,
                        graph=graph,
                        loco_line=loco_line,
                        operation_index=len(operations) + 1,
                    )
                    operations.append(operation)
                    state = candidate.state
                    status = "restored" if candidate.validation != "car_no_conflict" else "restored_with_note_conflict"
                    detail = candidate.validation
            else:
                status = "noop"
                detail = unsupported_noop_reason(label)

        trace_rows.append(
            trace_row(
                label,
                status,
                detail,
                operation=operation,
                before_state=before_state,
                after_state=state,
                candidate=candidate,
            )
        )

    if state.train and not blocked_reason:
        blocked_reason = "dirty_train_after_last_manual_hook:" + ",".join(state.train)
    success = not blocked_reason
    train_line = loco_line if physical.normalize_line(loco_line) in physical.TRACK_SPECS else "机库线"
    response = {
        "Success": success,
        "Message": "" if success else blocked_reason,
        "StatusCode": 200 if success else 409,
        "Data": {
            "Operations": operations,
            "GeneratedEndStatus": generated_end_status(state, train_line=train_line),
        },
    }
    summary = case_summary(case_id, labels, trace_rows, response, blocked_reason)
    return {"Request": payload, "Response": response, "Summary": summary, "Trace": trace_rows}, trace_rows, summary


def initial_identity_state(cars: list[dict[str, Any]]) -> manual_replay.IdentityState:
    lines: dict[str, tuple[str, ...]] = {}
    for line in sorted({car["Line"] for car in cars if car.get("Line")}):
        lines[line] = tuple(physical.line_access_order(cars, line))
    return manual_replay.freeze_state(lines, tuple())


def choose_candidate(
    label: dict[str, str],
    candidates: list[manual_replay.ReplayCandidate],
) -> manual_replay.ReplayCandidate | None:
    if not candidates:
        return None
    non_conflicting = [item for item in candidates if item.validation != "car_no_conflict"]
    pool = non_conflicting or candidates
    return min(pool, key=lambda item: candidate_sort_key(label, item))


def candidate_sort_key(label: dict[str, str], candidate: manual_replay.ReplayCandidate) -> tuple[Any, ...]:
    note = str(label.get("note") or "")
    endpoint_penalty = 1 if "北头" in note and candidate.endpoint != "position_low" else 0
    scope_penalty = 0 if len(candidate.line_scope) == 1 else 1
    return (
        VALIDATION_SCORE.get(candidate.validation, 8),
        endpoint_penalty,
        scope_penalty,
        "|".join(candidate.line_scope),
        candidate.endpoint,
        candidate.moved_nos,
    )


def build_move_operation(
    *,
    label: dict[str, str],
    candidate: manual_replay.ReplayCandidate,
    before_state: manual_replay.IdentityState,
    graph: physical.TrackGraph,
    loco_line: str,
    operation_index: int,
) -> tuple[dict[str, Any], str]:
    action = "Get" if label.get("method") == "+" else "Put"
    line = operation_source_line(label, candidate, before_state) if action == "Get" else operation_target_line(label, candidate)
    path = route_between(graph, loco_line, line)
    next_loco = (
        physical.post_put_loco_location(path, line).line
        if action == "Put"
        else physical.operation_stand_location(path, line).line
    )
    return (
        {
            "Index": operation_index,
            "ManualHook": int(label.get("manual_hook") or operation_index),
            "Line": line,
            "Action": action,
            "MoveCars": list(candidate.moved_nos),
            "TrainCars": list(candidate.state.train),
            "PassbyPath": physical.route_for_output(path),
        },
        next_loco,
    )


def build_closeout_operation(
    *,
    label: dict[str, str],
    state: manual_replay.IdentityState,
    graph: physical.TrackGraph,
    loco_line: str,
    operation_index: int,
) -> tuple[dict[str, Any], manual_replay.IdentityState, str]:
    line = closeout_line(label)
    move_nos = tuple(state.train)
    path = route_between(graph, loco_line, line)
    next_state = state_after_put_train(state, line, move_nos)
    operation = {
        "Index": operation_index,
        "ManualHook": int(label.get("manual_hook") or operation_index),
        "Line": line,
        "Action": "Put",
        "MoveCars": list(move_nos),
        "TrainCars": [],
        "PassbyPath": physical.route_for_output(path),
    }
    return operation, next_state, physical.post_put_loco_location(path, line).line


def state_after_put_train(
    state: manual_replay.IdentityState,
    line: str,
    move_nos: tuple[str, ...],
) -> manual_replay.IdentityState:
    moving = set(move_nos)
    lines = manual_replay.thaw_lines(state)
    existing = tuple(no for no in lines.get(line, tuple()) if no not in moving)
    lines[line] = (*move_nos, *existing)
    return manual_replay.freeze_state(lines, tuple(no for no in state.train if no not in moving))


def closeout_line(label: dict[str, str]) -> str:
    line = physical.normalize_line(label.get("resolved_line") or label.get("line") or label.get("line_raw"))
    if line in {"机库线", "机南", "机走棚", "机走北"}:
        return line
    return "机库线"


def build_weigh_operation(
    *,
    label: dict[str, str],
    state: manual_replay.IdentityState,
    cars: list[dict[str, Any]],
    weighed: set[str],
    graph: physical.TrackGraph,
    loco_line: str,
    operation_index: int,
) -> tuple[dict[str, Any] | None, str, str]:
    if not state.train:
        return None, loco_line, "weigh_no_train"
    by_no = {physical.car_no(car): car for car in cars}
    weigh_no = next((no for no in reversed(state.train) if by_no.get(no, {}).get("IsWeigh") and no not in weighed), "")
    if not weigh_no:
        return None, loco_line, "weigh_no_pending_weigh_car"
    weighed.add(weigh_no)
    path = route_between(graph, loco_line, physical.WEIGH_LINE)
    return (
        {
            "Index": operation_index,
            "ManualHook": int(label.get("manual_hook") or operation_index),
            "Line": physical.WEIGH_LINE,
            "Action": "Weigh",
            "MoveCars": [weigh_no],
            "TrainCars": list(state.train),
            "PassbyPath": physical.route_for_output(path),
        },
        physical.operation_stand_location(path, physical.WEIGH_LINE).line,
        "weigh_tail_pending_car",
    )


def operation_source_line(
    label: dict[str, str],
    candidate: manual_replay.ReplayCandidate,
    state: manual_replay.IdentityState,
) -> str:
    line_by_no = state_line_by_no(state)
    unique_lines = [line for line in dict.fromkeys(line_by_no.get(no, "") for no in candidate.moved_nos) if line]
    if len(unique_lines) == 1:
        return unique_lines[0]
    resolved = physical.normalize_line(label.get("resolved_line"))
    if resolved in candidate.line_scope:
        return resolved
    return unique_lines[0] if unique_lines else first_scope_line(candidate, label)


def operation_target_line(label: dict[str, str], candidate: manual_replay.ReplayCandidate) -> str:
    line = manual_replay.detach_target_line(label, physical.normalize_line(label.get("resolved_line")))
    return line or first_scope_line(candidate, label)


def first_scope_line(candidate: manual_replay.ReplayCandidate, label: dict[str, str]) -> str:
    if candidate.line_scope:
        return candidate.line_scope[0]
    return physical.normalize_line(label.get("line") or label.get("line_raw"))


def state_line_by_no(state: manual_replay.IdentityState) -> dict[str, str]:
    return {no: line for line, nos in state.lines for no in nos}


def route_between(graph: physical.TrackGraph, source: str, target: str) -> list[str]:
    source = physical.normalize_line(source)
    target = physical.normalize_line(target)
    if not source:
        source = target
    if not target:
        return [source] if source else []
    route = graph.route(source, target)
    if route:
        return route
    return [source, target] if source and source != target else [target]


def generated_end_status(state: manual_replay.IdentityState, *, train_line: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line, nos in state.lines:
        for position, no in enumerate(nos, start=1):
            rows.append({"No": no, "Line": line, "Position": position})
    for position, no in enumerate(state.train, start=1):
        rows.append({"No": no, "Line": train_line, "Position": position})
    return sorted(rows, key=lambda row: (row["Line"], row["Position"], row["No"]))


def is_weigh_label(label: dict[str, str]) -> bool:
    return label.get("method") == "称" or "称" in str(label.get("note") or "")


def is_helper_loco_attach(label: dict[str, str]) -> bool:
    return (
        int(label.get("manual_hook") or 0) == 1
        and str(label.get("line_raw") or "") in {"库", "注意库"}
        and label.get("method") == "+"
        and manual_replay.label_count(label) == 1
    )


def is_helper_loco_detach(label: dict[str, str]) -> bool:
    return (
        str(label.get("line_raw") or "") in {"库", "注意库"}
        and label.get("method") == "-"
        and manual_replay.label_count(label) == 1
    )


def is_train_closeout_label(label: dict[str, str]) -> bool:
    method = str(label.get("method") or "")
    count_raw = str(label.get("count_raw") or "")
    return method == "回" or (not method and count_raw in {"回", "停"})


def unsupported_noop_reason(label: dict[str, str]) -> str:
    method = str(label.get("method") or "")
    count_raw = str(label.get("count_raw") or "")
    if not method and count_raw in {"回", "停"}:
        return f"semantic_closeout:{count_raw}"
    if method:
        return f"unsupported_manual_method:{method}"
    return "manual_row_without_identity_delta"


def trace_row(
    label: dict[str, str],
    status: str,
    detail: str,
    *,
    operation: dict[str, Any] | None = None,
    before_state: manual_replay.IdentityState | None = None,
    after_state: manual_replay.IdentityState | None = None,
    candidate: manual_replay.ReplayCandidate | None = None,
) -> dict[str, Any]:
    return {
        "case_id": label.get("case_id", ""),
        "manual_file_id": label.get("manual_file_id", ""),
        "manual_file": label.get("manual_file", ""),
        "manual_hook": label.get("manual_hook", ""),
        "operation_index": (operation or {}).get("Index", ""),
        "status": status,
        "line_raw": label.get("line_raw", ""),
        "resolved_line": label.get("resolved_line", ""),
        "method": label.get("method", ""),
        "count": label.get("count", ""),
        "effective_count": label.get("effective_count", ""),
        "note": label.get("note", ""),
        "operation_action": (operation or {}).get("Action", ""),
        "operation_line": (operation or {}).get("Line", ""),
        "move_cars": "|".join((operation or {}).get("MoveCars", [])),
        "train_cars": "|".join((operation or {}).get("TrainCars", [])),
        "passby_path": "|".join((operation or {}).get("PassbyPath", [])),
        "candidate_validation": candidate.validation if candidate else "",
        "candidate_scope": "|".join(candidate.line_scope) if candidate else "",
        "candidate_endpoint": candidate.endpoint if candidate else "",
        "manual_state_before": manual_replay.state_to_text(before_state) if before_state else "",
        "manual_state_after": manual_replay.state_to_text(after_state) if after_state else "",
        "detail": detail,
        "issue_codes": label.get("issue_codes", ""),
    }


def case_summary(
    case_id: str,
    labels: list[dict[str, str]],
    trace_rows: list[dict[str, Any]],
    response: dict[str, Any],
    blocked_reason: str,
) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in trace_rows)
    operation_count = len((response.get("Data") or {}).get("Operations") or [])
    return {
        "case_id": case_id,
        "manual_file_id": labels[0].get("manual_file_id", "") if labels else "",
        "manual_file": labels[0].get("manual_file", "") if labels else "",
        "success": int(not blocked_reason),
        "manual_hook_count": len(labels),
        "operation_count": operation_count,
        "restored_hook_count": sum(statuses.get(item, 0) for item in PASSING_RESTORE_STATUSES),
        "noop_hook_count": statuses.get("noop", 0),
        "blocked_hook_count": statuses.get("blocked", 0) + statuses.get("blocked_after_prior_gap", 0),
        "status_counts": counter_text(statuses),
        "blocked_reason": blocked_reason,
    }


def missing_truth_summary(labels: list[dict[str, str]]) -> dict[str, Any]:
    trace_rows = [trace_row(label, "truth_missing", "truth_case_missing") for label in labels]
    return {
        "case_id": labels[0].get("case_id", "") if labels else "",
        "manual_file_id": labels[0].get("manual_file_id", "") if labels else "",
        "manual_file": labels[0].get("manual_file", "") if labels else "",
        "success": 0,
        "manual_hook_count": len(labels),
        "operation_count": 0,
        "restored_hook_count": 0,
        "noop_hook_count": 0,
        "blocked_hook_count": len(labels),
        "status_counts": f"truth_missing:{len(labels)}",
        "blocked_reason": "truth_case_missing",
        "_trace_rows": trace_rows,
    }


def build_summary(summaries: list[dict[str, Any]], trace_rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in trace_rows)
    success_count = sum(int(row.get("success") or 0) for row in summaries)
    operation_count = sum(int(row.get("operation_count") or 0) for row in summaries)
    hook_count = sum(int(row.get("manual_hook_count") or 0) for row in summaries)
    return {
        "manual_file_count": len(summaries),
        "success_file_count": success_count,
        "partial_or_blocked_file_count": len(summaries) - success_count,
        "manual_hook_count": hook_count,
        "operation_count": operation_count,
        "status_counts": dict(statuses),
        "operation_per_manual_hook_rate": round(operation_count / hook_count, 4) if hook_count else 0,
        "restoration_rules": {
            "库": "机库线",
            "机": "取车范围为机南 + 机走棚 + 机走北(机北3)，不包含机库；放车备注南时落机南，否则落机走棚",
            "辅助调车机": "第一勾库+1表示在机库挂另一台调车机，后续库-1优先解释为解除该调车机，不改变货车身份",
            "洗线组": "洗罐站 + 洗罐线北",
            "机走组": "机南 + 机走棚 + 机走北(机北3)",
            "修库组": "修N库内 + 修N库外",
            "调梁组": "调梁棚 + 调梁线北",
            "存5组": "存5线南 + 存5线北",
            "存2叉线": "预修线",
        },
    }


def counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{value}" for key, value in sorted(counter.items()))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
