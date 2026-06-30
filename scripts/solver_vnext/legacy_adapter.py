from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_physical_runtime_trace as legacy  # noqa: E402


REMOTE_INTERACTION_LINES = legacy.REMOTE_INTERACTION_LINES
DEPOT_LINES = legacy.DEPOT_LINES
DEPOT_TARGET_LINES = legacy.DEPOT_TARGET_LINES
FRONT_SERVICE_TARGET_LINES = legacy.FRONT_SERVICE_TARGET_LINES
RUNNING_LINES = legacy.RUNNING_LINES


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
    return legacy.unsatisfied_cars(cars, depot_assignment)


def state_signature(cars: list[dict[str, Any]], loco_location: Any) -> tuple[str, str, tuple[tuple[str, str, int], ...]]:
    return legacy.state_signature(cars, loco_location)


def line_loads(cars: list[dict[str, Any]]) -> Any:
    return legacy.line_loads(cars)


def cars_by_line(cars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return legacy.cars_by_line(cars)


def line_cars_in_access_order(
    *,
    cars: list[dict[str, Any]],
    line: str,
    graph: Any,
    loco_location: Any,
) -> list[dict[str, Any]]:
    return legacy.line_cars_in_access_order(
        cars=cars,
        line=line,
        access_context=legacy.PhysicalAccessContext(graph=graph, loco_location=loco_location),
    )


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
