from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import physical


def planned_positions_for_batch(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    batch_nos: set[str],
) -> dict[str, int]:
    if physical.is_spotting_line(target_line) and any(physical.force_positions(car) for car in batch):
        return _spotting_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
    return physical.planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
        grouped=physical.cars_by_line(cars),
    )


def _spotting_positions_for_batch(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    batch_nos: set[str],
) -> dict[str, int]:
    occupied = {
        int(car.get("Position") or 0)
        for car in cars
        if car["Line"] == target_line and physical.car_no(car) not in batch_nos
    }
    planned: dict[str, int] = {}
    used: set[int] = set()
    forced_groups: dict[tuple[int, ...], list[dict[str, Any]]] = defaultdict(list)
    free_batch = []
    for car in batch:
        forced = physical.force_positions(car)
        if forced:
            forced_groups[forced].append(car)
        else:
            free_batch.append(car)

    for forced, group in forced_groups.items():
        positions = _free_spotting_positions(
            target_line=target_line,
            forced=forced,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
            occupied=occupied,
            used=used,
            needed=len(group),
        )
        if len(positions) < len(group):
            return {}
        for car, position in zip(group, positions):
            planned[physical.car_no(car)] = position
            used.add(position)

    position = 1
    for car in free_batch:
        while position in occupied or position in used:
            position += 1
        planned[physical.car_no(car)] = position
        used.add(position)
    return planned


def _free_spotting_positions(
    *,
    target_line: str,
    forced: tuple[int, ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    batch_nos: set[str],
    occupied: set[int],
    used: set[int],
    needed: int,
) -> list[int]:
    existing_positions = physical.spotting_same_forced_positions(
        cars,
        target_line,
        forced,
        depot_assignment,
        excluded_nos=batch_nos,
    )
    capacity = physical.spotting_capacity(target_line, forced)
    if not capacity or len(existing_positions) + needed > capacity:
        return []
    window_sets = physical.spotting_physical_window_sets(target_line, forced)
    if not window_sets:
        return []
    existing_set = set(existing_positions)
    for window in window_sets:
        if not existing_set <= window:
            continue
        free_positions = [
            position
            for position in sorted(window, reverse=True)
            if position not in existing_set and position not in used
        ]
        if len(free_positions) >= needed:
            return free_positions
    return []
