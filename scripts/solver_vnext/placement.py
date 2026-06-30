from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import legacy_adapter as legacy


def planned_positions_for_batch(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    batch_nos: set[str],
) -> dict[str, int]:
    if legacy.legacy.is_spotting_line(target_line) and any(legacy.force_positions(car) for car in batch):
        return _spotting_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
    return legacy.legacy.planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
        grouped=legacy.cars_by_line(cars),
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
        if car["Line"] == target_line and legacy.car_no(car) not in batch_nos
    }
    planned: dict[str, int] = {}
    used: set[int] = set()
    forced_groups: dict[tuple[int, ...], list[dict[str, Any]]] = defaultdict(list)
    free_batch = []
    for car in batch:
        forced = legacy.force_positions(car)
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
            planned[legacy.car_no(car)] = position
            used.add(position)

    position = 1
    for car in free_batch:
        while position in occupied or position in used:
            position += 1
        planned[legacy.car_no(car)] = position
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
    existing_count = len(
        legacy.legacy.spotting_same_forced_positions(
            cars,
            target_line,
            forced,
            depot_assignment,
            excluded_nos=batch_nos,
        )
    )
    capacity = legacy.legacy.spotting_capacity(target_line, forced)
    if capacity and existing_count + needed > capacity:
        return []
    buffer_count = legacy.legacy.spotting_south_buffer_count(cars, target_line, forced, depot_assignment)
    valid_positions = legacy.legacy.spotting_effective_window(target_line, forced, buffer_count)
    return [
        position
        for position in sorted(valid_positions, reverse=True)
        if position not in occupied and position not in used
    ]
