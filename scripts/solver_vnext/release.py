from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from .domain import ContractFamily


STANDARD_RELEASE_MIN_COUNT = 6
STANDARD_RELEASE_TARGET_COUNT = 12


@dataclass(frozen=True)
class Cun4ReleaseGroup:
    line: str
    vehicle_nos: tuple[str, ...]
    target_lines: tuple[str, ...]
    prefix_hold_nos: tuple[str, ...]
    ready: bool
    reason: str

    @property
    def count(self) -> int:
        return len(self.vehicle_nos)


@dataclass(frozen=True)
class Cun4PortState:
    line: str
    mode: str
    release_nos: tuple[str, ...]
    release_target_lines: tuple[str, ...]
    prefix_hold_nos: tuple[str, ...]
    outbound_hold_nos: tuple[str, ...]
    dirty_nos: tuple[str, ...]
    release_ready: bool
    standard_ready: bool
    reason: str

    @property
    def release_count(self) -> int:
        return len(self.release_nos)


def cun4_release_group(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    graph: Any | None = None,
    loco_location: Any | None = None,
    min_count: int = STANDARD_RELEASE_MIN_COUNT,
) -> Cun4ReleaseGroup:
    """Return the currently exposed inbound group at the CUN4 release port.

    This is a mechanism fact. It does not decide whether the group should be
    released now; phase/policy layers consume the fact.
    """
    loads = physical.line_loads(cars)
    if graph is not None and loco_location is not None:
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
    else:
        line_cars = sorted(
            (car for car in cars if car["Line"] == "存4线"),
            key=lambda item: (int(item.get("Position") or 0), physical.car_no(item)),
        )

    prefix_hold: list[str] = []
    group: list[dict[str, Any]] = []
    target_lines: list[str] = []
    blocked_by = ""
    for car in line_cars:
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if not group and target_line == "存4线":
            prefix_hold.append(physical.car_no(car))
            continue
        if target_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
            group.append(car)
            target_lines.append(target_line)
            continue
        blocked_by = physical.car_no(car)
        break

    if not group:
        reason = "cun4_port_empty_or_non_repair_prefix"
    elif len(group) < min_count:
        reason = "cun4_release_group_below_min_count"
    elif blocked_by:
        reason = f"cun4_release_group_prefix_ready_until:{blocked_by}"
    else:
        reason = "cun4_release_group_ready"
    return Cun4ReleaseGroup(
        line="存4线",
        vehicle_nos=tuple(physical.car_no(car) for car in group),
        target_lines=tuple(target_lines),
        prefix_hold_nos=tuple(prefix_hold),
        ready=len(group) >= min_count,
        reason=reason,
    )


def cun4_port_state(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    graph: Any | None = None,
    loco_location: Any | None = None,
) -> Cun4PortState:
    """Classify CUN4 as a resource port, not a generic storage line."""
    group = cun4_release_group(
        cars=cars,
        depot_assignment=depot_assignment,
        graph=graph,
        loco_location=loco_location,
        min_count=STANDARD_RELEASE_MIN_COUNT,
    )
    loads = physical.line_loads(cars)
    if graph is not None and loco_location is not None:
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
    else:
        line_cars = sorted(
            (car for car in cars if car["Line"] == "存4线"),
            key=lambda item: (int(item.get("Position") or 0), physical.car_no(item)),
        )

    release_nos = set(group.vehicle_nos)
    prefix_hold_nos = set(group.prefix_hold_nos)
    outbound_hold: list[str] = []
    dirty: list[str] = []
    for car in line_cars:
        no = physical.car_no(car)
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if no in release_nos:
            continue
        if target_line == "存4线":
            outbound_hold.append(no)
            continue
        if target_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
            dirty.append(no)
            continue
        if target_line and target_line != "存4线":
            dirty.append(no)

    if not line_cars:
        mode = "FREE"
    elif group.vehicle_nos and group.prefix_hold_nos:
        mode = "MIXED_RELEASE_TAIL"
    elif group.vehicle_nos:
        mode = "INBOUND_RELEASE"
    elif outbound_hold:
        mode = "OUTBOUND_HOLD"
    else:
        mode = "DIRTY"

    release_ready = bool(group.vehicle_nos) and not dirty
    standard_ready = standard_cun4_chain_applicable(cars, depot_assignment) and group.ready and not dirty
    reason = group.reason
    if dirty:
        reason = "cun4_port_dirty:" + ",".join(dirty[:8])
    elif release_ready and group.prefix_hold_nos:
        reason = "cun4_tail_release_ready_with_prefix_hold"
    elif release_ready:
        reason = "cun4_release_ready"
    return Cun4PortState(
        line="存4线",
        mode=mode,
        release_nos=group.vehicle_nos,
        release_target_lines=group.target_lines,
        prefix_hold_nos=group.prefix_hold_nos,
        outbound_hold_nos=tuple(outbound_hold),
        dirty_nos=tuple(dirty),
        release_ready=release_ready,
        standard_ready=standard_ready,
        reason=reason,
    )


def cun4_release_group_count(
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> int:
    return cun4_release_group(cars=cars, depot_assignment=depot_assignment).count


def cun4_release_ready(
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> bool:
    state = cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    return standard_cun4_chain_applicable(cars, depot_assignment) and state.release_ready


def standard_cun4_chain_applicable(
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> bool:
    loads = physical.line_loads(cars)
    repair_inbound = 0
    depot_outbound = 0
    cun4_bound = 0
    for car in physical.unsatisfied_cars(cars, depot_assignment):
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        family = physical.classify_action_family(
            car["Line"],
            target_line,
            bool(car.get("IsWeigh")),
        )
        if family == ContractFamily.REPAIR_INBOUND and car["Line"] not in physical.DEPOT_TARGET_LINES:
            repair_inbound += 1
        elif family == ContractFamily.DEPOT_OUTBOUND:
            depot_outbound += 1
        elif family == ContractFamily.CUN4_PORT_STAGING:
            cun4_bound += 1
    return repair_inbound >= 2 and (depot_outbound + cun4_bound) >= 3
