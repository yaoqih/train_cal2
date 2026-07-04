from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical


OVERFLOW_ASSEMBLY_LINES = ("机南", "机走棚", "机走北", "洗油北")
TARGET_LINE = "存4线"
SOURCE_PULL_ORDER = (
    "卸轮线",
    "修1库外",
    "修1库内",
    "修2库外",
    "修2库内",
    "修3库外",
    "修3库内",
    "修4库外",
    "修4库内",
)


@dataclass(frozen=True)
class AssemblyGroup:
    line: str
    vehicle_nos: tuple[str, ...]
    length_m: float
    capacity_m: float


@dataclass(frozen=True)
class DepotOutboundAssemblyPlan:
    status: str
    reason: str
    outbound_nos: tuple[str, ...]
    route_blocker_nos: tuple[str, ...]
    non_cun4_nos: tuple[str, ...]
    cun4_target_nos: tuple[str, ...]
    pull_order_nos: tuple[str, ...]
    cun4_nos: tuple[str, ...]
    cun4_prefix_unsafe_nos: tuple[str, ...]
    cun4_reserved_by_outbound_hold_nos: tuple[str, ...]
    overflow_nos: tuple[str, ...]
    unplaced_nos: tuple[str, ...]
    source_lines: tuple[str, ...]
    target_lines: tuple[str, ...]
    total_length_m: float
    pullout_required_m: float
    depot_inner_free_after_pull_m: float
    depot_outer_free_after_pull_m: float
    remote_surplus_after_pull_m: float
    cun4_free_m: float
    cun4_budget_m: float
    pull_equivalent: int
    groups: tuple[AssemblyGroup, ...]

    @property
    def group_count(self) -> int:
        return sum(1 for group in self.groups if group.vehicle_nos)

    @property
    def temporary_line_by_no(self) -> dict[str, str]:
        return {
            no: group.line
            for group in self.groups
            for no in group.vehicle_nos
        }

    @property
    def assembly_complete(self) -> bool:
        return not self.outbound_nos


def build_depot_outbound_assembly_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    cun4_released_nos: set[str] | None = None,
) -> DepotOutboundAssemblyPlan:
    """Describe the second-stage large-depot outbound skeleton.

    This planning fact only owns cars that started in the large depot and whose
    true target is outside the large-depot/unwheel destination area.  The stage
    ends when those cars have been gathered to CUN4.  CUN4-target cars are kept
    after other targets in the planned pull order so they land farther south.
    """
    loads = physical.line_loads(cars)
    cun4_released_nos = cun4_released_nos or set()
    outbound_rows: list[tuple[dict[str, Any], str]] = []
    for car in physical.unsatisfied_cars(cars, depot_assignment):
        source_line = car["Line"]
        initial_line = car["_InitialLine"]
        if initial_line not in physical.DEPOT_LINES:
            continue
        if source_line == TARGET_LINE:
            continue
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if not target_line:
            continue
        if target_line in physical.DEPOT_TARGET_LINES:
            continue
        outbound_rows.append((car, target_line))

    if not outbound_rows:
        return _empty_plan("no_depot_outbound_debt")

    route_blocker_rows = _route_blocker_rows(
        cars=cars,
        depot_assignment=depot_assignment,
        loads=loads,
        outbound_rows=outbound_rows,
        excluded_nos=set(cun4_released_nos),
    )
    route_blocker_rows = [
        *route_blocker_rows,
        *_source_interleaved_blocker_rows(
            cars=cars,
            depot_assignment=depot_assignment,
            loads=loads,
            outbound_rows=outbound_rows,
            route_blocker_rows=route_blocker_rows,
            excluded_nos=set(cun4_released_nos),
        ),
    ]
    ordered_rows = sorted(
        [*outbound_rows, *route_blocker_rows],
        key=lambda row: (_source_rank(row[0]["Line"]), int(row[0].get("Position") or 0), physical.car_no(row[0])),
    )
    outbound_nos = tuple(
        physical.car_no(car)
        for car, _target_line in sorted(
            outbound_rows,
            key=lambda row: (_source_rank(row[0]["Line"]), int(row[0].get("Position") or 0), physical.car_no(row[0])),
        )
    )
    route_blocker_nos = tuple(
        physical.car_no(car)
        for car, _target_line in sorted(
            route_blocker_rows,
            key=lambda row: (_source_rank(row[0]["Line"]), int(row[0].get("Position") or 0), physical.car_no(row[0])),
        )
    )
    non_cun4_rows = [row for row in ordered_rows if row[1] != "存4线"]
    cun4_rows = [row for row in ordered_rows if row[1] == "存4线"]
    cun4_prefix_unsafe_nos = _cun4_prefix_unsafe_nos(
        ordered_rows=ordered_rows,
        non_cun4_nos={physical.car_no(car) for car, _target_line in non_cun4_rows},
    )
    if cun4_prefix_unsafe_nos or route_blocker_nos:
        pull_rows = ordered_rows
    else:
        pull_rows = [*non_cun4_rows, *cun4_rows]
    pull_order_nos = tuple(physical.car_no(car) for car, _target_line in pull_rows)
    moving_nos = set(outbound_nos)
    total_length_m = sum(physical.car_length(car) for car, _target_line in ordered_rows)
    pullout_required_m = total_length_m + physical.LOCO_LENGTH_M
    depot_inner_free_after_pull_m = sum(
        _line_free_after_removal(line, cars, moving_nos)
        for line in sorted(physical.DEPOT_LINES)
    )
    depot_outer_free_after_pull_m = sum(
        _line_free_after_removal(line, cars, moving_nos)
        for line in (*sorted(physical.DEPOT_OUTSIDE_LINES), "卸轮线")
    )
    remote_surplus_after_pull_m = depot_inner_free_after_pull_m + depot_outer_free_after_pull_m - pullout_required_m
    cun4_free_m = _line_free_after_removal(TARGET_LINE, cars, set(cun4_released_nos))
    cun4_budget_m = cun4_free_m
    cun4_nos = pull_order_nos
    overflow_nos: tuple[str, ...] = ()
    cun4_reserved_by_outbound_hold_nos: tuple[str, ...] = ()
    unplaced_nos: tuple[str, ...] = ()
    groups = (
        AssemblyGroup(
            line=TARGET_LINE,
            vehicle_nos=cun4_nos,
            length_m=_length_for_nos(cars, set(cun4_nos)),
            capacity_m=round(cun4_budget_m, 3),
        ),
    )
    pull_equivalent = physical.pull_equivalent([car for car, _target_line in ordered_rows])
    if total_length_m > cun4_budget_m + physical.LINE_LENGTH_TOLERANCE_M:
        status = "fail"
        reason = "cun4_capacity_insufficient_for_depot_outbound"
    elif pull_equivalent > physical.PULL_LIMIT_EQUIVALENT:
        status = "warn"
        reason = "single_pull_equivalent_exceeds_limit"
    elif cun4_prefix_unsafe_nos:
        status = "warn"
        reason = "cun4_target_suffix_requires_source_repack"
    elif route_blocker_nos:
        status = "warn"
        reason = "depot_outer_route_blocker_requires_source_order"
    else:
        status = "pass"
        reason = "capacity_plan_ready"

    return DepotOutboundAssemblyPlan(
        status=status,
        reason=reason,
        outbound_nos=outbound_nos,
        route_blocker_nos=route_blocker_nos,
        non_cun4_nos=tuple(physical.car_no(car) for car, _target_line in non_cun4_rows),
        cun4_target_nos=tuple(physical.car_no(car) for car, _target_line in cun4_rows),
        pull_order_nos=pull_order_nos,
        cun4_nos=cun4_nos,
        cun4_prefix_unsafe_nos=cun4_prefix_unsafe_nos,
        cun4_reserved_by_outbound_hold_nos=cun4_reserved_by_outbound_hold_nos,
        overflow_nos=overflow_nos,
        unplaced_nos=unplaced_nos,
        source_lines=tuple(dict.fromkeys(car["Line"] for car, _target_line in ordered_rows)),
        target_lines=tuple(dict.fromkeys(target_line for _car, target_line in ordered_rows)),
        total_length_m=round(total_length_m, 3),
        pullout_required_m=round(pullout_required_m, 3),
        depot_inner_free_after_pull_m=round(depot_inner_free_after_pull_m, 3),
        depot_outer_free_after_pull_m=round(depot_outer_free_after_pull_m, 3),
        remote_surplus_after_pull_m=round(remote_surplus_after_pull_m, 3),
        cun4_free_m=round(cun4_free_m, 3),
        cun4_budget_m=round(cun4_budget_m, 3),
        pull_equivalent=pull_equivalent,
        groups=groups,
    )


def _empty_plan(reason: str) -> DepotOutboundAssemblyPlan:
    return DepotOutboundAssemblyPlan(
        status="pass",
        reason=reason,
        outbound_nos=(),
        route_blocker_nos=(),
        non_cun4_nos=(),
        cun4_target_nos=(),
        pull_order_nos=(),
        cun4_nos=(),
        cun4_prefix_unsafe_nos=(),
        cun4_reserved_by_outbound_hold_nos=(),
        overflow_nos=(),
        unplaced_nos=(),
        source_lines=(),
        target_lines=(),
        total_length_m=0.0,
        pullout_required_m=0.0,
        depot_inner_free_after_pull_m=0.0,
        depot_outer_free_after_pull_m=0.0,
        remote_surplus_after_pull_m=0.0,
        cun4_free_m=0.0,
        cun4_budget_m=0.0,
        pull_equivalent=0,
        groups=(),
    )


def _source_rank(line: str) -> int:
    if line in SOURCE_PULL_ORDER:
        return SOURCE_PULL_ORDER.index(line)
    raise ValueError(f"unexpected_depot_outbound_source:{line}")


def _route_blocker_rows(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    loads: Any,
    outbound_rows: list[tuple[dict[str, Any], str]],
    excluded_nos: set[str],
) -> list[tuple[dict[str, Any], str]]:
    outbound_source_lines = {
        car["Line"]
        for car, _target_line in outbound_rows
        if car["Line"] in physical.DEPOT_LINES
    }
    if not outbound_source_lines:
        return []
    outbound_nos = {physical.car_no(car) for car, _target_line in outbound_rows}
    blocker_lines = {
        physical.DEPOT_INNER_BLOCKERS[line]
        for line in outbound_source_lines
        if line in physical.DEPOT_INNER_BLOCKERS
    }
    rows: list[tuple[dict[str, Any], str]] = []
    for blocker_line in SOURCE_PULL_ORDER:
        if blocker_line not in blocker_lines:
            continue
        for car in physical.line_cars_in_access_order(cars=cars, line=blocker_line):
            no = physical.car_no(car)
            if no in outbound_nos or no in excluded_nos:
                continue
            if physical.car_is_satisfied(car, depot_assignment, cars):
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if not target_line or target_line == blocker_line or target_line in physical.RUNNING_LINES:
                continue
            rows.append((car, target_line))
    return rows


def _source_interleaved_blocker_rows(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    loads: Any,
    outbound_rows: list[tuple[dict[str, Any], str]],
    route_blocker_rows: list[tuple[dict[str, Any], str]],
    excluded_nos: set[str],
) -> list[tuple[dict[str, Any], str]]:
    owned_nos = {
        physical.car_no(car)
        for car, _target_line in (*outbound_rows, *route_blocker_rows)
    } | set(excluded_nos)
    source_lines = {
        car["Line"]
        for car, _target_line in (*outbound_rows, *route_blocker_rows)
        if car["Line"] in SOURCE_PULL_ORDER
    }
    rows: list[tuple[dict[str, Any], str]] = []
    for source_line in sorted(source_lines, key=_source_rank):
        access_order = list(physical.line_cars_in_access_order(cars=cars, line=source_line))
        owned_indexes = [
            index
            for index, car in enumerate(access_order)
            if physical.car_no(car) in owned_nos
        ]
        if not owned_indexes:
            continue
        for car in access_order[: max(owned_indexes) + 1]:
            no = physical.car_no(car)
            if no in owned_nos or physical.car_is_satisfied(car, depot_assignment, cars):
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if not target_line or target_line == source_line or target_line in physical.RUNNING_LINES:
                continue
            rows.append((car, target_line))
            owned_nos.add(no)
    return rows


def _line_free_after_removal(
    line: str,
    cars: list[dict[str, Any]],
    moving_nos: set[str],
) -> float:
    spec = physical.TRACK_SPECS[line]
    load = physical.line_length_load(cars, line, excluded_nos=moving_nos)
    return spec.length_m - load


def _cun4_prefix_unsafe_nos(
    *,
    ordered_rows: list[tuple[dict[str, Any], str]],
    non_cun4_nos: set[str],
) -> tuple[str, ...]:
    unsafe: list[str] = []
    rows_by_source: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for row in ordered_rows:
        rows_by_source.setdefault(row[0]["Line"], []).append(row)
    for rows in rows_by_source.values():
        seen_cun4_target = False
        for car, _target_line in sorted(rows, key=lambda row: (int(row[0].get("Position") or 0), physical.car_no(row[0]))):
            no = physical.car_no(car)
            if no in non_cun4_nos:
                if seen_cun4_target:
                    unsafe.append(no)
                continue
            seen_cun4_target = True
    return tuple(unsafe)


def _cun4_outbound_hold_nos(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    loads: Any,
) -> tuple[str, ...]:
    holds: list[str] = []
    for car in physical.line_cars_in_access_order(cars=cars, line="存4线"):
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if target_line == "存4线":
            holds.append(physical.car_no(car))
    return tuple(holds)


def _overflow_groups(
    *,
    cars: list[dict[str, Any]],
    rows: list[tuple[dict[str, Any], str]],
) -> tuple[tuple[AssemblyGroup, ...], tuple[str, ...]]:
    remaining_capacity = {
        line: _line_free_after_removal(line, cars, set())
        for line in OVERFLOW_ASSEMBLY_LINES
    }
    grouped: dict[str, list[str]] = {line: [] for line in OVERFLOW_ASSEMBLY_LINES}
    unplaced: list[str] = []
    for car, _target_line in rows:
        no = physical.car_no(car)
        length = physical.car_length(car)
        placed = False
        for line in OVERFLOW_ASSEMBLY_LINES:
            if remaining_capacity[line] + physical.LINE_LENGTH_TOLERANCE_M < length:
                continue
            grouped[line].append(no)
            remaining_capacity[line] -= length
            placed = True
            break
        if not placed:
            unplaced.append(no)
    groups = tuple(
        AssemblyGroup(
            line=line,
            vehicle_nos=tuple(grouped[line]),
            length_m=round(_length_for_nos(cars, set(grouped[line])), 3),
            capacity_m=round(_line_free_after_removal(line, cars, set()), 3),
        )
        for line in OVERFLOW_ASSEMBLY_LINES
    )
    return groups, tuple(unplaced)


def _length_for_nos(cars: list[dict[str, Any]], nos: set[str]) -> float:
    return round(sum(physical.car_length(car) for car in cars if physical.car_no(car) in nos), 3)
