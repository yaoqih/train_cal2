from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import replay_validator as rv
from solver_vnext import physical

from .model import Duty, STORE4, Stage2Problem


STORE4_APPROACH = "存4南"


@dataclass(frozen=True)
class YardState:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    held: tuple[str, ...]
    loco: tuple[str, ...]


@dataclass(frozen=True)
class Operation:
    action: str
    line: str
    move: tuple[str, ...]
    path: tuple[str, ...]
    train_after: tuple[str, ...]
    purpose: str = ""
    episode: str = ""


@dataclass(frozen=True)
class Transition:
    state: YardState | None
    operation: Operation | None
    rejection: str = ""

    @property
    def accepted(self) -> bool:
        return self.state is not None and self.operation is not None and not self.rejection


class Yard:
    """The single Stage 2 boundary for atomic physical transitions."""

    def __init__(self, problem: Stage2Problem, initial_loco: tuple[str, ...]) -> None:
        self.problem = problem
        self.graph = physical.TrackGraph()
        self.initial_state = YardState(
            lines=self.pack_lines(problem.initial_order),
            held=(),
            loco=tuple(sorted(set(initial_loco))),
        )
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}

    @staticmethod
    def line_map(state: YardState) -> dict[str, tuple[str, ...]]:
        return {line: tuple(nos) for line, nos in state.lines}

    @staticmethod
    def pack_lines(lines: dict[str, Iterable[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        packed = [(line, tuple(nos)) for line, nos in lines.items() if line]
        return tuple(sorted((line, nos) for line, nos in packed if nos))

    def cars(self, state: YardState) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line, nos in state.lines:
            for position, no in enumerate(nos, start=1):
                car = dict(self.problem.meta[no])
                car["Line"] = line
                car["Position"] = position
                rows.append(car)
        for no in state.held:
            car = dict(self.problem.meta[no])
            car["Line"] = ""
            car["Position"] = 0
            rows.append(car)
        return rows

    def get(
        self,
        state: YardState,
        line: str,
        move: tuple[str, ...],
        *,
        purpose: str,
        episode: str,
    ) -> Transition:
        lines = self.line_map(state)
        if not move or lines.get(line, ())[: len(move)] != move:
            return Transition(None, None, "get_order_violation")
        held_after = (*state.held, *move)
        if self.problem.pull_equivalent(held_after) > rv.PULL_LIMIT:
            return Transition(None, None, "pull_limit_violation")
        path, rejection = self.route(state, "Get", line, move)
        if rejection:
            return Transition(None, None, rejection)
        lines[line] = lines.get(line, ())[len(move) :]
        next_state = YardState(
            lines=self.pack_lines(lines),
            held=held_after,
            loco=(line,),
        )
        return Transition(
            next_state,
            Operation("Get", line, move, path, held_after, purpose, episode),
        )

    def put(
        self,
        state: YardState,
        line: str,
        move: tuple[str, ...],
        *,
        purpose: str,
        episode: str,
        final_store4: bool = False,
    ) -> Transition:
        if not move or state.held[-len(move) :] != move:
            return Transition(None, None, "put_tail_order_violation")
        if line == STORE4 and not final_store4:
            return Transition(None, None, "store4_put_is_terminal_only")
        if line != STORE4 and self.closed_door_process_rejected(state.held):
            return Transition(None, None, "closed_door_process_violation")
        if not self.has_capacity(state, line, move):
            return Transition(None, None, f"target_capacity_violation:{line}")
        path, rejection = self.route(state, "Put", line, move)
        if rejection:
            return Transition(None, None, rejection)
        lines = self.line_map(state)
        lines[line] = (*move, *lines.get(line, ()))
        held_after = state.held[: -len(move)]
        next_state = YardState(
            lines=self.pack_lines(lines),
            held=held_after,
            loco=tuple(sorted(rv.put_loco_positions(path, line))),
        )
        return Transition(
            next_state,
            Operation("Put", line, move, path, held_after, purpose, episode),
        )

    def route(
        self,
        state: YardState,
        action: str,
        line: str,
        move: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str]:
        moving = set(state.held)
        train_length = self.problem.length(state.held)
        if action == "Get":
            moving.update(move)
        key = (
            state.lines,
            state.held,
            state.loco,
            action,
            line,
            tuple(sorted(moving)),
            round(train_length, 3),
        )
        cached = self.route_cache.get(key)
        if cached is not None:
            return cached
        cars = self.cars(state)
        choices: list[tuple[int, tuple[str, ...]]] = []
        for start in state.loco:
            approach = (
                physical.route_approach_lines_for_get(line)
                if action == "Get"
                else physical.route_approach_lines_for_put(line, cars, moving)
            )
            if action == "Put" and line == STORE4:
                approach = {STORE4_APPROACH}
            path = self.graph.route_avoiding_occupied(
                start,
                line,
                physical.occupied_lines_for_route(cars, moving),
                source_departure_lines=physical.route_departure_lines_for_source(
                    start, cars, moving
                ),
                target_approach_lines=approach,
                cars=cars,
                moving_nos=moving,
                train_length_m=train_length,
            )
            if path:
                choices.append((len(path), tuple(path)))
        if not choices:
            result = ((), f"{action.lower()}_route_blocked:{','.join(state.loco)}->{line}")
        else:
            result = (min(choices, key=lambda item: (item[0], item[1]))[1], "")
        self.route_cache[key] = result
        return result

    def has_capacity(self, state: YardState, line: str, move: Iterable[str]) -> bool:
        limit = rv.TRACK_LEN.get(line)
        if limit is None:
            return False
        existing = self.problem.length(self.line_map(state).get(line, ()))
        incoming = self.problem.length(move)
        return existing + incoming <= limit + rv.TOL

    def closed_door_process_rejected(self, held: tuple[str, ...]) -> bool:
        if not held or not self.problem.meta[held[0]].get("IsClosedDoor"):
            return False
        return len(held) > 10 or any(
            self.problem.meta[no].get("IsHeavy") for no in held
        )

    @staticmethod
    def add_cost(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(left[index] + right[index] for index in range(len(left)))

    def operation_cost(self, operation: Operation) -> tuple[int, int, int, int]:
        return (
            1,
            sum(
                self.problem.tasks[no].duty is Duty.DEFER
                for no in operation.move
            )
            if operation.purpose == "restore_source_order"
            else 0,
            int("联7" in operation.path),
            len(operation.path),
        )
