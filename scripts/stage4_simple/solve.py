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
    "机北1",
    "机北2",
    "机南",
    "洗油北",
    "存4南",
    "存1线",
    "存2线",
    "存3线",
    "存5线北",
    "存5线南",
    "调梁线北",
    "调梁棚",
    "预修线",
    "洗罐线北",
    "洗罐站",
    "油漆线",
    "抛丸线",
    "机走棚",
    "机走北",
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
        self.cars_cache: dict[tuple[Any, ...], tuple[tuple[tuple[str, Any], ...], ...]] = {}
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
        return candidates

    def solve(self) -> dict[str, Any]:
        if self.should_try_active_only_first():
            self.configure_managed(include_repair=False)
            active_only = self.solve_current_model()
            active_only["summary"]["move_model_mode"] = "active_only"
            return active_only
        self.configure_managed(include_repair=True)
        result = self.solve_current_model()
        result["summary"]["move_model_mode"] = "repair_enabled"
        return result

    def should_try_active_only_first(self) -> bool:
        self.configure_managed(include_repair=False)
        early = self.early_rejections()
        return not any(reason.startswith("active_blocked_by_fixed:") for reason in early)

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

        macro_state: State | None = None
        macro_ops: tuple[Op, ...] = ()
        original_deadline = self.deadline
        remaining = max(0.0, original_deadline - time.monotonic())
        if self.repair_nos:
            macro_budget = min(60.0, max(1.0, remaining * 0.65))
        else:
            macro_budget = min(5.0, max(0.5, remaining * 0.18))
        self.deadline = min(original_deadline, time.monotonic() + macro_budget)
        macro_state, macro_ops, _macro_reasons = self.macro_upper_bound(start)
        self.deadline = original_deadline

        upper_state, upper_ops = self.greedy_upper_bound(start)
        if macro_state is not None and self.complete(macro_state):
            macro_cost = self.ops_cost(macro_ops)
            greedy_cost = self.ops_cost(upper_ops) if upper_state is not None else None
            if greedy_cost is None or macro_cost < greedy_cost:
                upper_state, upper_ops = macro_state, macro_ops
        upper_cost = self.ops_cost(upper_ops) if upper_state is not None else None
        searched = self.search(start, upper_state, upper_ops, upper_cost)
        return self.result(searched)

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
            candidates = [candidate for candidate in self.macro_candidates(state) if candidate.state not in seen]
            if not candidates:
                return None, (), ("macro_no_candidate", *tuple(trace_reasons[-8:]))
            chosen = min(candidates, key=lambda item: item.score)
            ops.extend(chosen.ops)
            state = chosen.state
            trace_reasons.append(chosen.reason)
        return None, (), ("macro_step_limit", *tuple(trace_reasons[-8:]))

    def macro_candidates(self, state: State) -> Iterable[MacroCandidate]:
        yield from self.target_group_macros(state)
        if state.held:
            yield from self.macro_held_candidates(state)
            return
        yield from self.macro_empty_train_candidates(state)

    def target_group_macros(self, state: State) -> Iterable[MacroCandidate]:
        if state.held:
            target = self.common_target(state.held)
            if target:
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
        cache_lines = self.safe_cache_lines(after_get, move)
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
        prior_cleanup = len(self.macro_blocker_lines(prior))
        next_cleanup = len(self.macro_blocker_lines(next_state))
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
            reason_rank,
            next_cleanup,
            next_debt,
            next_must,
            self.unsatisfied_count(next_state),
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
        out: list[str] = []
        for line in CACHE_LINES:
            if line == own_target:
                continue
            if line in BLOCKED_LINES or line in RUNNING_LINES or line == STORE4:
                continue
            if line in physical.DEPOT_TARGET_LINES:
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
        projected_unsatisfied = {
            rv.car_no(car)
            for car in physical.unsatisfied_cars(projected, self.depot_assignment)
        }
        if note == "target" and (set(move) & projected_unsatisfied):
            return Op("Put", line, move, route, state.held, note), state, f"target_put_moved_still_unsatisfied:{line}"
        if note == "target" and len(self.debt_nos_in_state(next_state)) >= len(self.debt_nos_in_state(state)):
            return Op("Put", line, move, route, state.held, note), state, "target_put_no_progress"
        damaged = sorted(projected_unsatisfied & self.protected_satisfied_nos)
        damage_reason = self.protected_damage_reason(next_state, damaged)
        if damage_reason:
            return Op("Put", line, move, route, state.held, note), state, damage_reason
        return Op("Put", line, move, route, next_state.held, note), next_state, ""

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
            self.cars_cache[key] = tuple(tuple(sorted(row.items())) for row in rows)
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
