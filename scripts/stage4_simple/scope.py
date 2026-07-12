from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Mapping

from solver_vnext import physical


OUT_OF_SCOPE_TARGETS = physical.DEPOT_TARGET_LINES | {"卸轮线"}
OUT_OF_SCOPE_SOURCES = physical.RUNNING_LINES


@dataclass(frozen=True)
class Stage4Scope:
    target_by_no: Mapping[str, str]
    active_nos: frozenset[str]
    protected_nos: frozenset[str]
    out_of_scope_nos: frozenset[str]
    excluded_source_nos: frozenset[str]
    infeasible_nos: frozenset[str]
    infeasible_lines: frozenset[str]
    capacity_overflow_by_line: Mapping[str, float]
    capacity_holdout_count_by_line: Mapping[str, int]


def build_scope(
    cars: list[dict],
    depot_assignment: physical.DepotAssignment,
) -> Stage4Scope:
    by_no = {physical.car_no(car): car for car in cars}
    initial_unsatisfied = frozenset(
        physical.car_no(car)
        for car in physical.unsatisfied_cars(cars, depot_assignment)
    )
    protected = frozenset(set(by_no) - set(initial_unsatisfied))
    targets: dict[str, str] = {}
    active: set[str] = set()
    out_of_scope: set[str] = set()
    excluded: set[str] = set()
    loads = physical.line_loads(cars)

    for car in cars:
        no = physical.car_no(car)
        target, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if target:
            targets[no] = target
        if no not in initial_unsatisfied:
            continue
        if not target or target in OUT_OF_SCOPE_TARGETS or target not in physical.TRACK_SPECS:
            out_of_scope.add(no)
            continue
        if car.get("Line") in OUT_OF_SCOPE_SOURCES:
            excluded.add(no)
            continue
        active.add(no)

    overflow_by_line: dict[str, float] = {}
    holdout_count_by_line: dict[str, int] = {}
    infeasible: set[str] = set()
    exact_by_target: dict[str, list[dict]] = {}
    for car in cars:
        options = {
            line
            for line in physical.target_lines(car)
            if line in physical.TRACK_SPECS and line not in OUT_OF_SCOPE_TARGETS
        }
        if len(options) == 1:
            exact_by_target.setdefault(next(iter(options)), []).append(car)

    for target, members in exact_by_target.items():
        overflow = (
            sum(physical.car_length(car) for car in members)
            - physical.TRACK_SPECS[target].length_m
            - physical.LINE_LENGTH_TOLERANCE_M
        )
        if overflow <= 1e-9:
            continue
        overflow_by_line[target] = overflow
        candidates = [
            car
            for car in members
            if physical.car_no(car) in active
        ]
        choice = minimum_holdout(candidates, overflow)
        infeasible.update(physical.car_no(car) for car in choice)
        holdout_count_by_line[target] = len(choice)

    active.difference_update(infeasible)
    return Stage4Scope(
        target_by_no=targets,
        active_nos=frozenset(active),
        protected_nos=protected,
        out_of_scope_nos=frozenset(out_of_scope),
        excluded_source_nos=frozenset(excluded),
        infeasible_nos=frozenset(infeasible),
        infeasible_lines=frozenset(overflow_by_line),
        capacity_overflow_by_line=overflow_by_line,
        capacity_holdout_count_by_line=holdout_count_by_line,
    )


def minimum_holdout(cars: list[dict], overflow_m: float) -> tuple[dict, ...]:
    if not cars:
        return ()
    for size in range(1, len(cars) + 1):
        feasible = [
            group
            for group in combinations(cars, size)
            if sum(physical.car_length(car) for car in group) + 1e-9 >= overflow_m
        ]
        if feasible:
            return min(
                feasible,
                key=lambda group: (
                    round(sum(physical.car_length(car) for car in group) - overflow_m, 6),
                    sum(int(car.get("Position") or 0) for car in group),
                    tuple(sorted(physical.car_no(car) for car in group)),
                ),
            )
    return tuple(cars)
