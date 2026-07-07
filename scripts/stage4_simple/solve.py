#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv
from solver_vnext import physical


CAR_LENGTH_DEFAULT_M = 14.3
INF_COST = 10**9
DEFAULT_TIME_BUDGET_SECONDS = 300.0
MAX_EXPANSIONS = 500_000
MAX_GREEDY_STEPS = 180
MAX_TARGET_GROUP_GETS = 3
MAX_TARGET_GROUP_BRANCHES = 36
MAX_SPOTTING_REPACK_STEPS = 50
MACRO_BEAM_WIDTH = 8
MACRO_BEAM_BRANCHES = 10
MACRO_BEAM_DEPTH = 42
MACRO_BEAM_LOW_DEBT_LIMIT = 8
MACRO_BEAM_LOW_DEBT_STATES = 5
MACRO_CANDIDATE_POOL = 80
STORE4 = "存4线"
WEIGH_LINE = physical.WEIGH_LINE
# Stage 4 may route through running lines such as 联7.  The move model only
# excludes depot-related tracks as operation/cache lines.
BLOCKED_LINES: set[str] = set()
OUT_OF_SCOPE_STRATEGY_LINES = physical.DEPOT_INBOUND_DESTINATION_LINES
RUNNING_LINES = physical.RUNNING_LINES
HOT_THROATS = {"渡10"}
ROUTE_LENGTH_SIGNATURE_LINES = {
    line
    for _triplet, blockers in physical.REVERSAL_RULES_WITH_BLOCKER_LENGTH
    for line, _limit in blockers
}
CACHE_LINES = (
    "存1线",
    "存2线",
    "存3线",
    "洗罐站",
    "油漆线",
    "抛丸线",
    "机库线",
)
HIGH_CONFLICT_CACHE_LINES = {
    "机北1",
    "机北2",
    "机南",
    "洗油北",
    "存5线北",
    "存5线南",
    "调梁线北",
    "调梁棚",
    "预修线",
    "洗罐线北",
    "机走棚",
    "机走北",
}
GATE_INNER_TARGET_PAIRS = (
    ("调梁线北", "调梁棚"),
    ("洗罐线北", "洗罐站"),
    ("洗油北", "油漆线"),
)
SERVICE_PRIORITY_EDGES = (
    ("调梁线北", "调梁棚"),
    ("存5线北", "调梁棚"),
    ("存5线北", "存5线南"),
    ("存2线", "预修线"),
    ("存3线", "预修线"),
    ("存1线", "调梁棚"),
    ("机走棚", "洗油北"),
    ("机走棚", "油漆线"),
    ("机走北", "洗油北"),
    ("机走北", "油漆线"),
    ("机走棚", "抛丸线"),
    ("机走北", "抛丸线"),
    ("预修线", "抛丸线"),
)
SERVICE_TIE_BREAK = (
    "存2线",
    "存3线",
    "存5线北",
    "存1线",
    "调梁线北",
    "油漆线",
    "洗罐站",
    "抛丸线",
    "预修线",
    "调梁棚",
)
SWEEP_SOURCE_LINES = (
    "存5线北",
    "存5线南",
    "存2线",
    "存3线",
    "存1线",
    "预修线",
    "调梁线北",
    "洗罐线北",
    "洗罐站",
    "油漆线",
)
LEAF_CACHE_LINES = (
    "存1线",
    "存2线",
    "存3线",
    "油漆线",
    "洗罐站",
    "抛丸线",
    "机库线",
)
MOVE_MODEL_RESTRICTIONS = (
    "out_of_scope_and_unmanaged_fixed_cars_are_not_moved",
    "managed_repair_cars_may_move_but_must_finish_satisfied",
    "unmanaged_fixed_cars_keep_relative_order_and_are_repacked_behind_managed_cars",
    "cache_lines_are_whitelisted",
    "get_cuts_are_segment_boundaries_plus_single_car_plus_weigh_boundaries",
    "depot_related_lines_are_not_strategy_candidates",
    "depot_related_lines_are_excluded_from_get_put_cache_candidates",
    "cache_puts_are_generated_for_all_suffixes",
    "states_that_damage_unmanaged_protected_satisfied_cars_are_rejected",
    "store4_closed_door_front_positions_are_checked_at_completion",
    "gate_inner_target_macro_may_temporarily_cache_gate_target_cars",
    "service_sweep_uses_target_topology_and_leaf_cache_lines",
)


@dataclass(frozen=True)
class State:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    positions: tuple[tuple[str, int], ...]
    held: tuple[str, ...]
    loco: tuple[str, ...]
    weighed: tuple[str, ...]


@dataclass(frozen=True)
class Op:
    action: str
    line: str
    move: tuple[str, ...]
    path: tuple[str, ...]
    train_after: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class MacroCandidate:
    ops: tuple[Op, ...]
    state: State
    score: tuple[Any, ...]
    reason: str


@dataclass(frozen=True)
class SearchResult:
    status: str
    state: State | None
    ops: tuple[Op, ...]
    cost: tuple[int, int, int, int]
    reasons: tuple[str, ...]
    optimality: str
    expansions: int
    elapsed_seconds: float
    queue_bound: tuple[int, int, int, int] | None = None


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
    return rv.ncar(car)


def compact_positions_by_line(cars: list[dict[str, Any]]) -> None:
    by_line: dict[str, list[dict[str, Any]]] = {}
    for car in cars:
        line = rv.norm(car.get("Line"))
        if not line:
            car["Line"] = ""
            car["Position"] = 0
            continue
        car["Line"] = line
        by_line.setdefault(line, []).append(car)
    for line_cars in by_line.values():
        line_cars.sort(key=lambda item: (int(item.get("Position") or 0), rv.car_no(item)))
        for position, car in enumerate(line_cars, start=1):
            car["Position"] = position


def final_loco_after_response(request: dict[str, Any], response: dict[str, Any]) -> tuple[str, ...]:
    loco = {rv.norm((request.get("locoNode") or {}).get("Line"))}
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = rv.norm(row.get("Line"))
        if action == "Get":
            loco = {line}
        elif action == "Put":
            loco = rv.put_loco_positions(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            loco = {WEIGH_LINE}
    lines = tuple(sorted(line for line in loco if line))
    return lines or (WEIGH_LINE,)


class Stage4Solver:
    def __init__(
        self,
        case_id: str,
        request: dict[str, Any],
        depot_assignment: physical.DepotAssignment,
        stage3_request: dict[str, Any],
        stage3_response: dict[str, Any],
        stage3_combined_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
        max_expansions: int = MAX_EXPANSIONS,
    ) -> None:
        self.case_id = case_id
        self.original_request = request
        self.depot_assignment = depot_assignment
        self.stage3_request = stage3_request
        self.stage3_response = stage3_response
        self.stage3_combined_response = stage3_combined_response
        replayed, replay_bad = rv.replay(stage3_request, stage3_response)
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical"}]
        if hard_bad:
            detail = ";".join(f"{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:5])
            raise ValueError(f"stage3_replay_failed:{detail}")
        self.initial_cars = [normalize_car(car) for car in rv.final_cars(stage3_response, replayed)]
        self.apply_generated_end_status(stage3_response, self.initial_cars)
        compact_positions_by_line(self.initial_cars)
        self.stage3_final_loco = final_loco_after_response(stage3_request, stage3_response)
        self.initial_loco = self.stage3_final_loco
        self.meta = {rv.car_no(car): dict(car) for car in self.initial_cars}
        self.graph = physical.TrackGraph()
        self.started_at = time.monotonic()
        self.time_budget_seconds = time_budget_seconds
        self.deadline = self.started_at + time_budget_seconds
        self.max_expansions = max_expansions
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}
        self.target_by_no: dict[str, str] = {}
        self.target_reason_by_no: dict[str, str] = {}
        self.active_nos: set[str] = set()
        self.repair_nos: set[str] = set()
        self.all_repair_nos: set[str] = set()
        self.managed_nos: set[str] = set()
        self.out_of_scope_nos: set[str] = set()
        self.initialize_debt()
        self.initial_unsatisfied_nos = {
            rv.car_no(car)
            for car in physical.unsatisfied_cars(self.initial_cars, self.depot_assignment)
        }
        self.protected_satisfied_nos = {
            rv.car_no(car)
            for car in self.initial_cars
            if rv.car_no(car) not in self.initial_unsatisfied_nos
        }
        self.initialize_repair_cars()
        self.lower_bound_initial = 0
        self.cars_cache: dict[tuple[Any, ...], tuple[dict[str, Any], ...]] = {}
        self.unsatisfied_cache: dict[tuple[Any, ...], frozenset[str]] = {}
        self.lower_bound_cache: dict[tuple[Any, ...], int] = {}
        self.line_map_cache: dict[tuple[tuple[str, tuple[str, ...]], ...], dict[str, tuple[str, ...]]] = {}
        self.target_ready_cache: dict[tuple[tuple[Any, ...], str], bool] = {}
        self.route_cleanup_cache: dict[tuple[Any, ...], frozenset[str]] = {}
        self.configure_managed(include_repair=True)

    def apply_generated_end_status(self, response: dict[str, Any], cars: list[dict[str, Any]]) -> None:
        by_no = {rv.car_no(car): car for car in cars}
        for row in rv.generated(response):
            no = str(row.get("No") or "")
            car = by_no.get(no)
            if car is None:
                continue
            car["Line"] = rv.norm(row.get("Line"))
            car["Position"] = int(row.get("Position") or 0)

    def initialize_debt(self) -> None:
        loads = physical.line_loads(self.initial_cars)
        for car in physical.unsatisfied_cars(self.initial_cars, self.depot_assignment):
            no = rv.car_no(car)
            target, _pos, reason = physical.planned_target_for_car(
                car,
                self.initial_cars,
                self.depot_assignment,
                loads,
            )
            if target in OUT_OF_SCOPE_STRATEGY_LINES or car["Line"] in OUT_OF_SCOPE_STRATEGY_LINES:
                self.out_of_scope_nos.add(no)
                continue
            if not target:
                self.out_of_scope_nos.add(no)
                continue
            self.active_nos.add(no)
            self.target_by_no[no] = target
            self.target_reason_by_no[no] = reason

    def initialize_repair_cars(self) -> None:
        candidate_nos = self.initial_repair_candidate_nos()
        for car in self.initial_cars:
            no = rv.car_no(car)
            if no not in candidate_nos:
                continue
            if no in self.active_nos or no in self.out_of_scope_nos:
                continue
            if no not in self.protected_satisfied_nos:
                continue
            if not self.repair_candidate_allowed(car):
                continue
            self.all_repair_nos.add(no)
            self.target_by_no.setdefault(no, car["Line"])
            self.target_reason_by_no.setdefault(no, "repair_restore")

    def configure_managed(self, *, include_repair: bool) -> None:
        self.repair_nos = set(self.all_repair_nos if include_repair else set())
        self.managed_nos = set(self.active_nos) | set(self.repair_nos)
        self.fixed_cars = [dict(car) for car in self.initial_cars if rv.car_no(car) not in self.managed_nos]
        self.fixed_by_no = {rv.car_no(car): dict(car) for car in self.fixed_cars}
        self.clear_state_caches()

    def clear_state_caches(self) -> None:
        self.route_cache.clear()
        self.cars_cache.clear()
        self.unsatisfied_cache.clear()
        self.lower_bound_cache.clear()
        self.line_map_cache.clear()
        self.target_ready_cache.clear()
        self.route_cleanup_cache.clear()

    def repair_candidate_allowed(self, car: dict[str, Any]) -> bool:
        line = car["Line"]
        if not line or line in RUNNING_LINES or line in OUT_OF_SCOPE_STRATEGY_LINES:
            return False
        if line not in rv.TRACK_LEN:
            return False
        return True

    def initial_repair_candidate_nos(self) -> set[str]:
        candidates: set[str] = set()
        by_no = {rv.car_no(car): car for car in self.initial_cars}
        active_lines = sorted({by_no[no]["Line"] for no in self.active_nos if no in by_no})
        for line in active_lines:
            blockers: list[str] = []
            for no in physical.line_access_order(self.initial_cars, line):
                if no in self.active_nos:
                    candidates.update(blockers)
                else:
                    blockers.append(no)

        route_lines: set[str] = set()
        for no in sorted(self.active_nos):
            car = by_no.get(no)
            target = self.target_by_no.get(no, "")
            if not car or not target:
                continue
            path = self.graph.route(car["Line"], target)
            route_lines.update(path[1:-1])
        for car in self.initial_cars:
            if car["Line"] in route_lines:
                candidates.add(rv.car_no(car))
        spotting_targets = {
            self.target_by_no.get(no, "")
            for no in self.active_nos
            if physical.is_spotting_line(self.target_by_no.get(no, ""))
        }
        for car in self.initial_cars:
            line = car["Line"]
            if line in spotting_targets and physical.is_spotting_line(line):
                candidates.add(rv.car_no(car))
        return candidates

    def solve(self) -> dict[str, Any]:
        if self.should_try_active_only_first():
            self.configure_managed(include_repair=False)
            original_deadline = self.deadline
            remaining = max(0.0, original_deadline - time.monotonic())
            self.deadline = min(original_deadline, time.monotonic() + min(8.0, max(1.0, remaining * 0.2)))
            active_only = self.solve_current_model()
            self.deadline = original_deadline
            active_only["summary"]["move_model_mode"] = "active_only"
            if active_only["summary"].get("status") in {"complete", "feasible_unproved"}:
                return active_only
        self.configure_managed(include_repair=True)
        result = self.solve_current_model()
        result["summary"]["move_model_mode"] = "repair_enabled"
        return result

    def should_try_active_only_first(self) -> bool:
        self.configure_managed(include_repair=False)
        early = self.early_rejections()
        if any(reason.startswith("active_blocked_by_fixed:") for reason in early):
            return False
        self.configure_managed(include_repair=True)
        return not self.forced_spotting_repair_needed(self.initial_state())

    def forced_spotting_repair_needed(self, state: State) -> bool:
        spotting_lines = {line for line, _nos in state.lines if physical.is_spotting_line(line)}
        return any(
            self.target_by_no.get(no, "") in spotting_lines
            and physical.force_positions(self.meta.get(no, {}))
            for no in self.active_debt_nos_in_state(state)
        )

    def solve_current_model(self) -> dict[str, Any]:
        early = self.early_rejections()
        if early:
            failed = SearchResult(
                status="partial",
                state=None,
                ops=(),
                cost=(INF_COST, 0, 0, 0),
                reasons=tuple(early),
                optimality="not_applicable",
                expansions=0,
                elapsed_seconds=round(time.monotonic() - self.started_at, 3),
            )
            return self.result(failed)

        start = self.initial_state()
        self.lower_bound_initial = self.lower_bound(start)
        if self.complete(start):
            done = SearchResult(
                status="complete",
                state=start,
                ops=(),
                cost=(0, 0, 0, 0),
                reasons=(),
                optimality="proved_hook_count_within_move_model",
                expansions=0,
                elapsed_seconds=0.0,
                queue_bound=(0, 0, 0, 0),
            )
            return self.result(done)

        upper_state, upper_ops = self.greedy_upper_bound(start)
        remaining = max(0.0, self.deadline - time.monotonic())
        if remaining > 4.0 and (upper_state is None or self.forced_spotting_repair_needed(start)):
            macro_state, macro_ops, _macro_reasons = self.timed_macro_upper_bound(start, max_seconds=min(55.0, remaining * 0.88))
            upper_state, upper_ops = self.better_solution(upper_state, upper_ops, macro_state, macro_ops)

        remaining = max(0.0, self.deadline - time.monotonic())
        if remaining > 4.0 and upper_state is None:
            beam_state, beam_ops = self.macro_beam_upper_bound(start)
            upper_state, upper_ops = self.better_solution(upper_state, upper_ops, beam_state, beam_ops)

        remaining = max(0.0, self.deadline - time.monotonic())
        if remaining > 4.0 and upper_state is None:
            prefix_state, prefix_ops = self.macro_prefix_search_upper_bound(start)
            upper_state, upper_ops = self.better_solution(upper_state, upper_ops, prefix_state, prefix_ops)

        remaining = max(0.0, self.deadline - time.monotonic())
        if remaining > 4.0 and upper_state is None:
            prefixed_state, prefixed_ops = self.prefixed_macro_upper_bound(start)
            upper_state, upper_ops = self.better_solution(upper_state, upper_ops, prefixed_state, prefixed_ops)
        upper_cost = self.ops_cost(upper_ops) if upper_state is not None else None
        searched = self.search(start, upper_state, upper_ops, upper_cost)
        return self.result(searched)

    def better_solution(
        self,
        left_state: State | None,
        left_ops: tuple[Op, ...],
        right_state: State | None,
        right_ops: tuple[Op, ...],
    ) -> tuple[State | None, tuple[Op, ...]]:
        if right_state is None or not self.complete(right_state):
            return left_state, left_ops
        if left_state is None or not self.complete(left_state):
            return right_state, right_ops
        return (right_state, right_ops) if self.ops_cost(right_ops) < self.ops_cost(left_ops) else (left_state, left_ops)

    def timed_macro_upper_bound(
        self,
        start: State,
        *,
        max_seconds: float,
    ) -> tuple[State | None, tuple[Op, ...], tuple[str, ...]]:
        original_deadline = self.deadline
        self.deadline = min(original_deadline, time.monotonic() + max(0.5, max_seconds))
        try:
            return self.macro_upper_bound(start)
        finally:
            self.deadline = original_deadline

    def macro_beam_upper_bound(self, start: State) -> tuple[State | None, tuple[Op, ...]]:
        original_deadline = self.deadline
        remaining = max(0.0, original_deadline - time.monotonic())
        if remaining <= 2.0:
            return None, ()
        beam_budget = min(30.0, max(1.0, remaining * 0.45))
        self.deadline = min(original_deadline, time.monotonic() + beam_budget)
        best_state: State | None = None
        best_ops: tuple[Op, ...] = ()
        best_cost: tuple[int, int, int, int] | None = None
        low_debt: list[tuple[tuple[Any, ...], State, tuple[Op, ...]]] = []
        frontier: list[tuple[tuple[Any, ...], int, State, tuple[Op, ...]]] = [
            (self.macro_state_rank(start, ()), 0, start, ())
        ]
        best_seen: dict[State, tuple[int, int, int, int]] = {start: (0, 0, 0, 0)}
        sequence = 1
        try:
            for _depth in range(MACRO_BEAM_DEPTH):
                if self.deadline_reached() or not frontier:
                    break
                next_frontier: list[tuple[tuple[Any, ...], int, State, tuple[Op, ...]]] = []
                for _rank, _seq, state, ops in frontier:
                    if self.deadline_reached():
                        break
                    if self.complete(state):
                        best_state, best_ops, best_cost = self.keep_best_complete(
                            best_state,
                            best_ops,
                            best_cost,
                            state,
                            ops,
                        )
                        continue
                    candidates = self.ranked_fast_macro_candidates(state)[:MACRO_BEAM_BRANCHES]
                    for candidate in candidates:
                        next_ops = (*ops, *candidate.ops)
                        next_cost = self.ops_cost(next_ops)
                        if next_cost >= best_seen.get(candidate.state, (INF_COST, INF_COST, INF_COST, INF_COST)):
                            continue
                        best_seen[candidate.state] = next_cost
                        if self.complete(candidate.state):
                            best_state, best_ops, best_cost = self.keep_best_complete(
                                best_state,
                                best_ops,
                                best_cost,
                                candidate.state,
                                next_ops,
                            )
                            continue
                        rank = self.macro_state_rank(candidate.state, next_ops)
                        if len(self.debt_nos_in_state(candidate.state)) <= MACRO_BEAM_LOW_DEBT_LIMIT:
                            low_debt.append((rank, candidate.state, next_ops))
                        next_frontier.append((rank, sequence, candidate.state, next_ops))
                        sequence += 1
                next_frontier.sort(key=lambda item: item[0])
                frontier = next_frontier[:MACRO_BEAM_WIDTH]
                if best_state is not None:
                    break
        finally:
            self.deadline = original_deadline

        if best_state is not None:
            return best_state, best_ops
        return self.complete_low_debt_prefixes(low_debt, original_deadline)

    def keep_best_complete(
        self,
        best_state: State | None,
        best_ops: tuple[Op, ...],
        best_cost: tuple[int, int, int, int] | None,
        state: State,
        ops: tuple[Op, ...],
    ) -> tuple[State | None, tuple[Op, ...], tuple[int, int, int, int] | None]:
        cost = self.ops_cost(ops)
        if best_cost is None or cost < best_cost:
            return state, ops, cost
        return best_state, best_ops, best_cost

    def complete_low_debt_prefixes(
        self,
        low_debt: list[tuple[tuple[Any, ...], State, tuple[Op, ...]]],
        original_deadline: float,
    ) -> tuple[State | None, tuple[Op, ...]]:
        if not low_debt:
            return None, ()
        best_state: State | None = None
        best_ops: tuple[Op, ...] = ()
        best_cost: tuple[int, int, int, int] | None = None
        tried: set[State] = set()
        for _rank, state, ops in sorted(low_debt, key=lambda item: item[0])[:MACRO_BEAM_LOW_DEBT_STATES]:
            if state in tried:
                continue
            tried.add(state)
            remaining = max(0.0, original_deadline - time.monotonic())
            if remaining <= 2.0:
                break
            self.deadline = min(original_deadline, time.monotonic() + min(8.0, max(1.0, remaining * 0.35)))
            try:
                result = self.search(state, None, (), None)
            finally:
                self.deadline = original_deadline
            if result.state is None or result.status not in {"complete", "feasible_unproved"}:
                continue
            combined_ops = (*ops, *result.ops)
            best_state, best_ops, best_cost = self.keep_best_complete(
                best_state,
                best_ops,
                best_cost,
                result.state,
                combined_ops,
            )
        return best_state, best_ops

    def macro_state_rank(self, state: State, ops: tuple[Op, ...]) -> tuple[Any, ...]:
        active_debt = len(self.active_debt_nos_in_state(state))
        total_debt = len(self.debt_nos_in_state(state))
        must_move = len(self.must_move_nos_in_state(state, self.managed_nos))
        cleanup = len(self.macro_blocker_lines(state))
        unsatisfied = self.unsatisfied_count(state)
        cost = self.ops_cost(ops)
        held_penalty = 0 if not state.held else 1
        return (
            active_debt,
            total_debt,
            must_move,
            cleanup,
            unsatisfied,
            held_penalty,
            cost[0],
            cost[1],
            cost[2],
            cost[3],
        )

    def macro_prefix_search_upper_bound(self, start: State) -> tuple[State | None, tuple[Op, ...]]:
        original_deadline = self.deadline
        remaining = max(0.0, original_deadline - time.monotonic())
        if remaining <= 2.0:
            return None, ()
        self.deadline = min(original_deadline, time.monotonic() + min(25.0, max(1.0, remaining * 0.45)))
        state = start
        ops: list[Op] = []
        seen: set[State] = set()
        try:
            for _step in range(MAX_GREEDY_STEPS):
                if self.complete(state):
                    return state, tuple(ops)
                if self.deadline_reached() or state in seen:
                    break
                seen.add(state)
                candidates = self.ranked_macro_candidates(state, seen=seen)
                if not candidates:
                    break
                chosen = min(candidates, key=lambda item: item.score)
                ops.extend(chosen.ops)
                state = chosen.state
            if self.complete(state):
                return state, tuple(ops)
            if len(self.debt_nos_in_state(state)) > 6:
                return None, ()
            self.deadline = original_deadline
            remaining = max(0.0, original_deadline - time.monotonic())
            self.deadline = min(original_deadline, time.monotonic() + min(25.0, max(1.0, remaining * 0.6)))
            result = self.search(state, None, (), None)
            if result.state is None or result.status not in {"complete", "feasible_unproved"}:
                return None, ()
            return result.state, (*ops, *result.ops)
        finally:
            self.deadline = original_deadline

    def prefixed_macro_upper_bound(self, start: State) -> tuple[State | None, tuple[Op, ...]]:
        start_debt = len(self.debt_nos_in_state(start))
        candidates = sorted(self.spotting_repack_macros(start), key=lambda item: item.score)
        useful = [
            candidate
            for candidate in candidates[:4]
            if len(self.debt_nos_in_state(candidate.state)) < start_debt
            and len(self.debt_nos_in_state(candidate.state)) <= 6
        ]
        if not useful:
            return None, ()
        original_deadline = self.deadline
        remaining = max(0.0, original_deadline - time.monotonic())
        self.deadline = min(original_deadline, time.monotonic() + min(20.0, max(1.0, remaining * 0.35)))
        best_state: State | None = None
        best_ops: tuple[Op, ...] = ()
        best_cost: tuple[int, int, int, int] | None = None
        try:
            for candidate in useful:
                if self.deadline_reached():
                    break
                result = self.search(candidate.state, None, (), None)
                if result.state is None or result.status not in {"complete", "feasible_unproved"}:
                    continue
                ops = (*candidate.ops, *result.ops)
                cost = self.ops_cost(ops)
                if best_cost is None or cost < best_cost:
                    best_state = result.state
                    best_ops = ops
                    best_cost = cost
        finally:
            self.deadline = original_deadline
        return best_state, best_ops

    def early_rejections(self) -> list[str]:
        reasons: list[str] = []
        if self.out_of_scope_nos:
            reasons.append(f"stage3_residual_depot_debt:{len(self.out_of_scope_nos)}")
        duplicates = [
            no
            for no, count in Counter(rv.car_no(car) for car in self.initial_cars).items()
            if not no or count > 1
        ]
        if duplicates:
            reasons.append(f"duplicate_or_empty_car_no:{','.join(sorted(duplicates))}")
        for no in sorted(self.active_nos):
            target = self.target_by_no.get(no, "")
            if target in RUNNING_LINES:
                reasons.append(f"target_is_running_line:{no}:{target}")
            if target in BLOCKED_LINES:
                reasons.append(f"target_is_blocked_line:{no}:{target}")
        by_no = {rv.car_no(car): car for car in self.initial_cars}
        for line in sorted({by_no[no]["Line"] for no in self.active_nos if no in by_no}):
            seen_fixed_blocker = ""
            for no in physical.line_access_order(self.initial_cars, line):
                if no in self.active_nos:
                    if seen_fixed_blocker:
                        reasons.append(f"active_blocked_by_fixed:{no}:{line}:by={seen_fixed_blocker}")
                elif no not in self.managed_nos:
                    seen_fixed_blocker = no
        return reasons

    def initial_state(self) -> State:
        by_line: dict[str, list[tuple[int, str]]] = {}
        for no in self.managed_nos:
            car = self.meta[no]
            by_line.setdefault(car["Line"], []).append((int(car.get("Position") or 0), no))
        packed = {
            line: tuple(no for _pos, no in sorted(rows))
            for line, rows in by_line.items()
            if line
        }
        positions = {
            no: int(self.meta[no].get("Position") or 0)
            for no in self.managed_nos
            if self.meta[no].get("Line")
        }
        initially_weighed = tuple(sorted(no for no, car in self.meta.items() if car.get("_Weighed")))
        return State(
            lines=self.pack_lines_ordered(packed, positions),
            positions=self.pack_positions(positions),
            held=(),
            loco=tuple(sorted(self.initial_loco)),
            weighed=initially_weighed,
        )

    def search(
        self,
        start: State,
        upper_state: State | None,
        upper_ops: tuple[Op, ...],
        upper_cost: tuple[int, int, int, int] | None,
    ) -> SearchResult:
        queue: list[
            tuple[
                tuple[int, int, int, int, int, int],
                tuple[int, int, int, int],
                int,
                State,
                tuple[int, int, int, int],
            ]
        ] = []
        start_cost = (0, 0, 0, 0)
        start_bound = self.bound_tuple(start_cost, start)
        heapq.heappush(queue, (self.priority(start_cost, start), start_cost, 0, start, start_bound))
        best: dict[State, tuple[int, int, int, int]] = {start: start_cost}
        prev: dict[State, tuple[State, Op]] = {}
        sequence = 1
        expansions = 0
        rejections: Counter[str] = Counter()

        while queue:
            priority, cost, _seq, state, bound = heapq.heappop(queue)
            if best.get(state) != cost:
                continue
            if upper_cost is not None and bound >= upper_cost:
                return SearchResult(
                    status="complete",
                    state=upper_state,
                    ops=upper_ops,
                    cost=upper_cost,
                    reasons=(),
                    optimality="proved_hook_count_within_move_model",
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                    queue_bound=bound,
                )
            if self.complete(state):
                ops = self.reconstruct(prev, state)
                return SearchResult(
                    status="complete",
                    state=state,
                    ops=ops,
                    cost=cost,
                    reasons=(),
                    optimality="proved_hook_count_within_move_model",
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                    queue_bound=bound,
                )
            if self.deadline_reached():
                break
            expansions += 1
            if expansions > self.max_expansions:
                break
            dead_reason = self.dead_state_reason(state)
            if dead_reason:
                rejections[dead_reason] += 1
                continue

            yielded = False
            for op, next_state, reject in self.neighbors(state):
                if reject:
                    rejections[reject] += 1
                    continue
                yielded = True
                delta = self.delta(op)
                next_cost = (
                    cost[0] + delta[0],
                    cost[1] + delta[1],
                    cost[2] + delta[2],
                    cost[3] + delta[3],
                )
                if upper_cost is not None and self.bound_tuple(next_cost, next_state) >= upper_cost:
                    continue
                if next_cost >= best.get(next_state, (INF_COST, INF_COST, INF_COST, INF_COST)):
                    continue
                best[next_state] = next_cost
                prev[next_state] = (state, op)
                sequence += 1
                next_bound = self.bound_tuple(next_cost, next_state)
                heapq.heappush(
                    queue,
                    (self.priority(next_cost, next_state), next_cost, sequence, next_state, next_bound),
                )
            if not yielded:
                rejections["no_neighbor_from_state"] += 1

        if upper_state is not None and upper_cost is not None:
            exhausted_normally = not queue and not self.deadline_reached() and expansions <= self.max_expansions
            if exhausted_normally:
                return SearchResult(
                    status="complete",
                    state=upper_state,
                    ops=upper_ops,
                    cost=upper_cost,
                    reasons=(),
                    optimality="proved_hook_count_within_move_model",
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                    queue_bound=upper_cost,
                )
            reason = "stage4_time_or_expansion_budget_exhausted"
            return SearchResult(
                status="feasible_unproved",
                state=upper_state,
                ops=upper_ops,
                cost=upper_cost,
                reasons=(reason,),
                optimality="feasible_unproved",
                expansions=expansions,
                elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                queue_bound=None,
            )
        reasons = tuple(f"{key}:{value}" for key, value in rejections.most_common(12))
        if self.deadline_reached():
            reasons = ("stage4_global_time_budget_exhausted", *reasons)
        elif expansions > self.max_expansions:
            reasons = ("stage4_expansion_budget_exhausted", *reasons)
        return SearchResult(
            status="partial",
            state=None,
            ops=(),
            cost=(INF_COST, 0, 0, 0),
            reasons=reasons or ("stage4_no_solution",),
            optimality="not_proved",
            expansions=expansions,
            elapsed_seconds=round(time.monotonic() - self.started_at, 3),
            queue_bound=None,
        )

    def macro_upper_bound(self, start: State) -> tuple[State | None, tuple[Op, ...], tuple[str, ...]]:
        state = start
        ops: list[Op] = []
        seen: set[State] = set()
        trace_reasons: list[str] = []
        for _step in range(MAX_GREEDY_STEPS):
            if self.complete(state):
                return state, tuple(ops), ()
            if state in seen:
                return None, (), ("macro_state_cycle", *tuple(trace_reasons[-8:]))
            if self.deadline_reached():
                return None, (), ("macro_deadline_reached", *tuple(trace_reasons[-8:]))
            seen.add(state)
            candidates = self.ranked_macro_candidates(state, seen=seen)
            if not candidates:
                return None, (), ("macro_no_candidate", *tuple(trace_reasons[-8:]))
            chosen = candidates[0]
            ops.extend(chosen.ops)
            state = chosen.state
            trace_reasons.append(chosen.reason)
        return None, (), ("macro_step_limit", *tuple(trace_reasons[-8:]))

    def ranked_macro_candidates(self, state: State, *, seen: set[State]) -> list[MacroCandidate]:
        candidates: list[MacroCandidate] = []
        for candidate in self.macro_candidates(state):
            if self.deadline_reached():
                break
            if candidate.state in seen:
                continue
            candidates.append(candidate)
            if len(candidates) >= MACRO_CANDIDATE_POOL:
                break
        candidates.sort(key=lambda item: item.score)
        return candidates

    def ranked_fast_macro_candidates(self, state: State) -> list[MacroCandidate]:
        candidates: list[MacroCandidate] = []
        for candidate in self.fast_macro_candidates(state):
            if self.deadline_reached():
                break
            candidates.append(candidate)
            if len(candidates) >= MACRO_CANDIDATE_POOL:
                break
        candidates.sort(key=lambda item: item.score)
        return candidates

    def macro_candidates(self, state: State) -> Iterable[MacroCandidate]:
        yield from self.service_sweep_macros(state)
        yield from self.gate_inner_restore_macros(state)
        yield from self.spotting_force_rebuild_macros(state)
        yield from self.spotting_repack_macros(state)
        yield from self.target_group_macros(state)
        if state.held:
            yield from self.macro_held_candidates(state)
            return
        yield from self.macro_empty_train_candidates(state)

    def fast_macro_candidates(self, state: State) -> Iterable[MacroCandidate]:
        yield from self.service_sweep_macros(state)
        yield from self.gate_inner_restore_macros(state)
        yield from self.spotting_force_rebuild_macros(state)
        yield from self.spotting_repack_macros(state)
        if state.held:
            yield from self.macro_held_candidates(state)
            return
        yield from self.target_sweep_macros(state)
        yield from self.macro_empty_train_candidates(state)

    def service_sweep_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            return
        yielded: set[tuple[str, tuple[str, ...]]] = set()
        for line, move in self.sweep_get_prefixes(state):
            key = (line, move)
            if key in yielded:
                continue
            yielded.add(key)
            candidate = self.build_service_sweep_macro(state, line, move)
            if candidate:
                yield candidate

    def sweep_get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        cars = self.cars_from_state(state)
        held = set(state.held)
        debt = self.must_move_nos_in_state(state, self.managed_nos)
        line_map = self.line_map(state)
        active_lines = [line for line in SWEEP_SOURCE_LINES if line_map.get(line)]
        active_lines.extend(line for line in self.ordered_get_lines(state) if line not in set(active_lines))
        for line in active_lines:
            ordered = physical.line_access_order(cars, line, held)
            prefix: list[str] = []
            debt_seen = False
            target_changes = 0
            last_target = ""
            for no in ordered:
                if no not in self.managed_nos:
                    break
                trial = (*state.held, *prefix, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                target = self.target_by_no.get(no, "")
                if prefix and target != last_target:
                    target_changes += 1
                last_target = target
                prefix.append(no)
                if no in debt:
                    debt_seen = True
            if not prefix or not debt_seen:
                continue
            if len(prefix) < 2 and line not in self.route_cleanup_lines(state):
                continue
            # Prefer the largest mixed/front prefix, but keep a same-target tail cut
            # available when the full prefix cannot be swept cleanly.
            cuts = {len(prefix)}
            if target_changes:
                for index in range(1, len(prefix)):
                    if self.target_by_no.get(prefix[index - 1], "") != self.target_by_no.get(prefix[index], ""):
                        cuts.add(index)
            for cut in sorted(cuts, reverse=True):
                yield line, tuple(prefix[:cut])

    def build_service_sweep_macro(self, origin: State, line: str, move: tuple[str, ...]) -> MacroCandidate | None:
        before_debt = len(self.debt_nos_in_state(origin))
        before_must = len(self.must_move_nos_in_state(origin, self.managed_nos))
        ops: list[Op] = []
        seen: set[State] = {origin}
        get_op, state, reject = self.apply_get(origin, line, move)
        if reject:
            return None
        ops.append(get_op)
        seen.add(state)
        for _step in range(len(move) + 4):
            if self.deadline_reached():
                return None
            if not state.held:
                break
            weigh = self.weigh_candidate(state)
            if weigh is not None:
                weigh_op, after_weigh, weigh_reject = weigh
                if not weigh_reject and after_weigh not in seen:
                    ops.append(weigh_op)
                    state = after_weigh
                    seen.add(state)
                    continue
            target_put = self.best_service_target_put(state)
            if target_put is not None:
                put_op, after_put = target_put
                if after_put in seen:
                    return None
                ops.append(put_op)
                state = after_put
                seen.add(state)
                continue
            cache_put = self.best_service_cache_put(state)
            if cache_put is None:
                return None
            put_op, after_put = cache_put
            if after_put in seen:
                return None
            ops.append(put_op)
            state = after_put
            seen.add(state)
        if state.held:
            return None
        after_debt = len(self.debt_nos_in_state(state))
        after_must = len(self.must_move_nos_in_state(state, self.managed_nos))
        if after_debt >= before_debt and after_must >= before_must:
            return None
        return self.make_macro_candidate(
            origin,
            tuple(ops),
            state,
            reason_rank=-4,
            reason="macro_service_sweep",
        )

    def best_service_target_put(self, state: State) -> tuple[Op, State] | None:
        options: list[tuple[tuple[Any, ...], Op, State]] = []
        service_rank = self.service_rank_map(state)
        for line, move, note in self.put_suffixes(state, include_cache=False):
            if note != "target":
                continue
            target = line
            if self.service_target_deferred(state, target, service_rank):
                continue
            op, next_state, reject = self.apply_put(state, line, move, note=note)
            if reject:
                continue
            debt_drop = len(self.debt_nos_in_state(state)) - len(self.debt_nos_in_state(next_state))
            must_drop = len(self.must_move_nos_in_state(state, self.managed_nos)) - len(
                self.must_move_nos_in_state(next_state, self.managed_nos)
            )
            options.append(
                (
                    (
                        service_rank.get(target, 999),
                        -debt_drop,
                        -must_drop,
                        len(op.path),
                        -len(move),
                        target,
                    ),
                    op,
                    next_state,
                )
            )
        if not options:
            return None
        _rank, op, next_state = min(options, key=lambda item: item[0])
        return op, next_state

    def best_service_cache_put(self, state: State) -> tuple[Op, State] | None:
        move = self.service_cache_suffix(state)
        if not move:
            return None
        options: list[tuple[tuple[Any, ...], Op, State]] = []
        for line in self.leaf_cache_lines(state, move):
            if line in state.loco:
                continue
            op, next_state, reject = self.apply_put(state, line, move, note="cache")
            if reject:
                continue
            options.append(
                (
                    (
                        len(self.route_cleanup_lines(next_state)),
                        self.line_rank(line),
                        len(op.path),
                        line,
                    ),
                    op,
                    next_state,
                )
            )
        if not options:
            return None
        _rank, op, next_state = min(options, key=lambda item: item[0])
        return op, next_state

    def service_cache_suffix(self, state: State) -> tuple[str, ...]:
        held = state.held
        if not held:
            return ()
        tail_target = self.target_by_no.get(held[-1], "")
        start = len(held) - 1
        while start > 0 and self.target_by_no.get(held[start - 1], "") == tail_target:
            start -= 1
        return held[start:]

    def leaf_cache_lines(self, state: State, move: tuple[str, ...]) -> tuple[str, ...]:
        safe = set(self.safe_cache_lines(state, move))
        own_target = self.common_target(move)
        active_targets = {
            self.target_by_no.get(no, "")
            for no in self.active_debt_nos_in_state(state)
            if self.target_by_no.get(no, "")
        }
        out: list[str] = []
        for line in LEAF_CACHE_LINES:
            if line not in safe or line == own_target:
                continue
            if line in active_targets and line != own_target:
                continue
            out.append(line)
        if out:
            return tuple(out)
        return tuple(line for line in self.safe_cache_lines(state, move) if line not in {"机北1", "机北2", "机南", "洗油北", "存5线北"})

    def service_target_deferred(self, state: State, target: str, service_rank: dict[str, int]) -> bool:
        if target not in service_rank:
            return False
        for predecessor, successor in SERVICE_PRIORITY_EDGES:
            if successor == target and self.service_line_has_pending(state, predecessor):
                return True
        return False

    def service_line_has_pending(self, state: State, line: str) -> bool:
        line_map = self.line_map(state)
        managed_on_line = [no for no in line_map.get(line, ()) if no in self.managed_nos]
        if not managed_on_line:
            return False
        debt = self.debt_nos_in_state(state)
        must_move = self.must_move_nos_in_state(state, self.managed_nos)
        return any(no in debt or no in must_move for no in managed_on_line)

    def service_target_put_allowed(self, state: State, target: str) -> bool:
        return not self.service_target_deferred(state, target, self.service_rank_map(state))

    def service_rank_map(self, state: State) -> dict[str, int]:
        order = self.service_target_order(state)
        return {line: index for index, line in enumerate(order)}

    def service_target_order(self, state: State) -> tuple[str, ...]:
        targets = {
            self.target_by_no.get(no, "")
            for no in self.active_debt_nos_in_state(state)
            if self.target_by_no.get(no, "")
        }
        targets.update(self.target_by_no.get(no, "") for no in state.held if self.target_by_no.get(no, ""))
        targets = {line for line in targets if line}
        outgoing: dict[str, set[str]] = {line: set() for line in targets}
        indegree: dict[str, int] = {line: 0 for line in targets}
        for left, right in SERVICE_PRIORITY_EDGES:
            if left not in targets or right not in targets:
                continue
            if right in outgoing[left]:
                continue
            outgoing[left].add(right)
            indegree[right] += 1

        def tie(line: str) -> tuple[int, int, str]:
            try:
                rank = SERVICE_TIE_BREAK.index(line)
            except ValueError:
                rank = len(SERVICE_TIE_BREAK)
            return (rank, self.line_rank(line), line)

        ready = sorted((line for line in targets if indegree[line] == 0), key=tie)
        order: list[str] = []
        while ready:
            line = ready.pop(0)
            order.append(line)
            for right in sorted(outgoing[line], key=tie):
                indegree[right] -= 1
                if indegree[right] == 0:
                    ready.append(right)
                    ready.sort(key=tie)
        if len(order) < len(targets):
            remaining = sorted((line for line in targets if line not in set(order)), key=tie)
            order.extend(remaining)
        return tuple(order)

    def target_sweep_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            return
        targets: list[str] = []
        seen_targets: set[str] = set()
        for allow_repair in (False, True):
            for _line, move in self.get_prefixes(state, allow_repair=allow_repair):
                target = self.common_target(move)
                if not target or target in seen_targets:
                    continue
                seen_targets.add(target)
                targets.append(target)
        for target in targets:
            if not self.service_target_put_allowed(state, target):
                continue
            candidate = self.build_target_sweep_macro(state, target)
            if candidate:
                yield candidate

    def build_target_sweep_macro(self, origin: State, target: str) -> MacroCandidate | None:
        state = origin
        ops: list[Op] = []
        seen_states = {origin}
        best: MacroCandidate | None = None
        for _depth in range(MAX_TARGET_GROUP_GETS):
            choice = self.best_target_sweep_get(state, target)
            if choice is None:
                break
            get_op, next_state = choice
            if next_state in seen_states:
                break
            ops.append(get_op)
            state = next_state
            seen_states.add(state)
            if self.common_target(state.held) != target:
                break
            put_op, after_put, reject = self.apply_put(state, target, state.held, note="target")
            if not reject:
                candidate = self.make_macro_candidate(
                    origin,
                    (*ops, put_op),
                    after_put,
                    reason_rank=0,
                    reason="macro_target_sweep",
                )
                if best is None or candidate.score < best.score:
                    best = candidate
        return best

    def best_target_sweep_get(self, state: State, target: str) -> tuple[Op, State] | None:
        options: list[tuple[tuple[Any, ...], Op, State]] = []
        seq = 0
        debt = self.debt_nos_in_state(state)
        for allow_repair in (False, True):
            for line, move in self.get_prefixes(state, allow_repair=allow_repair):
                if self.common_target(move) != target:
                    continue
                op, next_state, reject = self.apply_get(state, line, move)
                if reject:
                    continue
                moving_debt = sum(1 for no in move if no in debt)
                rank = (
                    -moving_debt,
                    -len(move),
                    self.line_rank(line),
                    len(op.path),
                    seq,
                )
                options.append((rank, op, next_state))
                seq += 1
        if not options:
            return None
        _rank, op, next_state = min(options, key=lambda item: item[0])
        return op, next_state

    def gate_inner_restore_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            return
        line_map = self.line_map(state)
        debt = self.debt_nos_in_state(state)
        for gate, inner in GATE_INNER_TARGET_PAIRS:
            if not any(no in debt and self.target_by_no.get(no) == inner for no in line_map.get(inner, ())):
                if not any(
                    no in debt and self.target_by_no.get(no) == inner
                    for nos in line_map.values()
                    for no in nos
                ):
                    continue
            gate_move = self.gate_restore_move(state, gate)
            if not gate_move:
                continue
            candidate = self.build_gate_inner_restore_macro(state, gate, inner, gate_move)
            if candidate:
                yield candidate

    def gate_restore_move(self, state: State, gate: str) -> tuple[str, ...]:
        move: list[str] = []
        ordered = physical.line_access_order(self.cars_from_state(state), gate, set(state.held))
        for no in ordered:
            if no not in self.managed_nos:
                break
            if self.target_by_no.get(no) != gate:
                break
            trial = (*state.held, *move, no)
            if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                break
            move.append(no)
        if not move or self.common_target(tuple(move)) != gate:
            return ()
        return tuple(move)

    def build_gate_inner_restore_macro(
        self,
        origin: State,
        gate: str,
        inner: str,
        gate_move: tuple[str, ...],
    ) -> MacroCandidate | None:
        ops: list[Op] = []
        get_gate, state, reject = self.apply_get(origin, gate, gate_move)
        if reject:
            return None
        ops.append(get_gate)
        cache_put = self.best_cache_put(state, gate_move, avoid={origin})
        if cache_put is None:
            return None
        put_cache, state = cache_put
        cache_line = put_cache.line
        ops.append(put_cache)

        inner_candidate = self.best_inner_completion_macro(state, inner)
        if inner_candidate is None:
            return None
        ops.extend(inner_candidate.ops)
        state = inner_candidate.state

        get_cached, state_after_get, reject = self.apply_get(state, cache_line, gate_move)
        if reject:
            return None
        ops.append(get_cached)
        put_gate, state_after_put, reject = self.apply_put(state_after_get, gate, gate_move, note="target")
        if reject:
            return None
        ops.append(put_gate)
        return self.make_macro_candidate(
            origin,
            tuple(ops),
            state_after_put,
            reason_rank=-2,
            reason="macro_gate_inner_restore",
        )

    def best_inner_completion_macro(self, state: State, inner: str) -> MacroCandidate | None:
        options: list[MacroCandidate] = []
        before = self.target_debt_count(state, inner)
        force_rebuild = self.build_spotting_force_rebuild_macro(state, inner)
        if force_rebuild and self.target_debt_count(force_rebuild.state, inner) < before:
            options.append(force_rebuild)
        spotting = self.build_spotting_repack_macro(state, inner)
        if spotting and self.target_debt_count(spotting.state, inner) < before:
            options.append(spotting)
        for candidate in self.target_group_macros(state):
            if (
                any(op.note == "target" and op.line == inner for op in candidate.ops)
                and self.target_debt_count(candidate.state, inner) < before
            ):
                options.append(candidate)
        if not options:
            return None
        return min(options, key=lambda candidate: candidate.score)

    def target_debt_count(self, state: State, target: str) -> int:
        debt = self.debt_nos_in_state(state)
        return sum(1 for no in self.active_nos if no in debt and self.target_by_no.get(no) == target)

    def managed_target_debt_count(self, state: State, target: str) -> int:
        debt = self.debt_nos_in_state(state)
        return sum(1 for no in self.managed_nos if no in debt and self.target_by_no.get(no) == target)

    def spotting_force_rebuild_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            return
        targets = {
            self.target_by_no.get(no, "")
            for no in self.active_debt_nos_in_state(state)
            if physical.force_positions(self.meta.get(no, {}))
        }
        for target in sorted(targets, key=lambda line: (self.line_rank(line), line)):
            if not target or not physical.is_spotting_line(target):
                continue
            candidate = self.build_spotting_force_rebuild_macro(state, target)
            if candidate:
                yield candidate

    def build_spotting_force_rebuild_macro(self, origin: State, target: str) -> MacroCandidate | None:
        if origin.held or not physical.is_spotting_line(target):
            return None
        key = self.spotting_primary_forced_key(origin, target)
        if not key:
            return None
        before_target_debt = self.target_debt_count(origin, target)
        if before_target_debt <= 0:
            return None
        existing = tuple(physical.line_access_order(self.cars_from_state(origin), target, set(origin.held)))
        if not existing or any(no not in self.managed_nos for no in existing):
            return None
        ops: list[Op] = []
        get_existing, state, reject = self.apply_get(origin, target, existing)
        if reject:
            return None
        ops.append(get_existing)

        seen_states = {origin, state}
        for _step in range(12):
            if not self.spotting_target_remaining_on_lines(state, target):
                break
            segment = self.next_forced_debt_segment(state, target, key)
            if segment is None:
                return None
            line, move = segment
            get_op, next_state, reject = self.apply_get(state, line, move)
            if reject or next_state in seen_states:
                return None
            ops.append(get_op)
            state = next_state
            seen_states.add(state)
        if self.spotting_target_remaining_on_lines(state, target):
            return None

        if self.common_target(state.held) != target:
            return None
        put_forced, state, reject = self.apply_put(state, target, state.held, note="target")
        if reject:
            return None
        ops.append(put_forced)
        if self.target_debt_count(state, target) >= before_target_debt:
            return None
        return self.make_macro_candidate(
            origin,
            tuple(ops),
            state,
            reason_rank=1,
            reason="macro_spotting_force_rebuild",
        )

    def next_forced_debt_segment(self, state: State, target: str, key: tuple[int, ...]) -> tuple[str, tuple[str, ...]] | None:
        debt = self.debt_nos_in_state(state)
        best: tuple[int, str, tuple[str, ...]] | None = None
        for line in self.ordered_get_lines(state):
            if line == target:
                continue
            ordered = self.line_map(state).get(line, ())
            if not ordered:
                continue
            move: list[str] = []
            seen_forced_debt = False
            for no in ordered:
                if no not in self.managed_nos:
                    break
                is_target_debt = no in debt and self.target_by_no.get(no) == target
                if move and not is_target_debt:
                    break
                if not is_target_debt:
                    break
                trial = (*state.held, *move, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                move.append(no)
                if self.spotting_force_key(no) == key:
                    seen_forced_debt = True
            if not seen_forced_debt:
                continue
            candidate = (self.line_rank(line), line, tuple(move))
            if best is None or candidate < best:
                best = candidate
        if best is None:
            return None
        _rank, line, move = best
        return line, move

    def first_forced_suffix_index(self, held: tuple[str, ...], target: str, key: tuple[int, ...]) -> int | None:
        for index, no in enumerate(held):
            if self.target_by_no.get(no) == target and self.spotting_force_key(no) == key:
                return index
        return None

    def spotting_repack_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            return
        for target in self.spotting_repack_targets(state):
            candidate = self.build_spotting_repack_macro(state, target)
            if candidate:
                yield candidate

    def spotting_repack_targets(self, state: State) -> tuple[str, ...]:
        debt = self.debt_nos_in_state(state)
        targets: set[str] = set()
        for no in self.active_nos:
            target = self.target_by_no.get(no, "")
            if not target or not physical.is_spotting_line(target):
                continue
            if self.service_target_deferred(state, target, self.service_rank_map(state)):
                continue
            if no not in debt:
                continue
            if physical.force_positions(self.meta.get(no, {})):
                targets.add(target)
        return tuple(sorted(targets, key=lambda line: (self.line_rank(line), line)))

    def build_spotting_repack_macro(self, origin: State, target: str) -> MacroCandidate | None:
        state = origin
        ops: list[Op] = []
        seen: set[State] = {origin}
        for _step in range(MAX_SPOTTING_REPACK_STEPS):
            if self.spotting_target_complete(state, target):
                if not ops:
                    return None
                if self.managed_target_debt_count(state, target):
                    return None
                return self.make_macro_candidate(
                    origin,
                    tuple(ops),
                    state,
                    reason_rank=-1,
                    reason="macro_spotting_repack",
                )
            if self.deadline_reached():
                return None

            if state.held:
                if self.common_target(state.held) != target:
                    return None
                held_key = self.spotting_held_force_key(state)
                if self.spotting_held_should_stage(state, target):
                    put_op, after_put, reject = self.apply_put(state, target, state.held, note="target")
                    if reject or after_put in seen:
                        return None
                    ops.append(put_op)
                    state = after_put
                    seen.add(state)
                    continue
                if held_key and not self.spotting_target_key_remaining_on_lines(state, target, held_key):
                    put_op, after_put, reject = self.apply_put(state, target, state.held, note="target")
                    if reject or after_put in seen:
                        return None
                    ops.append(put_op)
                    state = after_put
                    seen.add(state)
                    continue
                if not self.spotting_target_remaining_on_lines(state, target):
                    put_op, after_put, reject = self.apply_put(state, target, state.held, note="target")
                    if reject or after_put in seen:
                        return None
                    ops.append(put_op)
                    state = after_put
                    seen.add(state)
                    continue

            segment = self.next_spotting_repack_segment(state, target)
            if segment is None:
                return None
            line, move, kind = self.resolve_spotting_get_segment(state, segment)
            get_op, after_get, reject = self.apply_get(state, line, move)
            if reject or after_get in seen:
                return None
            ops.append(get_op)
            state = after_get
            seen.add(state)

            weigh = self.weigh_candidate(state)
            if weigh is not None:
                weigh_op, after_weigh, weigh_reject = weigh
                if weigh_reject or after_weigh in seen:
                    return None
                ops.append(weigh_op)
                state = after_weigh
                seen.add(state)

            if kind == "target_keep":
                continue
            if kind == "target_cache":
                target_line = self.common_target(move)
                if target_line != target:
                    return None
                if (
                    self.spotting_force_key(move[0])
                    or not self.spotting_target_key_remaining_on_lines(
                        state,
                        target,
                        self.spotting_primary_forced_key(state, target),
                    )
                ):
                    put_op, after_put, reject = self.apply_put(state, target, state.held, note="target")
                    if reject or after_put in seen:
                        return None
                    ops.append(put_op)
                    state = after_put
                    seen.add(state)
                    continue
                continue

            target_line = self.common_target(move)
            if not target_line:
                return None
            put_op, after_put, put_reject = self.apply_put(state, target_line, move, note="target")
            if put_reject:
                if kind != "blocker_target":
                    return None
                put_result = self.best_cache_put(state, move, avoid=seen)
                if put_result is None:
                    return None
                put_op, after_put = put_result
            if after_put in seen:
                return None
            ops.append(put_op)
            state = after_put
            seen.add(state)
        return None

    def spotting_held_should_stage(self, state: State, target: str) -> bool:
        if not state.held or self.common_target(state.held) != target:
            return False
        if not self.spotting_target_remaining_on_lines(state, target):
            return False
        line_map = self.line_map(state)
        debt = self.debt_nos_in_state(state)
        for _line, nos in line_map.items():
            blockers = 0
            for no in nos:
                if no in debt and self.target_by_no.get(no) == target:
                    if blockers:
                        return True
                    break
                blockers += 1
        return False

    def resolve_spotting_get_segment(
        self,
        state: State,
        segment: tuple[str, tuple[str, ...], str],
    ) -> tuple[str, tuple[str, ...], str]:
        line, move, kind = segment
        _op, _after_get, reject = self.apply_get(state, line, move)
        if not reject.startswith("get_route_blocked:"):
            return segment
        blocker = self.route_blocker_segment_for_repack(state, line, move)
        return blocker or segment

    def route_blocker_segment_for_repack(
        self,
        state: State,
        blocked_line: str,
        blocked_move: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...], str] | None:
        line_map = self.line_map(state)
        cars = self.cars_from_state(state)
        for blocker_line in self.route_blockers_for_get(state, blocked_line, set(blocked_move)):
            if blocker_line in RUNNING_LINES or blocker_line in OUT_OF_SCOPE_STRATEGY_LINES:
                continue
            ordered = physical.line_access_order(cars, blocker_line, set(state.held))
            if not ordered or ordered[0] not in self.managed_nos:
                continue
            first = ordered[0]
            first_target = self.target_by_no.get(first, "")
            first_forced = bool(physical.force_positions(self.meta.get(first, {})))
            move: list[str] = []
            for no in ordered:
                if no not in self.managed_nos:
                    break
                if self.target_by_no.get(no, "") != first_target:
                    break
                if bool(physical.force_positions(self.meta.get(no, {}))) != first_forced:
                    break
                trial = (*state.held, *move, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                move.append(no)
            if not move:
                continue
            if first_target:
                return blocker_line, tuple(move), "blocker_target"
            if blocker_line in line_map:
                return blocker_line, tuple(move), "target_cache"
        return None

    def spotting_target_complete(self, state: State, target: str) -> bool:
        if state.held and any(self.target_by_no.get(no) == target for no in state.held):
            return False
        target_nos = {no for no in self.active_nos if self.target_by_no.get(no) == target}
        if not target_nos:
            return True
        return not (target_nos & self.debt_nos_in_state(state))

    def spotting_nonforced_remaining(self, state: State, target: str) -> bool:
        held = set(state.held)
        debt = self.debt_nos_in_state(state)
        for _line, nos in self.line_map(state).items():
            for no in nos:
                if no in held:
                    continue
                if self.target_by_no.get(no) != target:
                    continue
                if no not in self.active_nos or no not in debt:
                    continue
                if not physical.force_positions(self.meta.get(no, {})):
                    return True
        return False

    def spotting_target_remaining_on_lines(self, state: State, target: str) -> bool:
        held = set(state.held)
        debt = self.debt_nos_in_state(state)
        for _line, nos in self.line_map(state).items():
            for no in nos:
                if no in held:
                    continue
                if no in self.active_nos and no in debt and self.target_by_no.get(no) == target:
                    return True
        return False

    def next_spotting_repack_segment(self, state: State, target: str) -> tuple[str, tuple[str, ...], str] | None:
        line_map = self.line_map(state)
        cars = self.cars_from_state(state)
        held = set(state.held)
        relevant_lines = self.spotting_repack_relevant_lines(state, target)
        for line in relevant_lines:
            ordered = physical.line_access_order(cars, line, held)
            if not ordered:
                continue
            first = ordered[0]
            if first not in self.managed_nos:
                continue
            if line != target and not self.line_relevant_for_spotting_target(state, line_map.get(line, ()), target):
                continue
            first_target = self.target_by_no.get(first, "")
            if line == target and first_target == target and first not in self.debt_nos_in_state(state):
                continue
            first_forced = bool(physical.force_positions(self.meta.get(first, {})))
            move: list[str] = []
            for no in ordered:
                if no not in self.managed_nos:
                    break
                no_target = self.target_by_no.get(no, "")
                no_forced = bool(physical.force_positions(self.meta.get(no, {})))
                if no_target != first_target or no_forced != first_forced:
                    break
                trial = (*state.held, *move, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                move.append(no)
            if not move:
                continue
            move_tuple = tuple(move)
            held_target = self.common_target(state.held)
            if held_target == target and first_target != target:
                continue
            if first_target == target:
                first_key = self.spotting_force_key(first)
                held_key = self.spotting_held_force_key(state)
                if held_key and first_key != held_key:
                    return line, move_tuple, "target_cache"
                if not held_key and not first_key and self.spotting_forced_remaining_after_move(state, target, set(move_tuple)):
                    return line, move_tuple, "target_cache"
                return line, move_tuple, "target_keep"
            if any(self.target_by_no.get(no) == target for no in line_map.get(line, ())):
                return line, move_tuple, "blocker_target"
            if line == target:
                return line, move_tuple, "blocker_target"
        return None

    def spotting_repack_relevant_lines(self, state: State, target: str) -> tuple[str, ...]:
        line_map = self.line_map(state)
        debt = self.debt_nos_in_state(state)
        lines = []
        if any(no in debt for no in line_map.get(target, ())):
            lines.append(target)
        for line, nos in line_map.items():
            if line == target:
                continue
            if self.line_relevant_for_spotting_target(state, nos, target):
                lines.append(line)
        primary_key = self.spotting_primary_forced_key(state, target) if not state.held else ()
        cleanup_lines = self.route_cleanup_lines(state)

        def rank(line: str) -> tuple[int, int, str]:
            if line == target:
                return (0, self.line_rank(line), line)
            if line in cleanup_lines:
                return (1, self.line_rank(line), line)
            if primary_key and self.line_forced_key_blocked(state, line, target, primary_key):
                return (2, self.line_rank(line), line)
            if primary_key and self.line_has_forced_key(state, line, target, primary_key):
                return (3, self.line_rank(line), line)
            return (4, self.line_rank(line), line)

        return tuple(sorted(dict.fromkeys(lines), key=rank))

    def line_relevant_for_spotting_target(self, state: State, nos: tuple[str, ...], target: str) -> bool:
        debt = self.debt_nos_in_state(state)
        held_key = self.spotting_held_force_key(state)
        if held_key:
            return any(
                no in debt
                and self.target_by_no.get(no) == target
                and self.spotting_force_key(no) == held_key
                for no in nos
            )
        return any(no in debt and self.target_by_no.get(no) == target for no in nos)

    def spotting_nonforced_remaining_after_move(self, state: State, target: str, moving: set[str]) -> bool:
        debt = self.debt_nos_in_state(state)
        for _line, nos in self.line_map(state).items():
            for no in nos:
                if no in moving:
                    continue
                if self.target_by_no.get(no) != target:
                    continue
                if no not in self.active_nos or no not in debt:
                    continue
                if not physical.force_positions(self.meta.get(no, {})):
                    return True
        return False

    def spotting_forced_remaining_after_move(self, state: State, target: str, moving: set[str]) -> bool:
        held = set(state.held)
        debt = self.debt_nos_in_state(state)
        for _line, nos in self.line_map(state).items():
            for no in nos:
                if no in held or no in moving:
                    continue
                if self.target_by_no.get(no) != target or no not in self.active_nos or no not in debt:
                    continue
                if self.spotting_force_key(no):
                    return True
        return False

    def spotting_force_key(self, no: str) -> tuple[int, ...]:
        return tuple(physical.force_positions(self.meta.get(no, {})))

    def spotting_primary_forced_key(self, state: State, target: str) -> tuple[int, ...]:
        debt = self.debt_nos_in_state(state)
        keys = {
            self.spotting_force_key(no)
            for _line, nos in self.line_map(state).items()
            for no in nos
            if no in self.active_nos and no in debt and self.target_by_no.get(no) == target and self.spotting_force_key(no)
        }
        return min(keys) if keys else ()

    def line_has_forced_key(self, state: State, line: str, target: str, key: tuple[int, ...]) -> bool:
        debt = self.debt_nos_in_state(state)
        return any(
            no in self.active_nos
            and no in debt
            and self.target_by_no.get(no) == target
            and self.spotting_force_key(no) == key
            for no in self.line_map(state).get(line, ())
        )

    def line_forced_key_blocked(self, state: State, line: str, target: str, key: tuple[int, ...]) -> bool:
        debt = self.debt_nos_in_state(state)
        for index, no in enumerate(self.line_map(state).get(line, ())):
            if (
                no in self.active_nos
                and no in debt
                and self.target_by_no.get(no) == target
                and self.spotting_force_key(no) == key
            ):
                return index > 0
        return False

    def spotting_held_force_key(self, state: State) -> tuple[int, ...]:
        if not state.held:
            return ()
        keys = {self.spotting_force_key(no) for no in state.held}
        if len(keys) != 1:
            return ()
        return next(iter(keys))

    def spotting_target_key_remaining_on_lines(self, state: State, target: str, key: tuple[int, ...]) -> bool:
        held = set(state.held)
        debt = self.debt_nos_in_state(state)
        for _line, nos in self.line_map(state).items():
            for no in nos:
                if no in held:
                    continue
                if (
                    no in self.active_nos
                    and no in debt
                    and self.target_by_no.get(no) == target
                    and self.spotting_force_key(no) == key
                ):
                    return True
        return False

    def best_cache_put(
        self,
        state: State,
        move: tuple[str, ...],
        *,
        avoid: set[State] | None = None,
    ) -> tuple[Op, State] | None:
        avoid = avoid or set()
        options: list[tuple[tuple[int, int, int, str], Op, State]] = []
        for line in self.safe_cache_lines(state, move):
            if line in state.loco:
                continue
            op, next_state, reject = self.apply_put(state, line, move, note="cache")
            if reject:
                continue
            if next_state in avoid:
                continue
            options.append(((len(self.route_cleanup_lines(next_state)), len(op.path), self.line_rank(line), line), op, next_state))
        if not options:
            return None
        _rank, op, next_state = min(options, key=lambda item: item[0])
        return op, next_state

    def line_rank(self, line: str) -> int:
        if line in CACHE_LINES:
            return CACHE_LINES.index(line)
        if line in physical.TRACK_SPECS:
            return 100 + sorted(physical.TRACK_SPECS).index(line)
        return 999

    def target_group_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            target = self.common_target(state.held)
            if target and self.service_target_put_allowed(state, target):
                yield from self.extend_target_group_macros(
                    origin=state,
                    state=state,
                    ops=(),
                    target=target,
                    depth=0,
                    seen={state},
                )
            return

        yielded: set[tuple[str, tuple[str, ...]]] = set()
        for allow_repair in (False, True):
            for line, move in self.get_prefixes(state, allow_repair=allow_repair):
                target = self.common_target(move)
                if not target:
                    continue
                if not self.service_target_put_allowed(state, target):
                    continue
                key = (line, move)
                if key in yielded:
                    continue
                yielded.add(key)
                get_op, after_get, reject = self.apply_get(state, line, move)
                if reject:
                    continue
                yield from self.extend_target_group_macros(
                    origin=state,
                    state=after_get,
                    ops=(get_op,),
                    target=target,
                    depth=1,
                    seen={state, after_get},
                )
                if len(yielded) >= MAX_TARGET_GROUP_BRANCHES:
                    return

    def extend_target_group_macros(
        self,
        *,
        origin: State,
        state: State,
        ops: tuple[Op, ...],
        target: str,
        depth: int,
        seen: set[State],
    ) -> Iterable[MacroCandidate]:
        if not state.held or self.common_target(state.held) != target:
            return
        if not self.service_target_put_allowed(state, target):
            return

        put_op, after_put, put_reject = self.apply_put(state, target, state.held, note="target")
        if not put_reject:
            yield self.make_macro_candidate(
                origin,
                (*ops, put_op),
                after_put,
                reason_rank=0,
                reason="macro_target_group_put",
            )

        if depth >= MAX_TARGET_GROUP_GETS:
            return

        yielded: set[tuple[str, tuple[str, ...]]] = set()
        for allow_repair in (False, True):
            for line, move in self.get_prefixes(state, allow_repair=allow_repair):
                if self.common_target(move) != target:
                    continue
                key = (line, move)
                if key in yielded:
                    continue
                yielded.add(key)
                get_op, after_get, reject = self.apply_get(state, line, move)
                if reject or after_get in seen:
                    continue
                yield from self.extend_target_group_macros(
                    origin=origin,
                    state=after_get,
                    ops=(*ops, get_op),
                    target=target,
                    depth=depth + 1,
                    seen={*seen, after_get},
                )
                if len(yielded) >= MAX_TARGET_GROUP_BRANCHES:
                    return

    def macro_empty_train_candidates(self, state: State) -> Iterable[MacroCandidate]:
        yielded_gets: set[tuple[str, tuple[str, ...]]] = set()
        must_move_active = self.must_move_nos_in_state(state, self.active_nos)
        blocker_lines = self.macro_blocker_lines(state)
        for allow_repair in (False, True):
            for line, move in self.get_prefixes(state, allow_repair=allow_repair):
                key = (line, move)
                if key in yielded_gets:
                    continue
                yielded_gets.add(key)
                moving = set(move)
                target = self.common_target(move)
                contains_active_debt = bool(moving & must_move_active)
                if target and contains_active_debt:
                    candidate = self.build_get_target_put_macro(
                        state,
                        line,
                        move,
                        reason_rank=0,
                        reason="macro_direct_target",
                    )
                    if candidate:
                        yield candidate
                    hold = self.build_get_hold_for_route_macro(state, line, move)
                    if hold:
                        yield hold
                    if candidate is None and self.should_cache_for_spotting_order_release(state, line, move, target):
                        yield from self.build_get_cache_macros(
                            state,
                            line,
                            move,
                            reason_rank=1,
                            reason="macro_spotting_order_release",
                        )
                if line in blocker_lines:
                    yield from self.build_get_cache_macros(
                        state,
                        line,
                        move,
                        reason_rank=2,
                        reason="macro_clear_route_blocker",
                    )

    def macro_held_candidates(self, state: State) -> Iterable[MacroCandidate]:
        cleanup_lines = self.macro_blocker_lines(state)
        weigh = self.weigh_candidate(state)
        if weigh is not None:
            op, next_state, reject = weigh
            if not reject:
                yield self.make_macro_candidate(state, (op,), next_state, 0, "macro_weigh")

        for line, move, note in self.put_suffixes(state, include_cache=False):
            if note == "target" and not self.service_target_put_allowed(state, line):
                continue
            op, next_state, reject = self.apply_put(state, line, move, note=note)
            if reject:
                continue
            if line in cleanup_lines and not self.complete(next_state):
                continue
            yield self.make_macro_candidate(state, (op,), next_state, 1, "macro_target_put")

        if cleanup_lines:
            yielded_gets: set[tuple[str, tuple[str, ...]]] = set()
            for line, move in self.get_prefixes(state, allow_repair=True):
                if line not in cleanup_lines:
                    continue
                key = (line, move)
                if key in yielded_gets:
                    continue
                yielded_gets.add(key)
                yield from self.build_get_cache_macros(
                    state,
                    line,
                    move,
                    reason_rank=2,
                    reason="macro_clear_held_route_blocker",
                )

        for line, move, note in self.put_suffixes(state, include_cache=True):
            if note != "cache":
                continue
            if move != state.held:
                continue
            op, next_state, reject = self.apply_put(state, line, move, note=note)
            if reject:
                continue
            yield self.make_macro_candidate(state, (op,), next_state, 4, "macro_cache_held")

    def build_get_target_put_macro(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
        *,
        reason_rank: int,
        reason: str,
    ) -> MacroCandidate | None:
        get_op, after_get, reject = self.apply_get(state, line, move)
        if reject:
            return None
        ops: list[Op] = [get_op]
        after_weigh = after_get
        weigh = self.weigh_candidate(after_weigh)
        if weigh is not None:
            weigh_op, after_weigh, reject = weigh
            if reject:
                return None
            ops.append(weigh_op)
        target = self.common_target(after_weigh.held)
        if not target:
            return None
        if not self.service_target_put_allowed(after_weigh, target):
            return None
        put_op, after_put, reject = self.apply_put(after_weigh, target, after_weigh.held, note="target")
        if reject:
            return None
        if target in self.route_cleanup_lines(state) and not self.complete(after_put):
            return None
        ops.append(put_op)
        return self.make_macro_candidate(state, tuple(ops), after_put, reason_rank, reason)

    def build_get_hold_for_route_macro(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
    ) -> MacroCandidate | None:
        get_op, after_get, reject = self.apply_get(state, line, move)
        if reject:
            return None
        target = self.common_target(after_get.held)
        if not target:
            return None
        put_op, _after_put, put_reject = self.apply_put(after_get, target, after_get.held, note="target")
        del put_op
        if not put_reject.startswith("put_route_blocked:"):
            return None
        if not self.route_cleanup_lines(after_get):
            return None
        return self.make_macro_candidate(state, (get_op,), after_get, 3, "macro_hold_for_blocked_target")

    def build_get_cache_macros(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
        *,
        reason_rank: int,
        reason: str,
    ) -> Iterable[MacroCandidate]:
        get_op, after_get, reject = self.apply_get(state, line, move)
        if reject:
            return
        cache_lines = self.leaf_cache_lines(after_get, move)
        for cache_line in cache_lines:
            if cache_line in after_get.loco:
                continue
            put_op, after_put, put_reject = self.apply_put(after_get, cache_line, move, note="cache")
            if put_reject:
                continue
            yield self.make_macro_candidate(state, (get_op, put_op), after_put, reason_rank, reason)

    def make_macro_candidate(
        self,
        prior: State,
        ops: tuple[Op, ...],
        next_state: State,
        reason_rank: int,
        reason: str,
    ) -> MacroCandidate:
        prior_debt = len(self.debt_nos_in_state(prior))
        next_debt = len(self.debt_nos_in_state(next_state))
        prior_must = len(self.must_move_nos_in_state(prior, self.managed_nos))
        next_must = len(self.must_move_nos_in_state(next_state, self.managed_nos))
        prior_cleanup_lines = self.macro_blocker_lines(prior)
        next_cleanup_lines = self.macro_blocker_lines(next_state)
        prior_cleanup = len(prior_cleanup_lines)
        next_cleanup = len(next_cleanup_lines)
        new_cleanup = len(next_cleanup_lines - prior_cleanup_lines)
        progress_rank = 0
        if reason.startswith("macro_clear") and next_cleanup < prior_cleanup:
            progress_rank = 1
        elif next_debt < prior_debt:
            progress_rank = 0
        elif next_must < prior_must:
            progress_rank = 2
        elif reason.startswith("macro_clear"):
            progress_rank = 4
        elif reason == "macro_hold_for_blocked_target":
            progress_rank = 3
        else:
            progress_rank = 5
        cost = self.ops_cost(ops)
        target_put_cars = sum(len(op.move) for op in ops if op.action == "Put" and op.note == "target")
        max_move = max((len(op.move) for op in ops), default=0)
        cache_puts = sum(1 for op in ops if op.note == "cache")
        score = (
            progress_rank,
            next_debt,
            next_must,
            self.unsatisfied_count(next_state),
            reason_rank,
            new_cleanup,
            next_cleanup,
            cache_puts,
            cost[0],
            cost[1],
            cost[2],
            -max_move,
            -target_put_cars,
            reason,
        )
        return MacroCandidate(ops=ops, state=next_state, score=score, reason=reason)

    def should_cache_for_spotting_order_release(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
        target: str,
    ) -> bool:
        if not physical.is_spotting_line(target):
            return False
        if not any(physical.force_positions(self.meta[no]) for no in move if no in self.meta):
            return False
        line_nos = self.line_map(state).get(line, ())
        move_set = set(move)
        seen_move = False
        for no in line_nos:
            if no in move_set:
                seen_move = True
                continue
            if not seen_move:
                continue
            if self.target_by_no.get(no) == target and no in self.active_nos:
                return True
        return False

    def greedy_upper_bound(self, start: State) -> tuple[State | None, tuple[Op, ...]]:
        state = start
        ops: list[Op] = []
        seen: set[State] = set()
        for _step in range(MAX_GREEDY_STEPS):
            if self.complete(state):
                return state, tuple(ops)
            if state in seen or self.deadline_reached():
                return None, ()
            seen.add(state)

            chosen = self.greedy_put_or_weigh(state)
            if chosen is None:
                chosen = self.greedy_get(state)
            if chosen is None:
                return None, ()
            op, state = chosen
            ops.append(op)
        return None, ()

    def greedy_put_or_weigh(self, state: State) -> tuple[Op, State] | None:
        if state.held:
            weigh = self.weigh_candidate(state)
            if weigh is not None:
                op, next_state, reject = weigh
                if not reject:
                    return op, next_state
            puts = []
            seq = 0
        for line, move, note in self.put_suffixes(state, include_cache=False):
            if note == "target" and not self.service_target_put_allowed(state, line):
                continue
            op, next_state, reject = self.apply_put(state, line, move, note=note)
            if reject:
                continue
                puts.append((self.put_rank(state, op, next_state), seq, op, next_state))
                seq += 1
            if puts:
                _rank, _seq, op, next_state = min(puts)
                return op, next_state
            cleanup_get = self.greedy_get(state, cleanup_only=True)
            if cleanup_get is not None:
                return cleanup_get
            cache_puts = []
            seq = 0
            for line, move, note in self.put_suffixes(state, include_cache=True):
                if note != "cache":
                    continue
                op, next_state, reject = self.apply_put(state, line, move, note=note)
                if reject:
                    continue
                cache_puts.append((self.put_rank(state, op, next_state), seq, op, next_state))
                seq += 1
            if cache_puts:
                _rank, _seq, op, next_state = min(cache_puts)
                return op, next_state
        return None

    def greedy_get(self, state: State, *, cleanup_only: bool = False) -> tuple[Op, State] | None:
        gets = []
        seq = 0
        cleanup_lines = self.route_cleanup_lines(state) if cleanup_only else set()
        for line, move in self.get_prefixes(state, allow_repair=False):
            if cleanup_only and line not in cleanup_lines:
                continue
            op, next_state, reject = self.apply_get(state, line, move)
            if reject:
                continue
            gets.append((self.get_rank(state, op, next_state), seq, op, next_state))
            seq += 1
        if not gets:
            for line, move in self.get_prefixes(state, allow_repair=True):
                if cleanup_only and line not in cleanup_lines:
                    continue
                op, next_state, reject = self.apply_get(state, line, move)
                if reject:
                    continue
                gets.append((self.get_rank(state, op, next_state), seq, op, next_state))
                seq += 1
        if not gets:
            return None
        _rank, _seq, op, next_state = min(gets)
        return op, next_state

    def neighbors(self, state: State) -> Iterable[tuple[Op, State, str]]:
        if state.held:
            yielded_target_put = False
            weigh = self.weigh_candidate(state)
            if weigh is not None:
                op, next_state, reject = weigh
                yield op, next_state, reject
            for line, move, note in self.put_suffixes(state, include_cache=False):
                result = self.apply_put(state, line, move, note=note)
                if not result[2]:
                    yielded_target_put = True
                yield result
            if self.repair_nos or not yielded_target_put:
                for line, move, note in self.put_suffixes(state, include_cache=True):
                    if note != "cache":
                        continue
                    yield self.apply_put(state, line, move, note=note)
        yielded_gets: set[tuple[str, tuple[str, ...]]] = set()
        for line, move in self.get_prefixes(state, allow_repair=False):
            yielded_gets.add((line, move))
            yield self.apply_get(state, line, move)
        for line, move in self.get_prefixes(state, allow_repair=True):
            if (line, move) in yielded_gets:
                continue
            yield self.apply_get(state, line, move)

    def get_prefixes(self, state: State, *, allow_repair: bool) -> Iterable[tuple[str, tuple[str, ...]]]:
        cars = self.cars_from_state(state)
        managed = set(self.managed_nos if allow_repair else self.active_nos)
        held_set = set(state.held)
        debt = self.must_move_nos_in_state(state, managed)
        cleanup_lines = self.cleanup_candidate_lines(state) if allow_repair else set()
        for line in self.ordered_get_lines(state):
            full_order = physical.line_access_order(cars, line, held_set)
            prefix: list[str] = []
            seen_debt = False
            for no in full_order:
                if no not in managed:
                    break
                is_debt = no in debt
                if seen_debt and not is_debt:
                    break
                trial = (*state.held, *prefix, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                prefix.append(no)
                if is_debt:
                    seen_debt = True
            if not prefix:
                continue
            if not seen_debt and line not in cleanup_lines:
                continue
            for cut in self.useful_get_cuts(prefix):
                yield line, tuple(prefix[:cut])

    def ordered_get_lines(self, state: State) -> tuple[str, ...]:
        line_map = self.line_map(state)
        cleanup_lines = self.cleanup_candidate_lines(state) if self.repair_nos else set()

        def key(line: str) -> tuple[int, int, int, str]:
            nos = line_map.get(line, ())
            targets = [self.target_by_no.get(no, "") for no in nos]
            target_changes = sum(1 for left, right in zip(targets, targets[1:]) if left != right)
            ready_front = 0
            if nos and self.target_ready(state, self.target_by_no.get(nos[0], "")):
                ready_front = -1
            cleanup_rank = 0 if line in cleanup_lines else 1
            store4_release = 0 if line == STORE4 else 1
            return (cleanup_rank, ready_front, store4_release, target_changes, line)

        return tuple(sorted((line for line, nos in line_map.items() if nos), key=key))

    def useful_get_cuts(self, prefix: list[str]) -> tuple[int, ...]:
        cuts = {len(prefix)}
        for index in range(1, len(prefix)):
            left = prefix[index - 1]
            right = prefix[index]
            if self.target_by_no.get(left) != self.target_by_no.get(right):
                cuts.add(index)
            if self.pending_weigh(left) or self.pending_weigh(right):
                cuts.add(index)
        cuts.add(1)
        return tuple(sorted(cuts, reverse=True))

    def put_suffixes(
        self,
        state: State,
        *,
        include_cache: bool,
    ) -> Iterable[tuple[str, tuple[str, ...], str]]:
        held = state.held
        if not held:
            return
        for start in range(len(held)):
            move = held[start:]
            target = self.common_target(move)
            if target and target not in BLOCKED_LINES:
                if start == 0 and target in state.loco:
                    continue
                if start > 0 and target in state.loco:
                    continue
                yield target, move, "target"
        if not include_cache:
            return
        for start in range(len(held)):
            move = held[start:]
            if not self.cache_suffix_allowed(move):
                continue
            for line in self.safe_cache_lines(state, move):
                if line in state.loco:
                    continue
                yield line, move, "cache"

    def cache_suffix_allowed(self, move: tuple[str, ...]) -> bool:
        return bool(move)

    def safe_cache_lines(self, state: State, move: tuple[str, ...]) -> tuple[str, ...]:
        cars = self.cars_from_state(state)
        unsatisfied = self.unsatisfied_nos(state)
        own_target = self.common_target(move)
        active_targets = {
            self.target_by_no.get(no, "")
            for no in self.active_debt_nos_in_state(state)
            if self.target_by_no.get(no, "")
        }
        out: list[str] = []
        for line in CACHE_LINES:
            if line == own_target:
                continue
            if line in active_targets:
                continue
            if line in HIGH_CONFLICT_CACHE_LINES:
                continue
            if line in BLOCKED_LINES or line in RUNNING_LINES or line == STORE4:
                continue
            if line in physical.DEPOT_TARGET_LINES:
                continue
            if any(car["Line"] == line and rv.car_no(car) not in self.managed_nos for car in cars):
                continue
            if not self.repair_nos and any(car["Line"] == line for car in cars):
                continue
            if self.line_has_debt(cars, line, unsatisfied):
                continue
            if not self.line_has_capacity(state, line, move):
                continue
            out.append(line)
        return tuple(out)

    def common_target(self, nos: tuple[str, ...]) -> str:
        if not nos:
            return ""
        targets = {self.target_by_no.get(no, "") for no in nos}
        if len(targets) != 1:
            return ""
        target = next(iter(targets))
        return target if target else ""

    def target_ready(self, state: State, target: str) -> bool:
        if not target:
            return False
        cache_key = (self.state_projection_key(state), target)
        cached = self.target_ready_cache.get(cache_key)
        if cached is not None:
            return cached
        ready = True
        unsatisfied = self.unsatisfied_nos(state)
        for line, nos in self.line_map(state).items():
            if line != target:
                continue
            for no in nos:
                if self.target_by_no.get(no) != target or self.pending_weigh_in_state(state, no) or no in unsatisfied:
                    ready = False
                    break
            if not ready:
                break
        if len(self.target_ready_cache) < 200_000:
            self.target_ready_cache[cache_key] = ready
        return ready

    def line_has_unmanaged_debt(self, cars: list[dict[str, Any]], line: str, unsatisfied: set[str] | frozenset[str]) -> bool:
        return any(
            car["Line"] == line
            and rv.car_no(car) not in self.managed_nos
            and rv.car_no(car) in unsatisfied
            for car in cars
        )

    def line_has_debt(self, cars: list[dict[str, Any]], line: str, unsatisfied: set[str] | frozenset[str]) -> bool:
        return any(car["Line"] == line and rv.car_no(car) in unsatisfied for car in cars)

    def weigh_candidate(self, state: State) -> tuple[Op, State, str] | None:
        if not state.held:
            return None
        tail = state.held[-1]
        if not self.pending_weigh_in_state(state, tail):
            return None
        return self.apply_weigh(state, tail)

    def apply_get(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        line_map = self.line_map(state)
        if not move:
            return Op("Get", line, move, (), state.held), state, "empty_get"
        cars = self.cars_from_state(state)
        if tuple(physical.line_access_order(cars, line, set(state.held))[: len(move)]) != move:
            return Op("Get", line, move, (), state.held), state, "get_order_violation"
        held_after = (*state.held, *move)
        if self.pull_equivalent(held_after) > physical.PULL_LIMIT_EQUIVALENT:
            return Op("Get", line, move, (), state.held), state, "pull_limit_violation"
        closed_door_reason = self.closed_door_process_reason(held_after)
        if closed_door_reason:
            return Op("Get", line, move, (), state.held), state, closed_door_reason
        route, reject = self.route(state, "Get", line, move)
        if reject:
            return Op("Get", line, move, (), state.held), state, reject
        next_lines = dict(line_map)
        move_set = set(move)
        next_lines[line] = tuple(no for no in next_lines.get(line, ()) if no not in move_set)
        next_positions = self.position_map(state)
        for no in move:
            next_positions.pop(no, None)
        if not self.line_preserves_positions(line):
            for position, no in enumerate(next_lines.get(line, ()), start=1):
                next_positions[no] = position
        next_state = State(
            lines=self.pack_lines_ordered(next_lines, next_positions),
            positions=self.pack_positions(next_positions),
            held=held_after,
            loco=(line,),
            weighed=state.weighed,
        )
        damaged = sorted(self.unsatisfied_nos(next_state) & self.protected_satisfied_nos)
        damage_reason = self.protected_damage_reason(next_state, damaged)
        if damage_reason:
            return Op("Get", line, move, route, state.held), state, damage_reason
        return Op("Get", line, move, route, next_state.held), next_state, ""

    def apply_put(self, state: State, line: str, move: tuple[str, ...], *, note: str) -> tuple[Op, State, str]:
        if not move:
            return Op("Put", line, move, (), state.held, note), state, "empty_put"
        if line in BLOCKED_LINES or line in RUNNING_LINES:
            return Op("Put", line, move, (), state.held, note), state, f"target_blocked:{line}"
        if state.held[-len(move):] != move:
            return Op("Put", line, move, (), state.held, note), state, "put_tail_order_violation"
        if note == "target" and any(self.target_by_no.get(no) != line for no in move):
            return Op("Put", line, move, (), state.held, note), state, "target_put_mismatch"
        if note == "cache" and line == self.common_target(move):
            return Op("Put", line, move, (), state.held, note), state, "cache_to_own_target"
        closed_door_reason = self.closed_door_put_reason(state, line)
        if closed_door_reason:
            return Op("Put", line, move, (), state.held, note), state, closed_door_reason
        if not self.line_has_capacity(state, line, move):
            return Op("Put", line, move, (), state.held, note), state, f"target_capacity_violation:{line}"
        route, reject = self.route(state, "Put", line, move)
        if reject:
            return Op("Put", line, move, (), state.held, note), state, reject
        line_map = dict(self.line_map(state))
        existing = tuple(no for no in line_map.get(line, ()) if no not in set(move))
        cars_before_put = self.cars_from_state(state)
        put_positions = rv.forced_put_positions(cars_before_put, line, list(move))
        if not put_positions:
            put_positions = {no: position for position, no in enumerate((*move, *existing), start=1)}
        next_positions = self.position_map(state)
        for no, position in put_positions.items():
            next_positions[no] = int(position)
        line_map[line] = tuple(
            sorted(
                (*move, *existing),
                key=lambda no: (next_positions.get(no, 0), no),
            )
        )
        move_set = set(move)
        held_after = tuple(no for no in state.held if no not in move_set)
        next_state = State(
            lines=self.pack_lines_ordered(line_map, next_positions),
            positions=self.pack_positions(next_positions),
            held=held_after,
            loco=tuple(sorted(rv.put_loco_positions(list(route), line))),
            weighed=state.weighed,
        )
        projected = self.cars_from_state(next_state)
        if note == "cache" and not self.cache_front_accessible(projected, line, move):
            return Op("Put", line, move, route, state.held, note), state, f"cache_not_front_accessible:{line}"
        projected_unsatisfied = {
            rv.car_no(car)
            for car in physical.unsatisfied_cars(projected, self.depot_assignment)
        }
        if note == "target" and not physical.is_spotting_line(line) and (set(move) & projected_unsatisfied):
            return Op("Put", line, move, route, state.held, note), state, f"target_put_moved_still_unsatisfied:{line}"
        if (
            note == "target"
            and not physical.is_spotting_line(line)
            and len(self.debt_nos_in_state(next_state)) >= len(self.debt_nos_in_state(state))
        ):
            return Op("Put", line, move, route, state.held, note), state, "target_put_no_progress"
        damaged = sorted(projected_unsatisfied & self.protected_satisfied_nos)
        damage_reason = self.protected_damage_reason(next_state, damaged)
        if damage_reason:
            return Op("Put", line, move, route, state.held, note), state, damage_reason
        return Op("Put", line, move, route, next_state.held, note), next_state, ""

    def cache_front_accessible(self, cars: list[dict[str, Any]], line: str, move: tuple[str, ...]) -> bool:
        if not move:
            return False
        ordered = tuple(physical.line_access_order(cars, line, set()))
        return ordered[: len(move)] == move

    def apply_weigh(self, state: State, no: str) -> tuple[Op, State, str]:
        if not state.held or state.held[-1] != no:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, "weigh_car_not_tail"
        closed_door_reason = self.closed_door_process_reason(state.held)
        if closed_door_reason:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, closed_door_reason
        cars = self.cars_from_state(state)
        blockers = [
            rv.car_no(car)
            for car in cars
            if car["Line"] == WEIGH_LINE and rv.car_no(car) not in set(state.held)
        ]
        if blockers:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, "weigh_line_not_empty"
        if self.length(state.held) + physical.LOCO_LENGTH_M > rv.TRACK_LEN[WEIGH_LINE] + rv.TOL:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, "weigh_line_length_violation"
        route, reject = self.route(state, "Weigh", WEIGH_LINE, (no,))
        if reject:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, reject
        next_weighed = tuple(sorted({*state.weighed, no}))
        next_state = State(
            lines=state.lines,
            positions=state.positions,
            held=state.held,
            loco=(WEIGH_LINE,),
            weighed=next_weighed,
        )
        return Op("Weigh", WEIGH_LINE, (no,), route, state.held, "weigh"), next_state, ""

    def route(self, state: State, action: str, line: str, move: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
        cars = self.cars_from_state(state)
        if action == "Get":
            moving = set(state.held) | set(move)
            train_len = self.length(state.held)
            target_approach = physical.route_approach_lines_for_get(line)
        else:
            moving = set(state.held)
            train_len = self.length(state.held)
            target_approach = physical.route_approach_lines_for_put(line, cars, moving)
        occupied = physical.occupied_lines_for_route(cars, moving) | BLOCKED_LINES
        train_bucket = round(train_len, 1)
        cache_key = (
            action,
            tuple(sorted(state.loco)),
            line,
            tuple(sorted(occupied)),
            self.occupied_length_signature(cars, moving, occupied),
            tuple(
                tuple(sorted(physical.route_departure_lines_for_source(start, cars, moving)))
                for start in state.loco
            ),
            tuple(sorted(target_approach)),
            train_bucket,
        )
        cached = self.route_cache.get(cache_key)
        if cached is not None:
            return cached
        choices: list[tuple[int, tuple[str, ...]]] = []
        blockers: list[str] = []
        for start in state.loco:
            path = self.graph.route_avoiding_occupied(
                start,
                line,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(start, cars, moving),
                target_approach_lines=target_approach,
                cars=cars,
                moving_nos=moving,
                train_length_m=train_len,
            )
            if path and not (set(path) & BLOCKED_LINES):
                choices.append((len(path), tuple(path)))
            else:
                blockers.append(f"{start}->{line}")
        if not choices:
            result = ((), f"{action.lower()}_route_blocked:{blockers[0] if blockers else line}")
            if len(self.route_cache) < 200_000:
                self.route_cache[cache_key] = result
            return result
        result = (min(choices, key=lambda item: (item[0], item[1]))[1], "")
        if len(self.route_cache) < 200_000:
            self.route_cache[cache_key] = result
        return result

    def complete(self, state: State) -> bool:
        if state.held:
            return False
        unsatisfied = self.unsatisfied_nos(state)
        return (
            not (unsatisfied & self.active_nos)
            and not (unsatisfied & self.protected_satisfied_nos)
            and all(not self.pending_weigh_in_state(state, no) for no in self.active_nos)
            and not self.closed_door_store4_final_reason(state)
        )

    def active_satisfied_in_state(self, state: State, no: str) -> bool:
        return no not in self.unsatisfied_nos(state)

    def pending_weigh(self, no: str) -> bool:
        car = self.meta.get(no, {})
        return bool(car.get("IsWeigh")) and not bool(car.get("_Weighed"))

    def pending_weigh_in_state(self, state: State, no: str) -> bool:
        car = self.meta.get(no, {})
        if not car.get("IsWeigh"):
            return False
        return no not in set(state.weighed)

    def lower_bound(self, state: State) -> int:
        key = self.state_projection_key(state)
        cached = self.lower_bound_cache.get(key)
        if cached is not None:
            return cached
        line_map = self.line_map(state)
        debt = self.debt_nos_in_state(state)
        get_lines: set[str] = set()
        put_targets: set[str] = set()
        held_set = set(state.held)
        for no in self.managed_nos:
            target = self.target_by_no.get(no, "")
            if no in held_set:
                if target:
                    put_targets.add(target)
                continue
            if no not in debt:
                continue
            line = self.current_line(line_map, no)
            must_move = line != target or self.pending_weigh_in_state(state, no)
            if not must_move:
                continue
            if line:
                get_lines.add(line)
            if target:
                put_targets.add(target)
        result = len(get_lines) + len(put_targets)
        self.lower_bound_cache[key] = result
        return result

    def unsatisfied_nos(self, state: State) -> frozenset[str]:
        key = self.state_projection_key(state)
        cached = self.unsatisfied_cache.get(key)
        if cached is not None:
            return cached
        projected = self.cars_from_state(state)
        result = frozenset(rv.car_no(car) for car in physical.unsatisfied_cars(projected, self.depot_assignment))
        if len(self.unsatisfied_cache) < 200_000:
            self.unsatisfied_cache[key] = result
        return result

    def bound_tuple(self, cost: tuple[int, int, int, int], state: State) -> tuple[int, int, int, int]:
        return (cost[0] + self.lower_bound(state), cost[1], cost[2], cost[3])

    def priority(self, cost: tuple[int, int, int, int], state: State) -> tuple[int, int, int, int, int, int]:
        return (
            cost[0] + self.lower_bound(state),
            self.unsatisfied_count(state),
            -len(state.held),
            cost[1],
            cost[2],
            cost[3],
        )

    def unsatisfied_count(self, state: State) -> int:
        held = set(state.held)
        return sum(
            1
            for no in self.managed_nos
            if no not in held and not self.active_satisfied_in_state(state, no)
        ) + len(held)

    def reconstruct(self, prev: dict[State, tuple[State, Op]], state: State) -> tuple[Op, ...]:
        ops: list[Op] = []
        while state in prev:
            prior, op = prev[state]
            ops.append(op)
            state = prior
        ops.reverse()
        return tuple(ops)

    def put_rank(self, state: State, op: Op, next_state: State) -> tuple[int, int, int, str]:
        del next_state
        note_rank = 0 if op.note == "target" else 5
        ready_rank = 0 if op.note != "target" or self.target_ready(state, op.line) else 2
        return (note_rank + ready_rank, len(op.path), -len(op.move), op.line)

    def get_rank(self, state: State, op: Op, next_state: State) -> tuple[int, int, int, str]:
        del next_state
        target = self.target_by_no.get(op.move[0], "") if op.move else ""
        ready_rank = 0 if self.target_ready(state, target) else 1
        store4_release = 0 if op.line == STORE4 else 1
        return (ready_rank, store4_release, len(op.path), op.line)

    def closed_door_process_reason(self, held: tuple[str, ...]) -> str:
        if not held:
            return ""
        train = [self.meta[no] for no in held if no in self.meta]
        first = train[0] if train else {}
        if first.get("IsClosedDoor") and (len(train) > 10 or any(car.get("IsHeavy") for car in train)):
            return f"closed_door_process_violation:{rv.car_no(first)}"
        return ""

    def closed_door_put_reason(self, state: State, target_line: str) -> str:
        if not state.held:
            return ""
        if target_line == STORE4:
            return ""
        return self.closed_door_process_reason(state.held)

    def closed_door_store4_final_reason(self, state: State) -> str:
        cars = self.cars_from_state(state)
        for car in cars:
            if car["Line"] == STORE4 and car.get("IsClosedDoor") and int(car.get("Position") or 0) <= 3:
                return f"closed_door_store4_front_violation:{rv.car_no(car)}:{int(car.get('Position') or 0)}"
        return ""

    def protected_damage_reason(self, state: State, damaged: list[str]) -> str:
        if not damaged:
            return ""
        by_no = {rv.car_no(car): car for car in self.cars_from_state(state)}
        for no in damaged:
            if no in self.repair_nos:
                continue
            car = by_no.get(no)
            if self.is_transient_store4_closed_door_violation(car):
                continue
            return f"fixed_satisfied_car_damaged:{no}"
        return ""

    def is_transient_store4_closed_door_violation(self, car: dict[str, Any] | None) -> bool:
        return bool(
            car
            and car.get("Line") == STORE4
            and car.get("IsClosedDoor")
            and int(car.get("Position") or 0) <= 3
        )

    def line_has_capacity(self, state: State, line: str, move: tuple[str, ...]) -> bool:
        limit = rv.TRACK_LEN.get(line)
        if limit is None:
            return False
        line_map = self.line_map(state)
        active_existing = sum(self.car_length(no) for no in line_map.get(line, ()))
        fixed_existing = sum(
            float(car.get("Length") or CAR_LENGTH_DEFAULT_M)
            for car in self.fixed_cars
            if car["Line"] == line
        )
        return active_existing + fixed_existing + self.length(move) <= limit + rv.TOL

    def cars_from_state(self, state: State) -> list[dict[str, Any]]:
        key = self.state_projection_key(state)
        cached = self.cars_cache.get(key)
        if cached is not None:
            return [dict(row) for row in cached]
        rows: list[dict[str, Any]] = []
        weighed = set(state.weighed)
        positions = self.position_map(state)
        by_line: dict[str, list[dict[str, Any]]] = {}
        for line, nos in state.lines:
            for fallback_position, no in enumerate(nos, start=1):
                car = dict(self.meta[no])
                car["Line"] = line
                car["Position"] = int(positions.get(no) or fallback_position)
                if no in weighed:
                    car["_Weighed"] = True
                by_line.setdefault(line, []).append(car)
        for fixed in sorted(self.fixed_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item))):
            car = dict(fixed)
            if rv.car_no(car) in weighed:
                car["_Weighed"] = True
            by_line.setdefault(car["Line"], []).append(car)
        for line in sorted(by_line):
            for fallback_position, car in enumerate(
                sorted(by_line[line], key=lambda item: (int(item.get("Position") or 0), rv.car_no(item))),
                start=1,
            ):
                car["Line"] = line
                if not self.line_preserves_positions(line):
                    car["Position"] = fallback_position
                rows.append(car)
        for no in state.held:
            car = dict(self.meta[no])
            car["Line"] = ""
            car["Position"] = 0
            if no in weighed:
                car["_Weighed"] = True
            rows.append(car)
        if len(self.cars_cache) < 200_000:
            self.cars_cache[key] = tuple(dict(row) for row in rows)
        return rows

    def state_projection_key(self, state: State) -> tuple[Any, ...]:
        return (state.lines, state.positions, state.held, state.weighed)

    def occupied_length_signature(
        self,
        cars: list[dict[str, Any]],
        moving_nos: set[str],
        occupied_lines: set[str],
    ) -> tuple[tuple[str, float], ...]:
        relevant_lines = occupied_lines & ROUTE_LENGTH_SIGNATURE_LINES
        return tuple(
            sorted(
                (
                    line,
                    round(
                        sum(
                            physical.car_length(car)
                            for car in cars
                            if car["Line"] == line and rv.car_no(car) not in moving_nos
                        ),
                        1,
                    ),
                )
                for line in relevant_lines
            )
        )

    def debt_nos_in_state(self, state: State) -> set[str]:
        debt = set(self.unsatisfied_nos(state) & self.managed_nos)
        debt.update(no for no in self.active_nos if self.pending_weigh_in_state(state, no))
        return debt

    def active_debt_nos_in_state(self, state: State) -> set[str]:
        debt = set(self.unsatisfied_nos(state) & self.active_nos)
        debt.update(no for no in self.active_nos if self.pending_weigh_in_state(state, no))
        return debt

    def must_move_nos_in_state(self, state: State, scope: set[str]) -> set[str]:
        line_map = self.line_map(state)
        held = set(state.held)
        out: set[str] = set()
        for no in scope:
            target = self.target_by_no.get(no, "")
            if not target:
                continue
            if no in held:
                out.add(no)
                continue
            line = self.current_line(line_map, no)
            if line != target or self.pending_weigh_in_state(state, no):
                out.add(no)
        return out

    def route_cleanup_lines(self, state: State) -> set[str]:
        key = (*self.state_projection_key(state), state.loco)
        cached = self.route_cleanup_cache.get(key)
        if cached is not None:
            return set(cached)
        line_map = self.line_map(state)
        cleanup: set[str] = set()
        for line, _reason in self.route_blocker_requests(state):
            if self.cleanup_line_allowed(state, line, line_map):
                cleanup.add(line)
        if len(self.route_cleanup_cache) < 200_000:
            self.route_cleanup_cache[key] = frozenset(cleanup)
        return cleanup

    def cleanup_candidate_lines(self, state: State) -> set[str]:
        cleanup = set(self.route_cleanup_lines(state))
        if self.repair_nos:
            cleanup.update(self.target_release_lines(state))
        return cleanup

    def macro_blocker_lines(self, state: State) -> set[str]:
        return self.route_cleanup_lines(state) | self.target_release_lines(state)

    def target_release_lines(self, state: State) -> set[str]:
        line_map = self.line_map(state)
        targets: set[str] = set()
        for no in self.must_move_nos_in_state(state, self.active_nos):
            if no in set(state.held):
                continue
            target = self.target_by_no.get(no, "")
            if target and target not in RUNNING_LINES and target not in OUT_OF_SCOPE_STRATEGY_LINES:
                targets.add(target)
        if state.held:
            target = self.common_target(state.held)
            if target:
                targets.add(target)
        release: set[str] = set()
        for target in targets:
            nos = line_map.get(target, ())
            if nos and any(no in self.managed_nos for no in nos):
                release.add(target)
        return release

    def cleanup_line_allowed(
        self,
        state: State,
        line: str,
        line_map: dict[str, tuple[str, ...]] | None = None,
    ) -> bool:
        del state
        line_map = line_map or {}
        if line in BLOCKED_LINES or line in RUNNING_LINES or line in OUT_OF_SCOPE_STRATEGY_LINES:
            return False
        if line not in rv.TRACK_LEN:
            return False
        return any(no in self.managed_nos for no in line_map.get(line, ()))

    def route_blocker_requests(self, state: State) -> tuple[tuple[str, str], ...]:
        line_map = self.line_map(state)
        held_set = set(state.held)
        debt = self.must_move_nos_in_state(state, self.active_nos)
        requests: list[tuple[str, str]] = []
        seen_requests: set[tuple[str, str]] = set()

        def add(blocker_line: str, reason: str) -> None:
            if blocker_line in BLOCKED_LINES or blocker_line in RUNNING_LINES or blocker_line in OUT_OF_SCOPE_STRATEGY_LINES:
                return
            if not blocker_line or blocker_line not in rv.TRACK_LEN:
                return
            key = (blocker_line, reason)
            if key in seen_requests:
                return
            seen_requests.add(key)
            requests.append(key)

        if state.held:
            targets = {
                self.common_target(state.held[start:])
                for start in range(len(state.held))
            }
            for target in sorted(target for target in targets if target):
                for blocker_line in self.route_blockers_for_put_from_loco(state, target, set(state.held)):
                    add(blocker_line, f"clear_put_route_blocker_for_held:{target}")

        seeds: list[tuple[str, set[str], str, int]] = []
        for no in sorted(debt):
            if no in held_set:
                continue
            source = self.current_line(line_map, no)
            if not source:
                continue
            seeds.append((source, {no}, f"clear_get_route_blocker_for:{no}", 0))
            target = self.target_by_no.get(no, "")
            if target:
                for blocker_line in self.route_blockers_for_put_from_source(state, source, target, {no}):
                    if blocker_line != source:
                        add(blocker_line, f"clear_put_route_blocker_for:{no}")

        seen_seeds: set[tuple[str, tuple[str, ...], int]] = set()
        while seeds:
            source, moving_nos, reason, depth = seeds.pop(0)
            seed_key = (source, tuple(sorted(moving_nos)), depth)
            if seed_key in seen_seeds:
                continue
            seen_seeds.add(seed_key)
            for blocker_line in self.route_blockers_for_get(state, source, moving_nos):
                if blocker_line == source:
                    continue
                add(blocker_line, reason)
                if depth >= 1:
                    continue
                blocker_nos = tuple(no for no in line_map.get(blocker_line, ())[:1] if no in self.managed_nos)
                if blocker_nos:
                    seeds.append((blocker_line, set(blocker_nos), f"clear_get_route_blocker_for:{source}", depth + 1))
        return tuple(requests)

    def route_blockers_for_get(self, state: State, line: str, moving_nos: set[str]) -> tuple[str, ...]:
        blockers, _status = self.route_blocker_status_for_get(state, line, moving_nos)
        return blockers

    def route_blocker_status_for_get(
        self,
        state: State,
        line: str,
        moving_nos: set[str],
    ) -> tuple[tuple[str, ...], str]:
        cars = self.cars_from_state(state)
        moving = set(state.held) | set(moving_nos)
        occupied = physical.occupied_lines_for_get_route(cars, moving, line) | BLOCKED_LINES
        train_length = self.length(state.held)
        for start in state.loco:
            available = self.graph.route_avoiding_occupied(
                start,
                line,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(start, cars, moving),
                target_approach_lines=physical.route_approach_lines_for_get(line),
                cars=cars,
                moving_nos=moving,
                train_length_m=train_length,
            )
            if available and not (set(available) & BLOCKED_LINES):
                return (), "open"

        endpoints = {line, *state.loco}
        candidate_paths = self.get_static_access_paths(state, line)
        if not candidate_paths:
            return (), "missing_static_path"
        blocked_paths: list[tuple[int, int, list[str]]] = []
        for path in candidate_paths:
            blockers = [node for node in path if node in occupied and node not in endpoints]
            if blockers:
                blocked_paths.append((len(blockers), len(path), blockers))
        if not blocked_paths:
            return (), "non_occupancy_route_blocked"
        _count, _length, blockers = min(blocked_paths)
        return tuple(dict.fromkeys(blockers)), "occupied_blockers"

    def route_blockers_for_put_from_loco(
        self,
        state: State,
        target: str,
        moving_nos: set[str],
    ) -> tuple[str, ...]:
        return self.route_blockers_for_put(state, tuple(state.loco), target, moving_nos)

    def route_blockers_for_put_from_source(
        self,
        state: State,
        source: str,
        target: str,
        moving_nos: set[str],
    ) -> tuple[str, ...]:
        return self.route_blockers_for_put(state, (source,), target, moving_nos)

    def route_blockers_for_put(
        self,
        state: State,
        starts: tuple[str, ...],
        target: str,
        moving_nos: set[str],
    ) -> tuple[str, ...]:
        if not target:
            return ()
        cars = self.cars_from_state(state)
        occupied = physical.occupied_lines_for_route(cars, moving_nos) | BLOCKED_LINES
        train_length = self.length(moving_nos)
        target_approach = physical.route_approach_lines_for_put(target, cars, moving_nos)
        for start in starts:
            available = self.graph.route_avoiding_occupied(
                start,
                target,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(start, cars, moving_nos),
                target_approach_lines=target_approach,
                cars=cars,
                moving_nos=moving_nos,
                train_length_m=train_length,
            )
            if available and not (set(available) & BLOCKED_LINES):
                return ()

        endpoints = {*starts, target}
        blocked_paths: list[tuple[int, int, list[str]]] = []
        for start in starts:
            for path in self.get_static_put_paths(state, start, target):
                blockers = [node for node in path if node in occupied and node not in endpoints]
                if blockers:
                    blocked_paths.append((len(blockers), len(path), blockers))
        if not blocked_paths:
            return ()
        _count, _length, blockers = min(blocked_paths)
        return tuple(dict.fromkeys(blockers))

    def get_static_access_paths(self, state: State, line: str) -> list[list[str]]:
        approaches = sorted(physical.route_approach_lines_for_get(line))
        paths: list[list[str]] = []
        for start in state.loco:
            if not approaches:
                route = self.graph.route(start, line)
                if route:
                    paths.append(route)
                continue
            for approach in approaches:
                if approach == start:
                    paths.append([start, line])
                    continue
                route = self.graph.route_avoiding_occupied(start, approach, {line})
                if route:
                    paths.append([*route, line])
        return paths

    def get_static_put_paths(self, state: State, source: str, target: str) -> list[list[str]]:
        cars = self.cars_from_state(state)
        approaches = sorted(physical.route_approach_lines_for_put(target, cars, set()))
        if not approaches:
            route = self.graph.route(source, target)
            return [route] if route else []
        paths: list[list[str]] = []
        for approach in approaches:
            if approach == source:
                paths.append([source, target])
                continue
            route = self.graph.route_avoiding_occupied(source, approach, {target})
            if route:
                paths.append([*route, target])
        return paths

    def static_route_cleanup_lines(self, state: State) -> set[str]:
        line_map = self.line_map(state)
        cleanup: set[str] = set()
        for no in sorted(self.active_nos):
            target = self.target_by_no.get(no, "")
            if not target:
                continue
            source = self.current_line(line_map, no)
            if not source:
                continue
            path = self.graph.route(source, target)
            for line in path[1:-1]:
                if line in line_map and any(car_no in self.repair_nos for car_no in line_map.get(line, ())):
                    cleanup.add(line)
        return cleanup

    def dead_state_reason(self, state: State) -> str:
        debt = self.debt_nos_in_state(state)
        held = set(state.held)
        cars = self.cars_from_state(state)
        for line in sorted({car["Line"] for car in cars if car["Line"]}):
            seen_blocker = ""
            for no in physical.line_access_order(cars, line, held):
                if no in self.managed_nos and no in debt:
                    if seen_blocker:
                        return f"unsatisfied_active_blocked:{no}:{line}:by={seen_blocker}"
                elif no not in self.managed_nos:
                    seen_blocker = no
        return ""

    def line_map(self, state: State) -> dict[str, tuple[str, ...]]:
        cached = self.line_map_cache.get(state.lines)
        if cached is not None:
            return dict(cached)
        result = {line: tuple(nos) for line, nos in state.lines}
        if len(self.line_map_cache) < 200_000:
            self.line_map_cache[state.lines] = dict(result)
        return result

    def position_map(self, state: State) -> dict[str, int]:
        return {no: int(position) for no, position in state.positions}

    def line_preserves_positions(self, line: str) -> bool:
        return line in physical.DEPOT_TARGET_LINES or physical.is_spotting_line(line)

    def pack_lines(self, line_map: dict[str, tuple[str, ...] | list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(sorted((line, tuple(nos)) for line, nos in line_map.items() if nos))

    def pack_lines_ordered(
        self,
        line_map: dict[str, tuple[str, ...] | list[str]],
        positions: dict[str, int],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        ordered: dict[str, tuple[str, ...]] = {}
        for line, nos in line_map.items():
            if not nos:
                continue
            original_index = {no: index for index, no in enumerate(nos)}
            ordered[line] = tuple(
                sorted(
                    nos,
                    key=lambda no: (positions.get(no, original_index.get(no, 0) + 1), original_index.get(no, 0), no),
                )
            )
        return self.pack_lines(ordered)

    def pack_positions(self, positions: dict[str, int]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((no, int(position)) for no, position in positions.items() if int(position) > 0))

    def current_line(self, line_map: dict[str, tuple[str, ...]], no: str) -> str:
        for line, nos in line_map.items():
            if no in nos:
                return line
        return ""

    def pull_equivalent(self, nos: Iterable[str]) -> int:
        return sum(4 if self.meta[no].get("IsHeavy") else 1 for no in nos)

    def car_length(self, no: str) -> float:
        return float(self.meta[no].get("Length") or CAR_LENGTH_DEFAULT_M)

    def length(self, nos: Iterable[str]) -> float:
        return sum(self.car_length(no) for no in nos)

    def delta(self, op: Op) -> tuple[int, int, int, int]:
        hot = sum(1 for node in op.path if node in HOT_THROATS)
        primary_hook = 1 if op.action in {"Get", "Put"} else 0
        weigh_penalty = 1 if op.action == "Weigh" else 0
        cache_penalty = 1 if op.note == "cache" else 0
        return (primary_hook, hot, len(op.path), cache_penalty + weigh_penalty)

    def ops_cost(self, ops: Iterable[Op]) -> tuple[int, int, int, int]:
        cost = (0, 0, 0, 0)
        for op in ops:
            delta = self.delta(op)
            cost = (
                cost[0] + delta[0],
                cost[1] + delta[1],
                cost[2] + delta[2],
                cost[3] + delta[3],
            )
        return cost

    def deadline_reached(self) -> bool:
        return time.monotonic() >= self.deadline

    def result(self, chosen: SearchResult) -> dict[str, Any]:
        response = {"Data": {"Operations": self.response_operations(chosen.ops)}}
        combined = {"Data": {"Operations": self.combined_operations(response)}}
        stage4_request = self.stage4_request(chosen.ops)
        replayed, replay_bad = rv.replay(stage4_request, response)
        combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        hard_bad = [
            v for v in replay_bad if v.kind in {"schema", "physical", "business"}
        ]
        combined_hard_bad = [
            v for v in combined_bad if v.kind in {"schema", "physical", "business"}
        ]
        final_cars = [normalize_car(car) for car in rv.final_cars(response, replayed)]
        final_unsatisfied = physical.unsatisfied_cars(final_cars, self.depot_assignment)
        reasons = list(chosen.reasons)
        reasons.extend(f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12])
        reasons.extend(f"combined_replay_{v.kind}:{v.code}:{v.detail}" for v in combined_hard_bad[:12])
        if final_unsatisfied:
            reasons.append(f"final_unsatisfied:{len(final_unsatisfied)}")
        complete = (
            chosen.state is not None
            and chosen.status in {"complete", "feasible_unproved"}
            and not hard_bad
            and not combined_hard_bad
            and not final_unsatisfied
        )
        segment_certificate = self.segment_certificate(chosen)
        status = chosen.status if complete else "partial"
        proof_method = "stage1_style_macro" if chosen.optimality == "feasible_stage1_style_macro" else "astar"
        summary = {
            "case_id": self.case_id,
            "status": status,
            "stage3_final_loco": list(self.stage3_final_loco),
            "stage4_start_loco": list(self.initial_loco),
            "operations": len(chosen.ops),
            "business_hooks": sum(1 for op in chosen.ops if op.action in {"Get", "Put"}),
            "active_count": len(self.active_nos),
            "repair_count": len(self.repair_nos),
            "available_repair_count": len(self.all_repair_nos),
            "managed_count": len(self.managed_nos),
            "active_nos": sorted(self.active_nos),
            "repair_nos": sorted(self.repair_nos),
            "out_of_scope_count": len(self.out_of_scope_nos),
            "lower_bound_initial": self.lower_bound_initial,
            "best_solution_operations": len(chosen.ops),
            "best_solution_business_hooks": sum(1 for op in chosen.ops if op.action in {"Get", "Put"}),
            "optimality": chosen.optimality if complete else "not_proved",
            "proof": {
                "method": proof_method,
                "expanded_states": chosen.expansions,
                "remaining_queue_bound": list(chosen.queue_bound) if chosen.queue_bound else None,
            },
            "segment_certificate": segment_certificate,
            "move_model_restrictions": list(MOVE_MODEL_RESTRICTIONS),
            "blocking_reasons": reasons,
            "replay_physical_ok": not hard_bad,
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
            "combined_replay_physical_ok": not combined_hard_bad,
            "combined_replay_violations": [v.__dict__ for v in combined_hard_bad[:20]],
            "final_unsatisfied_count": len(final_unsatisfied),
            "final_unsatisfied_nos": [rv.car_no(car) for car in final_unsatisfied[:50]],
            "expansions": chosen.expansions,
            "elapsed_seconds": chosen.elapsed_seconds,
        }
        trace = [
            {
                "index": index,
                "action": op.action,
                "line": op.line,
                "move": list(op.move),
                "train_after": list(op.train_after),
                "path": list(op.path),
                "note": op.note,
            }
            for index, op in enumerate(chosen.ops, start=1)
        ]
        return {
            "response": response,
            "combined_response": combined,
            "stage4_request": stage4_request,
            "summary": summary,
            "trace": trace,
        }

    def segment_certificate(self, chosen: SearchResult) -> dict[str, Any]:
        try:
            start = self.initial_state()
            segments, edges, base_lb = self.segment_graph(start)
            fvs = self.minimum_feedback_vertex_set_size(len(segments), edges)
            slack = None
            if chosen.cost[0] < INF_COST:
                slack = chosen.cost[0] - base_lb
            return {
                "segment_count": len(segments),
                "edge_count": len(edges),
                "base_lb": base_lb,
                "admissible_lb": base_lb,
                "fvs_diagnostic": fvs,
                "slack": slack,
                "segments": segments[:40],
                "edges": [[left, right] for left, right in sorted(edges)[:80]],
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}:{exc}"}

    def segment_graph(self, state: State) -> tuple[list[dict[str, Any]], set[tuple[int, int]], int]:
        line_map = self.line_map(state)
        debt = self.debt_nos_in_state(state)
        segments: list[dict[str, Any]] = []
        no_to_segment: dict[str, int] = {}
        for line, nos in sorted(line_map.items()):
            current: list[str] = []
            current_key: tuple[Any, ...] | None = None
            for no in nos:
                key = (
                    self.target_by_no.get(no, ""),
                    self.pending_weigh_in_state(state, no),
                    no in self.repair_nos,
                )
                if current and key != current_key:
                    self.add_segment(segments, no_to_segment, line, current, debt)
                    current = []
                current_key = key
                current.append(no)
            if current:
                self.add_segment(segments, no_to_segment, line, current, debt)

        source_lines: set[str] = set()
        target_lines: set[str] = set()
        for segment in segments:
            if not segment["must_move"]:
                continue
            if segment["source"]:
                source_lines.add(segment["source"])
            if segment["target"]:
                target_lines.add(segment["target"])

        edges: set[tuple[int, int]] = set()
        for segment in segments:
            if not segment["must_move"] or not segment["target"]:
                continue
            for blocker_no in line_map.get(segment["target"], ()):
                blocker_id = no_to_segment.get(blocker_no)
                if blocker_id is None or blocker_id == segment["id"]:
                    continue
                blocker = segments[blocker_id]
                if blocker["target"] != segment["target"] or blocker["must_move"]:
                    edges.add((segment["id"], blocker_id))
        return segments, edges, len(source_lines) + len(target_lines)

    def add_segment(
        self,
        segments: list[dict[str, Any]],
        no_to_segment: dict[str, int],
        line: str,
        nos: list[str],
        debt: set[str],
    ) -> None:
        segment_id = len(segments)
        target = self.common_target(tuple(nos)) or self.target_by_no.get(nos[0], "")
        must_move = any(no in debt for no in nos) or any(self.target_by_no.get(no, "") != line for no in nos)
        segment = {
            "id": segment_id,
            "source": line,
            "target": target,
            "size": len(nos),
            "cars": list(nos),
            "must_move": must_move,
            "repair": all(no in self.repair_nos for no in nos),
        }
        segments.append(segment)
        for no in nos:
            no_to_segment[no] = segment_id

    def minimum_feedback_vertex_set_size(self, node_count: int, edges: set[tuple[int, int]]) -> int | None:
        if node_count > 18:
            return None
        if not edges:
            return 0
        for mask in range(1 << node_count):
            if self.segment_graph_acyclic_after_removal(node_count, edges, mask):
                return mask.bit_count()
        return None

    def segment_graph_acyclic_after_removal(self, node_count: int, edges: set[tuple[int, int]], remove_mask: int) -> bool:
        indegree = [0] * node_count
        outgoing: list[list[int]] = [[] for _ in range(node_count)]
        active_count = 0
        for node in range(node_count):
            if not (remove_mask >> node) & 1:
                active_count += 1
        for left, right in edges:
            if ((remove_mask >> left) & 1) or ((remove_mask >> right) & 1):
                continue
            outgoing[left].append(right)
            indegree[right] += 1
        queue = [node for node in range(node_count) if not ((remove_mask >> node) & 1) and indegree[node] == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for right in outgoing[node]:
                indegree[right] -= 1
                if indegree[right] == 0:
                    queue.append(right)
        return seen == active_count

    def response_operations(self, ops: Iterable[Op], *, start_index: int = 1) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        index = start_index
        for op in ops:
            rows.append({
                "Index": index,
                "Line": op.line,
                "Action": op.action,
                "MoveCars": list(op.move),
                "TrainCars": list(op.train_after),
                "PassbyPath": list(op.path),
            })
            index += 1
        return rows

    def combined_operations(self, stage4_response: dict[str, Any]) -> list[dict[str, Any]]:
        base = [dict(row) for row in rv.operations(self.stage3_combined_response)]
        start = len(base) + 1
        extra = self.response_operations(
            [
                Op(
                    action=row["Action"],
                    line=row["Line"],
                    move=tuple(row.get("MoveCars") or ()),
                    path=tuple(row.get("PassbyPath") or ()),
                    train_after=tuple(row.get("TrainCars") or ()),
                )
                for row in rv.operations(stage4_response)
            ],
            start_index=start,
        )
        return [*base, *extra]

    def stage4_request(self, ops: tuple[Op, ...]) -> dict[str, Any]:
        request = dict(self.original_request)
        request["StartStatus"] = [
            self.output_car(car)
            for car in sorted(
                self.initial_cars,
                key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item)),
            )
        ]
        loco_line = self.initial_loco[0]
        if ops and ops[0].path and ops[0].path[0] in set(self.initial_loco):
            loco_line = ops[0].path[0]
        request["locoNode"] = {"Line": loco_line, "End": "North"}
        return request

    def output_car(self, car: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in car.items() if not key.startswith("_") or key in {"_Weighed"}}


def request_paths(input_path: Path, case: str | None) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    paths = sorted(input_path.glob("*.json"))
    if case:
        target = case.upper()
        paths = [path for path in paths if case_id_from_path(path) == target]
    return paths


def solve_one(path: Path, stage3_out: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    stage3_summary_path = stage3_out / f"{case_id}_summary.json"
    combined_path = stage3_out / f"{case_id}_combined_response.json"
    stage3_request_path = stage3_out / f"{case_id}_stage3_request.json"
    stage3_response_path = stage3_out / f"{case_id}_response.json"
    if not combined_path.exists():
        summary = {
            "case_id": case_id,
            "status": "partial",
            "operations": 0,
            "business_hooks": 0,
            "active_count": 0,
            "out_of_scope_count": 0,
            "lower_bound_initial": 0,
            "best_solution_operations": 0,
            "optimality": "not_proved",
            "blocking_reasons": ["stage3_combined_response_missing"],
            "replay_physical_ok": False,
            "replay_violations": [],
            "final_unsatisfied_count": 0,
            "expansions": 0,
            "elapsed_seconds": 0.0,
        }
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    if stage3_summary_path.exists():
        stage3_summary = read_json(stage3_summary_path)
        if stage3_summary.get("status") != "complete":
            summary = {
                "case_id": case_id,
                "status": "partial",
                "operations": 0,
                "business_hooks": 0,
                "active_count": 0,
                "out_of_scope_count": 0,
                "lower_bound_initial": 0,
                "best_solution_operations": 0,
                "optimality": "not_proved",
                "blocking_reasons": [f"stage3_not_complete:{stage3_summary.get('status')}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "final_unsatisfied_count": 0,
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
            write_json(out_dir / f"{case_id}_summary.json", summary)
            return summary
    if not stage3_request_path.exists() or not stage3_response_path.exists():
        missing = []
        if not stage3_request_path.exists():
            missing.append("stage3_request_missing")
        if not stage3_response_path.exists():
            missing.append("stage3_response_missing")
        summary = {
            "case_id": case_id,
            "status": "partial",
            "operations": 0,
            "business_hooks": 0,
            "active_count": 0,
            "out_of_scope_count": 0,
            "lower_bound_initial": 0,
            "best_solution_operations": 0,
            "optimality": "not_proved",
            "blocking_reasons": missing,
            "replay_physical_ok": False,
            "replay_violations": [],
            "final_unsatisfied_count": 0,
            "expansions": 0,
            "elapsed_seconds": 0.0,
        }
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(path)
    stage3_request = read_json(stage3_request_path)
    stage3_response = read_json(stage3_response_path)
    combined = read_json(combined_path)
    solver = Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage3_request,
        stage3_response,
        combined,
        time_budget_seconds=args.time_budget_seconds,
        max_expansions=args.max_expansions,
    )
    result = solver.solve()
    write_json(out_dir / f"{case_id}_stage4_request.json", result["stage4_request"])
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    if args.verbose:
        summary = result["summary"]
        print(
            f"{summary['case_id']} {summary['status']} "
            f"ops={summary['operations']} active={summary['active_count']} "
            f"reasons={';'.join(summary.get('blocking_reasons') or [])}",
            flush=True,
        )
    return result["summary"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fourth-stage residual closeout solver.")
    parser.add_argument("input", type=Path, help="case JSON or directory containing validation_*.json")
    parser.add_argument("--stage3-out", type=Path, required=True, help="stage3 output directory")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--case", default="", help="case id filter for directory input")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--max-expansions", type=int, default=MAX_EXPANSIONS)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = request_paths(args.input, args.case or None)
    if args.limit:
        paths = paths[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in paths:
        try:
            summaries.append(solve_one(path, args.stage3_out, args.out, args))
        except Exception as exc:
            try:
                case_id = case_id_from_path(path)
            except Exception:
                case_id = path.stem
            summary = {
                "case_id": case_id,
                "status": "partial",
                "operations": 0,
                "business_hooks": 0,
                "active_count": 0,
                "out_of_scope_count": 0,
                "lower_bound_initial": 0,
                "best_solution_operations": 0,
                "optimality": "not_proved",
                "blocking_reasons": [f"{type(exc).__name__}:{exc}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "final_unsatisfied_count": 0,
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
            summaries.append(summary)
            write_json(args.out / f"{case_id}_summary.json", summary)
            if args.verbose or args.input.is_dir():
                print(f"{case_id} partial {type(exc).__name__}: {exc}", flush=True)
    complete = sum(1 for item in summaries if item.get("status") == "complete")
    feasible_unproved = sum(1 for item in summaries if item.get("status") == "feasible_unproved")
    partial = sum(1 for item in summaries if item.get("status") == "partial")
    aggregate = {
        "cases": len(summaries),
        "complete": complete,
        "feasible_unproved": feasible_unproved,
        "partial": partial,
        "avg_operations_complete": round(
            sum(item.get("operations", 0) for item in summaries if item.get("status") == "complete") / complete,
            3,
        )
        if complete
        else 0,
        "partial_reasons": dict(
            Counter(
                reason.split(":", 1)[0]
                for item in summaries
                if item.get("status") == "partial"
                for reason in item.get("blocking_reasons") or []
            ).most_common()
        ),
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps({k: v for k, v in aggregate.items() if k != "summaries"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
