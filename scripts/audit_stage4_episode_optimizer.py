from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import replay_validator as rv  # noqa: E402
from solver_vnext import physical  # noqa: E402
from stage4_simple.episode import OpenCarryEpisodeOptimizer  # noqa: E402
from stage4_simple.contracts import mandatory_rehook_prefix_hooks  # noqa: E402
from stage4_simple.planner import (  # noqa: E402
    ContractPlanner,
    PlanningConfig,
)
from stage4_simple.scope import build_scope  # noqa: E402
from stage4_simple.search import OperationTransitions, Stage4Problem  # noqa: E402


HARD_REPLAY_KINDS = frozenset({"schema", "physical", "train", "transition"})


@dataclass(frozen=True)
class CaseAudit:
    case_id: str
    dataset: str
    baseline_hooks: int
    optimized_hooks: int
    saved_hooks: int
    baseline_semantic_reopens: int
    optimized_semantic_reopens: int
    lower_bound: int
    evaluated_paths: int
    label_budget_exhausted: bool
    contractions: tuple[str, ...]


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object:{path}")
    return value


def original_case_path(data_root: Path, dataset: str, case_id: str) -> Path:
    matches = tuple((data_root / dataset).glob(f"*{case_id}.json"))
    if len(matches) != 1:
        raise ValueError(
            f"original_case_match:{dataset}:{case_id}:count={len(matches)}"
        )
    return matches[0]


def response_steps(response: dict[str, Any]) -> tuple[physical.PlanStep, ...]:
    data = response.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("Operations"), list):
        raise ValueError("response.Data.Operations must be an array")
    return tuple(
        physical.plan_step(
            str(row.get("Action") or ""),
            str(row.get("Line") or ""),
            tuple(str(no) for no in row.get("MoveCars") or ()),
        )
        for row in data["Operations"]
        if isinstance(row, dict)
    )


def physical_certificate(
    problem: Stage4Problem,
    steps: tuple[physical.PlanStep, ...],
) -> tuple[physical.HookCandidate, physical.PhysicalValidation] | None:
    if not steps:
        return None
    touched = {no for step in steps for no in step.move_car_nos}
    source = next(
        (step.line for step in steps if step.action == "Get"),
        problem.loco_location.line,
    )
    target = next(
        (step.line for step in reversed(steps) if step.action == "Put"),
        source,
    )
    candidate = physical.build_planlet_candidate(
        case_id=problem.case_id,
        hook_index=1,
        source_line=source,
        target_line=target,
        batch=[problem.by_no[no] for no in sorted(touched)],
        steps=steps,
        reason="open_carry_episode_audit",
        candidate_kind="blocker_relocation",
    )
    validation = physical.validate_planlet(
        problem.graph,
        candidate,
        problem.cars,
        problem.loco_location,
        problem.depot_assignment,
    )
    if not validation.accepted:
        raise ValueError(
            f"planlet_invalid:{problem.case_id}:{'|'.join(validation.reasons)}"
        )
    return candidate, validation


def audit_case(
    stage4_root: Path,
    data_root: Path,
    dataset: str,
    case_id: str,
    episode_max_labels: int = 8_192,
) -> CaseAudit:
    case_root = stage4_root / dataset
    request = read_json(case_root / f"{case_id}_stage4_request.json")
    response = read_json(case_root / f"{case_id}_response.json")
    _case, original_request, _cars, assignment, _loco = physical.read_case(
        original_case_path(data_root, dataset, case_id)
    )
    start_status = request.get("StartStatus")
    if not isinstance(start_status, list):
        raise ValueError(f"stage4_start_status_missing:{case_id}")
    cars = [
        physical.normalized_car(car)
        for car in start_status
        if isinstance(car, dict)
    ]
    scope = build_scope(cars, assignment)
    problem = Stage4Problem(
        case_id=case_id,
        cars=cars,
        loco_location=physical.initial_loco_location(request.get("locoNode") or {}),
        depot_assignment=assignment,
        target_by_no=scope.target_by_no,
        active_nos=scope.active_nos,
        protected_nos=scope.protected_nos,
        capacity_holdout_nos=scope.infeasible_nos,
    )
    transitions = OperationTransitions(problem)
    steps = response_steps(response)
    planner = ContractPlanner(
        problem,
        PlanningConfig(time_budget_seconds=300.0, max_expansions=30_000),
    )
    planner.resolve_depot_rehook()
    mandatory_prefix = planner.builder.node.steps
    if [
        (step.action, step.line, step.move_car_nos)
        for step in steps[: len(mandatory_prefix)]
    ] != [
        (step.action, step.line, step.move_car_nos)
        for step in mandatory_prefix
    ]:
        raise ValueError(f"rehook_prefix_mismatch:{case_id}")
    mandatory_hooks = mandatory_rehook_prefix_hooks(problem, mandatory_prefix)
    optimizer = OpenCarryEpisodeOptimizer(
        problem,
        transitions,
        mandatory_get_prefix_hooks=mandatory_hooks,
        max_labels=episode_max_labels,
    )
    baseline = optimizer.replay(steps)
    if baseline is None or not problem.complete(baseline):
        raise ValueError(f"baseline_invalid:{case_id}")
    optimized = optimizer.optimize(baseline)
    if optimized.node.cost > baseline.cost:
        raise ValueError(f"objective_regression:{case_id}")
    if optimized.node.steps[:mandatory_hooks] != mandatory_prefix[:mandatory_hooks]:
        raise ValueError(f"mandatory_rehook_changed:{case_id}")

    certificate = physical_certificate(problem, optimized.node.steps)
    operations = [] if certificate is None else [
        physical.response_operation(row)
        for row in physical.operation_rows(*certificate, 1)
    ]
    _replayed, violations = rv.replay(
        request,
        {"Data": {"Operations": operations}},
    )
    hard = [item for item in violations if item.kind in HARD_REPLAY_KINDS]
    if hard:
        detail = ";".join(
            f"{item.kind}:{item.code}:{item.detail}" for item in hard[:5]
        )
        raise ValueError(f"replay_invalid:{case_id}:{detail}")

    combined_response = read_json(
        case_root / f"{case_id}_combined_response.json"
    )
    baseline_combined = rv.operations(combined_response)
    if len(baseline_combined) < len(steps):
        raise ValueError(f"combined_stage4_suffix_missing:{case_id}")
    upstream = baseline_combined[: len(baseline_combined) - len(steps)]
    combined_operations = []
    for row in (*upstream, *operations):
        copied = dict(row)
        copied["Index"] = len(combined_operations) + 1
        combined_operations.append(copied)
    _combined, combined_violations = rv.replay(
        original_request,
        {"Data": {"Operations": combined_operations}},
    )
    combined_hard = [
        item for item in combined_violations if item.kind in HARD_REPLAY_KINDS
    ]
    if combined_hard:
        detail = ";".join(
            f"{item.kind}:{item.code}:{item.detail}"
            for item in combined_hard[:5]
        )
        raise ValueError(f"combined_replay_invalid:{case_id}:{detail}")

    baseline_hooks = baseline.cost.hooks
    optimized_hooks = optimized.node.cost.hooks
    initial = physical.initial_planlet_state(problem.cars, problem.loco_location)
    return CaseAudit(
        case_id=case_id,
        dataset=dataset,
        baseline_hooks=baseline_hooks,
        optimized_hooks=optimized_hooks,
        saved_hooks=baseline_hooks - optimized_hooks,
        baseline_semantic_reopens=baseline.cost.target_reopens,
        optimized_semantic_reopens=optimized.node.cost.target_reopens,
        lower_bound=problem.hook_lower_bound(initial),
        evaluated_paths=optimized.evaluated_paths,
        label_budget_exhausted=optimized.label_budget_exhausted,
        contractions=tuple(item.kind for item in optimized.contractions),
    )


def report(
    stage4_root: Path,
    data_root: Path,
    episode_max_labels: int,
) -> dict[str, Any]:
    cases: list[CaseAudit] = []
    for dataset in ("truth2", "truth3"):
        root = stage4_root / dataset
        for path in sorted(root.glob("[0-9]*_summary.json")):
            case_id = path.name.removesuffix("_summary.json")
            cases.append(audit_case(
                stage4_root,
                data_root,
                dataset,
                case_id,
                episode_max_labels,
            ))
    contractions = Counter(
        kind for case in cases for kind in case.contractions
    )
    return {
        "cases": len(cases),
        "baseline_hooks": sum(case.baseline_hooks for case in cases),
        "optimized_hooks": sum(case.optimized_hooks for case in cases),
        "saved_hooks": sum(case.saved_hooks for case in cases),
        "changed_cases": sum(case.saved_hooks > 0 for case in cases),
        "regressed_cases": sum(case.saved_hooks < 0 for case in cases),
        "label_budget_exhausted_cases": sum(
            case.label_budget_exhausted for case in cases
        ),
        "baseline_semantic_reopens": sum(
            case.baseline_semantic_reopens for case in cases
        ),
        "optimized_semantic_reopens": sum(
            case.optimized_semantic_reopens for case in cases
        ),
        "contractions": dict(sorted(contractions.items())),
        "case_results": [asdict(case) for case in cases],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Stage4 open-carry episode contractions"
    )
    parser.add_argument(
        "--stage4-root",
        type=Path,
        default=Path("artifacts/stage4_block_flow_final"),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--episode-max-labels", type=int, default=8_192)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = report(
        args.stage4_root,
        args.data_root,
        args.episode_max_labels,
    )
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    print(json.dumps({
        key: value for key, value in payload.items()
        if key != "case_results"
    }, ensure_ascii=False, indent=2))
    return int(payload["regressed_cases"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
