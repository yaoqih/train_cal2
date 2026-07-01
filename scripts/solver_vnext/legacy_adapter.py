from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_physical_runtime_trace as legacy  # noqa: E402


_original_car_no = legacy.car_no
_original_normalize_line = legacy.normalize_line
_original_active_serial_blockers_for_line = legacy.active_serial_blockers_for_line
_original_occupied_lines_for_route = legacy.occupied_lines_for_route
_original_cars_by_line = legacy.cars_by_line
_normalize_line_cache: dict[Any, str] = {}


def _fast_car_no(car: dict[str, Any]) -> str:
    try:
        return car["_No"]
    except KeyError:
        return _original_car_no(car)


def _cached_normalize_line(value: Any) -> str:
    try:
        cached = _normalize_line_cache.get(value)
    except TypeError:
        return _original_normalize_line(value)
    if cached is not None:
        return cached
    normalized = _original_normalize_line(value)
    _normalize_line_cache[value] = normalized
    return normalized


def _fast_active_serial_blockers_for_line(
    line: str,
    cars: list[dict[str, Any]],
    moving_nos: set[str],
) -> list[tuple[str, list[str]]]:
    normalized_line = legacy.normalize_line(line)
    occupied_by_line: dict[str, list[str]] = {}
    try:
        ordered_cars = sorted(cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), item["_No"]))
        for car in ordered_cars:
            no = car["_No"]
            car_line = car["Line"]
            if not car_line or no in moving_nos:
                continue
            occupied_by_line.setdefault(car_line, []).append(no)
    except KeyError:
        return _original_active_serial_blockers_for_line(line, cars, moving_nos)

    blockers: list[tuple[str, list[str]]] = []
    seen_blocker_lines: set[str] = set()
    pending = [normalized_line]
    seen_lines: set[str] = set()
    while pending:
        blocked_line = pending.pop(0)
        if blocked_line in seen_lines:
            continue
        seen_lines.add(blocked_line)
        for blocker_line in legacy.SERIAL_LINE_BLOCKERS.get(blocked_line, ()):
            blocker_nos = occupied_by_line.get(blocker_line, [])
            if blocker_nos and blocker_line not in seen_blocker_lines:
                blockers.append((blocker_line, blocker_nos))
                seen_blocker_lines.add(blocker_line)
            pending.append(blocker_line)
    return blockers


def _fast_occupied_lines_for_route(cars: list[dict[str, Any]], moving_nos: set[str]) -> set[str]:
    try:
        return {car["Line"] for car in cars if car["Line"] and car["_No"] not in moving_nos}
    except KeyError:
        return _original_occupied_lines_for_route(cars, moving_nos)


def _fast_cars_by_line(cars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        for car in cars:
            grouped.setdefault(car["Line"], []).append(car)
        for line_cars in grouped.values():
            line_cars.sort(key=lambda item: (int(item.get("Position") or 0), item["_No"]))
        return grouped
    except KeyError:
        return _original_cars_by_line(cars)


legacy.car_no = _fast_car_no
legacy.normalize_line = _cached_normalize_line
legacy.active_serial_blockers_for_line = _fast_active_serial_blockers_for_line
legacy.occupied_lines_for_route = _fast_occupied_lines_for_route
legacy.cars_by_line = _fast_cars_by_line

REMOTE_INTERACTION_LINES = legacy.REMOTE_INTERACTION_LINES
DEPOT_LINES = legacy.DEPOT_LINES
DEPOT_TARGET_LINES = legacy.DEPOT_TARGET_LINES
FRONT_SERVICE_TARGET_LINES = legacy.FRONT_SERVICE_TARGET_LINES
RUNNING_LINES = legacy.RUNNING_LINES

_access_order_cache: dict[tuple[int, int, str, str, str], list[dict[str, Any]]] = {}
_unsatisfied_cache: dict[tuple[int, int], tuple[dict[str, Any], ...]] = {}
_line_loads_cache: dict[int, Any] = {}


def clear_access_order_cache() -> None:
    _access_order_cache.clear()
    _unsatisfied_cache.clear()
    _line_loads_cache.clear()


def read_case(path: Path) -> tuple[str, dict[str, Any], list[dict[str, Any]], Any, Any]:
    payload = legacy.read_json(path)
    input_ok, errors = legacy.validate_input(payload)
    if not input_ok:
        raise ValueError("|".join(errors))
    case_id = legacy.case_id_from_path(path)
    cars = [legacy.normalized_car(car) for car in payload.get("StartStatus") or []]
    capacities = legacy.terminal_capacity_by_line(payload)
    depot_assignment = legacy.build_depot_assignment([dict(car) for car in cars], capacities)
    loco = legacy.initial_loco_location(payload.get("locoNode") or {})
    return case_id, payload, cars, depot_assignment, loco


def car_no(car: dict[str, Any]) -> str:
    return legacy.car_no(car)


def car_length(car: dict[str, Any]) -> float:
    return legacy.car_length(car)


def force_positions(car: dict[str, Any]) -> tuple[int, ...]:
    return legacy.force_positions(car)


def planned_target_for_car(
    car: dict[str, Any],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    loads: Any | None = None,
) -> tuple[str, int | None, str]:
    return legacy.planned_target_for_car(car, cars, depot_assignment, loads)


def car_is_satisfied(car: dict[str, Any], depot_assignment: Any, cars: list[dict[str, Any]]) -> bool:
    return legacy.car_is_satisfied(car, depot_assignment, cars)


def unsatisfied_cars(cars: list[dict[str, Any]], depot_assignment: Any) -> list[dict[str, Any]]:
    key = (id(cars), id(depot_assignment))
    cached = _unsatisfied_cache.get(key)
    if cached is not None:
        return list(cached)
    unsatisfied = tuple(legacy.unsatisfied_cars(cars, depot_assignment))
    _unsatisfied_cache[key] = unsatisfied
    return list(unsatisfied)


def state_signature(cars: list[dict[str, Any]], loco_location: Any) -> tuple[str, str, tuple[tuple[str, str, int], ...]]:
    return legacy.state_signature(cars, loco_location)


def line_loads(cars: list[dict[str, Any]]) -> Any:
    key = id(cars)
    cached = _line_loads_cache.get(key)
    if cached is None:
        cached = legacy.line_loads(cars)
        _line_loads_cache[key] = cached
    return cached.copy()


def cars_by_line(cars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return legacy.cars_by_line(cars)


def line_cars_in_access_order(
    *,
    cars: list[dict[str, Any]],
    line: str,
    graph: Any,
    loco_location: Any,
) -> list[dict[str, Any]]:
    key = (
        id(cars),
        id(graph),
        line,
        str(getattr(loco_location, "line", "")),
        str(getattr(loco_location, "node", "")),
    )
    cached = _access_order_cache.get(key)
    if cached is not None:
        return list(cached)
    ordered = legacy.line_cars_in_access_order(
        cars=cars,
        line=line,
        access_context=legacy.PhysicalAccessContext(graph=graph, loco_location=loco_location),
    )
    _access_order_cache[key] = list(ordered)
    return ordered


def pull_equivalent(cars: list[dict[str, Any]]) -> int:
    return legacy.pull_equivalent(cars)


def build_direct_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str = "vnext_target_move",
    planned_positions: dict[str, int] | None = None,
) -> Any | None:
    if planned_positions is None:
        candidate = legacy.direct_candidate_for_batch(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            generation_reason=reason,
            grouped=legacy.cars_by_line(cars),
        )
    else:
        candidate = legacy.hook_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            planned_positions=planned_positions,
            generation_reason=reason,
            candidate_kind=candidate_kind,
        )
    if candidate is None:
        return None
    return legacy.replace(
        candidate,
        candidate_kind=candidate_kind,
        candidate_id=legacy.planlet_candidate_id(
            case_id=case_id,
            hook_index=hook_index,
            candidate_kind=candidate_kind,
            steps=legacy.candidate_plan_steps(candidate),
        ),
    )


def build_staging_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    preferred_target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    reason: str,
    candidate_kind: str = "vnext_blocker_clear",
    depot_aware_staging: bool = True,
    staging_priority: tuple[str, ...] | None = None,
) -> Any | None:
    return legacy.staging_candidate_for_batch(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        preferred_target_line=preferred_target_line,
        batch=batch,
        cars=cars,
        reason=reason,
        candidate_kind=candidate_kind,
        depot_aware_staging=depot_aware_staging,
        staging_priority=staging_priority or legacy.STAGING_LINE_PRIORITY,
    )


def build_planlet_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    steps: tuple[Any, ...],
    reason: str,
    candidate_kind: str,
) -> Any:
    return legacy.planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        batch=batch,
        generation_reason=reason,
        candidate_kind=candidate_kind,
        steps=steps,
    )


def plan_step(action: str, line: str, move_nos: tuple[str, ...], planned_positions: dict[str, int] | None = None) -> Any:
    return legacy.PlanStep(action, line, move_nos, planned_positions or {})


def candidate_plan_steps(candidate: Any) -> tuple[Any, ...]:
    return legacy.candidate_plan_steps(candidate)


def operation_remote_business_transition_count(operations: list[Any]) -> int:
    return legacy.operation_remote_business_transition_count(operations)


def closed_door_replay_violation_reasons(operations: list[Any], cars: list[dict[str, Any]]) -> list[str]:
    return legacy.closed_door_replay_violation_reasons(operations, cars)


class PhysicalAdapter:
    def __init__(self) -> None:
        self.graph = legacy.TrackGraph()
        self.validator = legacy.PhysicalValidator(self.graph)

    def validate(self, candidate: Any, cars: list[dict[str, Any]], loco_location: Any, depot_assignment: Any) -> Any:
        return self.validator.validate(candidate, cars, loco_location, depot_assignment)

    def next_loco_location(self, candidate: Any, validation: Any) -> Any:
        final_line = legacy.candidate_final_line(candidate)
        return legacy.operation_stand_location(validation.put_path, final_line)

    def operation_rows(self, candidate: Any, validation: Any, start_index: int) -> list[Any]:
        return legacy.operation_rows(candidate, validation, start_index)

    def response_operation(self, row: Any) -> dict[str, Any]:
        return legacy.response_operation(row)

    def write_json(self, path: Path, payload: Any) -> None:
        legacy.write_json(path, payload)

    def write_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        legacy.write_csv(path, rows)
