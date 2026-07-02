from __future__ import annotations

from typing import Any

from . import physical
from . import remote_prefix
from . import serial
from .domain import ContractFamily, IntentKind
from .domain import CandidateEnvelope, ResourceDelta, ResourceKind, ResourceRequest


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
        if envelope.intent == IntentKind.REMOTE_PREFIX_LEASE:
            resources.append(ResourceKind.REMOTE_PREFIX_GATE)
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
        remote_prefix_leases: dict[str, Any] | None = None,
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
        violations.extend(
            self._remote_prefix_lease_violations(
                request,
                candidate=candidate,
                validation=validation,
                cars=cars,
                depot_assignment=depot_assignment,
                remote_prefix_leases=remote_prefix_leases or {},
            )
        )
        if "存4线" in request.put_lines and request.family not in {
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.SPECIAL_REPAIR_PROCESS,
        }:
            violations.append("cun4_buffer_requires_owner")
        if any(line in physical.DEPOT_OUTSIDE_LINES for line in request.put_lines) and request.family not in {
            ContractFamily.REMOTE_SESSION,
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

    def _remote_prefix_lease_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        remote_prefix_leases: dict[str, Any],
    ) -> list[str]:
        if not remote_prefix_leases:
            return []
        prospective = [dict(car) for car in cars]
        physical.apply_candidate(candidate, prospective, validation)
        violations: list[str] = []
        for source_line, lease in sorted(remote_prefix_leases.items()):
            if source_line not in request.put_lines:
                continue
            if request.intent == IntentKind.REMOTE_PREFIX_LEASE:
                continue
            if not remote_prefix.remaining_debt_nos(lease, cars=prospective, depot_assignment=depot_assignment):
                continue
            refilled = remote_prefix.blockers_on_source(lease, prospective)
            if refilled:
                violations.append(
                    "remote_prefix_lease_refill_before_debt_clear:"
                    f"{source_line}:{','.join(refilled)}:{','.join(lease.debt_nos[:8])}"
                )
        return violations

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

    def _depot_slot_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> list[str]:
        return physical.depot_resource_violations(
            request,
            candidate=candidate,
            validation=validation,
            cars=cars,
            depot_assignment=depot_assignment,
        )
