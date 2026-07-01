from __future__ import annotations

from typing import Any

from . import legacy_adapter as legacy


def reverse_serial_blockers() -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for blocked_line, blocker_lines in legacy.legacy.SERIAL_LINE_BLOCKERS.items():
        for blocker_line in blocker_lines:
            reverse.setdefault(blocker_line, set()).add(blocked_line)
    return reverse


def serial_blocker_lines() -> set[str]:
    return set(reverse_serial_blockers())


def serial_related_lines() -> set[str]:
    return set(legacy.legacy.SERIAL_LINE_BLOCKERS) | serial_blocker_lines()


def downstream_lines(blocker_line: str) -> set[str]:
    reverse = reverse_serial_blockers()
    pending = list(reverse.get(blocker_line, ()))
    seen: set[str] = set()
    while pending:
        line = pending.pop(0)
        if line in seen:
            continue
        seen.add(line)
        pending.extend(reverse.get(line, ()))
    return seen


def downstream_debt_nos(
    *,
    blocker_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    moving_nos: set[str],
) -> list[str]:
    blocked = downstream_lines(blocker_line)
    if not blocked:
        return []
    loads = legacy.line_loads(cars)
    debt: list[str] = []
    for car in legacy.unsatisfied_cars(cars, depot_assignment):
        no = legacy.car_no(car)
        if no in moving_nos:
            continue
        target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
        if car["Line"] in blocked or target_line in blocked:
            debt.append(no)
    return debt
