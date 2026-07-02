from __future__ import annotations

from typing import Any

from . import physical


def reverse_serial_blockers() -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for blocked_line, blocker_lines in physical.SERIAL_LINE_BLOCKERS.items():
        for blocker_line in blocker_lines:
            reverse.setdefault(blocker_line, set()).add(blocked_line)
    return reverse


def serial_blocker_lines() -> set[str]:
    return set(reverse_serial_blockers())


def serial_related_lines() -> set[str]:
    return set(physical.SERIAL_LINE_BLOCKERS) | serial_blocker_lines()


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
    loads = physical.line_loads(cars)
    debt: list[str] = []
    for car in physical.unsatisfied_cars(cars, depot_assignment):
        no = physical.car_no(car)
        if no in moving_nos:
            continue
        target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
        if car["Line"] in blocked or target_line in blocked:
            debt.append(no)
    return debt


def lease_service_nos(lease: Any, car_nos: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    debt_nos = set(getattr(lease, "debt_nos", ()) or ())
    blocker_nos = set(getattr(lease, "blocker_nos", ()) or ())
    return tuple(no for no in car_nos if no in debt_nos and no not in blocker_nos)


def lease_pollution_nos(lease: Any, car_nos: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    debt_nos = set(getattr(lease, "debt_nos", ()) or ())
    blocker_nos = set(getattr(lease, "blocker_nos", ()) or ())
    car_set = set(car_nos)
    return tuple(sorted((car_set & blocker_nos) | (car_set - debt_nos)))


def lease_allows_put(
    lease: Any,
    put_nos: tuple[str, ...] | list[str] | set[str],
    owner_contract_id: str = "",
) -> bool:
    if owner_contract_id and getattr(lease, "owner_contract_id", "") != owner_contract_id:
        return False
    put_set = set(put_nos)
    if not put_set:
        return False
    return not lease_pollution_nos(lease, put_set) and bool(lease_service_nos(lease, put_set))
