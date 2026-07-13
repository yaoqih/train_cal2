#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from solver_vnext import physical  # noqa: E402
from stage4_simple.optimizer import (  # noqa: E402
    BlockFlowOptimizer,
    OptimizationConfig,
)
from stage4_simple.contracts import classify_depot_rehook  # noqa: E402
from stage4_simple.scope import build_scope  # noqa: E402
from stage4_simple.search import Stage4Problem  # noqa: E402


DEFAULT_TIME_BUDGET_SECONDS = 300.0
DEFAULT_MAX_LABELS = 128
DEFAULT_MAX_EXPANSIONS = 30_000
HARD_REPLAY_KINDS = {"schema", "physical", "business", "state"}
MOVE_MODEL = (
    "dynamic_car_blocks_with_target_rank_ledger",
    "and_or_access_dependencies",
    "monotone_owned_staging_stacks",
    "semantic_open_sealed_target_windows",
    "retained_consist_cross_source_digestion",
    "paired_event_open_carry_projection",
    "bounded_skeleton_shortest_path",
    "topology_resource_leases_with_exit_certificates",
    "admissible_source_target_weigh_bound",
    "ordered_block_flow_label_estimate",
    "incremental_get_put_weigh_physics",
    "single_budget_block_flow_label_setting",
    "independent_full_planlet_and_replay_validation",
)


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"cannot infer case id from {path}")
    return match.group(1).upper()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clone_car(car: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in car.items():
        if isinstance(value, (list, tuple, set, dict)):
            copied[key] = value.copy() if hasattr(value, "copy") else tuple(value)
        else:
            copied[key] = value
    return copied


def normalize_car(car: dict[str, Any]) -> dict[str, Any]:
    return physical.normalized_car(rv.ncar(car))


def output_car(car: dict[str, Any]) -> dict[str, Any]:
    output = {
        key: value
        for key, value in car.items()
        if not key.startswith("_") or key == "_Weighed"
    }
    output["No"] = physical.car_no(car)
    output["Line"] = car.get("Line") or ""
    output["Position"] = int(car.get("Position") or 0)
    return output


def final_loco_after_response(request: dict[str, Any], response: dict[str, Any]) -> str:
    current = physical.LocoLocation(
        physical.normalize_line((request.get("locoNode") or {}).get("Line"))
    )
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = physical.normalize_line(row.get("Line"))
        if action == "Get":
            current = physical.LocoLocation(line)
        elif action == "Put":
            current = physical.post_put_loco_location(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            current = physical.LocoLocation(physical.WEIGH_LINE)
    return current.line or physical.WEIGH_LINE


def apply_generated_end_status(response: dict[str, Any], cars: list[dict[str, Any]]) -> None:
    by_no = {physical.car_no(car): car for car in cars}
    for row in rv.generated(response):
        car = by_no.get(str(row.get("No") or ""))
        if car is None:
            continue
        car["Line"] = physical.normalize_line(row.get("Line"))
        car["Position"] = int(row.get("Position") or 0)
        if "_Weighed" in row:
            car["_Weighed"] = bool(row.get("_Weighed"))


class Stage4Solver:
    def __init__(
        self,
        case_id: str,
        original_request: dict[str, Any],
        depot_assignment: physical.DepotAssignment,
        stage3_request: dict[str, Any],
        stage3_response: dict[str, Any],
        stage3_combined_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
        max_labels: int = DEFAULT_MAX_LABELS,
        max_expansions: int = DEFAULT_MAX_EXPANSIONS,
    ) -> None:
        self.case_id = case_id
        self.original_request = original_request
        self.depot_assignment = depot_assignment
        self.stage3_request = stage3_request
        self.stage3_response = stage3_response
        self.stage3_combined_response = stage3_combined_response
        self.time_budget_seconds = time_budget_seconds
        self.max_labels = max_labels
        self.max_expansions = max_expansions

        replayed, violations = rv.replay(stage3_request, stage3_response)
        hard = [item for item in violations if item.kind in {"schema", "physical"}]
        if hard:
            detail = ";".join(
                f"{item.kind}:{item.code}:{item.detail}" for item in hard[:8]
            )
            raise ValueError(f"stage3_replay_failed:{detail}")
        self.cars = [normalize_car(car) for car in rv.final_cars(stage3_response, replayed)]
        apply_generated_end_status(stage3_response, self.cars)
        self.initial_cars = [clone_car(car) for car in self.cars]
        self.initial_loco = physical.LocoLocation(
            final_loco_after_response(stage3_request, stage3_response)
        )
        self.scope = build_scope(self.cars, depot_assignment)
        self.problem = Stage4Problem(
            case_id=case_id,
            cars=self.cars,
            loco_location=self.initial_loco,
            depot_assignment=depot_assignment,
            target_by_no=self.scope.target_by_no,
            active_nos=self.scope.active_nos,
            protected_nos=self.scope.protected_nos,
            capacity_holdout_nos=self.scope.infeasible_nos,
        )

    def solve(self) -> dict[str, Any]:
        started = time.monotonic()
        optimization = BlockFlowOptimizer(
            self.problem,
            OptimizationConfig(
                time_budget_seconds=self.time_budget_seconds,
                max_labels=self.max_labels,
                max_expansions=self.max_expansions,
            ),
        ).solve()
        node = optimization.plan.node
        candidate, validation = self.physical_certificate(node.steps)
        operations = (
            [
                physical.response_operation(row)
                for row in physical.operation_rows(candidate, validation, 1)
            ]
            if candidate is not None
            else []
        )
        return self.build_result(
            operations=operations,
            node=node,
            optimization=optimization,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def physical_certificate(
        self,
        steps: tuple[physical.PlanStep, ...],
    ) -> tuple[physical.HookCandidate | None, physical.PhysicalValidation | None]:
        if not steps:
            return None, None
        touched = set(
            no
            for step in steps
            for no in step.move_car_nos
        )
        by_no = {physical.car_no(car): car for car in self.initial_cars}
        source = next(
            (step.line for step in steps if step.action == "Get"),
            self.initial_loco.line,
        )
        target = next(
            (step.line for step in reversed(steps) if step.action == "Put"),
            source,
        )
        candidate = physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=1,
            source_line=source,
            target_line=target,
            batch=[by_no[no] for no in sorted(touched)],
            steps=steps,
            reason="joint_flow_decision_certificate",
            candidate_kind="blocker_relocation",
        )
        validation = physical.validate_planlet(
            self.problem.graph,
            candidate,
            self.initial_cars,
            self.initial_loco,
            self.depot_assignment,
        )
        if not validation.accepted:
            raise RuntimeError(
                "final_physical_certificate_failed:" + "|".join(validation.reasons)
            )
        return candidate, validation

    def stage4_request_payload(self) -> dict[str, Any]:
        return {
            "StartStatus": [output_car(car) for car in self.initial_cars],
            "TerminalLines": (
                self.original_request.get("TerminalLines")
                or self.stage3_request.get("TerminalLines")
                or []
            ),
            "locoNode": {"Line": self.initial_loco.line, "End": "North"},
        }

    def build_result(
        self,
        *,
        operations: list[dict[str, Any]],
        node: Any,
        optimization: Any,
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        stage4_request = self.stage4_request_payload()
        response = {"Data": {"Operations": operations}}
        replayed, violations = rv.replay(stage4_request, response)
        hard = [item for item in violations if item.kind in HARD_REPLAY_KINDS]
        final_cars = [normalize_car(car) for car in rv.final_cars(response, replayed)]
        response["Data"]["GeneratedEndStatus"] = sorted_output_cars(final_cars)

        combined = {"Data": {"Operations": self.combined_operations(response)}}
        combined_replayed, _ = rv.replay(self.original_request, combined)
        combined_final = [
            normalize_car(car)
            for car in rv.final_cars(combined, combined_replayed)
        ]
        combined["Data"]["GeneratedEndStatus"] = sorted_output_cars(combined_final)
        _combined_replayed, combined_violations = rv.replay(
            self.original_request,
            combined,
        )
        combined_hard = [
            item for item in combined_violations if item.kind in HARD_REPLAY_KINDS
        ]

        final_unsatisfied = physical.unsatisfied_cars(final_cars, self.depot_assignment)
        final_unsatisfied_nos = {
            physical.car_no(car) for car in final_unsatisfied
        }
        active_unsatisfied = final_unsatisfied_nos & set(self.scope.active_nos)
        protected_damage = final_unsatisfied_nos & set(self.scope.protected_nos)
        infeasible_unsatisfied = final_unsatisfied_nos & set(self.scope.infeasible_nos)
        actionable_complete = not active_unsatisfied and not protected_damage
        complete = (
            actionable_complete
            and not final_unsatisfied
            and not hard
            and not combined_hard
        )
        reasons: list[str] = []
        if active_unsatisfied:
            reasons.append(f"active_unsatisfied:{len(active_unsatisfied)}")
        if infeasible_unsatisfied:
            reasons.append(f"hard_capacity_infeasible:{len(infeasible_unsatisfied)}")
        if protected_damage:
            reasons.append(f"protected_damage:{len(protected_damage)}")
        if final_unsatisfied_nos & set(self.scope.out_of_scope_nos):
            reasons.append(
                "out_of_scope_unsatisfied:"
                f"{len(final_unsatisfied_nos & set(self.scope.out_of_scope_nos))}"
            )
        if not optimization.plan.complete:
            reasons.append(optimization.plan.reason)
        reasons.extend(
            f"replay_{item.kind}:{item.code}:{item.detail}" for item in hard[:10]
        )
        reasons.extend(
            f"combined_replay_{item.kind}:{item.code}:{item.detail}"
            for item in combined_hard[:10]
        )
        if final_unsatisfied:
            reasons.append(f"final_unsatisfied:{len(final_unsatisfied)}")

        initial_state = physical.initial_planlet_state(
            self.initial_cars,
            self.initial_loco,
        )
        lower_bound = self.problem.hook_lower_bound(initial_state)
        diagnostics = self.problem.diagnostics(initial_state)
        hooks = len(operations)
        if hooks != node.cost.hooks:
            raise RuntimeError(
                f"operation_cost_mismatch:{hooks}!={node.cost.hooks}"
            )
        summary = {
            "case_id": self.case_id,
            "status": "complete" if complete else "partial",
            "stage4_strategy": "open_carry_episode_block_flow",
            "depot_rehook_mode": classify_depot_rehook(self.problem).mode.value,
            "stage3_final_loco": self.initial_loco.line,
            "stage4_start_loco": self.initial_loco.line,
            "operations": hooks,
            "business_hooks": hooks,
            "hook_count_definition": "operation_rows_get_put_weigh",
            "objective": [
                node.cost.hooks,
                node.cost.repeated_gets,
                node.cost.target_reopens,
                node.cost.route_cost,
            ],
            "hook_lower_bound": lower_bound,
            "hook_optimality_gap": max(0, hooks - lower_bound),
            "episode_contractions": sum(
                row.get("event") == "episode_contraction"
                for row in optimization.plan.trace
            ),
            "episode_saved_hooks": sum(
                int(row.get("removed_hooks") or 0)
                for row in optimization.plan.trace
                if row.get("event") == "episode_contraction"
            ),
            "flow_relaxation": {
                "block_count": len(diagnostics.blocks),
                "access_alternative_count": len(diagnostics.alternatives),
                "relaxed_hook_estimate": diagnostics.relaxed_hook_estimate,
                "target_windows": [
                    {
                        "line": window.line,
                        "inbound_count": len(window.inbound),
                        "outbound_count": len(window.outbound),
                        "source_run_count": len(window.source_runs),
                        "source_inversions": window.source_inversions,
                        "capacity_rounds": window.capacity_rounds,
                        "estimated_hooks": window.estimated_hooks,
                    }
                    for window in diagnostics.windows
                ],
            },
            "active_count": len(self.scope.active_nos),
            "active_nos": sorted(self.scope.active_nos),
            "infeasible_count": len(self.scope.infeasible_nos),
            "infeasible_nos": sorted(self.scope.infeasible_nos),
            "infeasible_lines": sorted(self.scope.infeasible_lines),
            "capacity_overflow_m_by_line": {
                line: round(value, 3)
                for line, value in sorted(self.scope.capacity_overflow_by_line.items())
            },
            "capacity_minimum_holdout_by_line": dict(
                sorted(self.scope.capacity_holdout_count_by_line.items())
            ),
            "out_of_scope_count": len(self.scope.out_of_scope_nos),
            "out_of_scope_nos": sorted(self.scope.out_of_scope_nos),
            "excluded_line_count": len(self.scope.excluded_source_nos),
            "stage4_debt": {
                "complete": complete,
                "actionable_complete": actionable_complete,
                "active_unsatisfied_count": len(active_unsatisfied),
                "active_unsatisfied_nos": sorted(active_unsatisfied),
                "infeasible_unsatisfied_count": len(infeasible_unsatisfied),
                "infeasible_unsatisfied_nos": sorted(infeasible_unsatisfied),
                "protected_damage_nos": sorted(protected_damage),
                "out_of_scope_unsatisfied_nos": sorted(
                    final_unsatisfied_nos & set(self.scope.out_of_scope_nos)
                ),
            },
            "optimality": "feasible_unproved" if actionable_complete else "not_proved",
            "move_model": list(MOVE_MODEL),
            "blocking_reasons": reasons,
            "replay_physical_ok": not hard,
            "replay_violations": [item.__dict__ for item in hard[:20]],
            "combined_replay_physical_ok": not combined_hard,
            "combined_replay_violations": [
                item.__dict__ for item in combined_hard[:20]
            ],
            "final_unsatisfied_count": len(final_unsatisfied),
            "final_unsatisfied_nos": sorted(final_unsatisfied_nos),
            "evaluated_labels": optimization.evaluated_labels,
            "feasible_labels": optimization.feasible_labels,
            "unrecovered_lease_count": len(optimization.plan.leases),
            "search_stop_reason": optimization.stop_reason,
            "elapsed_seconds": elapsed_seconds,
        }
        trace = [
            {
                "event": "search_summary",
                "evaluated_labels": optimization.evaluated_labels,
                "feasible_labels": optimization.feasible_labels,
                "stop_reason": optimization.stop_reason,
            },
            *optimization.plan.trace,
        ]
        return {
            "response": response,
            "combined_response": combined,
            "stage4_request": stage4_request,
            "summary": summary,
            "trace": trace,
        }

    def combined_operations(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        for source in (
            rv.operations(self.stage3_combined_response),
            rv.operations(response),
        ):
            for row in source:
                copied = dict(row)
                copied["Index"] = len(combined) + 1
                combined.append(copied)
        return combined


def sorted_output_cars(cars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        output_car(car)
        for car in sorted(
            cars,
            key=lambda item: (
                item.get("Line") or "",
                int(item.get("Position") or 0),
                physical.car_no(item),
            ),
        )
    ]


def solve_one(
    path: Path,
    stage3_out: Path,
    out_dir: Path,
    *,
    time_budget_seconds: float,
    max_labels: int,
    max_expansions: int,
) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    files = {
        "summary": stage3_out / f"{case_id}_summary.json",
        "request": stage3_out / f"{case_id}_stage3_request.json",
        "response": stage3_out / f"{case_id}_response.json",
        "combined": stage3_out / f"{case_id}_combined_response.json",
    }
    missing = [str(file) for file in files.values() if not file.exists()]
    if missing:
        summary = diagnostic_summary(
            case_id,
            "stage3_artifact_missing:" + ",".join(missing),
            status="unavailable",
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    stage3_summary = read_json(files["summary"])
    if stage3_summary.get("status") != "complete":
        summary = diagnostic_summary(
            case_id,
            f"stage3_not_complete:{stage3_summary.get('status')}",
            status="unavailable",
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    _case_id, request, _cars, assignment, _loco = physical.read_case(path)
    result = Stage4Solver(
        case_id,
        request,
        assignment,
        read_json(files["request"]),
        read_json(files["response"]),
        read_json(files["combined"]),
        time_budget_seconds=time_budget_seconds,
        max_labels=max_labels,
        max_expansions=max_expansions,
    ).solve()
    write_case_outputs(out_dir, case_id, result)
    return result["summary"]


def write_case_outputs(
    out_dir: Path,
    case_id: str,
    result: dict[str, Any],
) -> None:
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_stage4_request.json", result["stage4_request"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])


def diagnostic_summary(
    case_id: str,
    reason: str,
    *,
    status: str = "error",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "stage4_strategy": "open_carry_episode_block_flow",
        "operations": 0,
        "business_hooks": 0,
        "optimality": "not_proved",
        "blocking_reasons": [reason],
        "replay_physical_ok": False,
        "combined_replay_physical_ok": False,
        "final_unsatisfied_count": None,
        "final_unsatisfied_nos": [],
        "stage4_debt": {"evaluated": False, "actionable_complete": False},
        "evaluated_labels": 0,
        "elapsed_seconds": 0.0,
    }


def case_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"input path does not exist: {path}")
    files = sorted(path.glob("validation_*.json"))
    if not files:
        raise ValueError(f"input directory has no validation_*.json files: {path}")
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage4 joint-flow decision optimizer."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--stage3-out", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--time-budget-seconds",
        type=float,
        default=DEFAULT_TIME_BUDGET_SECONDS,
    )
    parser.add_argument("--max-labels", type=int, default=DEFAULT_MAX_LABELS)
    parser.add_argument(
        "--max-expansions",
        type=int,
        default=DEFAULT_MAX_EXPANSIONS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = case_files(args.input)
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in files:
        case_id = case_id_from_path(path)
        try:
            summary = solve_one(
                path,
                args.stage3_out,
                args.out,
                time_budget_seconds=args.time_budget_seconds,
                max_labels=args.max_labels,
                max_expansions=args.max_expansions,
            )
        except Exception as exc:
            summary = diagnostic_summary(
                case_id,
                f"solver_exception:{type(exc).__name__}:{exc}",
            )
            write_json(args.out / f"{case_id}_summary.json", summary)
        summaries.append(summary)
        if args.input.is_dir():
            print(
                f"{case_id} {summary.get('status')} "
                f"hooks={summary.get('business_hooks', 0)} "
                f"unsat={summary.get('final_unsatisfied_count', 0)}",
                flush=True,
            )
    complete = [item for item in summaries if item.get("status") == "complete"]
    actionable = [
        item
        for item in summaries
        if item.get("status") == "complete"
        or bool((item.get("stage4_debt") or {}).get("actionable_complete"))
    ]
    capacity_limited = [
        item
        for item in actionable
        if int((item.get("stage4_debt") or {}).get("infeasible_unsatisfied_count") or 0)
    ]
    upstream_residual = [
        item
        for item in actionable
        if item.get("status") != "complete" and item not in capacity_limited
    ]
    active_residual = [
        item
        for item in summaries
        if item.get("status") == "partial" and item not in actionable
    ]
    unavailable = [item for item in summaries if item.get("status") == "unavailable"]
    errors = [item for item in summaries if item.get("status") == "error"]
    aggregate = {
        "cases": len(summaries),
        "complete": len(complete),
        "actionable_complete": len(actionable),
        "capacity_limited": len(capacity_limited),
        "upstream_residual": len(upstream_residual),
        "active_residual": len(active_residual),
        "unavailable": len(unavailable),
        "error": len(errors),
        "avg_business_hooks_complete": round(
            sum(int(item.get("business_hooks") or 0) for item in complete)
            / max(1, len(complete)),
            3,
        ),
        "avg_business_hooks_actionable": round(
            sum(int(item.get("business_hooks") or 0) for item in actionable)
            / max(1, len(actionable)),
            3,
        ),
        "partial_reasons": dict(Counter(
            str(reason).split(":", 1)[0]
            for item in summaries
            if item.get("status") != "complete"
            for reason in item.get("blocking_reasons", [])[:3]
        )),
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps(
        {key: value for key, value in aggregate.items() if key != "summaries"},
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if len(complete) == len(summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
