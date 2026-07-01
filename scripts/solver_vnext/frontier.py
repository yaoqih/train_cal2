from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from . import serial


@dataclass(frozen=True)
class AccessFrontierRecord:
    case_id: str
    hook_index: int
    loco_line: str
    loco_node: str
    relevant_line_count: int
    reachable_line_count: int
    route_blocked_line_count: int
    route_missing_line_count: int
    accessible_prefix_line_count: int
    accessible_prefix_vehicle_count: int
    serial_blocker_line_count: int
    reachable_lines: str
    route_blocked_lines: str
    route_missing_lines: str
    accessible_prefixes: str
    serial_blocker_lines: str


class AccessFrontier:
    """Physical frontier facts for the current state.

    The frontier is a mechanism layer.  It reports what is reachable and what
    prefix is physically exposed; it does not rank contracts or generate moves.
    """

    def snapshot(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
    ) -> AccessFrontierRecord:
        relevant_lines = self._relevant_lines(cars, depot_assignment)
        reachable: list[str] = []
        route_blocked: list[str] = []
        route_missing: list[str] = []
        prefixes: list[str] = []
        prefix_vehicle_count = 0

        for line in relevant_lines:
            static_route = graph.route(loco_location.node, line)
            occupied = legacy.legacy.occupied_lines_for_get_route(cars, set(), line)
            dynamic_route = graph.route_avoiding_occupied(loco_location.node, line, occupied)
            if dynamic_route:
                reachable.append(line)
            elif static_route:
                route_blocked.append(line)
            else:
                route_missing.append(line)

            prefix = self._pull_limited_prefix(
                cars=legacy.line_cars_in_access_order(
                    cars=cars,
                    line=line,
                    graph=graph,
                    loco_location=loco_location,
                )
            )
            if prefix:
                prefixes.append(f"{line}:{','.join(prefix)}")
                prefix_vehicle_count += len(prefix)

        serial_blockers = self._serial_blockers(
            cars=cars,
            depot_assignment=depot_assignment,
        )
        return AccessFrontierRecord(
            case_id=case_id,
            hook_index=hook_index,
            loco_line=str(getattr(loco_location, "line", "")),
            loco_node=str(getattr(loco_location, "node", "")),
            relevant_line_count=len(relevant_lines),
            reachable_line_count=len(reachable),
            route_blocked_line_count=len(route_blocked),
            route_missing_line_count=len(route_missing),
            accessible_prefix_line_count=len(prefixes),
            accessible_prefix_vehicle_count=prefix_vehicle_count,
            serial_blocker_line_count=len(serial_blockers),
            reachable_lines="|".join(reachable),
            route_blocked_lines="|".join(route_blocked),
            route_missing_lines="|".join(route_missing),
            accessible_prefixes="|".join(prefixes),
            serial_blocker_lines="|".join(serial_blockers),
        )

    def _relevant_lines(self, cars: list[dict[str, Any]], depot_assignment: Any) -> tuple[str, ...]:
        loads = legacy.line_loads(cars)
        lines = {str(car.get("Line") or "") for car in cars if car.get("Line")}
        for car in legacy.unsatisfied_cars(cars, depot_assignment):
            target_line, _target_position, _reason = legacy.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line:
                lines.add(target_line)
        lines.update(legacy.REMOTE_INTERACTION_LINES)
        return tuple(sorted(line for line in lines if line))

    def _pull_limited_prefix(self, cars: list[dict[str, Any]]) -> tuple[str, ...]:
        prefix: list[dict[str, Any]] = []
        for car in cars:
            if legacy.pull_equivalent([*prefix, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
        return tuple(legacy.car_no(car) for car in prefix)

    def _serial_blockers(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        for blocker_line in sorted(serial.serial_blocker_lines()):
            blocker_nos = sorted(
                legacy.car_no(car)
                for car in cars
                if car["Line"] == blocker_line
            )
            if not blocker_nos:
                continue
            downstream_debt = serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
            if downstream_debt:
                blockers.append(
                    f"{blocker_line}:{','.join(blocker_nos)}->{','.join(sorted(downstream_debt)[:8])}"
                )
        return tuple(blockers)
