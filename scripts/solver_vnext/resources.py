from __future__ import annotations

from typing import Any

from . import legacy_adapter as legacy
from . import serial
from .domain import ContractFamily
from .domain import CandidateEnvelope, ResourceDelta, ResourceKind, ResourceRequest


class StationResourceGraph:
    """Hard resource arbiter for vNext candidates.

    The first implementation is deliberately small: it does not score business
    value, it only accepts/rejects resource requests and records why.
    """

    def request_for(self, envelope: CandidateEnvelope) -> ResourceRequest:
        candidate = envelope.candidate
        steps = legacy.candidate_plan_steps(candidate)
        touched_lines = tuple(dict.fromkeys(step.line for step in steps if step.action in {"Get", "Put", "Weigh"}))
        put_lines = tuple(dict.fromkeys(step.line for step in steps if step.action == "Put"))
        resources = [ResourceKind.LOCO_POSITION, ResourceKind.LOCO_CARRY, ResourceKind.ROUTE_GET, ResourceKind.ROUTE_PUT]
        if any(line in legacy.DEPOT_LINES for line in put_lines):
            resources.append(ResourceKind.DEPOT_SLOT)
        if any(line in legacy.legacy.DEPOT_OUTSIDE_LINES for line in put_lines):
            resources.append(ResourceKind.DEPOT_SLOT)
        if "存4线" in touched_lines:
            resources.append(ResourceKind.CUN4_NORTH_BUFFER)
        if any(line in legacy.REMOTE_INTERACTION_LINES for line in touched_lines):
            resources.append(ResourceKind.REMOTE_SESSION)
            resources.append(ResourceKind.GLOBAL_GATE)
        if candidate.has_weigh:
            resources.append(ResourceKind.WEIGH_STAND)
        if any(line not in legacy.DEPOT_LINES for line in put_lines):
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
    ) -> ResourceDelta:
        violations: list[str] = []
        if validation.reasons:
            violations.extend(f"physical:{reason}" for reason in validation.reasons)
        if any(line in legacy.RUNNING_LINES for line in request.touched_lines):
            violations.append("running_line_storage")
        violations.extend(
            self._serial_blocker_storage_violations(
                request,
                candidate=candidate,
                cars=cars,
                depot_assignment=depot_assignment,
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
        }:
            violations.append("cun4_buffer_requires_owner")
        if any(line in legacy.legacy.DEPOT_OUTSIDE_LINES for line in request.put_lines) and request.family not in {
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

    def _serial_blocker_storage_violations(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
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
        violations: list[str] = []
        for put_line in request.put_lines:
            if put_line in protected_blockers or put_line == request.source_line:
                continue
            blocked = serial.downstream_lines(put_line)
            if not blocked:
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
