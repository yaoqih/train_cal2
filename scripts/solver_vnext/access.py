from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from .placement import planned_positions_for_batch


@dataclass(frozen=True)
class PrefixAccessLeasePlan:
    candidate: Any
    progressed_nos: tuple[str, ...]
    source_return_nos: tuple[str, ...]


def build_prefix_access_lease_planlet(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    blocker_batch: list[dict[str, Any]],
    target_batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
) -> PrefixAccessLeasePlan | None:
    if not blocker_batch or not target_batch or not target_line or target_line == source_line:
        return None
    if any(car.get("IsWeigh") for car in blocker_batch):
        return None
    carry = [*blocker_batch, *target_batch]
    if legacy.pull_equivalent(carry) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
        return None
    if not legacy.legacy.reverse_length_fits(source_line, target_line, carry):
        return None

    all_move_nos = {legacy.car_no(car) for car in carry}
    target_positions = planned_positions_for_batch(
        batch=target_batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_move_nos,
    )
    if len(target_positions) != len(target_batch):
        return None
    grouped = legacy.cars_by_line(cars)
    if not legacy.legacy.candidate_positions_available(target_line, target_positions, cars, all_move_nos, grouped):
        return None
    if not legacy.legacy.line_has_length_capacity(target_line, cars, target_batch, all_move_nos, grouped=grouped):
        return None

    restore_positions = {
        legacy.car_no(car): int(car.get("Position") or index)
        for index, car in enumerate(blocker_batch, start=1)
    }
    if not legacy.legacy.candidate_positions_available(source_line, restore_positions, cars, all_move_nos, grouped):
        return None

    target_nos = tuple(legacy.car_no(car) for car in target_batch)
    blocker_nos = tuple(legacy.car_no(car) for car in blocker_batch)
    steps = (
        legacy.plan_step("Get", source_line, tuple(legacy.car_no(car) for car in carry)),
        legacy.plan_step("Put", target_line, target_nos, target_positions),
        legacy.plan_step("Put", source_line, blocker_nos, restore_positions),
    )
    candidate = legacy.build_planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=source_line,
        batch=carry,
        steps=steps,
        reason=reason,
        candidate_kind=candidate_kind,
    )
    return PrefixAccessLeasePlan(
        candidate=candidate,
        progressed_nos=target_nos,
        source_return_nos=blocker_nos,
    )
