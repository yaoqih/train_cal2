from __future__ import annotations

from dataclasses import dataclass
from itertools import product
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
        if physical.force_positions(car):
            return True
    return False


def build_spotting_target_repack_planlet(
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
        return None

    target_existing = tuple(
        physical.line_cars_in_access_order(
            cars=cars,
            line=target_line,
            graph=graph,
            loco_location=loco_location,
        )
    )
    if not target_existing:
        return None
    target_same = tuple(
        car
        for car in target_existing
        if _same_spotting_target(car, target_line, source.forced)
    )
    if not target_same:
        return None
    capacity = physical.spotting_capacity(target_line, source.forced)
    if not capacity or len(source.same) + len(target_same) > capacity:
        return None
    if not physical.line_has_length_capacity(
        target_line,
        cars,
        list(source_cars),
        set(_nos(source_cars)),
        grouped=physical.cars_by_line(cars),
    ):
        return None

    target_after_batch = [*target_existing, *source_cars]
    for intent in _repack_intents(
        target_existing=target_existing,
        target_line=target_line,
        source=source,
    ):
        if not _intent_is_target_valid(
            intent=intent,
            target_line=target_line,
            forced=source.forced,
            cars=cars,
            depot_assignment=depot_assignment,
            frontier=frontier,
            target_after_batch=target_after_batch,
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
    return None


def _source_partition(source_batch: tuple[dict[str, Any], ...]) -> _SourcePartition | None:
    forced_values = [physical.force_positions(car) for car in source_batch if physical.force_positions(car)]
    if not forced_values:
        return None
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


def _repack_intents(
    *,
    target_existing: tuple[dict[str, Any], ...],
    target_line: str,
    source: _SourcePartition,
) -> tuple[_RepackIntent, ...]:
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
    for car in target_existing:
        no = physical.car_no(car)
        if no in before_nos:
            role = "before"
        elif no in same_nos:
            role = "same"
        else:
            role = "after"
        groups.append(_TargetGroup(role, (car,)))
    return tuple(groups)


def _intent_is_target_valid(
    *,
    intent: _RepackIntent,
    target_line: str,
    forced: tuple[int, ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    frontier: Any,
    target_after_batch: list[dict[str, Any]],
) -> bool:
    projected = _project_target_order(cars=cars, target_line=target_line, final_order=intent.final_order)
    return (
        physical.spotting_group_is_acceptable(projected, target_line, forced, depot_assignment)
        and not frontier.target_put_violation_reasons(
            target_line=target_line,
            batch=target_after_batch,
            projected_cars=projected,
            depot_assignment=depot_assignment,
        )
    )


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
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[Any, ...]:
    steps: list[Any] = []
    staged_by_group: dict[int, str] = {}
    for group_index, group in enumerate(target_groups):
        staging_line = staging_assignment[group_index]
        positions = planned_positions_for_batch(
            batch=list(group.cars),
            target_line=staging_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=set(group.nos),
        )
        if len(positions) != len(group.cars):
            return ()
        steps.append(physical.plan_step("Get", target_line, group.nos))
        steps.append(physical.plan_step("Put", staging_line, group.nos, positions))
        staged_by_group[group_index] = staging_line

    source_batch = (*source.before, *source.same, *source.after)
    steps.append(physical.plan_step("Get", source_line, _nos((*source_blockers, *source_batch))))
    _append_put(steps, target_line, source.after)
    _append_return_groups(steps, target_line, "after", target_groups, staged_by_group)
    _append_return_groups(steps, target_line, "same", target_groups, staged_by_group)
    _append_put(steps, target_line, source.same)
    _append_put(steps, target_line, source.before)
    _append_return_groups(steps, target_line, "before", target_groups, staged_by_group)

    if source_blockers:
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(source_blockers, start=1)
        }
        steps.append(physical.plan_step("Put", source_line, _nos(source_blockers), restore_positions))
    return tuple(steps)


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


def _append_return_groups(
    steps: list[Any],
    target_line: str,
    role: str,
    groups: tuple[_TargetGroup, ...],
    staged_by_group: dict[int, str],
) -> None:
    for group_index, group in reversed([(index, group) for index, group in enumerate(groups) if group.role == role]):
        staging_line = staged_by_group[group_index]
        steps.append(physical.plan_step("Get", staging_line, group.nos))
        steps.append(physical.plan_step("Put", target_line, group.nos))


def _append_put(steps: list[Any], target_line: str, cars: tuple[dict[str, Any], ...]) -> None:
    if cars:
        steps.append(physical.plan_step("Put", target_line, _nos(cars)))


def _cars_for_role(groups: tuple[_TargetGroup, ...], role: str) -> tuple[dict[str, Any], ...]:
    return tuple(car for group in groups if group.role == role for car in group.cars)


def _same_spotting_target(car: dict[str, Any], target_line: str, forced: tuple[int, ...]) -> bool:
    return (
        physical.force_positions(car) == forced
        and target_line in (car.get("_TargetLineSet") or set(physical.target_lines(car)))
    )


def _nos(cars: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(physical.car_no(car) for car in cars)
