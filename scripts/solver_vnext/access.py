from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from .placement import planned_positions_for_batch
from .spotting import build_spotting_target_repack_planlet

SPLIT_PREFIX_STAGING_LINES = (
    "存2线",
    "存1线",
    "存3线",
    "存5线北",
    "存5线南",
    "预修线",
    "调梁棚",
    "调梁线北",
)


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
    frontier: Any | None = None,
    graph: Any | None = None,
    loco_location: Any | None = None,
    serial_gate_leases: dict[str, Any] | None = None,
) -> PrefixAccessLeasePlan | None:
    if not blocker_batch or not target_batch or not target_line or target_line == source_line:
        return None
    if any(car.get("IsWeigh") for car in blocker_batch):
        return None
    carry = [*blocker_batch, *target_batch]
    if (
        physical.is_spotting_line(target_line)
        and any(physical.force_positions(car) for car in target_batch)
        and physical.pull_equivalent(carry) <= physical.PULL_LIMIT_EQUIVALENT
        and frontier is not None
        and graph is not None
        and loco_location is not None
    ):
        spotting_plan = build_spotting_target_repack_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_batch=target_batch,
            source_blocker_batch=blocker_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=reason + ";spotting_repack=with_source_prefix",
            candidate_kind=candidate_kind,
            frontier=frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        )
        if spotting_plan is not None:
            return PrefixAccessLeasePlan(
                candidate=spotting_plan.candidate,
                progressed_nos=spotting_plan.progressed_nos,
                source_return_nos=spotting_plan.source_return_nos,
            )
    if physical.pull_equivalent(carry) > physical.PULL_LIMIT_EQUIVALENT:
        return _build_split_prefix_access_lease_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            blocker_batch=blocker_batch,
            target_batch=target_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=reason + ";split_prefix_access=carry_limit",
            candidate_kind=candidate_kind,
            frontier=frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        )
    all_move_nos = {physical.car_no(car) for car in carry}
    target_positions = planned_positions_for_batch(
        batch=target_batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_move_nos,
    )
    if len(target_positions) != len(target_batch):
        return None
    grouped = physical.cars_by_line(cars)
    if not physical.candidate_positions_available(target_line, target_positions, cars, all_move_nos, grouped):
        return None
    if not physical.line_has_length_capacity(target_line, cars, target_batch, all_move_nos, grouped=grouped):
        return None

    restore_positions = {
        physical.car_no(car): int(car.get("Position") or index)
        for index, car in enumerate(blocker_batch, start=1)
    }
    if not physical.candidate_positions_available(source_line, restore_positions, cars, all_move_nos, grouped):
        return None

    target_nos = tuple(physical.car_no(car) for car in target_batch)
    blocker_nos = tuple(physical.car_no(car) for car in blocker_batch)
    steps = (
        physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in carry)),
        physical.plan_step("Put", target_line, target_nos, target_positions),
        physical.plan_step("Put", source_line, blocker_nos, restore_positions),
    )
    if frontier is not None and graph is not None and loco_location is not None:
        if not frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return _build_split_prefix_access_lease_planlet(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                blocker_batch=blocker_batch,
                target_batch=target_batch,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=reason + ";split_prefix_access=plan_steps_unreachable",
                candidate_kind=candidate_kind,
                frontier=frontier,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            )
    candidate = physical.build_planlet_candidate(
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


def _build_split_prefix_access_lease_planlet(
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
    frontier: Any | None,
    graph: Any | None,
    loco_location: Any | None,
    serial_gate_leases: dict[str, Any],
) -> PrefixAccessLeasePlan | None:
    if frontier is None or graph is None or loco_location is None:
        return None
    if physical.pull_equivalent(target_batch) > physical.PULL_LIMIT_EQUIVALENT:
        return None

    blocker_nos = tuple(physical.car_no(car) for car in blocker_batch)
    target_nos = tuple(physical.car_no(car) for car in target_batch)
    all_move_nos = set(blocker_nos) | set(target_nos)
    grouped = physical.cars_by_line(cars)
    target_positions = planned_positions_for_batch(
        batch=target_batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_move_nos,
    )
    if len(target_positions) != len(target_batch):
        return None
    if not physical.line_has_length_capacity(target_line, cars, target_batch, all_move_nos, grouped=grouped):
        return None

    restore_positions = {
        physical.car_no(car): int(car.get("Position") or index)
        for index, car in enumerate(blocker_batch, start=1)
    }
    if not physical.line_has_length_capacity(source_line, cars, blocker_batch, all_move_nos, grouped=grouped):
        return None

    for chunks in _prefix_chunks(blocker_batch):
        staging_probe_batch = chunks[0]
        staging_lines = frontier.reachable_staging_lines(
            source_line=source_line,
            batch=staging_probe_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            candidate_lines=SPLIT_PREFIX_STAGING_LINES,
            excluded_lines={source_line, target_line},
        )
        for staging_line in staging_lines:
            if not physical.line_has_length_capacity(
                staging_line,
                cars,
                blocker_batch,
                set(blocker_nos),
                grouped=grouped,
            ):
                continue
            steps = _split_prefix_steps(
                source_line=source_line,
                target_line=target_line,
                staging_line=staging_line,
                chunks=chunks,
                target_nos=target_nos,
                target_positions=target_positions,
                restore_positions=restore_positions,
                cars=cars,
                depot_assignment=depot_assignment,
            )
            if not steps:
                continue
            if not frontier.plan_steps_are_reachable(
                steps=steps,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases,
            ):
                continue
            candidate = physical.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=source_line,
                batch=[*blocker_batch, *target_batch],
                steps=steps,
                reason=f"{reason};staging={staging_line};chunks={len(chunks)}",
                candidate_kind=candidate_kind,
            )
            return PrefixAccessLeasePlan(
                candidate=candidate,
                progressed_nos=target_nos,
                source_return_nos=blocker_nos,
            )
    return None


def _prefix_chunks(blocker_batch: list[dict[str, Any]]) -> tuple[tuple[list[dict[str, Any]], ...], ...]:
    plans: list[tuple[list[dict[str, Any]], ...]] = []
    seen: set[tuple[int, ...]] = set()
    for chunk_size in range(min(3, len(blocker_batch)), 0, -1):
        chunks = tuple(
            blocker_batch[index : index + chunk_size]
            for index in range(0, len(blocker_batch), chunk_size)
        )
        signature = tuple(len(chunk) for chunk in chunks)
        if signature in seen:
            continue
        seen.add(signature)
        if all(physical.pull_equivalent(chunk) <= physical.PULL_LIMIT_EQUIVALENT for chunk in chunks):
            plans.append(chunks)
    return tuple(plans)


def _split_prefix_steps(
    *,
    source_line: str,
    target_line: str,
    staging_line: str,
    chunks: tuple[list[dict[str, Any]], ...],
    target_nos: tuple[str, ...],
    target_positions: dict[str, int],
    restore_positions: dict[str, int],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[Any, ...]:
    steps: list[Any] = []
    for chunk in chunks:
        chunk_nos = tuple(physical.car_no(car) for car in chunk)
        staging_positions = planned_positions_for_batch(
            batch=chunk,
            target_line=staging_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=set(chunk_nos),
        )
        if len(staging_positions) != len(chunk):
            return ()
        steps.append(physical.plan_step("Get", source_line, chunk_nos))
        steps.append(physical.plan_step("Put", staging_line, chunk_nos, staging_positions))

    steps.append(physical.plan_step("Get", source_line, target_nos))
    steps.append(physical.plan_step("Put", target_line, target_nos, target_positions))

    for chunk in reversed(chunks):
        chunk_nos = tuple(physical.car_no(car) for car in chunk)
        chunk_restore_positions = {
            no: restore_positions[no]
            for no in chunk_nos
            if no in restore_positions
        }
        if len(chunk_restore_positions) != len(chunk):
            return ()
        steps.append(physical.plan_step("Get", staging_line, chunk_nos))
        steps.append(physical.plan_step("Put", source_line, chunk_nos, chunk_restore_positions))
    return tuple(steps)
