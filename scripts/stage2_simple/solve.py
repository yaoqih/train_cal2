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


DEPOT_IN = tuple(f"修{i}库内" for i in range(1, 5))
DEPOT_OUT = tuple(f"修{i}库外" for i in range(1, 5))
SOURCE_LINES = (*DEPOT_IN, *DEPOT_OUT, "卸轮线", "存4线")
DEFAULT_ALLOW_DEPOT_IN_BUFFER = False
DEFAULT_BUFFER_LINES = DEPOT_OUT
STORE4 = "存4线"
UNWHEEL = "卸轮线"
TAG_C4 = "C4"
TAG_OFF = "OFF"
TAG_OUT = "OUT"
TAG_U = "U"
TAG_STAY = "STAY"
DEFAULT_TIME_BUDGET_SECONDS = 300.0
MAX_EXPANSIONS = 200_000
STORE4_STAGE2_APPROACH = "存4南"


@dataclass(frozen=True)
class State:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    held: tuple[str, ...]
    loco: tuple[str, ...]
    new_store4: tuple[str, ...]


@dataclass(frozen=True)
class Op:
    action: str
    line: str
    move: tuple[str, ...]
    path: tuple[str, ...]
    train_after: tuple[str, ...]


@dataclass(frozen=True)
class Label:
    cost: tuple[int, int, int, int]
    state: State


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
            loco = {rv.WEIGH}
    lines = tuple(sorted(line for line in loco if line))
    return lines or (rv.WEIGH,)


class Stage2Solver:
    def __init__(
        self,
        case_id: str,
        request: dict[str, Any],
        stage1_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
        allow_depot_in_buffer: bool = DEFAULT_ALLOW_DEPOT_IN_BUFFER,
        accept_upper_bound: bool = False,
    ) -> None:
        self.case_id = case_id
        self.original_request = request
        self.stage1_response = stage1_response
        replayed, replay_bad = rv.replay(request, stage1_response)
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical"}]
        if hard_bad:
            detail = ";".join(f"{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:5])
            raise ValueError(f"stage1_replay_failed:{detail}")
        self.initial_cars = [normalize_car(car) for car in rv.final_cars(stage1_response, replayed)]
        self.initial_loco = final_loco_after_response(request, stage1_response)
        self.meta = {rv.car_no(car): dict(car) for car in self.initial_cars}
        self.tags = {no: self.classify(car) for no, car in self.meta.items()}
        self.active_nos = {no for no, tag in self.tags.items() if tag != TAG_STAY}
        self.graph = physical.TrackGraph()
        self.started_at = time.monotonic()
        self.time_budget_seconds = time_budget_seconds
        self.buffer_lines = (*DEPOT_OUT, *DEPOT_IN) if allow_depot_in_buffer else DEFAULT_BUFFER_LINES
        self.accept_upper_bound = accept_upper_bound
        self.blocking_reasons: list[str] = []
        self.expansions = 0
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}
        self.search_mode = "dijkstra"

    def classify(self, car: dict[str, Any]) -> str:
        line = car["Line"]
        targets = set(car.get("TargetLines") or [])
        if UNWHEEL in targets and line != UNWHEEL:
            return TAG_U
        if STORE4 in targets and line in set(SOURCE_LINES):
            return TAG_C4
        if targets & set(DEPOT_OUT) and line in set(SOURCE_LINES):
            return TAG_OUT
        if line in set(SOURCE_LINES) and not (targets & set(DEPOT_IN)) and UNWHEEL not in targets:
            return TAG_OFF
        return TAG_STAY

    def solve(self) -> dict[str, Any]:
        early = self.early_partial()
        if early:
            return self.result(None, [], early)
        state = self.initial_state()
        if self.complete(state):
            return self.result(state, [], [])
        upper_state, upper_ops = self.greedy_solution(state)
        if upper_state is not None and (self.accept_upper_bound or self.search_mode == "forced_deep_off_upper_unproved"):
            return self.result(upper_state, upper_ops, [])
        solved, ops, reasons = self.dijkstra(state, upper_state, upper_ops)
        return self.result(solved, ops, reasons)

    def early_partial(self) -> list[str]:
        reasons: list[str] = []
        source_set = set(SOURCE_LINES)
        initial_lines = self.line_map(self.initial_state())
        for no in sorted(self.active_nos):
            line = self.current_line(initial_lines, no)
            if line and line not in source_set:
                reasons.append(f"active_car_off_source_line:{line}:{no}")
        for line in SOURCE_LINES:
            ordered = [
                rv.car_no(car)
                for car in sorted(
                    (c for c in self.initial_cars if c["Line"] == line),
                    key=lambda item: (int(item.get("Position") or 0), rv.car_no(item)),
                )
            ]
            seen_stay = False
            for no in ordered:
                active = self.tags.get(no) in {TAG_C4, TAG_OFF, TAG_OUT, TAG_U}
                if line == STORE4:
                    active = self.tags.get(no) == TAG_U
                if active and seen_stay:
                    reasons.append(f"stay_car_blocks_outbound:{line}:{no}")
                    break
                if not active:
                    seen_stay = True
        reasons.extend(self.single_store4_put_precheck())
        return reasons

    def single_store4_put_precheck(self) -> list[str]:
        state = self.initial_state()
        line_map = self.line_map(state)
        final_store4_nos: list[str] = []
        pending_unwheel = {
            no
            for no in self.active_nos
            if self.tags.get(no) == TAG_U and self.current_line(line_map, no) != UNWHEEL
        }
        for no in self.active_nos:
            if self.tags.get(no) in {TAG_C4, TAG_OFF} and self.current_line(line_map, no) != STORE4:
                final_store4_nos.append(no)
        if not final_store4_nos:
            return []

        reasons: list[str] = []
        pull = self.pull_equivalent(final_store4_nos)
        if pull > rv.PULL_LIMIT:
            reasons.append(f"single_store4_pull_limit:{pull}>{rv.PULL_LIMIT}")

        existing_store4_len = sum(
            self.meta[no]["Length"]
            for no in line_map.get(STORE4, ())
            if no not in pending_unwheel
        )
        final_store4_len = existing_store4_len + self.length(final_store4_nos)
        if final_store4_len > rv.TRACK_LEN[STORE4] + rv.TOL:
            reasons.append(f"single_store4_capacity:{final_store4_len:.1f}>{rv.TRACK_LEN[STORE4]:.1f}")
        return reasons

    def initial_state(self) -> State:
        by_line: dict[str, list[str]] = {}
        for car in sorted(self.initial_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item))):
            by_line.setdefault(car["Line"], []).append(rv.car_no(car))
        return State(
            lines=tuple(sorted((line, tuple(nos)) for line, nos in by_line.items() if line)),
            held=(),
            loco=self.initial_loco,
            new_store4=(),
        )

    def dijkstra(
        self,
        start: State,
        upper_state: State | None = None,
        upper_ops: list[Op] | None = None,
    ) -> tuple[State | None, list[Op], list[str]]:
        queue: list[tuple[tuple[int, int, int, int, int, int, int, int], tuple[int, int, int, int], int, State]] = []
        start_cost = (0, 0, 0, 0)
        heapq.heappush(queue, (self.search_priority(start_cost, start), start_cost, 0, start))
        best: dict[State, tuple[int, int, int, int]] = {start: (0, 0, 0, 0)}
        prev: dict[State, tuple[State, Op]] = {}
        sequence = 1
        last_rejections: Counter[str] = Counter()

        while queue:
            _priority, cost, _seq, state = heapq.heappop(queue)
            if best.get(state) != cost:
                continue
            if upper_state is not None and upper_ops is not None and _priority[0] >= len(upper_ops):
                self.search_mode = "dijkstra_proved_upper_bound"
                return upper_state, upper_ops, []
            if self.complete(state):
                self.search_mode = "dijkstra"
                return state, self.reconstruct(prev, state), []
            if self.time_exhausted():
                if upper_state is not None and upper_ops is not None:
                    self.search_mode = "greedy_upper_bound_unproved"
                    return upper_state, upper_ops, []
                return None, [], ["stage2_time_budget_exhausted"]
            self.expansions += 1
            if self.expansions > MAX_EXPANSIONS:
                if upper_state is not None and upper_ops is not None:
                    self.search_mode = "greedy_upper_bound_expansion_unproved"
                    return upper_state, upper_ops, []
                return None, [], ["stage2_expansion_budget_exhausted"]

            yielded = False
            for op, next_state, delta, reject in self.neighbors(state):
                if reject:
                    last_rejections[reject] += 1
                    continue
                yielded = True
                next_cost = tuple(cost[i] + delta[i] for i in range(4))
                if next_cost >= best.get(next_state, (10**9, 10**9, 10**9, 10**9)):
                    continue
                best[next_state] = next_cost
                prev[next_state] = (state, op)
                heapq.heappush(queue, (self.search_priority(next_cost, next_state), next_cost, sequence, next_state))
                sequence += 1
            if not yielded:
                last_rejections["no_neighbor_from_state"] += 1

        reasons = [f"{key}:{value}" for key, value in last_rejections.most_common(12)]
        return None, [], reasons or ["stage2_no_solution"]

    def greedy_solution(self, start: State) -> tuple[State | None, list[Op]]:
        solved, ops = self.deterministic_greedy_solution(start)
        if solved is not None:
            self.search_mode = "deterministic_upper_unproved"
            return solved, ops
        solved, ops = self.forced_deep_off_solution(start)
        if solved is not None:
            self.search_mode = "forced_deep_off_upper_unproved"
            return solved, ops
        solved, ops = self.feasible_upper_bound_search(start)
        if solved is not None:
            self.search_mode = "feasible_upper_unproved"
        return solved, ops

    def forced_deep_off_solution(self, start: State) -> tuple[State | None, list[Op]]:
        state = start
        ops: list[Op] = []
        for _ in range(20):
            line = self.next_deep_off_line(state)
            if not line:
                break
            protected = self.matching_outer(line)
            if protected:
                moved = self.get_active_c4_prefix(state, protected)
                if moved:
                    op, state, reject = self.apply_get(state, protected, moved)
                    if reject:
                        return None, []
                    ops.append(op)
                    if not self.flush_bufferable_tail(state, ops, protected):
                        return None, []
                    state = self.state_after_ops(start, ops)

            prefix = self.c4_prefix_before_first_off(state, line)
            if not prefix:
                break
            op, state, reject = self.apply_get(state, line, prefix)
            if reject:
                return None, []
            ops.append(op)
            if not self.flush_bufferable_tail(state, ops, protected):
                return None, []
            state = self.state_after_ops(start, ops)

            off = self.first_direct_tag(state, line, TAG_OFF)
            if off:
                op, next_state, reject = self.apply_get(state, line, (off,))
                if reject and protected:
                    moved = self.get_active_c4_prefix(state, protected)
                    if not moved:
                        return None, []
                    op2, state2, reject2 = self.apply_get(state, protected, moved)
                    if reject2:
                        return None, []
                    ops.append(op2)
                    if not self.flush_bufferable_tail(state2, ops, protected):
                        return None, []
                    state = self.state_after_ops(start, ops)
                    op, next_state, reject = self.apply_get(state, line, (off,))
                if reject:
                    return None, []
                ops.append(op)
                state = next_state

        solved, tail_ops = self.deterministic_greedy_solution(state)
        if solved is None:
            return None, []
        return solved, [*ops, *tail_ops]

    def state_after_ops(self, start: State, ops: list[Op]) -> State:
        state = start
        for op in ops:
            if op.action == "Get":
                _op, state, reject = self.apply_get(state, op.line, op.move)
            else:
                _op, state, reject = self.apply_put(state, op.line, op.move)
            if reject:
                raise RuntimeError(reject)
        return state

    def flush_bufferable_tail(self, state: State, ops: list[Op], protected_line: str = "") -> bool:
        for _ in range(20):
            if not state.held:
                return True
            tail_tag = self.tags.get(state.held[-1], TAG_STAY)
            if tail_tag == TAG_U:
                op, next_state, reject = self.apply_put(state, UNWHEEL, (state.held[-1],))
                if reject:
                    return False
                ops.append(op)
                state = next_state
                continue
            if tail_tag not in {TAG_C4, TAG_OUT}:
                return True
            choices = [
                (line == protected_line, self.buffer_line_penalty(state, line), -len(move), len(op.path), line, move, op, next_state)
                for line, move, op, next_state in self.valid_puts(state)
                if line not in {STORE4, UNWHEEL}
            ]
            if not choices:
                return False
            _protected, _penalty, _move_len, _path_len, _line, _move, op, state = min(choices)
            ops.append(op)
        return False

    def next_deep_off_line(self, state: State) -> str:
        line_map = self.line_map(state)
        for line in self.ordered_source_lines(state):
            if line == STORE4:
                continue
            tags = [self.effective_line_tag(line, no) for no in line_map.get(line, ())]
            if TAG_OFF in tags:
                before = tags[: tags.index(TAG_OFF)]
                if TAG_C4 in before:
                    return line
        return ""

    def c4_prefix_before_first_off(self, state: State, line: str) -> tuple[str, ...]:
        nos = self.line_map(state).get(line, ())
        out: list[str] = []
        for no in nos:
            tag = self.effective_line_tag(line, no)
            if tag == TAG_OFF:
                break
            if tag in {TAG_C4, TAG_U}:
                out.append(no)
        return tuple(out)

    def first_direct_tag(self, state: State, line: str, tag: str) -> str:
        nos = self.line_map(state).get(line, ())
        if nos and self.effective_line_tag(line, nos[0]) == tag:
            return nos[0]
        return ""

    def get_active_c4_prefix(self, state: State, line: str) -> tuple[str, ...]:
        out: list[str] = []
        for no in self.line_map(state).get(line, ()):
            if self.effective_line_tag(line, no) not in {TAG_C4, TAG_OUT}:
                break
            out.append(no)
        return tuple(out)

    def matching_outer(self, inner_line: str) -> str:
        if inner_line in DEPOT_IN:
            return inner_line.replace("库内", "库外")
        return ""

    def deterministic_greedy_solution(self, start: State) -> tuple[State | None, list[Op]]:
        state = start
        ops: list[Op] = []
        seen: set[State] = set()
        for _step in range(80):
            if self.complete(state):
                return state, ops
            if state in seen:
                return None, []
            seen.add(state)

            applied = self.greedy_put(state)
            if applied:
                op, state = applied
                ops.append(op)
                continue

            applied = self.greedy_get(state)
            if applied:
                op, state = applied
                ops.append(op)
                continue

            return None, []
        return None, []

    def feasible_upper_bound_search(self, start: State, max_expansions: int = 8_000) -> tuple[State | None, list[Op]]:
        queue: list[tuple[tuple[int, int, int, int, int], int, int, State]] = []
        best_depth: dict[State, int] = {start: 0}
        prev: dict[State, tuple[State, Op]] = {}
        sequence = 1
        heapq.heappush(queue, (self.feasible_priority(start, 0), 0, 0, start))
        expansions = 0
        while queue and expansions < max_expansions and not self.time_exhausted():
            _priority, depth, _seq, state = heapq.heappop(queue)
            if best_depth.get(state) != depth:
                continue
            if self.complete(state):
                return state, self.reconstruct(prev, state)
            expansions += 1
            for op, next_state, _delta, reject in self.neighbors(state):
                if reject:
                    continue
                next_depth = depth + 1
                if next_depth >= best_depth.get(next_state, 10**9):
                    continue
                best_depth[next_state] = next_depth
                prev[next_state] = (state, op)
                heapq.heappush(queue, (self.feasible_priority(next_state, next_depth), next_depth, sequence, next_state))
                sequence += 1
        return None, []

    def feasible_priority(self, state: State, depth: int) -> tuple[int, int, int, int, int]:
        held_tags = [self.tags.get(no, TAG_STAY) for no in state.held]
        bad_held = 0
        if TAG_U in held_tags:
            bad_held += 20
        if self.has_c4_before_off(held_tags):
            bad_held += 10
        return (
            self.source_debt_count(state),
            self.pattern_debt_score(state) + bad_held,
            -len(state.held),
            depth,
            len(self.line_map(state)),
        )

    def greedy_put(self, state: State) -> tuple[Op, State] | None:
        if not state.held:
            return None
        puts = self.valid_puts(state)
        for line, move, op, next_state in puts:
            if line == UNWHEEL:
                return op, next_state
        for line, move, op, next_state in puts:
            if line == STORE4:
                return op, next_state
        if self.should_cache_tail(state) or self.should_buffer_off_tail(state) or self.should_buffer_flex_out_tail(state):
            buffer_puts = [
                (-len(move), self.buffer_line_penalty(state, line), len(op.path), line, move, op, next_state)
                for line, move, op, next_state in puts
                if line not in {STORE4, UNWHEEL}
            ]
            if buffer_puts:
                _move_len, _penalty, _path_len, _line, _move, op, next_state = min(buffer_puts)
                return op, next_state
        return None

    def greedy_get(self, state: State) -> tuple[Op, State] | None:
        gets = []
        for line, move in self.get_prefixes(state):
            op, next_state, reject = self.apply_get(state, line, move)
            if reject:
                continue
            gets.append((self.greedy_get_rank(state, line, move, op), op, next_state))
        if not gets:
            return None
        _rank, op, next_state = min(gets)
        return op, next_state

    def valid_puts(self, state: State) -> list[tuple[str, tuple[str, ...], Op, State]]:
        out: list[tuple[str, tuple[str, ...], Op, State]] = []
        for line, move in self.put_suffixes(state):
            op, next_state, reject = self.apply_put(state, line, move)
            if not reject:
                out.append((line, move, op, next_state))
        return out

    def should_cache_tail(self, state: State) -> bool:
        if not state.held or self.tags.get(state.held[-1], TAG_STAY) != TAG_C4:
            return False
        if self.held_has_blocked_unwheel(state.held):
            return True
        held_tags = [self.tags.get(no, TAG_STAY) for no in state.held]
        if self.has_c4_before_off(held_tags):
            return True
        if TAG_OFF not in held_tags and TAG_U not in held_tags and not self.has_gettable_direct_tag(state, {TAG_OFF}):
            return False
        if TAG_OFF not in held_tags and self.has_gettable_direct_tag(state, {TAG_OFF}):
            return True
        if TAG_OFF in held_tags and self.has_gettable_direct_tag(state, {TAG_OFF}):
            return True
        held_set = set(state.held)
        if TAG_OFF in held_tags and self.count_remaining(state, TAG_OFF, exclude=held_set) > 0:
            return not self.has_gettable_blocker_prefix(state)
        if TAG_OFF not in held_tags and self.count_remaining(state, TAG_OFF, exclude=held_set) > 0:
            return not self.has_gettable_blocker_prefix(state)
        return False

    def should_buffer_off_tail(self, state: State) -> bool:
        if not state.held or self.tags.get(state.held[-1], TAG_STAY) != TAG_OFF:
            return False
        tail_start = len(state.held) - 1
        while tail_start > 0 and self.tags.get(state.held[tail_start - 1], TAG_STAY) == TAG_OFF:
            tail_start -= 1
        return any(self.tags.get(no) == TAG_U for no in state.held[:tail_start])

    def should_buffer_flex_out_tail(self, state: State) -> bool:
        return bool(state.held) and self.tags.get(state.held[-1], TAG_STAY) == TAG_OUT

    def has_gettable_direct_tag(self, state: State, targets: set[str]) -> bool:
        for line, move in self.get_prefixes(state):
            if not move or self.tags.get(move[0], TAG_STAY) not in targets:
                continue
            op, _next_state, reject = self.apply_get(state, line, move)
            if not reject:
                return True
        return False

    def has_gettable_blocker_prefix(self, state: State) -> bool:
        for line, move in self.get_prefixes(state):
            tags = [self.tags.get(no, TAG_STAY) for no in move]
            if tags and tags[-1] == TAG_C4 and self.source_has_later_tag(state, line, len(move), {TAG_OFF, TAG_U}):
                op, _next_state, reject = self.apply_get(state, line, move)
                if not reject:
                    return True
        return False

    def source_has_later_tag(self, state: State, line: str, cut: int, targets: set[str]) -> bool:
        return any(self.tags.get(no, TAG_STAY) in targets for no in self.line_map(state).get(line, ())[cut:])

    def greedy_get_rank(self, state: State, line: str, move: tuple[str, ...], op: Op) -> tuple[int, int, int, str]:
        tags = [self.tags.get(no, TAG_STAY) for no in move]
        held_has_off = any(self.tags.get(no) == TAG_OFF for no in state.held)
        if line == UNWHEEL and any(tag in {TAG_C4, TAG_OFF, TAG_OUT} for tag in tags):
            rank = 0
        elif line == STORE4:
            rank = 1
        elif TAG_U in tags:
            rank = 2
        elif tags and tags[0] == TAG_OUT and self.flex_out_prefix_needed(state, line, len(move)):
            rank = 3
        elif tags and tags[0] == TAG_OFF:
            rank = 4
        elif held_has_off and tags and tags[-1] == TAG_C4 and self.source_has_later_tag(state, line, len(move), {TAG_OFF, TAG_U}):
            rank = 5
        elif self.outside_blocks_pending_target(state, line):
            rank = 6
        elif tags and tags[-1] == TAG_C4 and self.source_has_later_tag(state, line, len(move), {TAG_OFF, TAG_U}):
            rank = 7
        elif TAG_OFF in tags:
            rank = 8
        else:
            rank = 9
        return (rank, len(op.path), -len(move), line)

    def outside_blocks_pending_target(self, state: State, line: str) -> bool:
        if line not in DEPOT_OUT:
            return False
        inner = self.matching_inner(line)
        tags = [self.effective_line_tag(inner, no) for no in self.line_map(state).get(inner, ())]
        return TAG_U in tags or TAG_OFF in tags or self.has_c4_before_off(tags)

    def search_priority(self, cost: tuple[int, int, int, int], state: State) -> tuple[int, int, int, int, int, int, int, int]:
        # A* on the first objective. The lower bound only counts unavoidable
        # operation rows, so it does not change the optimal operation count.
        return (
            cost[0] + self.remaining_operation_lower_bound(state),
            self.source_debt_count(state),
            self.pattern_debt_score(state),
            -len(state.held),
            cost[1],
            cost[2],
            cost[3],
            cost[0],
        )

    def remaining_operation_lower_bound(self, state: State) -> int:
        line_map = self.line_map(state)
        source_lines: set[str] = set()
        need_store4 = False
        need_unwheel = False
        for no in self.active_nos:
            tag = self.tags[no]
            line = self.current_line(line_map, no)
            if no in state.held:
                line = ""
            if tag == TAG_U:
                if line != UNWHEEL:
                    need_unwheel = True
                    if line:
                        source_lines.add(line)
            elif tag in {TAG_C4, TAG_OFF}:
                if line != STORE4:
                    need_store4 = True
                    if line:
                        source_lines.add(line)
        return len(source_lines) + int(need_store4) + int(need_unwheel)

    def source_debt_count(self, state: State) -> int:
        line_map = self.line_map(state)
        count = 0
        for no in self.active_nos:
            line = self.current_line(line_map, no)
            tag = self.tags[no]
            if tag == TAG_U and line not in {"", UNWHEEL}:
                count += 1
            elif tag in {TAG_C4, TAG_OFF} and line not in {"", STORE4}:
                count += 1
        return count

    def pattern_debt_score(self, state: State) -> int:
        line_map = self.line_map(state)
        score = 0
        tags = [self.tags.get(no, TAG_STAY) for no in state.held]
        if TAG_U in tags:
            score += 50
            if tags and tags[-1] != TAG_U:
                score += 50
        if self.has_c4_before_off(tags):
            score += 30
        for _line, nos in line_map.items():
            line_tags = [self.effective_line_tag(_line, no) for no in nos]
            if self.has_c4_before_off(line_tags):
                score += 10
            if TAG_U in line_tags:
                score += 5
        return score

    def reconstruct(self, prev: dict[State, tuple[State, Op]], state: State) -> list[Op]:
        ops: list[Op] = []
        while state in prev:
            prior, op = prev[state]
            ops.append(op)
            state = prior
        ops.reverse()
        return ops

    def neighbors(self, state: State) -> Iterable[tuple[Op, State, tuple[int, int, int, int], str]]:
        if state.new_store4 and not self.complete(state):
            yield Op("Put", STORE4, (), (), state.held), state, (0, 0, 0, 0), "store4_closed_before_stage2_complete"
            return

        yielded_get = False
        for line, move in self.get_prefixes(state):
            yielded_get = True
            op, next_state, reject = self.apply_get(state, line, move)
            if reject:
                yield Op("Get", line, move, (), state.held), state, (0, 0, 0, 0), reject
                continue
            yield op, next_state, self.delta(op), ""

        # Let Dijkstra put from a non-empty consist even while more Get actions exist.
        if state.held:
            for line, move in self.put_suffixes(state):
                op, next_state, reject = self.apply_put(state, line, move)
                if reject:
                    yield Op("Put", line, move, (), state.held), state, (0, 0, 0, 0), reject
                    continue
                yield op, next_state, self.delta(op), ""
        elif not yielded_get:
            yield Op("Get", "", (), (), ()), state, (0, 0, 0, 0), "no_gettable_prefix"

    def get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        if state.new_store4:
            return
        line_map = self.line_map(state)
        held_equiv = self.pull_equivalent(state.held)
        for line in self.ordered_source_lines(state):
            ordered = line_map.get(line, ())
            if not ordered:
                continue
            prefix: list[str] = []
            for no in ordered:
                tag = self.effective_line_tag(line, no)
                if line == STORE4:
                    if tag != TAG_U:
                        break
                elif tag == TAG_STAY:
                    break
                prefix.append(no)
                if held_equiv + self.pull_equivalent(prefix) > rv.PULL_LIMIT:
                    prefix.pop()
                    break
            if prefix and all(self.tags.get(no) == TAG_OUT for no in prefix):
                if not self.flex_out_prefix_needed(state, line, len(prefix)):
                    continue
            for cut in self.useful_prefix_cuts(prefix):
                yield line, tuple(prefix[:cut])

    def flex_out_prefix_needed(self, state: State, line: str, cut: int) -> bool:
        line_map = self.line_map(state)
        later_tags = [self.effective_line_tag(line, no) for no in line_map.get(line, ())[cut:]]
        if any(tag in {TAG_C4, TAG_OFF, TAG_U} for tag in later_tags):
            return True
        if line in DEPOT_OUT:
            return self.inner_has_pending_active(line_map, self.matching_inner(line))
        return False

    def pending_c4_before_off_lines(self, line_map: dict[str, tuple[str, ...]]) -> set[str]:
        out: set[str] = set()
        for line in SOURCE_LINES:
            if line == STORE4:
                continue
            tags = [self.effective_line_tag(line, no) for no in line_map.get(line, ())]
            if self.has_c4_before_off(tags):
                out.add(line)
        return out

    def ordered_source_lines(self, state: State) -> tuple[str, ...]:
        line_map = self.line_map(state)

        def key(line: str) -> tuple[int, int, str]:
            tags = [self.effective_line_tag(line, no) for no in line_map.get(line, ())]
            if line == STORE4 and any(tag == TAG_U for tag in tags):
                return (0, 0, line)
            if any(tag == TAG_OUT for tag in tags) and self.flex_out_prefix_needed(state, line, 1):
                return (1, 0, line)
            if self.has_c4_before_off(tags):
                return (2, 0, line)
            if any(tag == TAG_OFF for tag in tags):
                return (3, 0, line)
            if any(tag == TAG_C4 for tag in tags):
                return (4, 0, line)
            if any(tag == TAG_U for tag in tags):
                return (5, 0, line)
            return (6, 0, line)

        return tuple(sorted(SOURCE_LINES, key=key))

    def has_c4_before_off(self, tags: Iterable[str]) -> bool:
        seen_c4 = False
        for tag in tags:
            if tag == TAG_C4:
                seen_c4 = True
            elif tag == TAG_OFF and seen_c4:
                return True
        return False

    def useful_prefix_cuts(self, prefix: list[str]) -> list[int]:
        if not prefix:
            return []
        seen_c4 = False
        for index, no in enumerate(prefix):
            tag = self.tags.get(no, TAG_STAY)
            if tag == TAG_C4:
                seen_c4 = True
            elif tag in {TAG_OFF, TAG_OUT, TAG_U} and seen_c4:
                # With a single final 存4 Put, C4 before an OFF/OUT tail must be
                # cached before the tail is exposed. U is the same kind of hard
                # boundary: it has to be exposed and sent to 卸轮线 before the
                # final 存4收口, so the C4 prefix before it is a useful cut.
                return [index]
        cuts = {len(prefix)}
        for index in range(1, len(prefix)):
            left = self.tags.get(prefix[index - 1], TAG_STAY)
            right = self.tags.get(prefix[index], TAG_STAY)
            # OFF/C4 boundaries are useful because the one final 存4 Put
            # requires the eventual consist to be OFF* followed by C4*.
            if {left, right} == {TAG_C4, TAG_OFF} or {left, right} == {TAG_C4, TAG_U} or TAG_OUT in {left, right}:
                cuts.add(index)
        return sorted(cuts, reverse=True)

    def put_suffixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        held = state.held
        if not held:
            return

        tail_tag = self.tags.get(held[-1], TAG_STAY)
        tail_start = len(held) - 1
        while tail_start > 0 and self.tags.get(held[tail_start - 1], TAG_STAY) == tail_tag:
            tail_start -= 1
        tail_run = held[tail_start:]

        if tail_tag == TAG_U:
            yield UNWHEEL, tail_run

        for line, move in self.buffer_puts(state, tail_run):
            yield line, move

        # 存4北只能从北侧操作。第二阶段一旦向存4北 Put，北端就被新段
        # 占住，后续不能再进存4北；因此 Put 存4线只能是最后一行，且
        # 必须一次性放下当前机后全部 C4/OFF。
        if self.final_store4_put_allowed(state, held):
            yield STORE4, held

    def buffer_puts(self, state: State, tail_run: tuple[str, ...]) -> Iterable[tuple[str, tuple[str, ...]]]:
        if not tail_run:
            return
        tail_tag = self.tags.get(tail_run[-1], TAG_STAY)
        if tail_tag == TAG_OFF:
            if not any(self.tags.get(no) == TAG_U for no in state.held[: len(state.held) - len(tail_run)]):
                return
        elif tail_tag == TAG_OUT:
            pass
        elif tail_tag != TAG_C4:
            return
        # C4 缓存用于暴露 C4 后方的 OFF，或暴露机后被 C4 尾段压住
        # 的卸轮车。两者都用同一套安全缓存重排。
        if (
            tail_tag == TAG_C4
            and self.count_remaining(state, TAG_OFF, exclude=set(state.held)) <= 0
            and not self.held_has_blocked_unwheel(state.held)
        ):
            return
        candidates: list[tuple[int, int, int, str, tuple[str, ...]]] = []
        for line in self.safe_buffer_lines(state, tail_tag):
            move = self.longest_suffix_that_fits(state, line, tail_run)
            if not move:
                continue
            route, reject = self.route(state, "Put", line, move)
            if reject:
                continue
            candidates.append((-len(move), self.buffer_line_penalty(state, line), len(route), line, move))
        for _move_len, _penalty, _route_len, line, move in sorted(candidates):
            yield line, move

    def safe_buffer_lines(self, state: State, tail_tag: str) -> Iterable[str]:
        line_map = self.line_map(state)
        for line in self.buffer_lines:
            existing = line_map.get(line, ())
            if existing:
                if tail_tag != TAG_C4:
                    continue
                if not self.can_stack_c4_buffer(state, line, existing):
                    continue
            yield line

    def can_stack_c4_buffer(self, state: State, line: str, existing: tuple[str, ...]) -> bool:
        if not existing or any(self.tags.get(no) not in {TAG_C4, TAG_OUT} for no in existing):
            return False
        if line in DEPOT_OUT:
            inner = self.matching_inner(line)
            tags = [self.effective_line_tag(inner, no) for no in self.line_map(state).get(inner, ())]
            if any(tag in {TAG_OFF, TAG_U} for tag in tags) or self.has_c4_before_off(tags):
                return False
        return True

    def buffer_line_penalty(self, state: State, line: str) -> int:
        penalty = 0
        # Putting a tail back onto the line where the locomotive currently sits
        # often recreates the previous state. Keep it legal, but let Dijkstra try
        # genuinely different buffers first.
        if line in state.loco:
            penalty += 100
        if line in DEPOT_OUT:
            inner = self.matching_inner(line)
            tags = [self.effective_line_tag(inner, no) for no in self.line_map(state).get(inner, ())]
            # Blocking a future OFF/U extraction is worse than blocking a pure
            # C4 source, but both are legitimate reversible buffers.
            if any(tag in {TAG_OFF, TAG_U} for tag in tags):
                penalty += 20
            elif any(tag == TAG_C4 for tag in tags):
                penalty += 5
        return penalty

    def longest_suffix_that_fits(self, state: State, line: str, tail_run: tuple[str, ...]) -> tuple[str, ...]:
        for start in range(len(tail_run)):
            move = tail_run[start:]
            if self.line_has_capacity(state, line, move):
                return move
        return ()

    def matching_inner(self, outside_line: str) -> str:
        if outside_line in DEPOT_OUT:
            return outside_line.replace("库外", "库内")
        return ""

    def inner_has_pending_active(self, line_map: dict[str, tuple[str, ...]], inner_line: str) -> bool:
        return any(self.effective_line_tag(inner_line, no) in {TAG_C4, TAG_OFF, TAG_U} for no in line_map.get(inner_line, ()))

    def held_has_blocked_unwheel(self, held: tuple[str, ...]) -> bool:
        return any(self.tags.get(no) == TAG_U for no in held[:-1])

    def final_store4_put_allowed(self, state: State, move: tuple[str, ...]) -> bool:
        if state.new_store4:
            return False
        if move != state.held:
            return False
        if not move or any(self.tags.get(no) not in {TAG_C4, TAG_OFF} for no in move):
            return False
        candidate = tuple(move)
        if not self.is_off_star_c4_star(candidate):
            return False
        if not self.c4_front_closed_door_ok(candidate):
            return False
        if not self.line_has_capacity(state, STORE4, move):
            return False
        exclude = set(move)
        if self.count_remaining(state, TAG_C4, exclude=exclude):
            return False
        if self.count_remaining(state, TAG_OFF, exclude=exclude):
            return False
        return not self.has_pending_unwheel(state)

    def apply_get(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        line_map = self.line_map(state)
        if tuple(line_map.get(line, ())[: len(move)]) != move:
            return Op("Get", line, move, (), state.held), state, "get_order_violation"
        route, reject = self.route(state, "Get", line, move)
        if reject:
            return Op("Get", line, move, (), state.held), state, reject
        next_lines = dict(line_map)
        next_lines[line] = tuple(no for no in next_lines.get(line, ()) if no not in set(move))
        held_after = (*state.held, *move)
        if self.closed_door_non_store4_reject(held_after):
            return Op("Get", line, move, route, held_after), state, "closed_door_process_violation"
        next_state = State(
            lines=self.pack_lines(next_lines),
            held=held_after,
            loco=(line,),
            new_store4=state.new_store4,
        )
        return Op("Get", line, move, route, next_state.held), next_state, ""

    def apply_put(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        if not move or state.held[-len(move):] != move:
            return Op("Put", line, move, (), state.held), state, "put_tail_order_violation"
        if line == STORE4 and not self.final_store4_put_allowed(state, move):
            return Op("Put", line, move, (), state.held), state, "store4_put_must_be_single_final_put"
        if line != STORE4 and self.closed_door_non_store4_reject(state.held):
            return Op("Put", line, move, (), state.held), state, "closed_door_process_violation"
        if not self.line_has_capacity(state, line, move):
            return Op("Put", line, move, (), state.held), state, f"target_capacity_violation:{line}"
        route, reject = self.route(state, "Put", line, move)
        if reject:
            return Op("Put", line, move, (), state.held), state, reject
        line_map = dict(self.line_map(state))
        line_map[line] = (*move, *line_map.get(line, ()))
        held_after = tuple(no for no in state.held if no not in set(move))
        next_state = State(
            lines=self.pack_lines(line_map),
            held=held_after,
            loco=tuple(sorted(rv.put_loco_positions(route, line))),
            new_store4=((*move, *state.new_store4) if line == STORE4 else state.new_store4),
        )
        return Op("Put", line, move, route, held_after), next_state, ""

    def route(self, state: State, action: str, line: str, move: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
        if action == "Get":
            moving = set(state.held) | set(move)
            train_len = self.length(state.held)
        else:
            moving = set(state.held)
            train_len = self.length(state.held)
        cache_key = (
            state.lines,
            tuple(sorted(state.held)),
            state.loco,
            action,
            line,
            tuple(sorted(moving)),
            round(train_len, 3),
        )
        cached = self.route_cache.get(cache_key)
        if cached is not None:
            return cached
        cars = self.cars_from_state(state)
        choices: list[tuple[int, tuple[str, ...]]] = []
        blockers: list[str] = []
        for start in state.loco:
            path = self.stage2_route(action, start, line, cars, moving, train_len)
            rejected: list[str] = [] if path else [f"{start}->{line}"]
            if path:
                choices.append((len(path), tuple(path)))
            else:
                blockers.extend(rejected)
        if not choices:
            detail = blockers[0] if blockers else f"{sorted(state.loco)}->{line}"
            result = ((), f"{action.lower()}_route_blocked:{detail}")
            self.route_cache[cache_key] = result
            return result
        result = (min(choices, key=lambda item: (item[0], item[1]))[1], "")
        self.route_cache[cache_key] = result
        return result

    def stage2_route(
        self,
        action: str,
        start: str,
        line: str,
        cars: list[dict[str, Any]],
        moving: set[str],
        train_len: float,
    ) -> list[str]:
        target_approach = physical.route_approach_lines_for_put(line, cars, moving)
        if action == "Get":
            target_approach = physical.route_approach_lines_for_get(line)
        if action == "Put" and line == STORE4:
            target_approach = {STORE4_STAGE2_APPROACH}
        return self.graph.route_avoiding_occupied(
            start,
            line,
            physical.occupied_lines_for_route(cars, moving),
            source_departure_lines=physical.route_departure_lines_for_source(start, cars, moving),
            target_approach_lines=target_approach,
            cars=cars,
            moving_nos=moving,
            train_length_m=train_len,
        )

    def complete(self, state: State) -> bool:
        if state.held:
            return False
        line_map = self.line_map(state)
        for no in self.active_nos:
            tag = self.tags[no]
            line = self.current_line(line_map, no)
            if tag == TAG_U and line != UNWHEEL:
                return False
            if tag in {TAG_C4, TAG_OFF} and line != STORE4:
                return False
        if not self.is_off_star_c4_star(state.new_store4):
            return False
        return self.c4_front_closed_door_ok(state.new_store4)

    def stage2_debt(self, state: State | None) -> dict[str, Any]:
        if state is None:
            pending = sorted(no for no in self.active_nos if self.tags.get(no) != TAG_OUT)
            return {
                "complete": False,
                "pending_stage2_nos": pending,
                "new_store4_segment": [],
                "new_store4_pattern": "",
            }
        line_map = self.line_map(state)
        pending: list[str] = []
        for no in sorted(self.active_nos):
            tag = self.tags[no]
            line = self.current_line(line_map, no)
            if tag == TAG_U and line != UNWHEEL:
                pending.append(no)
            elif tag in {TAG_C4, TAG_OFF} and line != STORE4:
                pending.append(no)
        pattern = "".join("O" if self.tags[no] == TAG_OFF else "C" for no in state.new_store4)
        return {
            "complete": not pending and self.complete(state),
            "pending_stage2_nos": pending,
            "held": list(state.held),
            "new_store4_segment": list(state.new_store4),
            "new_store4_pattern": pattern,
            "closed_door_c4_front_ok": self.c4_front_closed_door_ok(state.new_store4),
        }

    def flex_out_positions(self, state: State | None) -> list[dict[str, Any]]:
        state = state or self.initial_state()
        line_map = self.line_map(state)
        rows: list[dict[str, Any]] = []
        for no in sorted(self.active_nos):
            if self.tags.get(no) != TAG_OUT:
                continue
            line = self.current_line(line_map, no)
            position = 0
            if line:
                position = line_map.get(line, ()).index(no) + 1
            elif no in state.held:
                position = state.held.index(no) + 1
            rows.append({
                "car_no": no,
                "line": line or "机后",
                "position": position,
                "target_lines": sorted(self.meta[no].get("TargetLines") or []),
            })
        return rows

    def result(self, state: State | None, ops: list[Op], reasons: list[str]) -> dict[str, Any]:
        response = {"Data": {"Operations": self.response_operations(ops)}}
        combined = {"Data": {"Operations": self.combined_operations(response)}}
        stage2_request = self.stage2_request(ops)
        replayed, replay_bad = rv.replay(stage2_request, response)
        hard_bad = [
            v for v in replay_bad
            if v.kind in {"schema", "physical"} and not self.waived_stage2_replay_violation(v)
        ]
        waived_bad = [
            v for v in replay_bad
            if v.kind in {"schema", "physical"} and self.waived_stage2_replay_violation(v)
        ]
        stage2_debt = self.stage2_debt(state)
        store4_put_indexes = [
            int(row.get("Index") or 0)
            for row in rv.operations(response)
            if row.get("Action") == "Put" and row.get("Line") == STORE4
        ]
        op_count = len(rv.operations(response))
        store4_put_is_single_final = (
            len(store4_put_indexes) == 1
            and store4_put_indexes[-1] == op_count
        )
        if hard_bad and not reasons:
            reasons = [f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12]]
        flex_out_positions = self.flex_out_positions(state)
        summary = {
            "case_id": self.case_id,
            "status": "complete" if stage2_debt["complete"] and not hard_bad else "partial",
            "operations": op_count,
            "business_hooks": sum(1 for row in rv.operations(response) if row.get("Action") in {"Get", "Put"}),
            "stage2_debt": stage2_debt,
            "flex_out_positions": flex_out_positions,
            "flex_out_in_store4": [
                item["car_no"]
                for item in flex_out_positions
                if item["line"] == STORE4
            ],
            "store4_put_count": len(store4_put_indexes),
            "store4_put_indexes": store4_put_indexes,
            "store4_put_is_final": store4_put_is_single_final,
            "store4_put_rule_ok": len(store4_put_indexes) <= 1 and (not store4_put_indexes or store4_put_indexes[-1] == op_count),
            "blocking_reasons": reasons,
            "replay_physical_ok": not hard_bad,
            "waived_replay_differences": [v.__dict__ for v in waived_bad[:20]],
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
            "search_mode": self.search_mode,
            "expansions": self.expansions,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
        }
        trace = [
            {
                "index": index,
                "action": op.action,
                "line": op.line,
                "move": list(op.move),
                "train_after": list(op.train_after),
                "path": list(op.path),
            }
            for index, op in enumerate(ops, start=1)
        ]
        return {
            "response": response,
            "combined_response": combined,
            "stage2_request": stage2_request,
            "summary": summary,
            "trace": trace,
        }

    def waived_stage2_replay_violation(self, violation: Any) -> bool:
        del violation
        return False

    def response_operations(self, ops: list[Op], *, start_index: int = 1) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for offset, op in enumerate(ops):
            rows.append({
                "Index": start_index + offset,
                "Line": op.line,
                "Action": op.action,
                "MoveCars": list(op.move),
                "TrainCars": list(op.train_after),
                "PassbyPath": list(op.path),
            })
        return rows

    def combined_operations(self, stage2_response: dict[str, Any]) -> list[dict[str, Any]]:
        base = [dict(row) for row in rv.operations(self.stage1_response)]
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
                for row in rv.operations(stage2_response)
            ],
            start_index=start,
        )
        return [*base, *extra]

    def stage2_request(self, ops: list[Op] | None = None) -> dict[str, Any]:
        request = dict(self.original_request)
        loco_line = self.initial_loco[0]
        if ops and ops[0].path:
            loco_line = ops[0].path[0]
        request["StartStatus"] = [
            self.output_car(car)
            for car in sorted(self.initial_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item)))
        ]
        request["locoNode"] = {"Line": loco_line, "End": "North"}
        return request

    def output_car(self, car: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in car.items()
            if not key.startswith("_") or key in {"_Weighed"}
        }

    def line_map(self, state: State) -> dict[str, tuple[str, ...]]:
        return {line: tuple(nos) for line, nos in state.lines}

    def pack_lines(self, line_map: dict[str, tuple[str, ...]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(sorted((line, tuple(nos)) for line, nos in line_map.items() if nos))

    def cars_from_state(self, state: State) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line, nos in state.lines:
            for pos, no in enumerate(nos, start=1):
                car = dict(self.meta[no])
                car["Line"] = line
                car["Position"] = pos
                rows.append(car)
        for no in state.held:
            car = dict(self.meta[no])
            car["Line"] = ""
            car["Position"] = 0
            rows.append(car)
        return rows

    def current_line(self, line_map: dict[str, tuple[str, ...]], no: str) -> str:
        for line, nos in line_map.items():
            if no in nos:
                return line
        return ""

    def effective_line_tag(self, line: str, no: str) -> str:
        tag = self.tags.get(no, TAG_STAY)
        if tag == TAG_U and line == UNWHEEL:
            return TAG_STAY
        if tag in {TAG_C4, TAG_OFF} and line == STORE4:
            return TAG_STAY
        return tag

    def is_off_star_c4_star(self, nos: Iterable[str]) -> bool:
        seen_c4 = False
        for no in nos:
            tag = self.tags.get(no, TAG_STAY)
            if tag == TAG_C4:
                seen_c4 = True
            elif tag == TAG_OFF and seen_c4:
                return False
            elif tag not in {TAG_C4, TAG_OFF}:
                return False
        return True

    def c4_front_closed_door_ok(self, nos: Iterable[str]) -> bool:
        front_c4 = [no for no in nos if self.tags.get(no) == TAG_C4][:3]
        return not any(self.meta[no].get("IsClosedDoor") for no in front_c4)

    def count_remaining(self, state: State, tag: str, *, exclude: set[str]) -> int:
        line_map = self.line_map(state)
        count = 0
        for line, nos in line_map.items():
            if line == STORE4:
                continue
            count += sum(1 for no in nos if no not in exclude and self.tags.get(no) == tag)
        count += sum(1 for no in state.held if no not in exclude and self.tags.get(no) == tag)
        return count

    def has_pending_unwheel(self, state: State) -> bool:
        line_map = self.line_map(state)
        for no in self.active_nos:
            if self.tags.get(no) == TAG_U and self.current_line(line_map, no) != UNWHEEL:
                return True
        return False

    def line_has_capacity(self, state: State, line: str, move: tuple[str, ...]) -> bool:
        limit = rv.TRACK_LEN.get(line)
        if limit is None:
            return False
        line_map = self.line_map(state)
        existing = sum(self.meta[no]["Length"] for no in line_map.get(line, ()))
        incoming = sum(self.meta[no]["Length"] for no in move)
        return existing + incoming <= limit + rv.TOL

    def closed_door_non_store4_reject(self, held: tuple[str, ...]) -> bool:
        if not held:
            return False
        first = self.meta[held[0]]
        if not first.get("IsClosedDoor"):
            return False
        return len(held) > 10 or any(self.meta[no].get("IsHeavy") for no in held)

    def pull_equivalent(self, nos: Iterable[str]) -> int:
        return sum(4 if self.meta[no].get("IsHeavy") else 1 for no in nos)

    def length(self, nos: Iterable[str]) -> float:
        return sum(float(self.meta[no].get("Length") or 14.3) for no in nos)

    def delta(self, op: Op) -> tuple[int, int, int, int]:
        lian7 = 1 if "联7" in op.path else 0
        off_penalty = self.off_group_penalty(op)
        return (1, lian7, len(op.path), off_penalty)

    def off_group_penalty(self, op: Op) -> int:
        if op.line != STORE4 or op.action != "Put":
            return 0
        destinations = []
        for no in op.move:
            if self.tags.get(no) == TAG_OFF:
                destinations.append("/".join(sorted(self.meta[no].get("TargetLines") or [])))
        return sum(1 for left, right in zip(destinations, destinations[1:]) if left != right)

    def time_exhausted(self) -> bool:
        return time.monotonic() - self.started_at >= self.time_budget_seconds


def request_paths(input_path: Path, case: str | None) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    paths = sorted(input_path.glob("*.json"))
    if case:
        target = case.upper()
        paths = [path for path in paths if case_id_from_path(path) == target]
    return paths


def solve_one(path: Path, stage1_out: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    summary_path = stage1_out / f"{case_id}_summary.json"
    response_path = stage1_out / f"{case_id}_response.json"
    if not response_path.exists():
        summary = {
            "case_id": case_id,
            "status": "partial",
            "operations": 0,
            "business_hooks": 0,
            "stage2_debt": {"complete": False, "pending_stage2_nos": []},
            "blocking_reasons": ["stage1_response_missing"],
            "replay_physical_ok": False,
            "replay_violations": [],
            "expansions": 0,
            "elapsed_seconds": 0.0,
        }
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    if summary_path.exists():
        stage1_summary = read_json(summary_path)
        if stage1_summary.get("status") != "complete" and not args.include_stage1_partial:
            summary = {
                "case_id": case_id,
                "status": "partial",
                "operations": 0,
                "business_hooks": 0,
                "stage2_debt": {"complete": False, "pending_stage2_nos": []},
                "blocking_reasons": [f"stage1_not_complete:{stage1_summary.get('status')}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
            write_json(out_dir / f"{case_id}_summary.json", summary)
            return summary

    solver = Stage2Solver(
        case_id,
        read_json(path),
        read_json(response_path),
        time_budget_seconds=args.time_budget_seconds,
        allow_depot_in_buffer=args.allow_depot_in_buffer,
        accept_upper_bound=args.accept_upper_bound,
    )
    result = solver.solve()
    write_json(out_dir / f"{case_id}_stage2_request.json", result["stage2_request"])
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    return result["summary"]


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(summaries)
    complete = sum(1 for item in summaries if item.get("status") == "complete")
    ops = [int(item.get("operations") or 0) for item in summaries if item.get("status") == "complete"]
    flex_out_lines = Counter(
        row.get("line")
        for item in summaries
        for row in item.get("flex_out_positions") or []
    )
    flex_out_in_store4 = [
        no
        for item in summaries
        for no in item.get("flex_out_in_store4") or []
    ]
    reasons = Counter(
        reason.split(":", 1)[0]
        for item in summaries
        if item.get("status") != "complete"
        for reason in item.get("blocking_reasons") or []
    )
    return {
        "cases": total,
        "complete": complete,
        "partial": total - complete,
        "avg_operations_complete": round(sum(ops) / len(ops), 3) if ops else 0,
        "max_operations_complete": max(ops) if ops else 0,
        "partial_reasons": dict(reasons.most_common()),
        "flex_out_final_lines": dict(flex_out_lines.most_common()),
        "flex_out_in_store4_count": len(flex_out_in_store4),
        "flex_out_in_store4": flex_out_in_store4,
        "summaries": summaries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 2 simple exact solver.")
    parser.add_argument("input", type=Path, help="request JSON file or directory")
    parser.add_argument("--stage1-out", type=Path, default=Path("artifacts/stage1_simple_initial_depot_done"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/stage2_simple"))
    parser.add_argument("--case", help="case id filter, e.g. 0226Z")
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--include-stage1-partial", action="store_true")
    parser.add_argument("--allow-depot-in-buffer", action="store_true")
    parser.add_argument("--accept-upper-bound", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in request_paths(args.input, args.case):
        case_id = case_id_from_path(path)
        print(case_id, flush=True)
        try:
            summaries.append(solve_one(path, args.stage1_out, args.out, args))
        except Exception as exc:  # keep batch runs diagnosable
            summary = {
                "case_id": case_id,
                "status": "error",
                "operations": 0,
                "business_hooks": 0,
                "stage2_debt": {"complete": False, "pending_stage2_nos": []},
                "blocking_reasons": [f"{type(exc).__name__}:{exc}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
            summaries.append(summary)
            write_json(args.out / f"{case_id}_summary.json", summary)
    agg = aggregate(summaries)
    write_json(args.out / "aggregate_summary.json", agg)
    print(
        "done "
        f"cases={agg['cases']} complete={agg['complete']} partial={agg['partial']} "
        f"avg_ops={agg['avg_operations_complete']} max_ops={agg['max_operations_complete']}",
        flush=True,
    )
    return 0 if all(item.get("status") == "complete" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
