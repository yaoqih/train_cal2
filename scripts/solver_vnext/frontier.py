from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from . import serial


@dataclass(frozen=True)
class AccessFrontierRecord:
    case_id: str
    hook_index: int
    loco_line: str
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
            static_route = graph.route(loco_location.line, line)
            occupied = physical.occupied_lines_for_get_route(cars, set(), line)
            dynamic_route = graph.route_avoiding_occupied(
                loco_location.line,
                line,
                occupied,
                target_approach_lines=physical.route_approach_lines_for_get(line),
            )
            if dynamic_route:
                reachable.append(line)
            elif static_route:
                route_blocked.append(line)
            else:
                route_missing.append(line)

            prefix = self._pull_limited_prefix(
                cars=physical.line_cars_in_access_order(
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

    def reachable_staging_lines(
        self,
        *,
        source_line: str,
        batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        candidate_lines: tuple[str, ...],
        excluded_lines: set[str] | None = None,
    ) -> tuple[str, ...]:
        """Return physically usable staging lines for a carried batch.

        This is a mechanism query.  It delegates route/order/length checks to
        the same hard validator used for accepted candidates; the frontier only
        enumerates candidate staging lines.
        """
        if not batch:
            return ()
        excluded_lines = excluded_lines or set()
        moving_nos = {physical.car_no(car) for car in batch}
        grouped = physical.cars_by_line(cars)

        reachable: list[str] = []
        for line in candidate_lines:
            if line == source_line or line in excluded_lines:
                continue
            if line in physical.RUNNING_LINES or line in physical.DEPOT_TARGET_LINES:
                continue
            if line not in physical.TRACK_SPECS:
                continue

            planned_positions = physical.planned_positions_for_batch(
                batch=batch,
                target_line=line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=moving_nos,
                grouped=grouped,
            )
            if len(planned_positions) != len(batch):
                continue

            validation = self.direct_move_reachability(
                source_line=source_line,
                target_line=line,
                batch=batch,
                cars=cars,
                graph=graph,
                loco_location=loco_location,
                depot_assignment=depot_assignment,
                planned_positions=planned_positions,
                candidate_kind="blocker_relocation",
            )
            if not validation.accepted:
                continue
            reachable.append(line)
        return tuple(reachable)

    def direct_move_reachability(
        self,
        *,
        source_line: str,
        target_line: str,
        batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
        depot_assignment: Any | None = None,
        serial_gate_leases: dict[str, Any] | None = None,
        planned_positions: dict[str, int] | None = None,
        candidate_kind: str = "frontier_direct_probe",
    ) -> physical.PhysicalValidation:
        """Probe a single Get/Put move through the hard physical validator."""
        del serial_gate_leases
        if not batch:
            return self._rejected("empty_batch")
        if source_line == target_line:
            return self._rejected("same_line_move")
        candidate = physical.hook_candidate(
            case_id="frontier_probe",
            hook_index=0,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            planned_positions=planned_positions or {},
            generation_reason="frontier:direct_move_probe",
            candidate_kind=candidate_kind,
        )
        return physical.validate_candidate(
            graph,
            candidate,
            cars,
            loco_location,
            self._depot_assignment_or_empty(depot_assignment),
        )

    def direct_move_is_reachable(
        self,
        *,
        source_line: str,
        target_line: str,
        batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
        depot_assignment: Any | None = None,
        serial_gate_leases: dict[str, Any] | None = None,
        planned_positions: dict[str, int] | None = None,
    ) -> bool:
        """Return whether a single Get/Put move is route-reachable now."""
        return self.direct_move_reachability(
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            cars=cars,
            graph=graph,
            loco_location=loco_location,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases,
            planned_positions=planned_positions,
        ).accepted

    def plan_steps_reachability(
        self,
        *,
        steps: tuple[Any, ...],
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
        depot_assignment: Any | None = None,
        serial_gate_leases: dict[str, Any] | None = None,
        candidate_kind: str = "frontier_planlet_probe",
    ) -> physical.PhysicalValidation:
        """Probe a Get/Put/Weigh planlet through the hard physical validator."""
        del serial_gate_leases
        if not steps:
            return self._rejected("empty_plan_steps")

        move_nos = tuple(
            dict.fromkeys(
                no
                for step in steps
                for no in getattr(step, "move_car_nos", ())
            )
        )
        by_no = {physical.car_no(car): car for car in cars}
        batch = [by_no[no] for no in move_nos if no in by_no]
        source_line = next(
            (step.line for step in steps if getattr(step, "action", "") == "Get"),
            steps[0].line,
        )
        target_line = next(
            (step.line for step in reversed(steps) if getattr(step, "action", "") == "Put"),
            steps[-1].line,
        )
        planned_positions: dict[str, int] = {}
        for step in steps:
            planned_positions.update(getattr(step, "planned_positions", {}) or {})
        candidate = physical.HookCandidate(
            case_id="frontier_probe",
            hook_index=0,
            candidate_id=physical.planlet_candidate_id(
                case_id="frontier_probe",
                hook_index=0,
                candidate_kind=candidate_kind,
                steps=steps,
            ),
            source_line=source_line,
            target_line=target_line,
            move_car_nos=move_nos,
            action_family=physical.planlet_action_family(steps) or "FRONTIER_PROBE",
            train_length_m=round(sum(physical.car_length(car) for car in batch), 3),
            pull_equivalent_count=physical.pull_equivalent(batch),
            has_weigh=any(getattr(step, "action", "") == "Weigh" for step in steps),
            planned_positions=planned_positions,
            generation_reason="frontier:planlet_probe",
            candidate_kind=candidate_kind,
            plan_steps=steps,
        )
        return physical.validate_candidate(
            graph,
            candidate,
            cars,
            loco_location,
            self._depot_assignment_or_empty(depot_assignment),
        )

    def plan_steps_are_reachable(
        self,
        *,
        steps: tuple[Any, ...],
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
        depot_assignment: Any | None = None,
        serial_gate_leases: dict[str, Any] | None = None,
        candidate_kind: str = "frontier_planlet_probe",
    ) -> bool:
        """Return whether a planlet's Get/Put sequence is physically reachable."""
        return self.plan_steps_reachability(
            steps=steps,
            cars=cars,
            graph=graph,
            loco_location=loco_location,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases,
            candidate_kind=candidate_kind,
        ).accepted

    def target_put_violation_reasons(
        self,
        *,
        target_line: str,
        batch: list[dict[str, Any]],
        projected_cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, ...]:
        """Return target placement violations after a physical put projection."""
        batch_nos = {physical.car_no(car) for car in batch}
        projected_by_no = {physical.car_no(car): car for car in projected_cars}
        actual_positions = {
            no: int(projected_by_no[no].get("Position") or 0)
            for no in batch_nos
            if no in projected_by_no and projected_by_no[no]["Line"] == target_line
        }
        probe = physical.HookCandidate(
            case_id="",
            hook_index=0,
            candidate_id="target_put_frontier_probe",
            source_line="",
            target_line=target_line,
            move_car_nos=tuple(physical.car_no(car) for car in batch),
            action_family="FRONTIER_TARGET_PUT_PROBE",
            train_length_m=round(sum(physical.car_length(car) for car in batch), 3),
            pull_equivalent_count=physical.pull_equivalent(batch),
            has_weigh=any(bool(car.get("IsWeigh")) for car in batch),
            planned_positions=dict(actual_positions),
            generation_reason="vnext:depot_put_frontier_probe",
            candidate_kind="blocker_relocation",
        )
        return tuple(
            physical.validate_target_positions(
                probe,
                projected_cars,
                batch,
                depot_assignment,
            )
        )

    def depot_put_violation_reasons(
        self,
        *,
        target_line: str,
        batch: list[dict[str, Any]],
        projected_cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, ...]:
        """Return depot slot/order violations after a physical put projection."""
        if target_line not in physical.DEPOT_LINES:
            return ()
        return self.target_put_violation_reasons(
            target_line=target_line,
            batch=batch,
            projected_cars=projected_cars,
            depot_assignment=depot_assignment,
        )

    def _depot_assignment_or_empty(self, depot_assignment: Any | None) -> Any:
        return depot_assignment if depot_assignment is not None else physical.DepotAssignment({}, {})

    def _rejected(self, reason: str) -> physical.PhysicalValidation:
        return physical.PhysicalValidation(
            accepted=False,
            reasons=(reason,),
            get_path=(),
            weigh_path=(),
            put_path=(),
            operation_paths=(),
        )

    def _relevant_lines(self, cars: list[dict[str, Any]], depot_assignment: Any) -> tuple[str, ...]:
        loads = physical.line_loads(cars)
        lines = {str(car.get("Line") or "") for car in cars if car.get("Line")}
        for car in physical.unsatisfied_cars(cars, depot_assignment):
            target_line, _target_position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line:
                lines.add(target_line)
        lines.update(physical.REMOTE_INTERACTION_LINES)
        return tuple(sorted(line for line in lines if line))

    def _pull_limited_prefix(self, cars: list[dict[str, Any]]) -> tuple[str, ...]:
        prefix: list[dict[str, Any]] = []
        for car in cars:
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
        return tuple(physical.car_no(car) for car in prefix)

    def _serial_blockers(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        for blocker_line in sorted(serial.serial_blocker_lines()):
            blocker_nos = sorted(
                physical.car_no(car)
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
