from __future__ import annotations

from typing import Any

from . import legacy_adapter as legacy
from . import serial
from .domain import BorrowedBlockerDebt, ContractFamily
from .domain import CandidateEnvelope, IntentKind, ResourceDelta, ResourceKind, ResourceRequest


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
        target_line = (
            envelope.resource_request.target_line
            if envelope.intent == IntentKind.SOURCE_CLEAR_RESTORE
            else candidate.target_line
        )
        return ResourceRequest(
            contract_id=envelope.contract.contract_id,
            family=envelope.contract.family,
            candidate_id=candidate.candidate_id,
            resources=tuple(dict.fromkeys(resources)),
            source_line=candidate.source_line,
            target_line=target_line,
            move_nos=tuple(candidate.move_car_nos),
            touched_lines=touched_lines,
            put_lines=put_lines,
            intent=envelope.intent,
            borrowed_blockers=envelope.resource_request.borrowed_blockers,
            restored_borrowed_blockers=envelope.resource_request.restored_borrowed_blockers,
        )

    def acquire(
        self,
        request: ResourceRequest,
        *,
        candidate: Any,
        validation: Any,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        borrowed_debts: dict[tuple[str, ...], BorrowedBlockerDebt] | None = None,
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
        if request.intent == IntentKind.BLOCKER_CLEAR:
            moved = {legacy.car_no(car): car for car in cars if legacy.car_no(car) in set(request.move_nos)}
            loads = legacy.line_loads(cars)
            for no, car in moved.items():
                target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
                if target_line and target_line != request.target_line:
                    violations.append(f"blocker_has_own_target:{no}:{target_line}")
        if request.intent in {IntentKind.BORROWED_BLOCKER_CLEAR, IntentKind.DEPOT_OUTER_CLEAR}:
            debt_key = tuple(sorted(request.borrowed_blockers or request.move_nos))
            if borrowed_debts and debt_key in borrowed_debts:
                violations.append("borrowed_blocker_debt_already_open:" + ",".join(debt_key))
        released = (
            ()
            if request.intent == IntentKind.SOURCE_CLEAR_RESTORE
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
