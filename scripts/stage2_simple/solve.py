#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from stage2_simple.model import (  # noqa: E402
    Duty,
    STORE4,
    Store4Kind,
    Stage2Problem,
    UNWHEEL,
)
from stage2_simple.optimizer import OptimizationResult, Stage2Optimizer  # noqa: E402
from stage2_simple.yard import Operation, Yard  # noqa: E402


DEFAULT_TIME_BUDGET_SECONDS = 300.0
BLOCKING_REPLAY_KINDS = {"schema", "physical", "state"}


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"cannot infer case id from {path}")
    return match.group(1).upper()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_car(car: dict[str, Any]) -> dict[str, Any]:
    normalized = rv.ncar(car)
    normalized["TargetLines"] = [rv.norm(value) for value in normalized.get("TargetLines") or ()]
    return normalized


def final_loco_after_response(
    request: dict[str, Any], response: dict[str, Any]
) -> tuple[str, ...]:
    positions = {rv.norm((request.get("locoNode") or {}).get("Line"))}
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = rv.norm(row.get("Line"))
        if action == "Get":
            positions = {line}
        elif action == "Put":
            positions = rv.put_loco_positions(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            positions = {rv.WEIGH}
    result = tuple(sorted(line for line in positions if line))
    if not result:
        raise ValueError("stage1_final_loco_undefined")
    return result


class Stage2Solver:
    def __init__(
        self,
        case_id: str,
        request: dict[str, Any],
        stage1_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> None:
        self.case_id = case_id
        self.original_request = request
        self.stage1_response = stage1_response
        replayed, violations = rv.replay(request, stage1_response)
        blocking = [
            violation
            for violation in violations
            if violation.kind in BLOCKING_REPLAY_KINDS
        ]
        if blocking:
            detail = "|".join(
                f"{item.kind}:{item.code}:{item.detail}" for item in blocking[:12]
            )
            raise ValueError(f"stage1_replay_failed:{detail}")
        self.initial_cars = [
            normalize_car(car) for car in rv.final_cars(stage1_response, replayed)
        ]
        self.initial_loco = final_loco_after_response(request, stage1_response)
        self.problem = Stage2Problem(self.initial_cars)
        self.yard = Yard(self.problem, self.initial_loco)
        self.started_at = time.monotonic()
        self.deadline = self.started_at + float(time_budget_seconds)

    def solve(self) -> dict[str, Any]:
        optimizer = Stage2Optimizer(
            self.problem,
            self.yard,
            lambda: time.monotonic() >= self.deadline,
        )
        result = optimizer.solve()
        return self._result(result)

    def _result(self, result: OptimizationResult) -> dict[str, Any]:
        response = {"Data": {"Operations": self._response_operations(result.operations)}}
        combined_response = {
            "Data": {"Operations": self._combined_operations(result.operations)}
        }
        stage2_request = self._stage2_request(result.operations)

        standalone_cars, standalone_bad = rv.replay(stage2_request, response)
        combined_cars, combined_bad = rv.replay(self.original_request, combined_response)
        standalone_blocking = self._blocking_violations(standalone_bad)
        combined_blocking = self._blocking_violations(combined_bad)
        replay_state_consistent = (
            self._line_signature(standalone_cars)
            == self._line_signature(combined_cars)
        )
        model_state_consistent = (
            result.state is None
            or self._line_signature(self.yard.cars(result.state))
            == self._line_signature(standalone_cars)
        )

        decisions = result.decisions
        selected_collect = frozenset(
            no for decision in decisions for no in decision.collect
        )
        selected_unwheel = frozenset(
            no for decision in decisions for no in decision.unload
        )
        deferred_final = tuple(
            sorted(no for decision in decisions for no in decision.deferred_final)
        )
        deferred_optional = tuple(
            sorted(no for decision in decisions for no in decision.deferred_optional)
        )
        final_lines = {
            rv.car_no(car): rv.norm(car.get("Line")) for car in standalone_cars
        }
        collector = tuple(
            operation.move
            for operation in result.operations
            if operation.action == "Put" and operation.line == STORE4
        )
        final_segment = collector[-1] if collector else ()

        pending = set()
        pending.update(
            no for no in selected_collect if final_lines.get(no) != STORE4
        )
        pending.update(
            no for no in self.problem.required_unwheel_nos
            if final_lines.get(no) != UNWHEEL
        )
        pending.update(
            no for no in selected_unwheel if final_lines.get(no) != UNWHEEL
        )
        stage2_complete = (
            result.state is not None
            and not pending
            and not standalone_blocking
            and not combined_blocking
            and replay_state_consistent
            and model_state_consistent
            and self._store4_commit_rule_ok(result.operations, bool(selected_collect))
        )

        reasons = list(result.reasons)
        reasons.extend(self._violation_reasons("stage2_replay", standalone_blocking))
        reasons.extend(self._violation_reasons("combined_replay", combined_blocking))
        if not replay_state_consistent:
            reasons.append("stage2_combined_state_mismatch")
        if not model_state_consistent:
            reasons.append("stage2_model_replay_state_mismatch")
        reasons = list(dict.fromkeys(reasons))

        store4_indexes = [
            index
            for index, operation in enumerate(result.operations, start=1)
            if operation.action == "Put" and operation.line == STORE4
        ]
        debt = {
            "complete": stage2_complete,
            "pending_stage2_nos": sorted(pending),
            "deferred_store4_nos": list(deferred_final),
            "deferred_optional_unwheel_nos": list(deferred_optional),
            "new_store4_segment": list(final_segment),
            "new_store4_pattern": "".join(
                "O"
                if self.problem.store4_kind(no) is Store4Kind.OFF
                else "C"
                for no in final_segment
            ),
            "closed_door_c4_front_ok": self.problem.collector_closed_door_ok(
                final_segment
            ),
        }
        summary = {
            "case_id": self.case_id,
            "status": "complete" if stage2_complete else "partial",
            "operations": len(result.operations),
            "business_hooks": sum(
                operation.action in {"Get", "Put"}
                for operation in result.operations
            ),
            "stage2_debt": debt,
            "deferred_store4_nos": list(deferred_final),
            "paint_to_unwheel_nos": sorted(
                selected_unwheel & self.problem.optional_unwheel_nos
            ),
            "required_unwheel_nos": sorted(self.problem.required_unwheel_nos),
            "unweighed_handoff_nos": list(self.problem.unweighed_nos),
            "flex_out_positions": self._flex_positions(
                standalone_cars, deferred_final, deferred_optional
            ),
            "flex_out_in_store4": [],
            "store4_put_count": len(store4_indexes),
            "store4_put_indexes": store4_indexes,
            "store4_put_is_final": (
                not store4_indexes or store4_indexes[-1] == len(result.operations)
            ),
            "store4_put_rule_ok": self._store4_commit_rule_ok(
                result.operations, bool(selected_collect)
            ),
            "off_segment": [
                no
                for no in final_segment
                if self.problem.store4_kind(no) is Store4Kind.OFF
            ],
            "blocking_reasons": reasons,
            "replay_physical_ok": not standalone_blocking,
            "combined_replay_physical_ok": not combined_blocking,
            "replay_state_consistent": replay_state_consistent,
            "model_state_consistent": model_state_consistent,
            "replay_violations": [item.__dict__ for item in standalone_blocking[:20]],
            "combined_replay_violations": [
                item.__dict__ for item in combined_blocking[:20]
            ],
            "search_mode": "monotone_episode_label_setting",
            "optimality": "optimal_in_monotone_episode_model" if result.state else "none",
            "objective": self._objective(result.cost),
            "expansions": result.expansions,
            "episode_expansions": result.episode_expansions,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
        }
        trace = {
            "model": {
                "source_lines": list(self.problem.source_lines),
                "allow_final_deferral": self.problem.allow_final_deferral,
                "final_nos": sorted(self.problem.final_nos),
                "required_unwheel_nos": sorted(self.problem.required_unwheel_nos),
                "optional_unwheel_nos": sorted(self.problem.optional_unwheel_nos),
                "unweighed_handoff_nos": list(self.problem.unweighed_nos),
            },
            "decisions": [
                {
                    "source": decision.source,
                    "collect": list(decision.collect),
                    "unload": list(decision.unload),
                    "deferred_final": list(decision.deferred_final),
                    "deferred_optional": list(decision.deferred_optional),
                }
                for decision in decisions
            ],
            "operations": [
                {
                    "index": index,
                    "episode": operation.episode,
                    "purpose": operation.purpose,
                    "action": operation.action,
                    "line": operation.line,
                    "move": list(operation.move),
                    "train_after": list(operation.train_after),
                    "path": list(operation.path),
                }
                for index, operation in enumerate(result.operations, start=1)
            ],
        }
        return {
            "response": response,
            "combined_response": combined_response,
            "stage2_request": stage2_request,
            "summary": summary,
            "trace": trace,
        }

    @staticmethod
    def _blocking_violations(violations: Iterable[Any]) -> list[Any]:
        return [
            violation
            for violation in violations
            if violation.kind in BLOCKING_REPLAY_KINDS
        ]

    @staticmethod
    def _violation_reasons(prefix: str, violations: Iterable[Any]) -> list[str]:
        return [
            f"{prefix}:{item.kind}:{item.code}:{item.detail}"
            for item in list(violations)[:12]
        ]

    @staticmethod
    def _line_signature(cars: Iterable[dict[str, Any]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        by_line: dict[str, list[dict[str, Any]]] = {}
        for car in cars:
            line = rv.norm(car.get("Line"))
            if line:
                by_line.setdefault(line, []).append(car)
        return tuple(
            sorted(
                (
                    line,
                    tuple(
                        rv.car_no(car)
                        for car in sorted(
                            rows,
                            key=lambda item: (
                                int(item.get("Position") or 0),
                                rv.car_no(item),
                            ),
                        )
                    ),
                )
                for line, rows in by_line.items()
            )
        )

    @staticmethod
    def _store4_commit_rule_ok(
        operations: tuple[Operation, ...], collector_required: bool
    ) -> bool:
        indexes = [
            index
            for index, operation in enumerate(operations, start=1)
            if operation.action == "Put" and operation.line == STORE4
        ]
        if not collector_required:
            return not indexes
        return len(indexes) == 1 and indexes[0] == len(operations)

    @staticmethod
    def _objective(
        cost: tuple[int, int, int, int, int, int, int] | None,
    ) -> dict[str, int] | None:
        if cost is None:
            return None
        names = (
            "deferred_final_count",
            "deferred_optional_count",
            "operation_count",
            "restored_defer_count",
            "lian7_count",
            "route_node_count",
            "off_fragment_count",
        )
        return dict(zip(names, cost))

    def _flex_positions(
        self,
        cars: list[dict[str, Any]],
        deferred_final: tuple[str, ...],
        deferred_optional: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        flex = set(deferred_final) | set(deferred_optional) | {
            no
            for no, task in self.problem.tasks.items()
            if task.duty is Duty.DEFER
        }
        by_no = {rv.car_no(car): car for car in cars}
        return [
            {
                "car_no": no,
                "line": rv.norm(by_no[no].get("Line")),
                "position": int(by_no[no].get("Position") or 0),
                "target_lines": list(self.problem.meta[no].get("TargetLines") or ()),
            }
            for no in sorted(flex)
            if no in by_no
        ]

    def _response_operations(
        self, operations: tuple[Operation, ...], *, start_index: int = 1
    ) -> list[dict[str, Any]]:
        return [
            {
                "Index": start_index + offset,
                "Line": operation.line,
                "Action": operation.action,
                "MoveCars": list(operation.move),
                "TrainCars": list(operation.train_after),
                "PassbyPath": list(operation.path),
            }
            for offset, operation in enumerate(operations)
        ]

    def _combined_operations(
        self, operations: tuple[Operation, ...]
    ) -> list[dict[str, Any]]:
        stage1 = [dict(row) for row in rv.operations(self.stage1_response)]
        return [
            *stage1,
            *self._response_operations(operations, start_index=len(stage1) + 1),
        ]

    def _stage2_request(
        self, operations: tuple[Operation, ...]
    ) -> dict[str, Any]:
        request = dict(self.original_request)
        loco_line = self.initial_loco[0]
        if operations and operations[0].path:
            loco_line = operations[0].path[0]
        request["StartStatus"] = [
            self._output_car(car)
            for car in sorted(
                self.initial_cars,
                key=lambda item: (
                    rv.norm(item.get("Line")),
                    int(item.get("Position") or 0),
                    rv.car_no(item),
                ),
            )
        ]
        request["locoNode"] = {"Line": loco_line, "End": "North"}
        return request

    @staticmethod
    def _output_car(car: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in car.items()
            if not key.startswith("_") or key == "_Weighed"
        }


def case_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"input path does not exist: {input_path}")
    files = sorted(input_path.glob("validation_*.json"))
    if not files:
        raise ValueError(
            f"input directory has no validation_*.json files: {input_path}"
        )
    return files


def diagnostic_summary(
    case_id: str,
    reason: str,
    *,
    status: str = "error",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "operations": 0,
        "business_hooks": 0,
        "stage2_debt": {"complete": False, "pending_stage2_nos": []},
        "blocking_reasons": [reason],
        "replay_physical_ok": False,
        "combined_replay_physical_ok": False,
        "search_mode": "monotone_episode_label_setting",
        "expansions": 0,
        "episode_expansions": 0,
        "elapsed_seconds": 0.0,
    }


def solve_one(
    path: Path,
    stage1_out: Path,
    out_dir: Path,
    *,
    time_budget_seconds: float,
) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    response_path = stage1_out / f"{case_id}_response.json"
    summary_path = stage1_out / f"{case_id}_summary.json"
    missing = [str(path) for path in (response_path, summary_path) if not path.exists()]
    if missing:
        summary = diagnostic_summary(
            case_id,
            "stage1_artifact_missing:" + ",".join(missing),
            status="unavailable",
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    stage1_summary = read_json(summary_path)
    if stage1_summary.get("status") != "complete":
        summary = diagnostic_summary(
            case_id,
            f"stage1_not_complete:{stage1_summary.get('status')}",
            status="unavailable",
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary

    result = Stage2Solver(
        case_id,
        read_json(path),
        read_json(response_path),
        time_budget_seconds=time_budget_seconds,
    ).solve()
    write_json(out_dir / f"{case_id}_stage2_request.json", result["stage2_request"])
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    return result["summary"]


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in summaries if item.get("status") == "complete"]
    partial = [item for item in summaries if item.get("status") == "partial"]
    unavailable = [item for item in summaries if item.get("status") == "unavailable"]
    errors = [item for item in summaries if item.get("status") == "error"]
    operation_counts = [int(item.get("operations") or 0) for item in completed]
    reasons = Counter(
        str(reason).split(":", 1)[0]
        for item in summaries
        if item.get("status") != "complete"
        for reason in item.get("blocking_reasons") or ()
    )
    return {
        "cases": len(summaries),
        "complete": len(completed),
        "partial": len(partial),
        "unavailable": len(unavailable),
        "error": len(errors),
        "avg_operations_complete": (
            round(sum(operation_counts) / len(operation_counts), 3)
            if operation_counts
            else 0
        ),
        "max_operations_complete": max(operation_counts, default=0),
        "operation_distribution": dict(Counter(operation_counts)),
        "total_elapsed_seconds": round(
            sum(float(item.get("elapsed_seconds") or 0.0) for item in summaries), 3
        ),
        "partial_reasons": dict(reasons.most_common()),
        "summaries": summaries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 2 monotone episode solver")
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--stage1-out",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--time-budget-seconds",
        type=float,
        default=DEFAULT_TIME_BUDGET_SECONDS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in case_files(args.input):
        case_id = case_id_from_path(path)
        print(case_id, flush=True)
        try:
            summaries.append(
                solve_one(
                    path,
                    args.stage1_out,
                    args.out,
                    time_budget_seconds=args.time_budget_seconds,
                )
            )
        except Exception as exc:
            summary = diagnostic_summary(
                case_id,
                f"solver_exception:{type(exc).__name__}:{exc}",
            )
            summaries.append(summary)
            write_json(args.out / f"{case_id}_summary.json", summary)
    result = aggregate(summaries)
    write_json(args.out / "aggregate_summary.json", result)
    print(
        f"done cases={result['cases']} complete={result['complete']} "
        f"partial={result['partial']} unavailable={result['unavailable']} "
        f"error={result['error']} avg_ops={result['avg_operations_complete']} "
        f"max_ops={result['max_operations_complete']}",
        flush=True,
    )
    return 0 if result["complete"] == result["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
