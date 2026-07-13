from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import replay_validator as rv  # noqa: E402
from audit_stage4_episode_optimizer import (  # noqa: E402
    HARD_REPLAY_KINDS,
    original_case_path,
    physical_certificate,
    read_json,
    response_steps,
)
from solver_vnext import physical  # noqa: E402
from stage4_simple.optimizer import (  # noqa: E402
    BlockFlowOptimizer,
    OptimizationConfig,
)
from stage4_simple.scope import build_scope  # noqa: E402
from stage4_simple.search import Stage4Problem  # noqa: E402


@dataclass(frozen=True)
class ResolveAudit:
    case_id: str
    dataset: str
    baseline_hooks: int
    resolved_hooks: int | None
    delta_hooks: int | None
    complete: bool
    replay_clean: bool
    combined_replay_clean: bool
    evaluated_labels: int
    feasible_labels: int
    stop_reason: str
    reason: str


def hard_violations(violations: list[Any]) -> list[Any]:
    return [item for item in violations if item.kind in HARD_REPLAY_KINDS]


def replay_combined(
    original_request: dict[str, Any],
    combined_response: dict[str, Any],
    baseline_steps: int,
    operations: list[dict[str, Any]],
) -> bool:
    baseline_combined = rv.operations(combined_response)
    if len(baseline_combined) < baseline_steps:
        return False
    upstream = baseline_combined[: len(baseline_combined) - baseline_steps]
    combined: list[dict[str, Any]] = []
    for row in (*upstream, *operations):
        copied = dict(row)
        copied["Index"] = len(combined) + 1
        combined.append(copied)
    _state, violations = rv.replay(
        original_request,
        {"Data": {"Operations": combined}},
    )
    return not hard_violations(violations)


def audit_case(
    stage4_root: Path,
    data_root: Path,
    dataset: str,
    case_id: str,
    config: OptimizationConfig,
) -> ResolveAudit:
    case_root = stage4_root / dataset
    request = read_json(case_root / f"{case_id}_stage4_request.json")
    baseline_response = read_json(case_root / f"{case_id}_response.json")
    combined_response = read_json(
        case_root / f"{case_id}_combined_response.json"
    )
    original_path = original_case_path(data_root, dataset, case_id)
    _case, original_request, _cars, assignment, _loco = physical.read_case(
        original_path
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
    baseline = response_steps(baseline_response)
    result = BlockFlowOptimizer(problem, config).solve()
    if not result.plan.complete:
        return ResolveAudit(
            case_id=case_id,
            dataset=dataset,
            baseline_hooks=len(baseline),
            resolved_hooks=None,
            delta_hooks=None,
            complete=False,
            replay_clean=False,
            combined_replay_clean=False,
            evaluated_labels=result.evaluated_labels,
            feasible_labels=result.feasible_labels,
            stop_reason=result.stop_reason,
            reason=result.plan.reason,
        )

    certificate = physical_certificate(problem, result.plan.node.steps)
    operations = [] if certificate is None else [
        physical.response_operation(row)
        for row in physical.operation_rows(*certificate, 1)
    ]
    _state, violations = rv.replay(
        request,
        {"Data": {"Operations": operations}},
    )
    replay_clean = not hard_violations(violations)
    combined_clean = replay_combined(
        original_request,
        combined_response,
        len(baseline),
        operations,
    )
    hooks = result.plan.node.cost.hooks
    return ResolveAudit(
        case_id=case_id,
        dataset=dataset,
        baseline_hooks=len(baseline),
        resolved_hooks=hooks,
        delta_hooks=hooks - len(baseline),
        complete=True,
        replay_clean=replay_clean,
        combined_replay_clean=combined_clean,
        evaluated_labels=result.evaluated_labels,
        feasible_labels=result.feasible_labels,
        stop_reason=result.stop_reason,
        reason=result.plan.reason,
    )


def selected_cases(
    stage4_root: Path,
    datasets: tuple[str, ...],
    case_ids: frozenset[str],
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for dataset in datasets:
        for path in sorted((stage4_root / dataset).glob("[0-9]*_summary.json")):
            case_id = path.name.removesuffix("_summary.json")
            if case_ids and case_id not in case_ids:
                continue
            selected.append((dataset, case_id))
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-solve and validate frozen Stage4 starts",
    )
    parser.add_argument(
        "--stage4-root",
        type=Path,
        default=Path("artifacts/stage4_block_flow_final"),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--dataset", action="append", choices=("truth2", "truth3"))
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--time-budget-seconds", type=float, default=30.0)
    parser.add_argument("--max-labels", type=int, default=128)
    parser.add_argument("--max-expansions", type=int, default=30_000)
    parser.add_argument("--episode-max-labels", type=int, default=8_192)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = OptimizationConfig(
        time_budget_seconds=args.time_budget_seconds,
        max_labels=args.max_labels,
        max_expansions=args.max_expansions,
        episode_max_labels=args.episode_max_labels,
    )
    rows: list[ResolveAudit] = []
    for dataset, case_id in selected_cases(
        args.stage4_root,
        tuple(args.dataset or ("truth2", "truth3")),
        frozenset(args.case),
    ):
        row = audit_case(
            args.stage4_root,
            args.data_root,
            dataset,
            case_id,
            config,
        )
        rows.append(row)
        print(
            f"{dataset}/{case_id} complete={row.complete} "
            f"hooks={row.resolved_hooks} delta={row.delta_hooks} "
            f"stop={row.stop_reason}",
            flush=True,
        )
    failures = [
        row for row in rows
        if not row.complete
        or not row.replay_clean
        or not row.combined_replay_clean
        or (row.delta_hooks is not None and row.delta_hooks > 0)
    ]
    payload = {
        "cases": len(rows),
        "complete": sum(row.complete for row in rows),
        "regressed": sum(
            row.delta_hooks is not None and row.delta_hooks > 0
            for row in rows
        ),
        "failures": len(failures),
        "baseline_hooks": sum(row.baseline_hooks for row in rows),
        "resolved_hooks": sum(row.resolved_hooks or 0 for row in rows),
        "case_results": [asdict(row) for row in rows],
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    print(json.dumps(
        {key: value for key, value in payload.items() if key != "case_results"},
        ensure_ascii=False,
        indent=2,
    ))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
