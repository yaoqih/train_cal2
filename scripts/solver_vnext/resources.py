from __future__ import annotations

from typing import Any

from . import physical
from . import serial
from .domain import ContractFamily, IntentKind
from .domain import CandidateEnvelope, ResourceDelta, ResourceKind, ResourceRequest


SAME_PLAN_STAGING_OWNER_KINDS = {
    "vnext_spotting_repack",
    "vnext_tail_blocker_peel_digest",
    "vnext_stage4_linear_sweep",
    "vnext_remote_session_prefix_batch_digest_restore",
    "vnext_depot_cun4_source_repack_exchange",
    "vnext_depot_cun4_inbound_outbound_exchange",
    "vnext_depot_inbound_cun4_open_release",
    "vnext_depot_inbound_assembly_release",
}


class StationResourceGraph:
    """Hard resource arbiter for vNext candidates.

    Physical reachability is decided before this layer.  Serial gate checks here
    are strategy/resource constraints: they protect downstream work from being
    re-blocked after an entrance has been opened.
    """

    def request_for(self, envelope: CandidateEnvelope) -> ResourceRequest:
        candidate = envelope.candidate
        steps = physical.candidate_plan_steps(candidate)
        touched_lines = tuple(dict.fromkeys(step.line for step in steps if step.action in {"Get", "Put", "Weigh"}))
        put_lines = tuple(dict.fromkeys(step.line for step in steps if step.action == "Put"))
        resources = [ResourceKind.LOCO_POSITION, ResourceKind.LOCO_CARRY, ResourceKind.ROUTE_GET, ResourceKind.ROUTE_PUT]
        if any(line in physical.DEPOT_LINES for line in put_lines):
            resources.append(ResourceKind.DEPOT_SLOT)
        if any(line in physical.DEPOT_OUTSIDE_LINES for line in put_lines):
            resources.append(ResourceKind.DEPOT_SLOT)
        if "存4线" in touched_lines:
            resources.append(ResourceKind.CUN4_NORTH_BUFFER)
        if any(line in physical.REMOTE_INTERACTION_LINES for line in touched_lines):
            resources.append(ResourceKind.REMOTE_SESSION)
            resources.append(ResourceKind.GLOBAL_GATE)
        if candidate.has_weigh:
            resources.append(ResourceKind.WEIGH_STAND)
        if any(line not in physical.DEPOT_LINES for line in put_lines):
            resources.append(ResourceKind.LINE_CAPACITY)
        if any(
            line in serial.serial_related_lines()
            for line in tuple(dict.fromkeys((*touched_lines, *put_lines)))
        ):
            resources.append(ResourceKind.SERIAL_LINE_GATE)
        return ResourceRequest(
            contract_id=envelope.contract.contract_id,
            family=envelope.contract.family,
            candidate_id=candidate.candidate_id,
            resources=tuple(dict.fromkeys(resources)),
            source_line=candidate.source_line,
            target_line=envelope.resource_request.target_line or candidate.target_line,
            move_nos=tuple(candidate.move_car_nos),
            touched_lines=touched_lines,
            put_lines=put_lines,
            intent=envelope.intent,
            same_plan_source_return_nos=envelope.resource_request.same_plan_source_return_nos,
        )

    def acquire(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        serial_gate_leases: dict[str, Any] | None = None,
    ) -> ResourceDelta:
        violations: list[str] = []
        if validation.reasons:
            violations.extend(f"physical:{reason}" for reason in validation.reasons)
        if any(line in physical.RUNNING_LINES for line in request.touched_lines):
            violations.append("running_line_storage")
        violations.extend(
            self._serial_blocker_storage_violations(
                request,
                candidate=candidate,
                cars=cars,
                depot_assignment=depot_assignment,
                serial_gate_leases=serial_gate_leases or {},
            )
        )
        violations.extend(
            self._serial_gate_lease_violations(
                request,
                candidate=candidate,
                validation=validation,
                cars=cars,
                depot_assignment=depot_assignment,
                serial_gate_leases=serial_gate_leases or {},
            )
        )
        violations.extend(
            self._depot_slot_violations(
                request,
                candidate=candidate,
                validation=validation,
                cars=cars,
                depot_assignment=depot_assignment,
            )
        )
        if (
            "存4线" in request.put_lines
            and request.family not in {
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.SPECIAL_REPAIR_PROCESS,
            }
            and not self._cun4_put_is_same_plan_source_return(request, candidate)
        ):
            violations.append("cun4_buffer_requires_owner")
        if any(line in physical.DEPOT_OUTSIDE_LINES for line in request.put_lines) and request.family not in {
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
        }:
            violations.append("depot_outer_requires_depot_owner")
        released = (
            ()
            if request.same_plan_source_return_nos
            else (request.source_line,) if request.source_line != request.target_line else ()
        )
        return ResourceDelta(
            request=request,
            acquired=() if violations else request.resources,
            released_lines=released,
            violations=tuple(violations),
        )

    def _serial_blocker_storage_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        serial_gate_leases: dict[str, Any],
    ) -> list[str]:
        protected_blockers = {
            "存4线",
            "存3线",
            "修1库外",
            "修2库外",
            "修3库外",
            "修4库外",
        }
        move_nos = set(request.move_nos)
        put_nos_by_line = self._put_nos_by_line(candidate)
        violations: list[str] = []
        for put_line in request.put_lines:
            if put_line in protected_blockers or put_line == request.source_line:
                continue
            if self._depot_inbound_assembly_put_owned(
                request=request,
                put_line=put_line,
                put_nos=put_nos_by_line.get(put_line, ()),
                cars=cars,
                depot_assignment=depot_assignment,
            ):
                continue
            if self._same_plan_staging_cleared(
                candidate=candidate,
                line=put_line,
                put_nos=put_nos_by_line.get(put_line, ()),
            ):
                continue
            blocked = serial.downstream_lines(put_line)
            if not blocked:
                continue
            lease = serial_gate_leases.get(put_line)
            if lease and serial.lease_allows_put(lease, put_nos_by_line.get(put_line, ()), request.contract_id):
                continue
            downstream_debt = serial.downstream_debt_nos(
                blocker_line=put_line,
                cars=cars,
                depot_assignment=depot_assignment,
                moving_nos=move_nos,
            )
            if downstream_debt:
                violations.append(
                    "serial_blocker_storage_before_downstream_clear:"
                    f"{put_line}:{','.join(sorted(blocked))}:{','.join(sorted(downstream_debt)[:8])}"
                )
        return violations

    def _same_plan_staging_cleared(
        self,
        *,
        candidate: Any,
        line: str,
        put_nos: tuple[str, ...],
    ) -> bool:
        if not put_nos:
            return False
        if getattr(candidate, "candidate_kind", "") not in SAME_PLAN_STAGING_OWNER_KINDS:
            return False
        pending: set[str] = set()
        cleared: set[str] = set()
        for step in physical.candidate_plan_steps(candidate):
            if step.line != line:
                continue
            step_nos = set(step.move_car_nos)
            if step.action == "Put":
                pending.update(step_nos)
                cleared.difference_update(step_nos)
            elif step.action == "Get":
                cleared.update(step_nos & pending)
        return set(put_nos) <= cleared

    def _depot_inbound_assembly_put_owned(
        self,
        *,
        request: ResourceRequest,
        put_line: str,
        put_nos: tuple[str, ...],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> bool:
        if request.intent != IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return False
        if put_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return False
        if not put_nos:
            return False
        loads = physical.line_loads(cars)
        by_no = {physical.car_no(car): car for car in cars}
        for no in put_nos:
            car = by_no.get(no)
            if car is None:
                return False
            if car.get("IsWeigh"):
                return False
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                return False
        return True

    def _serial_gate_lease_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        serial_gate_leases: dict[str, Any],
    ) -> list[str]:
        leased_put_lines = [line for line in request.put_lines if line in serial_gate_leases]
        if not leased_put_lines:
            return []
        prospective = [dict(car) for car in cars]
        physical.apply_candidate(candidate, prospective, validation)
        put_nos_by_line = self._put_nos_by_line(candidate)
        violations: list[str] = []
        for blocker_line in leased_put_lines:
            lease = serial_gate_leases[blocker_line]
            after_debt = serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=prospective,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
            after_blockers = [
                physical.car_no(car)
                for car in prospective
                if car["Line"] == blocker_line
            ]
            pollution_nos = serial.lease_pollution_nos(lease, after_blockers)
            put_allowed = serial.lease_allows_put(lease, put_nos_by_line.get(blocker_line, ()), request.contract_id)
            if after_debt and after_blockers and (pollution_nos or not put_allowed):
                violations.append(
                    "serial_gate_lease_polluted_before_downstream_clear:"
                    f"{blocker_line}:{','.join((pollution_nos or sorted(after_blockers))[:8])}:"
                    f"{','.join(sorted(after_debt)[:8])}"
                )
        return violations

    def _put_nos_by_line(self, candidate: Any) -> dict[str, tuple[str, ...]]:
        by_line: dict[str, list[str]] = {}
        for step in physical.candidate_plan_steps(candidate):
            if step.action != "Put":
                continue
            by_line.setdefault(step.line, []).extend(step.move_car_nos)
        return {line: tuple(dict.fromkeys(nos)) for line, nos in by_line.items()}

    def _cun4_put_is_same_plan_source_return(self, request: ResourceRequest, candidate: Any) -> bool:
        if not request.same_plan_source_return_nos:
            return False
        cun4_put_nos = set(self._put_nos_by_line(candidate).get("存4线", ()))
        if not cun4_put_nos or not cun4_put_nos <= set(request.same_plan_source_return_nos):
            return False
        cun4_get_nos = {
            no
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Get" and step.line == "存4线"
            for no in step.move_car_nos
        }
        return cun4_put_nos <= cun4_get_nos

    def _depot_slot_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> list[str]:
        violations = physical.depot_resource_violations(
            request,
            candidate=candidate,
            validation=validation,
            cars=cars,
            depot_assignment=depot_assignment,
        )
        if getattr(candidate, "candidate_kind", "") != "vnext_depot_inbound_cun4_open_release":
            return violations
        allowed_nos = self._cun4_open_temporary_depot_put_nos(
            candidate=candidate,
            cars=cars,
            depot_assignment=depot_assignment,
        )
        if not allowed_nos:
            return violations
        return [
            violation
            for violation in violations
            if not self._cun4_open_violation_allowed(violation=violation, allowed_nos=allowed_nos)
        ]

    def _cun4_open_temporary_depot_put_nos(
        self,
        *,
        candidate: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> set[str]:
        origin_by_no: dict[str, str] = {}
        for step in physical.candidate_plan_steps(candidate):
            if step.action != "Get":
                continue
            for no in step.move_car_nos:
                origin_by_no[no] = step.line
        loads = physical.line_loads(cars)
        by_no = {physical.car_no(car): car for car in cars}
        allowed: set[str] = set()
        for no, origin_line in origin_by_no.items():
            if origin_line != "存4线":
                continue
            car = by_no.get(no)
            if car is None:
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
                allowed.add(no)
        return allowed

    def _cun4_open_violation_allowed(self, *, violation: str, allowed_nos: set[str]) -> bool:
        if not violation.startswith("depot_slot_unsatisfied_put:"):
            return False
        parts = violation.split(":", 2)
        return len(parts) >= 2 and parts[1] in allowed_nos
