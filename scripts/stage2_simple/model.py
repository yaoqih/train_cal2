from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import Any, Iterable

import replay_validator as rv


DEPOT_IN = tuple(f"修{i}库内" for i in range(1, 5))
DEPOT_OUT = tuple(f"修{i}库外" for i in range(1, 5))
STORE4 = "存4线"
UNWHEEL = "卸轮线"
PAINT = "油漆线"
SOURCE_LINES = (*DEPOT_IN, *DEPOT_OUT, UNWHEEL, STORE4)
SCRATCH_LINES = DEPOT_OUT


class Duty(str, Enum):
    FINAL4 = "final4"
    UNWHEEL = "unwheel"
    OPTIONAL_UNWHEEL = "optional_unwheel"
    RESTORE = "restore"
    DEFER = "defer"


class Store4Kind(str, Enum):
    OFF = "off"
    C4 = "c4"


@dataclass(frozen=True)
class CarTask:
    no: str
    source: str
    duty: Duty
    store4_kind: Store4Kind | None = None


@dataclass(frozen=True)
class EpisodeChoice:
    source: str
    collect: tuple[str, ...]
    unload: tuple[str, ...]
    deferred_final: tuple[str, ...]
    deferred_optional: tuple[str, ...]
    relocate: tuple[str, ...] = ()
    relocate_to: str = ""

    @property
    def selected(self) -> frozenset[str]:
        return frozenset((*self.collect, *self.unload))


class Stage2Problem:
    """Immutable business model compiled from the Stage 1 end state."""

    def __init__(self, cars: list[dict[str, Any]]) -> None:
        self.cars = tuple(dict(car) for car in cars)
        self.meta = {rv.car_no(car): dict(car) for car in self.cars}
        self.initial_order = self._line_orders(self.cars)
        self.tasks = {
            no: self._classify(car)
            for no, car in self.meta.items()
        }
        self.final_nos = frozenset(
            no for no, task in self.tasks.items() if task.duty is Duty.FINAL4
        )
        self.required_unwheel_nos = frozenset(
            no for no, task in self.tasks.items() if task.duty is Duty.UNWHEEL
        )
        self.optional_unwheel_nos = frozenset(
            no
            for no, task in self.tasks.items()
            if task.duty is Duty.OPTIONAL_UNWHEEL
        )
        self.unweighed_nos = tuple(
            sorted(
                no
                for no, car in self.meta.items()
                if car.get("IsWeigh") and not car.get("_Weighed")
            )
        )
        obligation_lines = tuple(
            line
            for line in SOURCE_LINES
            if any(
                self.tasks[no].duty
                in {Duty.FINAL4, Duty.UNWHEEL, Duty.OPTIONAL_UNWHEEL}
                for no in self.initial_order.get(line, ())
            )
        )
        self.corridor_lines = tuple(
            outer
            for outer in DEPOT_OUT
            if self.initial_order.get(outer)
            and outer not in obligation_lines
            and outer.replace("库外", "库内") in obligation_lines
        )
        active_lines = set(obligation_lines) | set(self.corridor_lines)
        self.source_lines = tuple(line for line in SOURCE_LINES if line in active_lines)
        self.allow_final_deferral = self.pull_equivalent(self.final_nos) > rv.PULL_LIMIT
        self.minimum_final_deferrals = self._minimum_final_deferrals()
        self.admissible_final_deferrals = self._admissible_final_deferrals()
        self.minimum_optional_deferrals = self._minimum_optional_deferrals()
        self.admissible_optional_deferrals = self._admissible_optional_deferrals()

    def _classify(self, car: dict[str, Any]) -> CarTask:
        no = rv.car_no(car)
        line = rv.norm(car.get("Line"))
        targets = {rv.norm(value) for value in car.get("TargetLines") or ()}
        if line not in SOURCE_LINES:
            return CarTask(no, line, Duty.RESTORE)
        if UNWHEEL in targets and line != UNWHEEL:
            return CarTask(no, line, Duty.UNWHEEL)
        if PAINT in targets:
            duty = Duty.DEFER if line == UNWHEEL else Duty.OPTIONAL_UNWHEEL
            return CarTask(no, line, duty)
        if STORE4 in targets and line != STORE4:
            return CarTask(no, line, Duty.FINAL4, Store4Kind.C4)
        if targets & set(DEPOT_OUT):
            return CarTask(no, line, Duty.DEFER)
        if not (targets & set(DEPOT_IN)) and UNWHEEL not in targets:
            return CarTask(no, line, Duty.FINAL4, Store4Kind.OFF)
        return CarTask(no, line, Duty.RESTORE)

    @staticmethod
    def _line_orders(cars: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
        by_line: dict[str, list[dict[str, Any]]] = {}
        for car in cars:
            line = rv.norm(car.get("Line"))
            if line:
                by_line.setdefault(line, []).append(car)
        return {
            line: tuple(
                rv.car_no(car)
                for car in sorted(
                    rows,
                    key=lambda item: (int(item.get("Position") or 0), rv.car_no(item)),
                )
            )
            for line, rows in by_line.items()
        }

    def choices(self, source: str) -> tuple[EpisodeChoice, ...]:
        ordered = self.initial_order.get(source, ())
        if source in self.corridor_lines:
            return tuple(
                EpisodeChoice(
                    source=source,
                    collect=(),
                    unload=(),
                    deferred_final=(),
                    deferred_optional=(),
                    relocate=ordered,
                    relocate_to=target,
                )
                for target in self._corridor_targets(source, ordered)
            )
        off = tuple(
            no
            for no in ordered
            if self.tasks[no].duty is Duty.FINAL4
            and self.tasks[no].store4_kind is Store4Kind.OFF
        )
        c4 = tuple(
            no
            for no in ordered
            if self.tasks[no].duty is Duty.FINAL4
            and self.tasks[no].store4_kind is Store4Kind.C4
        )
        required_unwheel = tuple(
            no for no in ordered if self.tasks[no].duty is Duty.UNWHEEL
        )
        optional = tuple(
            no
            for no in ordered
            if self.tasks[no].duty is Duty.OPTIONAL_UNWHEEL
        )
        local_deferrals = {
            tuple(no for no in c4 if no in deferral)
            for deferral in self.admissible_final_deferrals
        }
        collect_options = tuple(
            tuple(no for no in c4 if no not in set(deferred))
            for deferred in sorted(local_deferrals)
        )
        local_optional_deferrals = {
            tuple(no for no in optional if no in deferral)
            for deferral in self.admissible_optional_deferrals
        }
        optional_options = tuple(
            tuple(no for no in optional if no not in set(deferred))
            for deferred in sorted(local_optional_deferrals)
        )
        choices: list[EpisodeChoice] = []
        for selected_c4 in collect_options:
            selected_c4_set = set(selected_c4)
            for selected_optional in optional_options:
                selected_optional_set = set(selected_optional)
                choices.append(
                    EpisodeChoice(
                        source=source,
                        collect=tuple(
                            no for no in ordered if no in set(off) | selected_c4_set
                        ),
                        unload=tuple(
                            no
                            for no in ordered
                            if no in set(required_unwheel) | selected_optional_set
                        ),
                        deferred_final=tuple(no for no in c4 if no not in selected_c4_set),
                        deferred_optional=tuple(
                            no for no in optional if no not in selected_optional_set
                        ),
                    )
                )
        return tuple(
            sorted(
                choices,
                key=lambda choice: (
                    len(choice.deferred_final),
                    len(choice.deferred_optional),
                    -len(choice.collect),
                    -len(choice.unload),
                    choice.collect,
                    choice.unload,
                    choice.relocate_to,
                ),
            )
        )

    def _corridor_targets(
        self, source: str, move: tuple[str, ...]
    ) -> tuple[str, ...]:
        requested = tuple(
            line
            for line in DEPOT_OUT
            if line != source
            and all(line in set(self.meta[no].get("TargetLines") or ()) for no in move)
        )
        remaining = tuple(line for line in DEPOT_OUT if line != source and line not in requested)
        return (*requested, *remaining)

    def _minimum_final_deferrals(self) -> int:
        excess = self.pull_equivalent(self.final_nos) - rv.PULL_LIMIT
        if excess <= 0:
            return 0
        values = tuple(
            sorted(
                no
                for no in self.final_nos
                if self.store4_kind(no) is Store4Kind.C4
            )
        )
        for count in range(1, len(values) + 1):
            if any(
                self.pull_equivalent(values[index] for index in indexes) >= excess
                for indexes in combinations(range(len(values)), count)
            ):
                return count
        return len(values)

    def _admissible_final_deferrals(self) -> tuple[frozenset[str], ...]:
        if not self.allow_final_deferral:
            return (frozenset(),)
        c4 = tuple(
            sorted(
                no
                for no in self.final_nos
                if self.store4_kind(no) is Store4Kind.C4
            )
        )
        candidates: list[tuple[int, frozenset[str]]] = []
        for indexes in combinations(range(len(c4)), self.minimum_final_deferrals):
            deferred = frozenset(c4[index] for index in indexes)
            selected = self.final_nos - deferred
            if self.pull_equivalent(selected) > rv.PULL_LIMIT:
                continue
            selected_c4 = [
                no for no in selected if self.store4_kind(no) is Store4Kind.C4
            ]
            required_clear_front = min(3, len(selected_c4))
            if sum(not self.meta[no].get("IsClosedDoor") for no in selected_c4) < required_clear_front:
                continue
            candidates.append((self._deferral_inversions(deferred), deferred))
        if not candidates:
            return ()
        minimum_inversions = min(score for score, _deferral in candidates)
        return tuple(
            sorted(
                (
                    deferral
                    for score, deferral in candidates
                    if score == minimum_inversions
                ),
                key=lambda values: tuple(sorted(values)),
            )
        )

    def _deferral_inversions(self, deferred: frozenset[str]) -> int:
        inversions = 0
        for line in SOURCE_LINES:
            seen_deferred = False
            for no in self.initial_order.get(line, ()):
                if no not in self.final_nos:
                    continue
                if no in deferred:
                    seen_deferred = True
                elif seen_deferred:
                    inversions += 1
        return inversions

    def _minimum_optional_deferrals(self) -> int:
        optional = tuple(sorted(self.optional_unwheel_nos))
        capacity = self._optional_unwheel_capacity()
        for selected_count in range(len(optional), -1, -1):
            if any(
                self.length(optional[index] for index in indexes) <= capacity + rv.TOL
                for indexes in combinations(range(len(optional)), selected_count)
            ):
                return len(optional) - selected_count
        return len(optional)

    def _admissible_optional_deferrals(self) -> tuple[frozenset[str], ...]:
        optional = tuple(sorted(self.optional_unwheel_nos))
        if not optional:
            return (frozenset(),)
        selected_count = len(optional) - self.minimum_optional_deferrals
        capacity = self._optional_unwheel_capacity()
        candidates: list[tuple[int, frozenset[str]]] = []
        for indexes in combinations(range(len(optional)), selected_count):
            selected = frozenset(optional[index] for index in indexes)
            if self.length(selected) > capacity + rv.TOL:
                continue
            deferred = frozenset(optional) - selected
            candidates.append((self._optional_deferral_inversions(deferred), deferred))
        if not candidates:
            return (frozenset(optional),)
        minimum_inversions = min(score for score, _deferral in candidates)
        return tuple(
            sorted(
                (
                    deferral
                    for score, deferral in candidates
                    if score == minimum_inversions
                ),
                key=lambda values: tuple(sorted(values)),
            )
        )

    def _optional_unwheel_capacity(self) -> float:
        base = tuple(
            no
            for no in self.initial_order.get(UNWHEEL, ())
            if self.tasks[no].duty is not Duty.FINAL4
        )
        required = tuple(self.required_unwheel_nos)
        return rv.TRACK_LEN[UNWHEEL] - self.length((*base, *required))

    def _optional_deferral_inversions(self, deferred: frozenset[str]) -> int:
        inversions = 0
        for line in SOURCE_LINES:
            seen_deferred = False
            for no in self.initial_order.get(line, ()):
                if no not in self.optional_unwheel_nos:
                    continue
                if no in deferred:
                    seen_deferred = True
                elif seen_deferred:
                    inversions += 1
        return inversions

    def store4_kind(self, no: str) -> Store4Kind | None:
        return self.tasks[no].store4_kind

    def collector_pattern_ok(self, nos: Iterable[str]) -> bool:
        seen_c4 = False
        for no in nos:
            kind = self.store4_kind(no)
            if kind is Store4Kind.C4:
                seen_c4 = True
            elif kind is Store4Kind.OFF and seen_c4:
                return False
            elif kind is None:
                return False
        return True

    def collector_closed_door_ok(self, nos: Iterable[str]) -> bool:
        front_c4 = [
            no for no in nos if self.store4_kind(no) is Store4Kind.C4
        ][:3]
        return not any(self.meta[no].get("IsClosedDoor") for no in front_c4)

    def pull_equivalent(self, nos: Iterable[str]) -> int:
        return sum(4 if self.meta[no].get("IsHeavy") else 1 for no in nos)

    def length(self, nos: Iterable[str]) -> float:
        return sum(float(self.meta[no].get("Length") or 0.0) for no in nos)

    def target_key(self, no: str) -> str:
        return "/".join(sorted(rv.norm(value) for value in self.meta[no].get("TargetLines") or ()))
