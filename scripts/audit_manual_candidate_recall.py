#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from solver_vnext import physical
from solver_vnext.contracts import build_contracts
from solver_vnext.delta import build_contract_delta, simulate_candidate
from solver_vnext.domain import CandidateEnvelope
from solver_vnext.episodes import EPISODES
from solver_vnext.resources import StationResourceGraph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit manual action candidate recall on manual replay states.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--label-dir", required=True)
    parser.add_argument("--replay-dir", required=True)
    parser.add_argument("--owner-blocker-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-candidates-per-action", type=int, default=512)
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    label_rows = read_csv(root / args.label_dir / "manual_action_labels.csv")
    replay_rows = read_csv(root / args.replay_dir / "manual_identity_replay_trace.csv")
    owner_rows = read_csv(root / args.owner_blocker_dir / "manual_owner_blocker_labels.csv")
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    truth_index = build_truth_index(root / args.truth_dir)
    replay_by_key = {(row.get("case_id", ""), row.get("manual_hook", "")): row for row in replay_rows}
    owner_by_key = {(row.get("case_id", ""), row.get("manual_hook", "")): row for row in owner_rows}

    recall_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    example_rows: list[dict[str, Any]] = []
    for label in sorted(label_rows, key=lambda row: (row.get("case_id", ""), to_int(row.get("manual_hook")))):
        key = (label.get("case_id", ""), label.get("manual_hook", ""))
        replay = replay_by_key.get(key, {})
        owner = owner_by_key.get(key, {})
        result, examples = audit_one_action(
            label=label,
            replay=replay,
            owner=owner,
            truth=truth_index.get(label.get("case_id", "")),
            max_candidates=args.max_candidates_per_action,
        )
        recall_rows.append(result)
        example_rows.extend(examples)
        if result["equivalent_level"] == "none":
            gap_rows.append(
                {
                    "case_id": result["case_id"],
                    "manual_hook": result["manual_hook"],
                    "phase_label": result["phase_label"],
                    "manual_intent": result["manual_intent"],
                    "gap_reason": result["gap_reason"],
                    "rejected_stage": result["rejected_stage"],
                    "reject_reason": result["reject_reason"],
                    "state_source": result["state_source"],
                }
            )

    summary = build_summary(recall_rows, gap_rows)
    physical.write_csv(output_dir / "manual_candidate_recall.csv", recall_rows)
    physical.write_csv(output_dir / "manual_candidate_gap_records.csv", gap_rows)
    physical.write_csv(output_dir / "manual_equivalent_candidate_examples.csv", example_rows)
    physical.write_json(output_dir / "manual_candidate_recall_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote: {output_dir}")
    return 0


def audit_one_action(
    *,
    label: dict[str, str],
    replay: dict[str, str],
    owner: dict[str, str],
    truth: dict[str, Any] | None,
    max_candidates: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = {
        "case_id": label.get("case_id", ""),
        "manual_hook": label.get("manual_hook", ""),
        "phase_label": label.get("phase_label", ""),
        "manual_intent": label.get("intent", ""),
        "manual_method": label.get("method", ""),
        "manual_line": physical.normalize_line(label.get("resolved_line", "")),
        "owner_nos": owner.get("owner_nos", ""),
        "blocker_nos": owner.get("blocker_nos", ""),
        "candidate_generated": 0,
        "candidate_template": "",
        "equivalent_level": "none",
        "physical_pass": "",
        "resource_pass": "",
        "contract_reduction": "",
        "gap_reason": "",
        "state_source": "manual_replay",
        "rejected_stage": "",
        "reject_reason": "",
        "candidate_count": 0,
    }
    if label.get("manual_reference_scope") == "exclude_from_algorithm_learning":
        base["gap_reason"] = "excluded_reference"
        return base, []
    if owner.get("label_status") == "gap" or not owner.get("owner_nos"):
        base["gap_reason"] = f"owner_label_gap:{owner.get('gap_reason', '')}"
        return base, []
    state_text = replay.get("manual_state_before", "")
    if not state_text:
        base["gap_reason"] = "manual_state_snapshot_missing_or_not_unique"
        base["state_source"] = "manual_replay_gap"
        return base, []
    state = parse_state_text(state_text)
    if state["train"]:
        base["gap_reason"] = "manual_train_state_not_solver_hook_boundary"
        return base, []
    if truth is None:
        base["gap_reason"] = "truth_case_missing"
        return base, []
    method = label.get("method", "")
    if method not in {"+", "-"}:
        base["gap_reason"] = "manual_action_not_candidate_move"
        return base, []

    cars = cars_from_state(truth["cars"], state)
    depot_assignment = physical.current_depot_assignment(truth["depot_assignment"], cars)
    graph = physical.TrackGraph()
    resources = StationResourceGraph()
    loco = physical.LocoLocation(base["manual_line"])
    owner_set = set(split_nos(owner.get("owner_nos", "")))
    blocker_set = set(split_nos(owner.get("blocker_nos", "")))
    exact_examples: list[dict[str, Any]] = []
    structural_examples: list[dict[str, Any]] = []
    generated_count = 0
    reject_stages: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()

    for contract in build_contracts(cars, depot_assignment):
        for episode in EPISODES:
            if not episode.applies(contract):
                continue
            for raw_envelope in episode.generate(
                case_id=label.get("case_id", ""),
                hook_index=to_int(label.get("manual_hook")),
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco,
                serial_gate_leases={},
                contract=contract,
            ):
                generated_count += 1
                if generated_count > max_candidates:
                    base["candidate_count"] = generated_count
                    base["gap_reason"] = "candidate_budget_exceeded"
                    base["rejected_stage"] = "budget"
                    return base, exact_examples + structural_examples
                resource_request = resources.request_for(raw_envelope)
                envelope = CandidateEnvelope(
                    candidate=raw_envelope.candidate,
                    contract=raw_envelope.contract,
                    intent=raw_envelope.intent,
                    resource_request=resource_request,
                    template_name=raw_envelope.template_name,
                )
                candidate = envelope.candidate
                candidate_nos = set(candidate.move_car_nos)
                if not owner_set <= candidate_nos:
                    continue
                validation = physical.validate_candidate(graph, candidate, cars, loco, depot_assignment)
                if validation.reasons:
                    reject_stages["physical"] += 1
                    reject_reasons.update(validation.reasons)
                    continue
                resource_delta = resources.acquire(
                    resource_request,
                    candidate=candidate,
                    validation=validation,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    serial_gate_leases={},
                )
                if resource_delta.violations:
                    reject_stages["resource"] += 1
                    reject_reasons.update(resource_delta.violations)
                    continue
                prospective = simulate_candidate(candidate, cars, validation)
                delta = build_contract_delta(
                    envelope,
                    cars=cars,
                    prospective_cars=prospective,
                    depot_assignment=depot_assignment,
                )
                if delta.contract_reduction <= 0:
                    reject_stages["contract"] += 1
                    reject_reasons.update(["contract_delta_mismatch"])
                    continue
                level = equivalent_level(
                    method=method,
                    manual_line=base["manual_line"],
                    candidate_source=candidate.source_line,
                    candidate_target=resource_request.target_line,
                    owner_set=owner_set,
                    blocker_set=blocker_set,
                    candidate_nos=candidate_nos,
                )
                if level == "exact":
                    exact_examples.append(example_row(label, envelope, delta.contract_reduction, "exact"))
                elif level == "structural":
                    structural_examples.append(example_row(label, envelope, delta.contract_reduction, "structural"))

    base["candidate_generated"] = int(generated_count > 0)
    base["candidate_count"] = generated_count
    if exact_examples:
        chosen = exact_examples[0]
        base.update(
            {
                "candidate_template": chosen["candidate_template"],
                "equivalent_level": "exact",
                "physical_pass": 1,
                "resource_pass": 1,
                "contract_reduction": chosen["contract_reduction"],
                "gap_reason": "",
            }
        )
    elif structural_examples:
        chosen = structural_examples[0]
        base.update(
            {
                "candidate_template": chosen["candidate_template"],
                "equivalent_level": "structural",
                "physical_pass": 1,
                "resource_pass": 1,
                "contract_reduction": chosen["contract_reduction"],
                "gap_reason": "",
            }
        )
    else:
        base["gap_reason"] = "no_equivalent_candidate" if generated_count else "no_applicable_episode"
        base["rejected_stage"] = reject_stages.most_common(1)[0][0] if reject_stages else "none"
        base["reject_reason"] = reject_reasons.most_common(1)[0][0] if reject_reasons else ""
    return base, exact_examples + structural_examples


def equivalent_level(
    *,
    method: str,
    manual_line: str,
    candidate_source: str,
    candidate_target: str,
    owner_set: set[str],
    blocker_set: set[str],
    candidate_nos: set[str],
) -> str:
    line_match = candidate_source == manual_line if method == "+" else candidate_target == manual_line
    blocker_match = blocker_set <= candidate_nos
    if owner_set <= candidate_nos and line_match and blocker_match:
        return "exact"
    if owner_set <= candidate_nos:
        return "structural"
    return "none"


def example_row(
    label: dict[str, str],
    envelope: CandidateEnvelope,
    contract_reduction: int,
    level: str,
) -> dict[str, Any]:
    candidate = envelope.candidate
    return {
        "case_id": label.get("case_id", ""),
        "manual_hook": label.get("manual_hook", ""),
        "equivalent_level": level,
        "candidate_id": candidate.candidate_id,
        "candidate_template": envelope.template_name,
        "source_line": candidate.source_line,
        "target_line": envelope.resource_request.target_line,
        "move_nos": "|".join(candidate.move_car_nos),
        "contract_id": envelope.contract.contract_id,
        "contract_reduction": contract_reduction,
    }


def build_truth_index(truth_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(truth_dir.glob("*.json")):
        if path.name == "conversion_summary.json":
            continue
        case_id, _payload, cars, depot_assignment, _loco = physical.read_case(path)
        result[case_id] = {
            "cars": {physical.car_no(car): dict(car) for car in cars},
            "depot_assignment": depot_assignment,
        }
    return result


def cars_from_state(base_cars: dict[str, dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    result = {no: dict(car) for no, car in base_cars.items()}
    for car in result.values():
        car["Line"] = ""
        car["Position"] = 0
    seen: set[str] = set()
    for line, nos in state["lines"].items():
        for index, no in enumerate(nos, start=1):
            if no in result:
                result[no]["Line"] = line
                result[no]["Position"] = index
                seen.add(no)
    for no in state["train"]:
        if no in result:
            result[no]["Line"] = ""
            result[no]["Position"] = 0
            seen.add(no)
    missing = sorted(set(result) - seen)
    if missing:
        raise ValueError(f"manual state missing cars: {','.join(missing[:10])}")
    return [result[no] for no in sorted(result)]


def parse_state_text(text: str) -> dict[str, Any]:
    if not text.startswith("lines="):
        raise ValueError(f"invalid manual state text: {text[:80]}")
    line_part, train_part = text.split(";train=", 1)
    line_part = line_part.removeprefix("lines=")
    lines: dict[str, list[str]] = {}
    if line_part:
        for chunk in line_part.split("|"):
            line, nos_text = chunk.split(":", 1)
            lines[line] = [no for no in nos_text.split(",") if no]
    train = [no for no in train_part.split(",") if no]
    return {"lines": lines, "train": train}


def build_summary(rows: list[dict[str, Any]], gap_rows: list[dict[str, Any]]) -> dict[str, Any]:
    reference = [row for row in rows if not str(row["gap_reason"]).startswith("excluded_reference")]
    by_phase = defaultdict(list)
    for row in reference:
        by_phase[row["phase_label"]].append(row)
    phase_recall = {
        phase: {
            "count": len(items),
            "exact": sum(1 for item in items if item["equivalent_level"] == "exact"),
            "structural": sum(1 for item in items if item["equivalent_level"] == "structural"),
            "none": sum(1 for item in items if item["equivalent_level"] == "none"),
        }
        for phase, items in sorted(by_phase.items())
    }
    return {
        "action_count": len(rows),
        "reference_action_count": len(reference),
        "equivalent_level_counts": dict(Counter(row["equivalent_level"] for row in reference)),
        "gap_reason_counts": dict(Counter(row["gap_reason"] for row in gap_rows if row["gap_reason"]).most_common(20)),
        "phase_recall": phase_recall,
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def split_nos(value: str) -> list[str]:
    return [item for item in str(value or "").split("|") if item]


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
