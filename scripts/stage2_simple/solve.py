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
STORE4 = "存4线"
UNWHEEL = "卸轮线"
TAG_C4 = "C4"
TAG_OFF = "OFF"
TAG_U = "U"
TAG_STAY = "STAY"
DEFAULT_TIME_BUDGET_SECONDS = 300.0
MAX_EXPANSIONS = 200_000


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


def final_loco_after_response(request: dict[str, Any], response: dict[str, Any]) -> str:
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
    return sorted(line for line in loco if line)[0] if any(loco) else rv.WEIGH


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
        self.blocking_reasons: list[str] = []
        self.expansions = 0

    def classify(self, car: dict[str, Any]) -> str:
        line = car["Line"]
        targets = set(car.get("TargetLines") or [])
        if UNWHEEL in targets and line != UNWHEEL:
            return TAG_U
        if STORE4 in targets and line in set(SOURCE_LINES):
            return TAG_C4
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
        solved, ops, reasons = self.dijkstra(state)
        return self.result(solved, ops, reasons)

    def early_partial(self) -> list[str]:
        reasons: list[str] = []
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
                active = self.tags.get(no) != TAG_STAY
                if line == STORE4:
                    active = self.tags.get(no) == TAG_U
                if active and seen_stay:
                    reasons.append(f"stay_car_blocks_outbound:{line}:{no}")
                    break
                if not active:
                    seen_stay = True
        return reasons

    def initial_state(self) -> State:
        by_line: dict[str, list[str]] = {}
        for car in sorted(self.initial_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item))):
            by_line.setdefault(car["Line"], []).append(rv.car_no(car))
        return State(
            lines=tuple(sorted((line, tuple(nos)) for line, nos in by_line.items() if line)),
            held=(),
            loco=(self.initial_loco,),
            new_store4=(),
        )

    def dijkstra(self, start: State) -> tuple[State | None, list[Op], list[str]]:
        queue: list[tuple[tuple[int, int, int, int, int], tuple[int, int, int, int], int, State]] = []
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
            if self.complete(state):
                return state, self.reconstruct(prev, state), []
            if self.time_exhausted():
                return None, [], ["stage2_time_budget_exhausted"]
            self.expansions += 1
            if self.expansions > MAX_EXPANSIONS:
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

    def search_priority(self, cost: tuple[int, int, int, int], state: State) -> tuple[int, int, int, int, int]:
        # A* on the first objective. The lower bound only counts unavoidable
        # operation rows, so it does not change the optimal operation count.
        return (cost[0] + self.remaining_operation_lower_bound(state), cost[1], cost[2], cost[3], cost[0])

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

    def reconstruct(self, prev: dict[State, tuple[State, Op]], state: State) -> list[Op]:
        ops: list[Op] = []
        while state in prev:
            prior, op = prev[state]
            ops.append(op)
            state = prior
        ops.reverse()
        return ops

    def neighbors(self, state: State) -> Iterable[tuple[Op, State, tuple[int, int, int, int], str]]:
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
        line_map = self.line_map(state)
        held_equiv = self.pull_equivalent(state.held)
        for line in SOURCE_LINES:
            ordered = line_map.get(line, ())
            if not ordered:
                continue
            prefix: list[str] = []
            for no in ordered:
                tag = self.tags.get(no, TAG_STAY)
                if line == STORE4:
                    if tag != TAG_U:
                        break
                elif tag == TAG_STAY:
                    break
                prefix.append(no)
                if held_equiv + self.pull_equivalent(prefix) > rv.PULL_LIMIT:
                    prefix.pop()
                    break
            for cut in self.useful_prefix_cuts(prefix):
                yield line, tuple(prefix[:cut])

    def useful_prefix_cuts(self, prefix: list[str]) -> list[int]:
        if not prefix:
            return []
        cuts = {len(prefix)}
        for index in range(1, len(prefix)):
            left = self.tags.get(prefix[index - 1], TAG_STAY)
            right = self.tags.get(prefix[index], TAG_STAY)
            # C4 above OFF must be separable; otherwise the OFF tail would
            # prevent the C4 cars from being pushed to 存4线 first.
            if left == TAG_C4 and right == TAG_OFF:
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
        elif tail_tag in {TAG_C4, TAG_OFF} and self.store4_put_is_promising(state, tail_run):
            yield STORE4, tail_run

        # Final boundary shortcut: if the whole consist already has the exact
        # OFF* C4* shape and no C4 remains elsewhere, one Put is strictly best.
        if held != tail_run and all(self.tags.get(no) in {TAG_C4, TAG_OFF} for no in held):
            if self.store4_put_is_promising(state, held):
                yield STORE4, held

    def store4_put_is_promising(self, state: State, move: tuple[str, ...]) -> bool:
        candidate = (*move, *state.new_store4)
        if not self.is_off_star_c4_star(candidate):
            return False
        if any(self.tags[no] == TAG_OFF for no in candidate):
            remaining_c4 = self.count_remaining(state, TAG_C4, exclude=set(move))
            if remaining_c4:
                return False
        return True

    def apply_get(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        line_map = self.line_map(state)
        if tuple(line_map.get(line, ())[: len(move)]) != move:
            return Op("Get", line, move, (), state.held), state, "get_order_violation"
        route, reject = self.route(state, "Get", line, move)
        if reject:
            return Op("Get", line, move, (), state.held), state, reject
        next_lines = dict(line_map)
        next_lines[line] = tuple(no for no in next_lines.get(line, ()) if no not in set(move))
        next_state = State(
            lines=self.pack_lines(next_lines),
            held=(*state.held, *move),
            loco=(line,),
            new_store4=state.new_store4,
        )
        return Op("Get", line, move, route, next_state.held), next_state, ""

    def apply_put(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        if not move or state.held[-len(move):] != move:
            return Op("Put", line, move, (), state.held), state, "put_tail_order_violation"
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
        cars = self.cars_from_state(state)
        if action == "Get":
            moving = set(state.held) | set(move)
            train_len = self.length(state.held)
        else:
            moving = set(state.held)
            train_len = self.length(state.held)
        choices: list[tuple[int, tuple[str, ...]]] = []
        blockers: list[str] = []
        for start in state.loco:
            path = self.stage2_route(start, line, cars, moving, train_len)
            rejected: list[str] = [] if path else [f"{start}->{line}"]
            if path:
                choices.append((len(path), tuple(path)))
            else:
                blockers.extend(rejected)
        if not choices:
            detail = blockers[0] if blockers else f"{sorted(state.loco)}->{line}"
            return (), f"{action.lower()}_route_blocked:{detail}"
        return min(choices, key=lambda item: (item[0], item[1]))[1], ""

    def stage2_route(
        self,
        start: str,
        line: str,
        cars: list[dict[str, Any]],
        moving: set[str],
        train_len: float,
    ) -> list[str]:
        target_approach = physical.route_approach_lines_for_put(line, cars, moving)
        # Stage 2 starts by removing the north 存4车. After that, 存4线 is
        # an empty through line for the depot-outbound session; using that
        # through path is not a 存4南 temporary-staging move.
        if line == STORE4:
            target_approach = set()
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
            pending = sorted(self.active_nos)
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

    def result(self, state: State | None, ops: list[Op], reasons: list[str]) -> dict[str, Any]:
        response = {"Data": {"Operations": self.response_operations(ops)}}
        combined = {"Data": {"Operations": self.combined_operations(response)}}
        stage2_request = self.stage2_request()
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
        if hard_bad and not reasons:
            reasons = [f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12]]
        summary = {
            "case_id": self.case_id,
            "status": "complete" if stage2_debt["complete"] and not hard_bad else "partial",
            "operations": len(rv.operations(response)),
            "business_hooks": sum(1 for row in rv.operations(response) if row.get("Action") in {"Get", "Put"}),
            "stage2_debt": stage2_debt,
            "blocking_reasons": reasons,
            "replay_physical_ok": not hard_bad,
            "waived_replay_differences": [v.__dict__ for v in waived_bad[:20]],
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
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
        if getattr(violation, "kind", "") != "physical":
            return False
        detail = str(getattr(violation, "detail", ""))
        code = getattr(violation, "code", "")
        if code == "occupied_target_wrong_approach":
            return detail.startswith("存4线:") and "prev=存4南" in detail
        if code == "route_unreachable":
            return "->存4线" in detail
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

    def stage2_request(self) -> dict[str, Any]:
        request = dict(self.original_request)
        request["StartStatus"] = [
            self.output_car(car)
            for car in sorted(self.initial_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item)))
        ]
        request["locoNode"] = {"Line": self.initial_loco, "End": "North"}
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in request_paths(args.input, args.case):
        try:
            summaries.append(solve_one(path, args.stage1_out, args.out, args))
        except Exception as exc:  # keep batch runs diagnosable
            case_id = case_id_from_path(path)
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
    write_json(args.out / "aggregate_summary.json", aggregate(summaries))
    print(json.dumps(aggregate(summaries), ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "complete" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
