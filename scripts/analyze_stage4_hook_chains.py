#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CACHE_LINES = {
    "存1线",
    "存2线",
    "存3线",
    "机走北",
    "调梁线北",
    "洗罐线北",
    "机走棚",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def accepted_rows(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in trace if row.get("accepted")]


def parse_candidate(candidate_id: str) -> list[dict[str, Any]]:
    if ":stage4_" in candidate_id:
        body = candidate_id.split(":stage4_", 1)[1]
        body = body.split(":", 1)[1] if ":" in body else body
    else:
        body = candidate_id
    steps: list[dict[str, Any]] = []
    for raw in body.split(";"):
        parts = raw.split(":", 2)
        if len(parts) < 2:
            continue
        action = parts[0]
        line = parts[1]
        nos = []
        if len(parts) == 3 and parts[2]:
            nos = [item for item in parts[2].split(",") if item]
        if action in {"Get", "Put", "Weigh"}:
            steps.append({"action": action, "line": line, "nos": nos})
    return steps


def purpose_for(reason: str) -> str:
    if "cun5" in reason:
        return "存5南北分段转移"
    if "spotting_cross_repack" in reason:
        return "强对位跨线重排"
    if "target_rebuild_route_session" in reason:
        return "目标重建+路径会话"
    if "target_rebuild" in reason:
        return "目标线重建"
    if "put_unblock" in reason or "get_unblock" in reason:
        return "路径/取放清障"
    if "cache" in reason:
        return "缓存解阻/临时编组"
    if "service_sweep_target" in reason:
        return "直送/连续分放"
    return reason or "unknown"


def debt_count(row: dict[str, Any], key: str) -> int:
    debt = row.get(key) or {}
    return int(debt.get("active_unsatisfied_count") or 0)


def blocked_count(row: dict[str, Any], key: str) -> int:
    debt = row.get(key) or {}
    return int(debt.get("blocked_active_count") or 0)


def prep_drop(prev: tuple[int, ...] | None, cur: tuple[int, ...]) -> int:
    if prev is None:
        return 0
    return sum(max(0, before - after) for before, after in zip(prev, cur))


def put_lines(steps: list[dict[str, Any]]) -> set[str]:
    return {step["line"] for step in steps if step["action"] == "Put"}


def get_lines(steps: list[dict[str, Any]]) -> set[str]:
    return {step["line"] for step in steps if step["action"] == "Get"}


def put_nos_by_line(steps: list[dict[str, Any]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for step in steps:
        if step["action"] == "Put":
            out[step["line"]].update(step["nos"])
    return out


def get_nos_by_line(steps: list[dict[str, Any]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for step in steps:
        if step["action"] == "Get":
            out[step["line"]].update(step["nos"])
    return out


def transition_kind(cur: dict[str, Any], nxt: dict[str, Any] | None) -> str:
    if nxt is None:
        return "END"
    cur_steps = cur["_steps"]
    nxt_steps = nxt["_steps"]
    cur_put_lines = put_lines(cur_steps)
    nxt_get_lines = get_lines(nxt_steps)
    cur_put_nos = put_nos_by_line(cur_steps)
    nxt_get_nos = get_nos_by_line(nxt_steps)
    for line in sorted(cur_put_lines & nxt_get_lines):
        if line in CACHE_LINES and cur_put_nos[line] & nxt_get_nos[line]:
            return "缓存立即回取"
    if cur.get("reason") == nxt.get("reason") == "closed_target_rebuild" and cur.get("target") == nxt.get("target"):
        return "同目标连续重建"
    if cur.get("target") and cur.get("target") == nxt.get("target"):
        return "同目标连续服务"
    if cur.get("source") and cur.get("source") == nxt.get("source"):
        return "同源连续取送"
    if cur_put_lines & nxt_get_lines:
        return "本宏落点成为下一宏来源"
    return "切换作业对象"


@dataclass
class MacroRecord:
    case_id: str
    status: str
    macro: int
    reason: str
    purpose: str
    source: str
    target: str
    operations: int
    debt_before: int
    debt_after: int
    debt_drop: int
    blocked_before: int
    blocked_after: int
    blocked_drop: int
    prep_drop: int
    transition_to_next: str
    immediate_redeemed: bool
    redeemed_within_3: bool
    move_count: int
    get_lines: str
    put_lines: str


def load_case(out_dir: Path, summary_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = read_json(summary_path)
    case_id = summary.get("case_id") or summary_path.name.split("_", 1)[0]
    trace_path = out_dir / f"{case_id}_trace.json"
    trace = read_json(trace_path) if trace_path.exists() else []
    return summary, trace


def build_records(out_dir: Path) -> tuple[list[MacroRecord], list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[MacroRecord] = []
    case_summaries: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    for summary_path in sorted(out_dir.glob("*_summary.json")):
        if summary_path.name == "aggregate_summary.json":
            continue
        summary, trace = load_case(out_dir, summary_path)
        case_id = summary.get("case_id") or summary_path.name.split("_", 1)[0]
        rows = accepted_rows(trace)
        for row in rows:
            row["_steps"] = parse_candidate(str(row.get("accepted") or ""))
        prev_prep: tuple[int, ...] | None = None
        for idx, row in enumerate(rows):
            nxt = rows[idx + 1] if idx + 1 < len(rows) else None
            cur_prep = tuple(int(x) for x in row.get("prep_after") or [])
            d_before = debt_count(row, "debt_before")
            d_after = debt_count(row, "debt_after")
            b_before = blocked_count(row, "debt_before")
            b_after = blocked_count(row, "debt_after")
            future_drops = [
                max(0, debt_count(future, "debt_before") - debt_count(future, "debt_after"))
                for future in rows[idx + 1 : idx + 4]
            ]
            steps = row["_steps"]
            rec = MacroRecord(
                case_id=case_id,
                status=str(summary.get("status") or ""),
                macro=int(row.get("macro") or 0),
                reason=str(row.get("reason") or ""),
                purpose=purpose_for(str(row.get("reason") or "")),
                source=str(row.get("source") or ""),
                target=str(row.get("target") or ""),
                operations=int(row.get("operations") or 0),
                debt_before=d_before,
                debt_after=d_after,
                debt_drop=d_before - d_after,
                blocked_before=b_before,
                blocked_after=b_after,
                blocked_drop=b_before - b_after,
                prep_drop=prep_drop(prev_prep, cur_prep),
                transition_to_next=transition_kind(row, nxt),
                immediate_redeemed=bool(nxt and debt_count(nxt, "debt_before") > debt_count(nxt, "debt_after")),
                redeemed_within_3=any(drop > 0 for drop in future_drops),
                move_count=len(row.get("move") or []),
                get_lines="|".join(sorted(get_lines(steps))),
                put_lines="|".join(sorted(put_lines(steps))),
            )
            records.append(rec)
            prev_prep = cur_prep
        reason_counter: Counter[str] = Counter()
        for row in trace:
            if row.get("reason") == "candidate_rejections":
                for rejection in row.get("rejected") or []:
                    for violation in rejection.get("violations") or []:
                        reason_counter[str(violation).split(":", 1)[0]] += 1
                        rejection_rows.append(
                            {
                                "case_id": case_id,
                                "candidate_reason": rejection.get("reason", ""),
                                "violation": str(violation),
                            }
                        )
        case_summaries.append(
            {
                "case_id": case_id,
                "status": summary.get("status", ""),
                "hooks": int(summary.get("business_hooks") or 0),
                "operations": int(summary.get("operations") or 0),
                "final_unsatisfied": int(summary.get("final_unsatisfied_count") or 0),
                "active_unsatisfied": int((summary.get("stage4_debt") or {}).get("active_unsatisfied_count") or 0),
                "strategy": summary.get("stage4_strategy", ""),
                "accepted_macros": len(rows),
                "rejection_top": ";".join(f"{k}:{v}" for k, v in reason_counter.most_common(5)),
            }
        )
    return records, case_summaries, rejection_rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def aggregate(records: list[MacroRecord]) -> dict[str, Any]:
    by_purpose: dict[str, dict[str, Any]] = {}
    for rec in records:
        item = by_purpose.setdefault(
            rec.purpose,
            {
                "macros": 0,
                "hooks": 0,
                "debt_drop": 0,
                "blocked_drop": 0,
                "prep_drop": 0,
                "unredeemed_prep": 0,
            },
        )
        item["macros"] += 1
        item["hooks"] += rec.operations
        item["debt_drop"] += max(0, rec.debt_drop)
        item["blocked_drop"] += rec.blocked_drop
        item["prep_drop"] += rec.prep_drop
        if rec.debt_drop <= 0 and rec.prep_drop > 0 and not rec.redeemed_within_3:
            item["unredeemed_prep"] += 1
    return by_purpose


def chain_rebuilds(records: list[MacroRecord]) -> list[dict[str, Any]]:
    chains: list[dict[str, Any]] = []
    by_case: dict[str, list[MacroRecord]] = defaultdict(list)
    for rec in records:
        by_case[rec.case_id].append(rec)
    for case_id, items in by_case.items():
        i = 0
        while i < len(items):
            rec = items[i]
            if rec.reason != "closed_target_rebuild":
                i += 1
                continue
            j = i + 1
            while j < len(items) and items[j].reason == rec.reason and items[j].target == rec.target:
                j += 1
            if j - i >= 2:
                part = items[i:j]
                chains.append(
                    {
                        "case_id": case_id,
                        "start_macro": part[0].macro,
                        "end_macro": part[-1].macro,
                        "target": rec.target,
                        "chain_len": len(part),
                        "hooks": sum(x.operations for x in part),
                        "debt_drop": sum(max(0, x.debt_drop) for x in part),
                        "sources": "|".join(sorted({x.source for x in part if x.source})),
                    }
                )
            i = j
    chains.sort(key=lambda row: (-int(row["hooks"]), -int(row["chain_len"]), row["case_id"]))
    return chains


def markdown_table(rows: list[list[Any]], headers: list[str]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def write_report(
    path: Path,
    out_dir: Path,
    records: list[MacroRecord],
    cases: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    chains: list[dict[str, Any]],
) -> None:
    complete_cases = [c for c in cases if c["status"] == "complete"]
    partial_cases = [c for c in cases if c["status"] != "complete"]
    purpose = aggregate(records)
    purpose_rows = []
    for name, item in sorted(purpose.items(), key=lambda kv: -kv[1]["hooks"]):
        debt = item["debt_drop"]
        purpose_rows.append(
            [
                name,
                item["macros"],
                item["hooks"],
                debt,
                item["blocked_drop"],
                item["prep_drop"],
                item["unredeemed_prep"],
                f"{item['hooks'] / debt:.2f}" if debt else "-",
            ]
        )
    transition_counter = Counter(rec.transition_to_next for rec in records)
    transition_rows = [[k, v] for k, v in transition_counter.most_common()]
    rejection_counter = Counter(str(row["violation"]).split(":", 1)[0] for row in rejections)
    rejection_rows = [[k, v] for k, v in rejection_counter.most_common(12)]
    high_hook = sorted(complete_cases, key=lambda row: -int(row["hooks"]))[:15]
    high_rows = [
        [row["case_id"], row["hooks"], row["accepted_macros"], row["strategy"]]
        for row in high_hook
    ]
    partial_rows = [
        [row["case_id"], row["hooks"], row["active_unsatisfied"], row["final_unsatisfied"], row["strategy"], row["rejection_top"]]
        for row in sorted(partial_cases, key=lambda row: (-int(row["active_unsatisfied"]), row["case_id"]))
        if row["active_unsatisfied"]
    ][:20]
    chain_rows = [
        [
            row["case_id"],
            f"M{row['start_macro']}-M{row['end_macro']}",
            row["target"],
            row["chain_len"],
            row["hooks"],
            row["debt_drop"],
            row["sources"],
        ]
        for row in chains[:15]
    ]
    lines = [
        "# Stage4 Hook Chain Diagnosis",
        "",
        f"- artifact: `{out_dir}`",
        f"- cases: {len(cases)}",
        f"- complete: {len(complete_cases)}",
        f"- partial/unusable: {len(partial_cases)}",
        f"- accepted macros: {len(records)}",
        f"- complete avg hooks: {sum(int(c['hooks']) for c in complete_cases) / max(1, len(complete_cases)):.2f}",
        "",
        "## Macro Purpose Cost",
        "",
        markdown_table(
            purpose_rows,
            ["purpose", "macros", "hooks", "debt_drop", "blocked_drop", "prep_drop", "unredeemed_prep", "hooks/debt"],
        ),
        "",
        "## Adjacent Hook Linkage",
        "",
        markdown_table(transition_rows, ["linkage", "count"]),
        "",
        "## Highest Hook Complete Cases",
        "",
        markdown_table(high_rows, ["case", "hooks", "macros", "strategy"]),
        "",
        "## Highest Cost Consecutive Target Rebuild Chains",
        "",
        markdown_table(chain_rows, ["case", "macro_range", "target", "chain_len", "hooks", "debt_drop", "sources"]),
        "",
        "## Remaining Partial Rejection Signals",
        "",
        markdown_table(rejection_rows, ["violation", "count"]),
        "",
        "## Remaining Partial Cases",
        "",
        markdown_table(partial_rows, ["case", "hooks", "active_unsat", "final_unsat", "strategy", "top_rejections"]),
        "",
        "## Diagnosis",
        "",
        "1. Most accepted hooks are locally necessary: they either reduce active debt, reduce blocking, or reduce route/access prep metrics.",
        "2. The largest hook-reduction opportunity is not direct sweep, but repeated target rebuild chains, especially on 调梁棚.",
        "3. Cache moves are usually useful, but immediate cache re-get still indicates missing reservation/session planning.",
        "4. Remaining partial cases are dominated by route occupancy and access-end/window constraints; these are missing capabilities, not scoring noise.",
        "5. The next solver step should be target/session-driven planning: aggregate multiple source fronts for one target, clear route blockers as part of the same session, and rebuild only the required window.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    records, case_summaries, rejection_rows = build_records(args.out_dir)
    report_path = args.report or args.out_dir / "hook_chain_diagnosis_v2.md"
    macro_rows = [rec.__dict__ for rec in records]
    write_csv(
        args.out_dir / "macro_link_analysis_v2.csv",
        macro_rows,
        list(MacroRecord.__annotations__.keys()),
    )
    write_csv(
        args.out_dir / "case_chain_summary_v2.csv",
        case_summaries,
        ["case_id", "status", "hooks", "operations", "final_unsatisfied", "active_unsatisfied", "strategy", "accepted_macros", "rejection_top"],
    )
    chains = chain_rebuilds(records)
    write_csv(
        args.out_dir / "same_target_rebuild_chains_v2.csv",
        chains,
        ["case_id", "start_macro", "end_macro", "target", "chain_len", "hooks", "debt_drop", "sources"],
    )
    write_report(report_path, args.out_dir, records, case_summaries, rejection_rows, chains)
    print(report_path)


if __name__ == "__main__":
    main()
