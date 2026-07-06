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
STORE4 = "存4线"
WEIGH_LINE = physical.WEIGH_LINE
# Stage 4 may route through running lines such as 联7.  The move model only
# excludes depot-related tracks as operation/cache lines.
BLOCKED_LINES: set[str] = set()
OUT_OF_SCOPE_STRATEGY_LINES = physical.DEPOT_INBOUND_DESTINATION_LINES
RUNNING_LINES = physical.RUNNING_LINES
HOT_THROATS = {"渡10"}
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
    "fixed_satisfied_cars_are_not_moved",
    "cache_lines_are_whitelisted",
    "get_cuts_are_segment_boundaries",
    "depot_related_lines_are_not_strategy_candidates",
    "depot_related_lines_are_excluded_from_get_put_cache_candidates",
    "cache_puts_generated_only_when_no_target_put_is_available",
)


@dataclass(frozen=True)
class State:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
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
    out = rv.ncar(car)
    out["_TargetSet"] = set(out.get("TargetLines") or [])
    return out


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
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical", "business"}]
        if hard_bad:
            detail = ";".join(f"{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:5])
            raise ValueError(f"stage3_replay_failed:{detail}")
        self.initial_cars = [normalize_car(car) for car in rv.final_cars(stage3_response, replayed)]
        self.apply_generated_end_status(stage3_response, self.initial_cars)
        self.stage3_final_loco = final_loco_after_response(stage3_request, stage3_response)
        self.initial_loco = self.stage3_final_loco
        self.meta = {rv.car_no(car): dict(car) for car in self.initial_cars}
        self.graph = physical.TrackGraph()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + time_budget_seconds
        self.max_expansions = max_expansions
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}
        self.target_by_no: dict[str, str] = {}
        self.target_reason_by_no: dict[str, str] = {}
        self.active_nos: set[str] = set()
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
        self.fixed_cars = [dict(car) for car in self.initial_cars if rv.car_no(car) not in self.active_nos]
        self.fixed_by_no = {rv.car_no(car): dict(car) for car in self.fixed_cars}
        self.lower_bound_initial = 0
        self.unsatisfied_cache: dict[State, frozenset[str]] = {}
        self.lower_bound_cache: dict[State, int] = {}

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

    def solve(self) -> dict[str, Any]:
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
                optimality="proved_within_move_model",
                expansions=0,
                elapsed_seconds=0.0,
                queue_bound=(0, 0, 0, 0),
            )
            return self.result(done)

        upper_state, upper_ops = self.greedy_upper_bound(start)
        upper_cost = self.ops_cost(upper_ops) if upper_state is not None else None
        searched = self.search(start, upper_state, upper_ops, upper_cost)
        return self.result(searched)

    def early_rejections(self) -> list[str]:
        reasons: list[str] = []
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
            seen_blocker = False
            for no in physical.line_access_order(self.initial_cars, line):
                if no in self.active_nos:
                    if seen_blocker:
                        reasons.append(f"active_blocked_by_fixed:{no}:{line}")
                        break
                else:
                    seen_blocker = True
        return reasons

    def initial_state(self) -> State:
        by_line: dict[str, list[tuple[int, str]]] = {}
        for no in self.active_nos:
            car = self.meta[no]
            by_line.setdefault(car["Line"], []).append((int(car.get("Position") or 0), no))
        packed = {
            line: tuple(no for _pos, no in sorted(rows))
            for line, rows in by_line.items()
            if line
        }
        initially_weighed = tuple(sorted(no for no, car in self.meta.items() if car.get("_Weighed")))
        return State(
            lines=self.pack_lines(packed),
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
                    optimality="proved_within_move_model",
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
                    optimality="proved_within_move_model",
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                    queue_bound=bound,
                )
            if self.deadline_reached():
                break
            expansions += 1
            if expansions > self.max_expansions:
                break

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
                    optimality="proved_within_move_model",
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

    def greedy_get(self, state: State) -> tuple[Op, State] | None:
        gets = []
        seq = 0
        for line, move in self.get_prefixes(state):
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
            target_results: list[tuple[Op, State, str]] = []
            for line, move, note in self.put_suffixes(state, include_cache=False):
                result = self.apply_put(state, line, move, note=note)
                if not result[2]:
                    yielded_target_put = True
                target_results.append(result)
            for result in target_results:
                yield result
            if not yielded_target_put:
                for line, move, note in self.put_suffixes(state, include_cache=True):
                    if note != "cache":
                        continue
                    yield self.apply_put(state, line, move, note=note)
        for line, move in self.get_prefixes(state):
            yield self.apply_get(state, line, move)

    def get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        cars = self.cars_from_state(state)
        active = set(self.active_nos)
        held_set = set(state.held)
        unsatisfied = self.unsatisfied_nos(state)
        for line in self.ordered_get_lines(state):
            full_order = physical.line_access_order(cars, line, held_set)
            prefix: list[str] = []
            for no in full_order:
                if no not in active:
                    break
                if no not in unsatisfied:
                    break
                trial = (*state.held, *prefix, no)
                if self.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                prefix.append(no)
            if not prefix:
                continue
            for cut in self.useful_get_cuts(prefix):
                yield line, tuple(prefix[:cut])

    def ordered_get_lines(self, state: State) -> tuple[str, ...]:
        line_map = self.line_map(state)

        def key(line: str) -> tuple[int, int, str]:
            nos = line_map.get(line, ())
            targets = [self.target_by_no.get(no, "") for no in nos]
            target_changes = sum(1 for left, right in zip(targets, targets[1:]) if left != right)
            ready_front = 0
            if nos and self.target_ready(state, self.target_by_no.get(nos[0], "")):
                ready_front = -1
            store4_release = 0 if line == STORE4 else 1
            return (ready_front, store4_release, target_changes, line)

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
                yield target, move, "target"
        if not include_cache:
            return
        for start in range(len(held)):
            move = held[start:]
            if not self.cache_suffix_allowed(move):
                continue
            for line in self.safe_cache_lines(state, move):
                yield line, move, "cache"

    def cache_suffix_allowed(self, move: tuple[str, ...]) -> bool:
        if not move:
            return False
        targets = {self.target_by_no.get(no, "") for no in move}
        if len(targets) > 1:
            return False
        # Cache is a repair action, not a normal alternative to a ready target put.
        return True

    def safe_cache_lines(self, state: State, move: tuple[str, ...]) -> tuple[str, ...]:
        line_map = self.line_map(state)
        cars = self.cars_from_state(state)
        source_lines = {line for line, nos in line_map.items() if nos}
        target_lines = {self.target_by_no.get(no, "") for no in self.active_nos}
        out: list[str] = []
        for line in CACHE_LINES:
            if line in BLOCKED_LINES or line in RUNNING_LINES or line == STORE4:
                continue
            if line in physical.DEPOT_TARGET_LINES:
                continue
            if line in source_lines:
                continue
            if line in target_lines and self.target_ready(state, line):
                continue
            if any(car["Line"] == line for car in cars):
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
        for line, nos in self.line_map(state).items():
            if line != target:
                continue
            for no in nos:
                if self.target_by_no.get(no) != target or self.pending_weigh_in_state(state, no):
                    return False
        return True

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
        route, reject = self.route(state, "Get", line, move)
        if reject:
            return Op("Get", line, move, (), state.held), state, reject
        next_lines = dict(line_map)
        move_set = set(move)
        next_lines[line] = tuple(no for no in next_lines.get(line, ()) if no not in move_set)
        next_state = State(
            lines=self.pack_lines(next_lines),
            held=held_after,
            loco=(line,),
            weighed=state.weighed,
        )
        damaged = sorted(self.unsatisfied_nos(next_state) & self.protected_satisfied_nos)
        if damaged:
            return Op("Get", line, move, route, state.held), state, f"fixed_satisfied_car_damaged:{damaged[0]}"
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
        if self.closed_door_put_reject(state, line):
            return Op("Put", line, move, (), state.held, note), state, "closed_door_process_violation"
        if not self.line_has_capacity(state, line, move):
            return Op("Put", line, move, (), state.held, note), state, f"target_capacity_violation:{line}"
        route, reject = self.route(state, "Put", line, move)
        if reject:
            return Op("Put", line, move, (), state.held, note), state, reject
        line_map = dict(self.line_map(state))
        line_map[line] = (*move, *line_map.get(line, ()))
        move_set = set(move)
        held_after = tuple(no for no in state.held if no not in move_set)
        next_state = State(
            lines=self.pack_lines(line_map),
            held=held_after,
            loco=tuple(sorted(rv.put_loco_positions(list(route), line))),
            weighed=state.weighed,
        )
        projected = self.cars_from_state(next_state)
        projected_unsatisfied = {
            rv.car_no(car)
            for car in physical.unsatisfied_cars(projected, self.depot_assignment)
        }
        damaged = sorted(projected_unsatisfied & self.protected_satisfied_nos)
        if damaged:
            return Op("Put", line, move, route, state.held, note), state, f"fixed_satisfied_car_damaged:{damaged[0]}"
        return Op("Put", line, move, route, next_state.held, note), next_state, ""

    def apply_weigh(self, state: State, no: str) -> tuple[Op, State, str]:
        if not state.held or state.held[-1] != no:
            return Op("Weigh", WEIGH_LINE, (no,), (), state.held, "weigh"), state, "weigh_car_not_tail"
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
        next_state = State(lines=state.lines, held=state.held, loco=(WEIGH_LINE,), weighed=next_weighed)
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
        return not (unsatisfied & self.active_nos) and not (unsatisfied & self.protected_satisfied_nos)

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
        cached = self.lower_bound_cache.get(state)
        if cached is not None:
            return cached
        line_map = self.line_map(state)
        unsatisfied = self.unsatisfied_nos(state)
        unresolved: set[str] = set()
        get_lines: set[str] = set()
        put_targets: set[str] = set()
        held_set = set(state.held)
        for no in self.active_nos:
            if no not in unsatisfied:
                continue
            unresolved.add(no)
            target = self.target_by_no.get(no, "")
            if target:
                put_targets.add(target)
            if no not in held_set:
                line = self.current_line(line_map, no)
                if line:
                    get_lines.add(line)
        lb_sum = len(get_lines) + len(put_targets)
        held_put = 1 if state.held else 0
        result = max(lb_sum, held_put)
        self.lower_bound_cache[state] = result
        return result

    def unsatisfied_nos(self, state: State) -> frozenset[str]:
        cached = self.unsatisfied_cache.get(state)
        if cached is not None:
            return cached
        projected = self.cars_from_state(state)
        result = frozenset(rv.car_no(car) for car in physical.unsatisfied_cars(projected, self.depot_assignment))
        if len(self.unsatisfied_cache) < 200_000:
            self.unsatisfied_cache[state] = result
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
        return sum(1 for no in self.active_nos if not self.active_satisfied_in_state(state, no)) + len(state.held)

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

    def closed_door_put_reject(self, state: State, target_line: str) -> bool:
        if not state.held:
            return False
        train = [self.meta[no] for no in state.held if no in self.meta]
        if target_line == STORE4:
            return False
        first = train[0] if train else {}
        return bool(first.get("IsClosedDoor")) and (len(train) > 10 or any(car.get("IsHeavy") for car in train))

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
        rows = [dict(car) for car in self.fixed_cars]
        weighed = set(state.weighed)
        for line, nos in state.lines:
            for position, no in enumerate(nos, start=1):
                car = dict(self.meta[no])
                car["Line"] = line
                car["Position"] = position
                if no in weighed:
                    car["_Weighed"] = True
                rows.append(car)
        for no in state.held:
            car = dict(self.meta[no])
            car["Line"] = ""
            car["Position"] = 0
            if no in weighed:
                car["_Weighed"] = True
            rows.append(car)
        return rows

    def line_map(self, state: State) -> dict[str, tuple[str, ...]]:
        return {line: tuple(nos) for line, nos in state.lines}

    def pack_lines(self, line_map: dict[str, tuple[str, ...] | list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(sorted((line, tuple(nos)) for line, nos in line_map.items() if nos))

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
        cache_penalty = 1 if op.note == "cache" else 0
        return (1, hot, len(op.path), cache_penalty)

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
        if hard_bad and not reasons:
            reasons = [f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12]]
        if combined_hard_bad and not reasons:
            reasons = [f"combined_replay_{v.kind}:{v.code}:{v.detail}" for v in combined_hard_bad[:12]]
        if final_unsatisfied and not reasons:
            reasons = [f"final_unsatisfied:{len(final_unsatisfied)}"]
        complete = (
            chosen.state is not None
            and chosen.status in {"complete", "feasible_unproved"}
            and not hard_bad
            and not combined_hard_bad
            and not final_unsatisfied
        )
        status = chosen.status if complete else "partial"
        summary = {
            "case_id": self.case_id,
            "status": status,
            "stage3_final_loco": list(self.stage3_final_loco),
            "stage4_start_loco": list(self.initial_loco),
            "operations": len(chosen.ops),
            "business_hooks": sum(1 for op in chosen.ops if op.action in {"Get", "Put"}),
            "active_count": len(self.active_nos),
            "active_nos": sorted(self.active_nos),
            "out_of_scope_count": len(self.out_of_scope_nos),
            "lower_bound_initial": self.lower_bound_initial,
            "best_solution_operations": len(chosen.ops),
            "optimality": chosen.optimality if complete else "not_proved",
            "proof": {
                "method": "astar",
                "expanded_states": chosen.expansions,
                "remaining_queue_bound": list(chosen.queue_bound) if chosen.queue_bound else None,
            },
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
        request["locoNode"] = {"Line": self.initial_loco[0], "End": "North"}
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
