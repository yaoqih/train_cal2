from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product
from typing import Any

from . import physical
from .placement import planned_positions_for_batch


SPOTTING_REPACK_STAGING_LINES = (
    "存2线",
    "存1线",
    "存3线",
    "存5线北",
    "存5线南",
    "预修线",
    "调梁线北",
    "洗罐线北",
)


@dataclass(frozen=True)
class SpottingRepackPlan:
    candidate: Any
    progressed_nos: tuple[str, ...]
    source_return_nos: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SourcePartition:
    forced: tuple[int, ...]
    before: tuple[dict[str, Any], ...]
    same: tuple[dict[str, Any], ...]
    after: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _TargetGroup:
    role: str
    cars: tuple[dict[str, Any], ...]

    @property
    def nos(self) -> tuple[str, ...]:
        return _nos(self.cars)


@dataclass(frozen=True)
class _RepackIntent:
    target_groups: tuple[_TargetGroup, ...]
    final_order: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _SameLinePlacement:
    move_order: tuple[dict[str, Any], ...]
    final_positions: dict[str, int]


def spotting_nonforced_prefix_would_pollute(
    *,
    contract: Any,
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> bool:
    if not physical.is_spotting_line(target_line):
        return False
    if any(physical.force_positions(car) for car in batch):
        return False

    by_no = {physical.car_no(car): car for car in cars}
    batch_nos = {physical.car_no(car) for car in batch}
    planned = planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
    )
    planned_positions = set(planned.values())
    if len(planned_positions) != len(batch):
        return True
    for no in contract.subject_nos:
        if no in batch_nos:
            continue
        car = by_no.get(no)
        if not car or physical.car_is_satisfied(car, depot_assignment, cars):
            continue
        if car["Line"] not in contract.source_lines:
            continue
        if target_line not in (car.get("_TargetLineSet") or set(physical.target_lines(car))):
            continue
        forced = physical.force_positions(car)
        if forced and any(planned_positions & window for window in physical.spotting_physical_window_sets(target_line, forced)):
            return True
    return False


def build_spotting_cross_line_repack_planlet(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    source_batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
    source_blocker_batch: list[dict[str, Any]] | None = None,
) -> SpottingRepackPlan | None:
    source_blockers = tuple(source_blocker_batch or ())
    source_cars = tuple(source_batch)
    if (
        not source_cars
        or source_line == target_line
        or not physical.is_spotting_line(target_line)
        or any(car.get("IsWeigh") for car in (*source_cars, *source_blockers))
    ):
        return None

    source = _source_partition(source_cars)
    if source is None:
        return _build_general_spotting_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_cars=source_cars,
            source_blockers=source_blockers,
            target_existing=tuple(
                physical.line_cars_in_access_order(
                    cars=cars,
                    line=target_line,
                    graph=graph,
                    loco_location=loco_location,
                )
            ),
            cars=cars,
            depot_assignment=depot_assignment,
            reason=reason,
            candidate_kind=candidate_kind,
            frontier=frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        )

    target_existing = tuple(
        physical.line_cars_in_access_order(
            cars=cars,
            line=target_line,
            graph=graph,
            loco_location=loco_location,
        )
    )
    if not target_existing:
        return _build_general_spotting_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_cars=source_cars,
            source_blockers=source_blockers,
            target_existing=target_existing,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=reason,
            candidate_kind=candidate_kind,
            frontier=frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        )
    target_same = tuple(
        car
        for car in target_existing
        if source.forced and _same_spotting_target(car, target_line, source.forced)
    )
    capacity = physical.spotting_capacity(target_line, source.forced) if source.forced else 0
    if source.forced and (not capacity or len(source.same) + len(target_same) > capacity):
        return None
    if not physical.line_has_length_capacity(
        target_line,
        cars,
        list(source_cars),
        set(_nos(source_cars)),
        grouped=physical.cars_by_line(cars),
    ):
        return None

    for intent in _repack_intents(
        target_existing=target_existing,
        target_line=target_line,
        source=source,
    ):
        if not _intent_is_target_valid(
            intent=intent,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
        ):
            continue
        plan = _build_plan_for_intent(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source=source,
            source_blockers=source_blockers,
            intent=intent,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=reason,
            candidate_kind=candidate_kind,
            frontier=frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        )
        if plan is not None:
            return plan
    return _build_general_spotting_planlet(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        source_cars=source_cars,
        source_blockers=source_blockers,
        target_existing=target_existing,
        cars=cars,
        depot_assignment=depot_assignment,
        reason=reason,
        candidate_kind=candidate_kind,
        frontier=frontier,
        graph=graph,
        loco_location=loco_location,
        serial_gate_leases=serial_gate_leases,
    )


def build_spotting_same_line_repack_planlet(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
) -> SpottingRepackPlan | None:
    if not physical.is_spotting_line(line):
        return None
    line_cars = tuple(
        physical.line_cars_in_access_order(
            cars=cars,
            line=line,
            graph=graph,
            loco_location=loco_location,
        )
    )
    if len(line_cars) < 2 or any(car.get("IsWeigh") for car in line_cars):
        return None
    if physical.pull_equivalent(list(line_cars)) > physical.PULL_LIMIT_EQUIVALENT:
        return None

    for placement in _same_line_final_placements(line=line, line_cars=line_cars, depot_assignment=depot_assignment):
        original_positions = {
            physical.car_no(car): int(car.get("Position") or 0)
            for car in line_cars
        }
        if placement.final_positions == original_positions:
            continue
        projected = _project_target_positions(
            cars=cars,
            target_line=line,
            final_cars=placement.move_order,
            final_positions=placement.final_positions,
        )
        if not _all_spotting_groups_are_acceptable(projected, line, depot_assignment):
            continue
        if not _spotting_final_state_is_valid(projected, line, depot_assignment):
            continue
        move_set = set(_nos(placement.move_order))
        current_order = tuple(car for car in line_cars if physical.car_no(car) in move_set)
        current_nos = _nos(current_order)
        if set(current_nos) != move_set:
            continue
        route_blocker_options = _same_line_route_blocker_stage_options(
            line=line,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
        )
        for blocker_pre_steps, blocker_post_steps, blocker_cars, blocker_staging_line in route_blocker_options:
            if blocker_pre_steps:
                staging_lines = _same_line_candidate_staging_lines(
                    line=line,
                    excluded_lines={blocker_staging_line},
                )
            else:
                staging_lines = _same_line_staging_lines(
                    line=line,
                    batch=current_order,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    frontier=frontier,
                    graph=graph,
                    loco_location=loco_location,
                )
            for staging_line in staging_lines:
                if staging_line == blocker_staging_line:
                    continue
                plan = _same_line_repack_plan_with_staging(
                    case_id=case_id,
                    hook_index=hook_index,
                    line=line,
                    staging_line=staging_line,
                    current_order=current_order,
                    current_nos=current_nos,
                    final_positions=placement.final_positions,
                    blocker_pre_steps=blocker_pre_steps,
                    blocker_post_steps=blocker_post_steps,
                    blocker_cars=blocker_cars,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    reason=reason,
                    candidate_kind=candidate_kind,
                    frontier=frontier,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases,
                )
                if plan is not None:
                    return plan
            plan = _same_line_repack_plan_with_chunk_staging(
                case_id=case_id,
                hook_index=hook_index,
                line=line,
                placement=placement,
                current_order=current_order,
                current_nos=current_nos,
                blocker_pre_steps=blocker_pre_steps,
                blocker_post_steps=blocker_post_steps,
                blocker_cars=blocker_cars,
                blocker_staging_line=blocker_staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=reason,
                candidate_kind=candidate_kind,
                frontier=frontier,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases,
            )
            if plan is not None:
                return plan
    return None


def _same_line_repack_plan_with_staging(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    staging_line: str,
    current_order: tuple[dict[str, Any], ...],
    current_nos: tuple[str, ...],
    final_positions: dict[str, int],
    blocker_pre_steps: tuple[Any, ...],
    blocker_post_steps: tuple[Any, ...],
    blocker_cars: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
) -> SpottingRepackPlan | None:
    staging_positions = planned_positions_for_batch(
        batch=list(current_order),
        target_line=staging_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=set(current_nos),
    )
    if len(staging_positions) != len(current_order):
        return None
    steps = [
        *blocker_pre_steps,
        physical.plan_step("Get", line, current_nos),
        physical.plan_step("Put", staging_line, current_nos, staging_positions),
        physical.plan_step("Get", staging_line, current_nos),
    ]
    for put_nos in _target_group_put_chunks(current_nos, final_positions):
        steps.append(
            physical.plan_step(
                "Put",
                line,
                put_nos,
                {no: final_positions[no] for no in put_nos},
            )
        )
    steps.extend(blocker_post_steps)
    if not frontier.plan_steps_are_reachable(
        steps=tuple(steps),
        cars=cars,
        depot_assignment=depot_assignment,
        graph=graph,
        loco_location=loco_location,
        serial_gate_leases=serial_gate_leases,
        candidate_kind=candidate_kind,
    ):
        return None
    candidate = physical.build_planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=line,
        target_line=line,
        batch=[*current_order, *blocker_cars],
        steps=tuple(steps),
        reason=f"{reason};spotting_repack=same_line;staging={staging_line}",
        candidate_kind=candidate_kind,
    )
    return SpottingRepackPlan(
        candidate=candidate,
        progressed_nos=current_nos,
        source_return_nos=(),
    )


def _same_line_repack_plan_with_chunk_staging(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    placement: _SameLinePlacement,
    current_order: tuple[dict[str, Any], ...],
    current_nos: tuple[str, ...],
    blocker_pre_steps: tuple[Any, ...],
    blocker_post_steps: tuple[Any, ...],
    blocker_cars: tuple[dict[str, Any], ...],
    blocker_staging_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
) -> SpottingRepackPlan | None:
    chunks = _same_line_reorder_chunks(current_order, placement.move_order)
    if len(chunks) <= 1 or len(chunks) > 6:
        return None
    chunk_by_no = {
        physical.car_no(car): index
        for index, chunk in enumerate(chunks)
        for car in chunk
    }
    final_chunk_order: list[int] = []
    for car in placement.move_order:
        index = chunk_by_no[physical.car_no(car)]
        if not final_chunk_order or final_chunk_order[-1] != index:
            final_chunk_order.append(index)
    if set(final_chunk_order) != set(range(len(chunks))):
        return None

    blocked_lines = {
        step.line
        for step in blocker_pre_steps
        if step.action == "Get"
    }
    staging_lines = _same_line_candidate_staging_lines(
        line=line,
        excluded_lines={blocker_staging_line, *blocked_lines},
    )
    if len(staging_lines) < len(chunks):
        return None

    by_no = {physical.car_no(car): car for car in cars}
    all_batch_nos = set(current_nos) | set(_nos(blocker_cars))
    for assigned_lines in permutations(staging_lines, len(chunks)):
        planning = [dict(car) for car in cars]
        steps = list(blocker_pre_steps)
        _apply_plan_steps_for_projection(planning, blocker_pre_steps)
        steps.append(physical.plan_step("Get", line, current_nos))
        physical.apply_physical_get_order(planning, line, current_nos)

        feasible = True
        for index in reversed(range(len(chunks))):
            chunk = chunks[index]
            chunk_nos = _nos(chunk)
            staging_line = assigned_lines[index]
            positions = planned_positions_for_batch(
                batch=list(chunk),
                target_line=staging_line,
                cars=planning,
                depot_assignment=depot_assignment,
                batch_nos=all_batch_nos,
            )
            if len(positions) != len(chunk):
                feasible = False
                break
            steps.append(physical.plan_step("Put", staging_line, chunk_nos, positions))
            physical.apply_physical_put_order(planning, staging_line, list(chunk_nos), positions)
        if not feasible:
            continue

        for index in reversed(final_chunk_order):
            chunk = chunks[index]
            chunk_nos = _nos(chunk)
            staging_line = assigned_lines[index]
            steps.append(physical.plan_step("Get", staging_line, chunk_nos))
            physical.apply_physical_get_order(planning, staging_line, chunk_nos)
            positions = {
                no: placement.final_positions[no]
                for no in chunk_nos
            }
            steps.append(physical.plan_step("Put", line, chunk_nos, positions))
            physical.apply_physical_put_order(planning, line, list(chunk_nos), positions)

        steps.extend(blocker_post_steps)
        if not frontier.plan_steps_are_reachable(
            steps=tuple(steps),
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind=candidate_kind,
        ):
            continue
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=line,
            target_line=line,
            batch=[*(by_no[no] for no in current_nos), *blocker_cars],
            steps=tuple(steps),
            reason=(
                f"{reason};spotting_repack=same_line_chunks;"
                f"chunks={len(chunks)};staging={','.join(assigned_lines)}"
            ),
            candidate_kind=candidate_kind,
        )
        return SpottingRepackPlan(
            candidate=candidate,
            progressed_nos=current_nos,
            source_return_nos=(),
        )
    return None


def _same_line_reorder_chunks(
    current_order: tuple[dict[str, Any], ...],
    final_order: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], ...]:
    current_index = {
        physical.car_no(car): index
        for index, car in enumerate(current_order)
    }
    chunks_in_final_order: list[list[dict[str, Any]]] = []
    for car in final_order:
        no = physical.car_no(car)
        if no not in current_index:
            return ()
        if (
            chunks_in_final_order
            and current_index[no]
            == current_index[physical.car_no(chunks_in_final_order[-1][-1])] + 1
        ):
            chunks_in_final_order[-1].append(car)
        else:
            chunks_in_final_order.append([car])
    chunks = sorted(
        (tuple(chunk) for chunk in chunks_in_final_order),
        key=lambda chunk: current_index[physical.car_no(chunk[0])],
    )
    flattened = tuple(physical.car_no(car) for chunk in chunks for car in chunk)
    if flattened != _nos(current_order):
        return ()
    return tuple(chunks)


def _apply_plan_steps_for_projection(
    cars: list[dict[str, Any]],
    steps: tuple[Any, ...],
) -> None:
    for step in steps:
        if step.action == "Get":
            physical.apply_physical_get_order(cars, step.line, step.move_car_nos)
        elif step.action == "Put":
            physical.apply_physical_put_order(
                cars,
                step.line,
                list(step.move_car_nos),
                step.planned_positions,
            )


def _build_general_spotting_planlet(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    source_cars: tuple[dict[str, Any], ...],
    source_blockers: tuple[dict[str, Any], ...],
    target_existing: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
) -> SpottingRepackPlan | None:
    final_positions = _general_spotting_final_positions(
        target_line=target_line,
        source_cars=source_cars,
        target_existing=target_existing,
        cars=cars,
        depot_assignment=depot_assignment,
    )
    if not final_positions:
        return None
    final_nos = set(final_positions)
    all_final_nos = set(_nos(source_cars)) | set(_nos(target_existing))
    if final_nos != all_final_nos:
        return None
    if physical.pull_equivalent([*source_blockers, *source_cars]) > physical.PULL_LIMIT_EQUIVALENT:
        return None
    if any(physical.pull_equivalent(list(group.cars)) > physical.PULL_LIMIT_EQUIVALENT for group in _target_groups_for_full_line(target_existing)):
        return None

    target_groups = _target_groups_for_full_line(target_existing)
    for staging_assignment in _staging_assignments(
        target_groups=target_groups,
        source_line=source_line,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
    ):
        steps = _general_plan_steps(
            source_line=source_line,
            target_line=target_line,
            source_cars=source_cars,
            source_blockers=source_blockers,
            target_groups=target_groups,
            staging_assignment=staging_assignment,
            final_positions=final_positions,
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
            candidate_kind=candidate_kind,
        ):
            continue
        return _candidate_plan(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_batch=source_cars,
            source_blockers=source_blockers,
            target_groups=target_groups,
            steps=steps,
            reason=f"{reason};spotting_repack=general_target_line",
            candidate_kind=candidate_kind,
        )
    return None


def _general_spotting_final_positions(
    *,
    target_line: str,
    source_cars: tuple[dict[str, Any], ...],
    target_existing: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> dict[str, int]:
    total = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target_line, 0)
    if not total:
        return {}
    all_cars = tuple((*target_existing, *source_cars))
    if len(all_cars) > total:
        return {}
    all_nos = set(_nos(all_cars))
    positions = planned_positions_for_batch(
        batch=list(all_cars),
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_nos,
    )
    if (
        len(positions) != len(all_cars)
        or physical.target_put_order_reasons(target_line, _nos(source_cars), positions)
        or physical.target_put_order_reasons(target_line, _nos(target_existing), positions)
    ):
        positions = _structured_spotting_final_positions(
            target_line=target_line,
            all_cars=all_cars,
        )
    if (
        not target_existing
        and len(positions) == len(all_cars)
        and physical.target_put_order_reasons(target_line, _nos(source_cars), positions)
    ):
        positions = _empty_target_ordered_spotting_positions(
            target_line=target_line,
            source_cars=source_cars,
            cars=cars,
            depot_assignment=depot_assignment,
        )
    if len(positions) != len(all_cars):
        return {}
    projected = _project_target_positions(
        cars=cars,
        target_line=target_line,
        final_cars=all_cars,
        final_positions=positions,
    )
    if not _all_spotting_groups_are_acceptable(projected, target_line, depot_assignment):
        return {}
    return positions


def _empty_target_ordered_spotting_positions(
    *,
    target_line: str,
    source_cars: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> dict[str, int]:
    total = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target_line, 0)
    if not total or len(source_cars) > total:
        return {}
    for start in range(1, total - len(source_cars) + 2):
        positions = {
            physical.car_no(car): start + index
            for index, car in enumerate(source_cars)
        }
        projected = _project_target_positions(
            cars=cars,
            target_line=target_line,
            final_cars=source_cars,
            final_positions=positions,
        )
        if _all_spotting_groups_are_acceptable(projected, target_line, depot_assignment):
            return positions
    return {}


def _structured_spotting_final_positions(
    *,
    target_line: str,
    all_cars: tuple[dict[str, Any], ...],
) -> dict[str, int]:
    total = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target_line, 0)
    if not total or len(all_cars) > total:
        return {}

    final_positions: dict[str, int] = {}
    occupied: set[int] = set()
    forced_groups = tuple(
        sorted(
            {
                physical.force_positions(car)
                for car in all_cars
                if physical.force_positions(car)
                and target_line in (car.get("_TargetLineSet") or set(physical.target_lines(car)))
            }
        )
    )
    for forced in forced_groups:
        group = tuple(car for car in all_cars if _same_spotting_target(car, target_line, forced))
        capacity = physical.spotting_capacity(target_line, forced)
        if not capacity or len(group) > capacity:
            return {}
        for before_count in _physical_before_counts(
            target_line=target_line,
            forced=forced,
            forced_count=len(group),
        ):
            segment = tuple(range(before_count + 1, before_count + 1 + len(group)))
            if any(position in occupied for position in segment):
                continue
            for car, position in zip(group, segment):
                final_positions[physical.car_no(car)] = position
            occupied.update(segment)
            break
        else:
            return {}

    free_positions = [position for position in range(1, total + 1) if position not in occupied]
    for car in all_cars:
        no = physical.car_no(car)
        if no in final_positions:
            continue
        if not free_positions:
            return {}
        final_positions[no] = free_positions.pop(0)
    return final_positions


def _target_groups_for_full_line(target_existing: tuple[dict[str, Any], ...]) -> tuple[_TargetGroup, ...]:
    return (_TargetGroup("existing", target_existing),) if target_existing else ()


def _general_plan_steps(
    *,
    source_line: str,
    target_line: str,
    source_cars: tuple[dict[str, Any], ...],
    source_blockers: tuple[dict[str, Any], ...],
    target_groups: tuple[_TargetGroup, ...],
    staging_assignment: tuple[str, ...],
    final_positions: dict[str, int],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[Any, ...]:
    return _target_line_rebuild_steps(
        source_line=source_line,
        target_line=target_line,
        source_batch=source_cars,
        source_blockers=source_blockers,
        target_groups=target_groups,
        staging_assignment=staging_assignment,
        final_positions=final_positions,
        cars=cars,
        depot_assignment=depot_assignment,
    )


def _target_group_put_chunks(
    group_nos: tuple[str, ...],
    final_positions: dict[str, int],
) -> tuple[tuple[str, ...], ...]:
    remaining = list(group_nos)
    chunks: list[tuple[str, ...]] = []
    while remaining:
        selected_start = len(remaining) - 1
        for start in range(0, len(remaining)):
            candidate = tuple(remaining[start:])
            if not physical.target_put_order_reasons("target", candidate, final_positions):
                selected_start = start
                break
        chunk = tuple(remaining[selected_start:])
        chunks.append(chunk)
        del remaining[selected_start:]
    return tuple(chunks)


def _source_partition(source_batch: tuple[dict[str, Any], ...]) -> _SourcePartition | None:
    forced_values = [physical.force_positions(car) for car in source_batch if physical.force_positions(car)]
    if not forced_values:
        return _SourcePartition(forced=(), before=(), same=source_batch, after=())
    forced = forced_values[0]
    if any(item != forced for item in forced_values):
        return None

    forced_indexes = [
        index
        for index, car in enumerate(source_batch)
        if physical.force_positions(car) == forced
    ]
    first = forced_indexes[0]
    last = forced_indexes[-1]
    if forced_indexes != list(range(first, last + 1)):
        return None

    before = source_batch[:first]
    same = source_batch[first : last + 1]
    after = source_batch[last + 1 :]
    if any(physical.force_positions(car) for car in (*before, *after)):
        return None
    return _SourcePartition(forced=forced, before=before, same=same, after=after)


def _same_line_final_placements(
    *,
    line: str,
    line_cars: tuple[dict[str, Any], ...],
    depot_assignment: Any,
) -> tuple[_SameLinePlacement, ...]:
    forced_groups = tuple(
        sorted(
            {
                physical.force_positions(car)
                for car in line_cars
                if physical.force_positions(car)
                and line in (car.get("_TargetLineSet") or set(physical.target_lines(car)))
            }
        )
    )
    if not forced_groups:
        return ()

    placements: list[_SameLinePlacement] = []
    original_nos = _nos(line_cars)
    for forced in forced_groups:
        forced_cars = tuple(car for car in line_cars if _same_spotting_target(car, line, forced))
        if not forced_cars:
            continue
        capacity = physical.spotting_capacity(line, forced)
        if not capacity or len(forced_cars) > capacity:
            continue
        other_cars = tuple(car for car in line_cars if physical.car_no(car) not in set(_nos(forced_cars)))
        for window in physical.spotting_physical_window_sets(line, forced):
            ordered_window = tuple(sorted(window))
            if len(ordered_window) < len(forced_cars):
                continue
            slots = ordered_window[: len(forced_cars)]
            total = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
            if not total:
                continue
            final_slots: list[dict[str, Any] | None] = [None] * total
            forced_nos = set(_nos(forced_cars))
            forced_indexes = [
                index
                for index, car in enumerate(line_cars)
                if physical.car_no(car) in forced_nos
            ]
            if not forced_indexes:
                continue
            original_before_cars = tuple(
                car
                for car in line_cars[: forced_indexes[0]]
                if physical.car_no(car) not in forced_nos
            )
            before_counts = sorted(
                range(len(other_cars) + 1),
                key=lambda count: (abs(count - len(original_before_cars)), count),
            )
            for before_count in before_counts:
                final_slots = [None] * total
                before_cars = other_cars[:before_count]
                after_cars = other_cars[before_count:]
                for car, position in zip(forced_cars, slots):
                    if position < 1 or position > len(final_slots):
                        break
                    final_slots[position - 1] = car
                else:
                    before_positions = [index for index in range(0, slots[0] - 1) if final_slots[index] is None]
                    after_positions = [index for index in range(slots[-1], total) if final_slots[index] is None]
                    if len(before_cars) > len(before_positions) or len(after_cars) > len(after_positions):
                        continue
                    for car, index in zip(before_cars, before_positions):
                        final_slots[index] = car
                    for car, index in zip(after_cars, after_positions):
                        final_slots[index] = car
                    order = tuple(car for car in final_slots if car is not None)
                    if set(_nos(order)) != set(original_nos):
                        continue
                    final_positions = {
                        physical.car_no(car): position
                        for position, car in enumerate(final_slots, start=1)
                        if car is not None
                    }
                    projected = _project_target_positions(
                        cars=list(line_cars),
                        target_line=line,
                        final_cars=order,
                        final_positions=final_positions,
                    )
                    if _all_spotting_groups_are_acceptable(projected, line, depot_assignment):
                        placements.append(_SameLinePlacement(move_order=order, final_positions=final_positions))
    deduped: list[_SameLinePlacement] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for placement in placements:
        key = tuple(sorted(placement.final_positions.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(placement)
    return tuple(deduped)


def _same_line_staging_lines(
    *,
    line: str,
    batch: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    frontier: Any,
    graph: Any,
    loco_location: Any,
) -> tuple[str, ...]:
    return frontier.reachable_staging_lines(
        source_line=line,
        batch=list(batch),
        cars=cars,
        depot_assignment=depot_assignment,
        graph=graph,
        loco_location=loco_location,
        candidate_lines=SPOTTING_REPACK_STAGING_LINES,
        excluded_lines={line},
    )


def _same_line_candidate_staging_lines(
    *,
    line: str,
    excluded_lines: set[str],
) -> tuple[str, ...]:
    lines: list[str] = []
    for candidate in SPOTTING_REPACK_STAGING_LINES:
        if candidate == line or candidate in excluded_lines:
            continue
        if candidate in physical.RUNNING_LINES or candidate in physical.DEPOT_TARGET_LINES:
            continue
        if candidate not in physical.TRACK_SPECS:
            continue
        lines.append(candidate)
    return tuple(lines)


def _same_line_route_blocker_stage_options(
    *,
    line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    graph: Any,
    loco_location: Any,
) -> tuple[tuple[tuple[Any, ...], tuple[Any, ...], tuple[dict[str, Any], ...], str], ...]:
    options: list[tuple[tuple[Any, ...], tuple[Any, ...], tuple[dict[str, Any], ...], str]] = [
        ((), (), (), "")
    ]
    blocker_lines = tuple(physical.SERIAL_LINE_BLOCKERS.get(line, ()))
    if not blocker_lines:
        return tuple(options)
    loads = physical.line_loads(cars)
    for blocker_line in blocker_lines:
        blocker_cars = tuple(
            physical.line_cars_in_access_order(
                cars=cars,
                line=blocker_line,
                graph=graph,
                loco_location=loco_location,
            )
        )
        if not blocker_cars:
            continue
        if physical.pull_equivalent(list(blocker_cars)) > physical.PULL_LIMIT_EQUIVALENT:
            continue
        if any(car.get("IsWeigh") and not car.get("_Weighed") for car in blocker_cars):
            continue
        if any(not physical.car_is_satisfied(car, depot_assignment, cars) for car in blocker_cars):
            continue
        if any(
            physical.planned_target_for_car(car, cars, depot_assignment, loads)[0] != blocker_line
            for car in blocker_cars
        ):
            continue
        blocker_nos = _nos(blocker_cars)
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(blocker_cars, start=1)
        }
        for staging_line in _same_line_candidate_staging_lines(
            line=line,
            excluded_lines={blocker_line},
        ):
            positions = planned_positions_for_batch(
                batch=list(blocker_cars),
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=set(blocker_nos),
            )
            if len(positions) != len(blocker_cars):
                continue
            options.append(
                (
                    (
                        physical.plan_step("Get", blocker_line, blocker_nos),
                        physical.plan_step("Put", staging_line, blocker_nos, positions),
                    ),
                    (
                        physical.plan_step("Get", staging_line, blocker_nos),
                        physical.plan_step("Put", blocker_line, blocker_nos, restore_positions),
                    ),
                    blocker_cars,
                    staging_line,
                )
            )
    return tuple(options)


def _repack_intents(
    *,
    target_existing: tuple[dict[str, Any], ...],
    target_line: str,
    source: _SourcePartition,
) -> tuple[_RepackIntent, ...]:
    if not source.forced:
        return _nonforced_repack_intents(
            target_existing=target_existing,
            source=source,
        )

    intents: list[_RepackIntent] = []
    expected_nos = set(_nos(target_existing)) | set(_nos((*source.before, *source.same, *source.after)))
    forced_count = len(source.same) + sum(
        1 for car in target_existing if _same_spotting_target(car, target_line, source.forced)
    )
    for physical_before_count in _physical_before_counts(
        target_line=target_line,
        forced=source.forced,
        forced_count=forced_count,
    ):
        target_before_count = physical_before_count - len(source.before)
        if target_before_count < 0:
            continue
        intent = _repack_intent_for_target_before_count(
            target_existing=target_existing,
            target_line=target_line,
            source=source,
            before_count=target_before_count,
        )
        if intent is None:
            continue
        if set(_nos(intent.final_order)) != expected_nos:
            continue
        intents.append(intent)
    return tuple(intents)


def _nonforced_repack_intents(
    *,
    target_existing: tuple[dict[str, Any], ...],
    source: _SourcePartition,
) -> tuple[_RepackIntent, ...]:
    intents: list[_RepackIntent] = []
    expected_nos = set(_nos(target_existing)) | set(_nos(source.same))
    for insert_index in range(len(target_existing) + 1):
        before_nos = set(_nos(target_existing[:insert_index]))
        groups = _target_groups(target_existing, before_nos, set())
        if not groups and target_existing:
            continue
        final_order = (
            *_cars_for_role(groups, "before"),
            *source.same,
            *_cars_for_role(groups, "after"),
        )
        if set(_nos(final_order)) != expected_nos:
            continue
        intents.append(_RepackIntent(target_groups=groups, final_order=final_order))
    return tuple(intents)


def _physical_before_counts(
    *,
    target_line: str,
    forced: tuple[int, ...],
    forced_count: int,
) -> tuple[int, ...]:
    counts: list[int] = []
    for window in physical.spotting_physical_window_sets(target_line, forced):
        if len(window) < forced_count:
            continue
        ordered = sorted(window)
        for start_index in range(0, len(ordered) - forced_count + 1):
            segment = ordered[start_index : start_index + forced_count]
            if segment == list(range(segment[0], segment[-1] + 1)):
                counts.append(segment[0] - 1)
    return tuple(sorted(set(counts)))


def _repack_intent_for_target_before_count(
    *,
    target_existing: tuple[dict[str, Any], ...],
    target_line: str,
    source: _SourcePartition,
    before_count: int,
) -> _RepackIntent | None:
    target_other = [
        car
        for car in target_existing
        if not _same_spotting_target(car, target_line, source.forced)
    ]
    if before_count > len(target_other):
        return None

    before_nos = set(_nos(target_other[:before_count]))
    same_nos = {
        physical.car_no(car)
        for car in target_existing
        if _same_spotting_target(car, target_line, source.forced)
    }
    groups = _target_groups(target_existing, before_nos, same_nos)
    if not groups:
        return None

    final_order = (
        *_cars_for_role(groups, "before"),
        *source.before,
        *source.same,
        *_cars_for_role(groups, "same"),
        *_cars_for_role(groups, "after"),
        *source.after,
    )
    return _RepackIntent(target_groups=groups, final_order=final_order)


def _target_groups(
    target_existing: tuple[dict[str, Any], ...],
    before_nos: set[str],
    same_nos: set[str],
) -> tuple[_TargetGroup, ...]:
    groups: list[_TargetGroup] = []
    current_role = ""
    current_cars: list[dict[str, Any]] = []
    for car in target_existing:
        no = physical.car_no(car)
        if no in before_nos:
            role = "before"
        elif no in same_nos:
            role = "same"
        else:
            role = "after"
        if role != current_role and current_cars:
            groups.append(_TargetGroup(current_role, tuple(current_cars)))
            current_cars = []
        current_role = role
        current_cars.append(car)
    if current_cars:
        groups.append(_TargetGroup(current_role, tuple(current_cars)))
    return tuple(groups)


def _intent_is_target_valid(
    *,
    intent: _RepackIntent,
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> bool:
    projected = _project_target_order(cars=cars, target_line=target_line, final_order=intent.final_order)
    return _all_spotting_groups_are_acceptable(
        projected,
        target_line,
        depot_assignment,
    ) and _spotting_final_state_is_valid(projected, target_line, depot_assignment)


def _all_spotting_groups_are_acceptable(
    cars: list[dict[str, Any]],
    target_line: str,
    depot_assignment: Any,
) -> bool:
    forced_groups = {
        physical.force_positions(car)
        for car in cars
        if car["Line"] == target_line
        and physical.force_positions(car)
        and target_line in (car.get("_TargetLineSet") or set(physical.target_lines(car)))
    }
    return all(
        physical.spotting_group_is_acceptable(cars, target_line, forced, depot_assignment)
        for forced in forced_groups
    )


def _spotting_final_state_is_valid(
    cars: list[dict[str, Any]],
    target_line: str,
    depot_assignment: Any,
) -> bool:
    total = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target_line, 0)
    if not total:
        return False
    positions = [
        int(car.get("Position") or 0)
        for car in cars
        if car["Line"] == target_line
    ]
    if any(position <= 0 or position > total for position in positions):
        return False
    if len(positions) != len(set(positions)):
        return False
    return _all_spotting_groups_are_acceptable(cars, target_line, depot_assignment)


def _project_target_order(
    *,
    cars: list[dict[str, Any]],
    target_line: str,
    final_order: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    final_nos = set(_nos(final_order))
    projected = [dict(car) for car in cars if not (car["Line"] == target_line or physical.car_no(car) in final_nos)]
    for position, car in enumerate(final_order, start=1):
        item = dict(car)
        item["Line"] = target_line
        item["Position"] = position
        projected.append(item)
    return projected


def _project_target_positions(
    *,
    cars: list[dict[str, Any]],
    target_line: str,
    final_cars: tuple[dict[str, Any], ...],
    final_positions: dict[str, int],
) -> list[dict[str, Any]]:
    final_nos = set(_nos(final_cars))
    projected = [dict(car) for car in cars if not (car["Line"] == target_line or physical.car_no(car) in final_nos)]
    for car in final_cars:
        no = physical.car_no(car)
        if no not in final_positions:
            continue
        item = dict(car)
        item["Line"] = target_line
        item["Position"] = int(final_positions[no])
        projected.append(item)
    return projected


def _build_plan_for_intent(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    source: _SourcePartition,
    source_blockers: tuple[dict[str, Any], ...],
    intent: _RepackIntent,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    reason: str,
    candidate_kind: str,
    frontier: Any,
    graph: Any,
    loco_location: Any,
    serial_gate_leases: dict[str, Any],
) -> SpottingRepackPlan | None:
    if any(physical.pull_equivalent(list(group.cars)) > physical.PULL_LIMIT_EQUIVALENT for group in intent.target_groups):
        return None
    source_batch = (*source.before, *source.same, *source.after)
    if physical.pull_equivalent([*source_blockers, *source_batch]) > physical.PULL_LIMIT_EQUIVALENT:
        return None

    for staging_assignment in _staging_assignments(
        target_groups=intent.target_groups,
        source_line=source_line,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
    ):
        steps = _plan_steps_for_assignment(
            source_line=source_line,
            target_line=target_line,
            source=source,
            source_blockers=source_blockers,
            target_groups=intent.target_groups,
            staging_assignment=staging_assignment,
            final_order=intent.final_order,
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
            candidate_kind=candidate_kind,
        ):
            continue
        return _candidate_plan(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_batch=source_batch,
            source_blockers=source_blockers,
            target_groups=intent.target_groups,
            steps=steps,
            reason=reason,
            candidate_kind=candidate_kind,
        )
    return None


def _plan_steps_for_assignment(
    *,
    source_line: str,
    target_line: str,
    source: _SourcePartition,
    source_blockers: tuple[dict[str, Any], ...],
    target_groups: tuple[_TargetGroup, ...],
    staging_assignment: tuple[str, ...],
    final_order: tuple[dict[str, Any], ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[Any, ...]:
    final_positions = {
        physical.car_no(car): position
        for position, car in enumerate(final_order, start=1)
    }
    source_batch = (*source.before, *source.same, *source.after)
    return _target_line_rebuild_steps(
        source_line=source_line,
        target_line=target_line,
        source_batch=source_batch,
        source_blockers=source_blockers,
        target_groups=target_groups,
        staging_assignment=staging_assignment,
        final_positions=final_positions,
        cars=cars,
        depot_assignment=depot_assignment,
    )


def _target_line_rebuild_steps(
    *,
    source_line: str,
    target_line: str,
    source_batch: tuple[dict[str, Any], ...],
    source_blockers: tuple[dict[str, Any], ...],
    target_groups: tuple[_TargetGroup, ...],
    staging_assignment: tuple[str, ...],
    final_positions: dict[str, int],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[Any, ...]:
    steps: list[Any] = []
    staged_by_group: dict[int, str] = {}
    target_chunk_lines: dict[tuple[int, tuple[str, ...]], str] = {}
    target_access_chunks: dict[int, tuple[tuple[str, ...], ...]] = {}
    used_target_staging_lines: set[str] = set()
    for group_index, group in enumerate(target_groups):
        staging_line = staging_assignment[group_index]
        tail_chunks = _target_group_put_chunks(group.nos, final_positions)
        if not tail_chunks:
            return ()
        desired_chunks = tuple(sorted(tail_chunks, key=lambda chunk: _deep_to_near_chunk_key(chunk, final_positions)))
        steps.append(physical.plan_step("Get", target_line, group.nos))
        if len(tail_chunks) == 1 or tuple(reversed(tail_chunks)) == desired_chunks:
            positions = planned_positions_for_batch(
                batch=list(group.cars),
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=set(group.nos),
            )
            if len(positions) != len(group.cars):
                return ()
            for put_nos in tail_chunks:
                steps.append(physical.plan_step("Put", staging_line, put_nos, _positions_for(put_nos, positions)))
            staged_by_group[group_index] = staging_line
            target_access_chunks[group_index] = tuple(reversed(tail_chunks))
            used_target_staging_lines.add(staging_line)
            continue

        chunk_assignment = _target_chunk_staging_assignment(
            chunks=tail_chunks,
            group_cars=group.cars,
            preferred_line=staging_line,
            source_line=source_line,
            target_line=target_line,
            excluded_lines=used_target_staging_lines,
            cars=cars,
            depot_assignment=depot_assignment,
        )
        if chunk_assignment is None:
            return ()
        for put_nos in tail_chunks:
            chunk_staging_line, positions = chunk_assignment[put_nos]
            steps.append(physical.plan_step("Put", chunk_staging_line, put_nos, positions))
            target_chunk_lines[(group_index, put_nos)] = chunk_staging_line
            used_target_staging_lines.add(chunk_staging_line)

    source_with_blockers = (*source_blockers, *source_batch)
    if source_with_blockers:
        steps.append(physical.plan_step("Get", source_line, _nos(source_with_blockers)))
    source_chunks = _target_group_put_chunks(_nos(source_batch), final_positions) if source_batch else ()

    rebuild_chunks: list[tuple[str, int, tuple[str, ...]]] = []
    for group_index, chunks in target_access_chunks.items():
        for chunk in chunks:
            rebuild_chunks.append(("target", group_index, chunk))
    for group_index, chunk in target_chunk_lines:
        rebuild_chunks.append(("target", group_index, chunk))
    for chunk in source_chunks:
        rebuild_chunks.append(("source", -1, chunk))
    rebuild_chunks.sort(key=lambda item: _deep_to_near_chunk_key(item[2], final_positions))
    source_staging = _source_chunk_staging_assignment(
        source_chunks=source_chunks,
        rebuild_chunks=tuple(rebuild_chunks),
        source_batch=source_batch,
        source_line=source_line,
        target_line=target_line,
        excluded_lines=set(staged_by_group.values()) | set(target_chunk_lines.values()),
        cars=cars,
        depot_assignment=depot_assignment,
    )
    if source_staging is None:
        return ()

    next_source_chunk = 0
    staged_source_chunks: dict[int, str] = {}
    source_chunk_indexes = {chunk: index for index, chunk in enumerate(source_chunks)}
    next_target_chunk = {group_index: 0 for group_index in target_access_chunks}
    for origin, group_index, chunk in rebuild_chunks:
        if origin == "source":
            chunk_index = source_chunk_indexes.get(chunk)
            if chunk_index is None:
                return ()
            if chunk_index > next_source_chunk:
                for staged_index in range(next_source_chunk, chunk_index):
                    staged_chunk = source_chunks[staged_index]
                    assignment = source_staging.get(staged_index)
                    if assignment is None:
                        return ()
                    staging_line, staging_positions = assignment
                    steps.append(physical.plan_step("Put", staging_line, staged_chunk, staging_positions))
                    staged_source_chunks[staged_index] = staging_line
                next_source_chunk = chunk_index
            elif chunk_index < next_source_chunk:
                staging_line = staged_source_chunks.pop(chunk_index, "")
                if not staging_line:
                    return ()
                steps.append(physical.plan_step("Get", staging_line, chunk))
                steps.append(physical.plan_step("Put", target_line, chunk, _positions_for(chunk, final_positions)))
                continue
            if next_source_chunk >= len(source_chunks) or source_chunks[next_source_chunk] != chunk:
                return ()
            steps.append(physical.plan_step("Put", target_line, chunk, _positions_for(chunk, final_positions)))
            next_source_chunk += 1
            continue
        chunk_staging_line = target_chunk_lines.pop((group_index, chunk), "")
        if chunk_staging_line:
            steps.append(physical.plan_step("Get", chunk_staging_line, chunk))
            steps.append(physical.plan_step("Put", target_line, chunk, _positions_for(chunk, final_positions)))
            continue
        chunks = target_access_chunks[group_index]
        chunk_index = next_target_chunk[group_index]
        if chunk_index >= len(chunks) or chunks[chunk_index] != chunk:
            return ()
        staging_line = staged_by_group[group_index]
        steps.append(physical.plan_step("Get", staging_line, chunk))
        steps.append(physical.plan_step("Put", target_line, chunk, _positions_for(chunk, final_positions)))
        next_target_chunk[group_index] = chunk_index + 1

    if next_source_chunk != len(source_chunks):
        return ()
    if staged_source_chunks:
        return ()
    if target_chunk_lines:
        return ()
    if any(next_target_chunk[index] != len(chunks) for index, chunks in target_access_chunks.items()):
        return ()
    if source_blockers:
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(source_blockers, start=1)
        }
        steps.append(physical.plan_step("Put", source_line, _nos(source_blockers), restore_positions))
    return tuple(steps)


def _target_chunk_staging_assignment(
    *,
    chunks: tuple[tuple[str, ...], ...],
    group_cars: tuple[dict[str, Any], ...],
    preferred_line: str,
    source_line: str,
    target_line: str,
    excluded_lines: set[str],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> dict[tuple[str, ...], tuple[str, dict[str, int]]] | None:
    by_no = {physical.car_no(car): car for car in group_cars}
    grouped = physical.cars_by_line(cars)
    used_lines = {source_line, target_line, *excluded_lines}
    candidate_lines = tuple(dict.fromkeys((preferred_line, *SPOTTING_REPACK_STAGING_LINES)))
    assignment: dict[tuple[str, ...], tuple[str, dict[str, int]]] = {}
    for chunk in chunks:
        batch = tuple(by_no[no] for no in chunk if no in by_no)
        if len(batch) != len(chunk):
            return None
        moving_nos = set(chunk)
        for staging_line in candidate_lines:
            if staging_line in used_lines:
                continue
            positions = planned_positions_for_batch(
                batch=list(batch),
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=moving_nos,
            )
            if (
                len(positions) == len(batch)
                and physical.candidate_positions_available(staging_line, positions, cars, moving_nos, grouped)
                and physical.line_has_length_capacity(staging_line, cars, list(batch), moving_nos, grouped=grouped)
            ):
                assignment[chunk] = (staging_line, positions)
                used_lines.add(staging_line)
                break
        else:
            return None
    return assignment


def _source_chunk_staging_assignment(
    *,
    source_chunks: tuple[tuple[str, ...], ...],
    rebuild_chunks: tuple[tuple[str, int, tuple[str, ...]], ...],
    source_batch: tuple[dict[str, Any], ...],
    source_line: str,
    target_line: str,
    excluded_lines: set[str],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> dict[int, tuple[str, dict[str, int]]] | None:
    required_indexes = _source_chunk_indexes_requiring_staging(source_chunks, rebuild_chunks)
    if required_indexes is None:
        return None
    if not required_indexes:
        return {}

    by_no = {physical.car_no(car): car for car in source_batch}
    grouped = physical.cars_by_line(cars)
    used_lines = {source_line, target_line, *excluded_lines}
    assignment: dict[int, tuple[str, dict[str, int]]] = {}
    for chunk_index in sorted(required_indexes):
        chunk = source_chunks[chunk_index]
        batch = tuple(by_no[no] for no in chunk if no in by_no)
        if len(batch) != len(chunk):
            return None
        moving_nos = set(chunk)
        for staging_line in SPOTTING_REPACK_STAGING_LINES:
            if staging_line in used_lines:
                continue
            positions = planned_positions_for_batch(
                batch=list(batch),
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=moving_nos,
            )
            if (
                len(positions) == len(batch)
                and physical.candidate_positions_available(staging_line, positions, cars, moving_nos, grouped)
                and physical.line_has_length_capacity(staging_line, cars, list(batch), moving_nos, grouped=grouped)
            ):
                assignment[chunk_index] = (staging_line, positions)
                used_lines.add(staging_line)
                break
        else:
            return None
    return assignment


def _source_chunk_indexes_requiring_staging(
    source_chunks: tuple[tuple[str, ...], ...],
    rebuild_chunks: tuple[tuple[str, int, tuple[str, ...]], ...],
) -> set[int] | None:
    if not source_chunks:
        return set()
    chunk_indexes = {chunk: index for index, chunk in enumerate(source_chunks)}
    required: set[int] = set()
    staged: set[int] = set()
    next_source_chunk = 0
    for origin, _, chunk in rebuild_chunks:
        if origin != "source":
            continue
        chunk_index = chunk_indexes.get(chunk)
        if chunk_index is None:
            return None
        if chunk_index > next_source_chunk:
            for staged_index in range(next_source_chunk, chunk_index):
                required.add(staged_index)
                staged.add(staged_index)
            next_source_chunk = chunk_index + 1
            continue
        if chunk_index == next_source_chunk:
            next_source_chunk += 1
            continue
        if chunk_index not in staged:
            return None
        staged.remove(chunk_index)
    if next_source_chunk != len(source_chunks) or staged:
        return None
    return required


def _deep_to_near_chunk_key(
    chunk: tuple[str, ...],
    final_positions: dict[str, int],
) -> tuple[int, int, tuple[str, ...]]:
    positions = [final_positions[no] for no in chunk if no in final_positions]
    if not positions:
        return (0, 0, chunk)
    return (-max(positions), -min(positions), chunk)


def _candidate_plan(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    source_batch: tuple[dict[str, Any], ...],
    source_blockers: tuple[dict[str, Any], ...],
    target_groups: tuple[_TargetGroup, ...],
    steps: tuple[Any, ...],
    reason: str,
    candidate_kind: str,
) -> SpottingRepackPlan:
    batch = [*source_blockers, *source_batch, *[car for group in target_groups for car in group.cars]]
    blocker_nos = _nos(source_blockers)
    candidate = physical.build_planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=target_line,
        target_line=target_line,
        batch=batch,
        steps=steps,
        reason=f"{reason};spotting_repack=target_line;target_groups={','.join(group.role for group in target_groups)}",
        candidate_kind=candidate_kind,
    )
    return SpottingRepackPlan(
        candidate=candidate,
        progressed_nos=_nos(source_batch),
        source_return_nos=blocker_nos,
    )


def _staging_assignments(
    *,
    target_groups: tuple[_TargetGroup, ...],
    source_line: str,
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[tuple[str, ...], ...]:
    labels = tuple(dict.fromkeys(group.role for group in target_groups))
    candidates_by_label: dict[str, list[str]] = {}
    excluded = {source_line, target_line}
    grouped = physical.cars_by_line(cars)

    for label in labels:
        label_cars = _cars_for_role(target_groups, label)
        moving_nos = set(_nos(label_cars))
        candidates: list[str] = []
        for staging_line in SPOTTING_REPACK_STAGING_LINES:
            if staging_line in excluded:
                continue
            if not _staging_line_accepts(
                staging_line=staging_line,
                batch=label_cars,
                moving_nos=moving_nos,
                cars=cars,
                depot_assignment=depot_assignment,
                grouped=grouped,
            ):
                continue
            candidates.append(staging_line)
        if not candidates:
            return ()
        candidates_by_label[label] = candidates

    assignments: list[tuple[str, ...]] = []
    for selected_lines in product(*(candidates_by_label[label] for label in labels)):
        label_to_line = dict(zip(labels, selected_lines))
        if len(set(label_to_line.values())) != len(label_to_line):
            continue
        assignments.append(tuple(label_to_line[group.role] for group in target_groups))
    return tuple(assignments)


def _staging_line_accepts(
    *,
    staging_line: str,
    batch: tuple[dict[str, Any], ...],
    moving_nos: set[str],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    grouped: dict[str, list[dict[str, Any]]],
) -> bool:
    positions = planned_positions_for_batch(
        batch=list(batch),
        target_line=staging_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=moving_nos,
    )
    return (
        len(positions) == len(batch)
        and physical.candidate_positions_available(staging_line, positions, cars, moving_nos, grouped)
        and physical.line_has_length_capacity(staging_line, cars, list(batch), moving_nos, grouped=grouped)
    )


def _positions_for(nos: tuple[str, ...], final_positions: dict[str, int]) -> dict[str, int]:
    return {no: final_positions[no] for no in nos if no in final_positions}


def _cars_for_role(groups: tuple[_TargetGroup, ...], role: str) -> tuple[dict[str, Any], ...]:
    return tuple(car for group in groups if group.role == role for car in group.cars)


def _same_spotting_target(car: dict[str, Any], target_line: str, forced: tuple[int, ...]) -> bool:
    return (
        physical.force_positions(car) == forced
        and target_line in (car.get("_TargetLineSet") or set(physical.target_lines(car)))
    )


def _nos(cars: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(physical.car_no(car) for car in cars)
