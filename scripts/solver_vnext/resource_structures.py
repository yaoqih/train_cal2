from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import physical
from . import serial
from .contracts import build_car_refs
from .domain import ContractFamily, IntentKind, SerialGateLease


@dataclass(frozen=True)
class ResourceStructureRecord:
    case_id: str
    hook_index: int
    structure: str
    status: str
    owner_contract_id: str
    candidate_id: str
    mode: str
    resource_key: str
    subject_nos: str
    violation: str
    detail: str


def hook_resource_records(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    serial_gate_leases: dict[str, SerialGateLease] | None = None,
) -> list[ResourceStructureRecord]:
    serial_gate_leases = serial_gate_leases or {}
    return [
        _cun4_record(case_id=case_id, hook_index=hook_index, cars=cars, depot_assignment=depot_assignment),
        _depot_slot_record(case_id=case_id, hook_index=hook_index, cars=cars, depot_assignment=depot_assignment),
        _serial_gate_record(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases,
        ),
    ]


def selected_resource_records(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
    envelope: Any,
    resource_delta: Any,
    contract_delta: Any,
    serial_gate_leases: dict[str, SerialGateLease],
) -> list[ResourceStructureRecord]:
    records = [
        _loco_carry_record(
            case_id=case_id,
            hook_index=hook_index,
            envelope=envelope,
            resource_delta=resource_delta,
        ),
        _serial_gate_lease_record(
            case_id=case_id,
            hook_index=hook_index,
            envelope=envelope,
            contract_delta=contract_delta,
        ),
        _selected_cun4_record(
            case_id=case_id,
            hook_index=hook_index,
            envelope=envelope,
            resource_delta=resource_delta,
        ),
        _selected_depot_slot_record(
            case_id=case_id,
            hook_index=hook_index,
            envelope=envelope,
            resource_delta=resource_delta,
        ),
        _selected_depot_swap_record(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            prospective_cars=prospective_cars,
            depot_assignment=depot_assignment,
            envelope=envelope,
            resource_delta=resource_delta,
        ),
    ]
    records.extend(
        _serial_gate_lifecycle_records(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            prospective_cars=prospective_cars,
            depot_assignment=depot_assignment,
            envelope=envelope,
            contract_delta=contract_delta,
            serial_gate_leases=serial_gate_leases,
        )
    )
    return records


def next_serial_gate_leases(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
    envelope: Any,
    contract_delta: Any,
    serial_gate_leases: dict[str, SerialGateLease],
) -> dict[str, SerialGateLease]:
    next_leases = dict(serial_gate_leases)
    for blocker_line in list(next_leases):
        after_debt = serial.downstream_debt_nos(
            blocker_line=blocker_line,
            cars=prospective_cars,
            depot_assignment=depot_assignment,
            moving_nos=set(),
        )
        if not after_debt:
            del next_leases[blocker_line]

    if envelope.intent == IntentKind.SERIAL_GATE_CLEAR and "serial_gate_lease_opened" in contract_delta.fulfilled:
        blocker_line = envelope.candidate.source_line
        debt_nos = serial.downstream_debt_nos(
            blocker_line=blocker_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos=set(envelope.candidate.move_car_nos),
        )
        if debt_nos:
            next_leases[blocker_line] = SerialGateLease(
                lease_id=f"{case_id}:{blocker_line}:{hook_index}",
                owner_contract_id=envelope.contract.contract_id,
                blocker_line=blocker_line,
                opened_hook=hook_index,
                blocker_nos=tuple(envelope.candidate.move_car_nos),
                debt_nos=tuple(sorted(debt_nos)),
            )
    return next_leases


def _cun4_record(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> ResourceStructureRecord:
    mode, inbound_nos, outbound_nos = _cun4_mode(cars, depot_assignment)
    violation = "cun4_mixed_dirty" if mode == "MIXED_DIRTY" else ""
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="CUN4_NORTH_BUFFER",
        status="fail" if violation else "pass",
        owner_contract_id="",
        candidate_id="",
        mode=mode,
        resource_key="存4线",
        subject_nos="|".join((*inbound_nos, *outbound_nos)),
        violation=violation,
        detail=f"inbound={','.join(inbound_nos)};outbound={','.join(outbound_nos)}",
    )


def _depot_slot_record(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> ResourceStructureRecord:
    by_no = {physical.car_no(car): car for car in cars}
    occupied_stay = 0
    occupied_outbound = 0
    reserved_missing = 0
    locked_stayers = 0
    for no, slot in depot_assignment.slots.items():
        car = by_no.get(no)
        if not car:
            continue
        if getattr(slot, "locked", False):
            locked_stayers += 1
        if car["Line"] == slot.line and int(car.get("Position") or 0) == int(slot.position):
            target_line, target_position, _reason = physical.planned_target_for_car(car, cars, depot_assignment)
            if target_line == slot.line and (target_position in {None, int(slot.position)}):
                occupied_stay += 1
            else:
                occupied_outbound += 1
        elif car["Line"] not in physical.DEPOT_TARGET_LINES:
            reserved_missing += 1
    capacities = ",".join(
        f"{line}:{physical.depot_line_capacity(depot_assignment, line)}"
        for line in sorted(physical.DEPOT_LINES)
    )
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="DEPOT_SLOT_GRAPH",
        status="pass",
        owner_contract_id="",
        candidate_id="",
        mode="slot_snapshot",
        resource_key="修1-修4",
        subject_nos="",
        violation="",
        detail=(
            f"slots={len(depot_assignment.slots)};"
            f"stay={occupied_stay};outbound={occupied_outbound};"
            f"reserved_missing={reserved_missing};locked={locked_stayers};"
            f"failures={len(getattr(depot_assignment, 'failures', {}) or {})};"
            f"capacities={capacities}"
        ),
    )


def _serial_gate_record(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    serial_gate_leases: dict[str, SerialGateLease],
) -> ResourceStructureRecord:
    blocked: list[str] = []
    leased_refilled: list[str] = []
    for blocker_line in sorted(serial.serial_blocker_lines()):
        debt = serial.downstream_debt_nos(
            blocker_line=blocker_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos=set(),
        )
        if debt:
            blockers = sorted(
                physical.car_no(car)
                for car in cars
                if car["Line"] == blocker_line
            )
            if blockers:
                blocked.append(f"{blocker_line}:{','.join(blockers)}->{','.join(sorted(debt)[:8])}")
                lease = serial_gate_leases.get(blocker_line)
                if lease:
                    leased_refilled.append(f"{blocker_line}:{lease.lease_id}:{','.join(blockers)}")
    if leased_refilled:
        mode = "leased_refilled"
    elif blocked:
        mode = "blocked_needs_lease"
    else:
        mode = "clear"
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="SERIAL_GATE_LEASE",
        status="fail" if leased_refilled else "pass",
        owner_contract_id="",
        candidate_id="",
        mode=mode,
        resource_key="serial_lines",
        subject_nos="",
        violation="serial_gate_lease_refilled_before_downstream_clear" if leased_refilled else "",
        detail="|".join(leased_refilled or blocked),
    )


def _loco_carry_record(
    *,
    case_id: str,
    hook_index: int,
    envelope: Any,
    resource_delta: Any,
) -> ResourceStructureRecord:
    steps = physical.candidate_plan_steps(envelope.candidate)
    carry: list[str] = []
    segments: list[str] = []
    dirty = False
    no_family = {no: envelope.contract.family.value for no in envelope.contract.subject_nos}
    for step in steps:
        step_nos = tuple(getattr(step, "move_car_nos", ()) or ())
        if step.action == "Get":
            carry.extend(no for no in step_nos if no not in carry)
        elif step.action == "Put":
            if step_nos and carry[-len(step_nos):] != list(step_nos):
                dirty = True
            for no in step_nos:
                if no in carry:
                    carry.remove(no)
        if step_nos:
            families = Counter(no_family.get(no, "support") for no in step_nos)
            segments.append(f"{step.action}:{step.line}:{','.join(step_nos)}:{','.join(sorted(families))}")
    violation = "dirty_carry_order" if dirty else ""
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="LOCO_CARRY_STATE",
        status="fail" if violation else "pass",
        owner_contract_id=envelope.contract.contract_id,
        candidate_id=envelope.candidate.candidate_id,
        mode="DIRTY_CARRY" if dirty else "ORDERED_SEGMENTS",
        resource_key="LOCO_CARRY",
        subject_nos="|".join(resource_delta.request.move_nos),
        violation=violation,
        detail="|".join(segments),
    )


def _serial_gate_lease_record(
    *,
    case_id: str,
    hook_index: int,
    envelope: Any,
    contract_delta: Any,
) -> ResourceStructureRecord:
    opened = envelope.intent == IntentKind.SERIAL_GATE_CLEAR
    status = "pass" if (not opened or "serial_gate_lease_opened" in contract_delta.fulfilled) else "fail"
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="SERIAL_GATE_LEASE_DELTA",
        status=status,
        owner_contract_id=envelope.contract.contract_id,
        candidate_id=envelope.candidate.candidate_id,
        mode="opened" if opened else "not_applicable",
        resource_key=envelope.candidate.source_line,
        subject_nos="|".join(envelope.candidate.move_car_nos),
        violation="" if status == "pass" else "serial_gate_clear_without_lease_delta",
        detail="|".join((*contract_delta.fulfilled, *contract_delta.reduced)),
    )


def _selected_cun4_record(
    *,
    case_id: str,
    hook_index: int,
    envelope: Any,
    resource_delta: Any,
) -> ResourceStructureRecord:
    touches = "存4线" in resource_delta.request.touched_lines
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="CUN4_NORTH_BUFFER_DELTA",
        status="pass",
        owner_contract_id=envelope.contract.contract_id,
        candidate_id=envelope.candidate.candidate_id,
        mode="requested" if touches else "not_applicable",
        resource_key="存4线",
        subject_nos="|".join(resource_delta.request.move_nos),
        violation="",
        detail=f"resources={','.join(resource.value for resource in resource_delta.request.resources)}",
    )


def _selected_depot_slot_record(
    *,
    case_id: str,
    hook_index: int,
    envelope: Any,
    resource_delta: Any,
) -> ResourceStructureRecord:
    requests_slot = any(line in physical.DEPOT_TARGET_LINES for line in resource_delta.request.put_lines)
    has_resource = any(resource.value == "DEPOT_SLOT" for resource in resource_delta.request.resources)
    status = "pass" if not requests_slot or has_resource else "fail"
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="DEPOT_SLOT_DELTA",
        status=status,
        owner_contract_id=envelope.contract.contract_id,
        candidate_id=envelope.candidate.candidate_id,
        mode="requested" if requests_slot else "not_applicable",
        resource_key="|".join(resource_delta.request.put_lines),
        subject_nos="|".join(resource_delta.request.move_nos),
        violation="" if status == "pass" else "depot_slot_put_without_resource",
        detail=f"resources={','.join(resource.value for resource in resource_delta.request.resources)}",
    )


def _selected_depot_swap_record(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
    envelope: Any,
    resource_delta: Any,
) -> ResourceStructureRecord:
    steps = physical.candidate_plan_steps(envelope.candidate)
    depot_get = any(step.action == "Get" and step.line in physical.DEPOT_TARGET_LINES for step in steps)
    depot_put_lines = tuple(dict.fromkeys(step.line for step in steps if step.action == "Put" and step.line in physical.DEPOT_TARGET_LINES))
    touches_depot = depot_get or bool(depot_put_lines)
    if not touches_depot:
        return ResourceStructureRecord(
            case_id=case_id,
            hook_index=hook_index,
            structure="DEPOT_SWAP_DELTA",
            status="pass",
            owner_contract_id=envelope.contract.contract_id,
            candidate_id=envelope.candidate.candidate_id,
            mode="not_applicable",
            resource_key="",
            subject_nos="|".join(resource_delta.request.move_nos),
            violation="",
            detail="",
        )

    after_by_no = {physical.car_no(car): car for car in prospective_cars}
    violations: list[str] = []
    satisfied: list[str] = []
    for no in resource_delta.request.move_nos:
        after = after_by_no.get(no)
        if not after or after["Line"] not in physical.DEPOT_TARGET_LINES:
            continue
        target_line, target_position, reason = physical.planned_target_for_car(after, prospective_cars, depot_assignment)
        if target_line not in physical.DEPOT_TARGET_LINES:
            violations.append(f"non_depot_vehicle_put_to_depot:{no}:{after['Line']}")
            continue
        if not physical.car_is_satisfied(after, depot_assignment, prospective_cars):
            actual = f"{after['Line']}#{int(after.get('Position') or 0)}"
            expected = f"{target_line}#{target_position or ''}"
            violations.append(f"depot_vehicle_not_satisfied:{no}:{actual}->{expected}:{reason}")
            continue
        satisfied.append(f"{no}:{after['Line']}#{int(after.get('Position') or 0)}")

    depot_graph = physical.DepotSlotGraph(depot_assignment)
    before_collisions = depot_graph.locked_slot_collisions(cars)
    after_collisions = depot_graph.locked_slot_collisions(prospective_cars)
    for collision in sorted(after_collisions - before_collisions):
        violations.append(f"locked_slot_collision:{collision}")

    if violations:
        mode = "SWAP_VIOLATION"
    elif depot_put_lines:
        mode = "SLOT_DIGESTED"
    else:
        mode = "SWAP_RELEASED"
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="DEPOT_SWAP_DELTA",
        status="fail" if violations else "pass",
        owner_contract_id=envelope.contract.contract_id,
        candidate_id=envelope.candidate.candidate_id,
        mode=mode,
        resource_key="|".join(depot_put_lines),
        subject_nos="|".join(resource_delta.request.move_nos),
        violation="|".join(violations),
        detail=";".join(
            (
                f"resources={','.join(resource.value for resource in resource_delta.request.resources)}",
                f"satisfied={','.join(satisfied)}",
            )
        ),
    )

def _serial_gate_lifecycle_records(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
    envelope: Any,
    contract_delta: Any,
    serial_gate_leases: dict[str, SerialGateLease],
) -> list[ResourceStructureRecord]:
    records: list[ResourceStructureRecord] = []
    for blocker_line, lease in sorted(serial_gate_leases.items()):
        before_debt = set(
            serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
        )
        after_debt = set(
            serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=prospective_cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
        )
        after_blockers = sorted(
            physical.car_no(car)
            for car in prospective_cars
            if car["Line"] == blocker_line
        )
        if after_blockers and after_debt:
            mode = "refilled_before_served"
            status = "fail"
            violation = "serial_gate_lease_refilled_before_downstream_clear"
        elif not after_debt:
            mode = "closed"
            status = "pass"
            violation = ""
        elif len(after_debt) < len(before_debt):
            mode = "serving"
            status = "pass"
            violation = ""
        else:
            mode = "holding"
            status = "pass"
            violation = ""
        records.append(
            ResourceStructureRecord(
                case_id=case_id,
                hook_index=hook_index,
                structure="SERIAL_GATE_LEASE_LIFECYCLE",
                status=status,
                owner_contract_id=lease.owner_contract_id,
                candidate_id=envelope.candidate.candidate_id,
                mode=mode,
                resource_key=blocker_line,
                subject_nos="|".join(lease.blocker_nos),
                violation=violation,
                detail=(
                    f"lease_id={lease.lease_id};age={hook_index - lease.opened_hook};"
                    f"before_debt={len(before_debt)};after_debt={len(after_debt)};"
                    f"after_blockers={','.join(after_blockers)}"
                ),
            )
        )

    if envelope.intent == IntentKind.SERIAL_GATE_CLEAR:
        blocker_line = envelope.candidate.source_line
        after_debt = set(
            serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=prospective_cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
        )
        after_blockers = sorted(
            physical.car_no(car)
            for car in prospective_cars
            if car["Line"] == blocker_line
        )
        opened = "serial_gate_lease_opened" in contract_delta.fulfilled
        status = "pass" if opened and not (after_blockers and after_debt) else "fail"
        records.append(
            ResourceStructureRecord(
                case_id=case_id,
                hook_index=hook_index,
                structure="SERIAL_GATE_LEASE_LIFECYCLE",
                status=status,
                owner_contract_id=envelope.contract.contract_id,
                candidate_id=envelope.candidate.candidate_id,
                mode="opened",
                resource_key=blocker_line,
                subject_nos="|".join(envelope.candidate.move_car_nos),
                violation="" if status == "pass" else "serial_gate_clear_without_clean_open",
                detail=(
                    f"opened={opened};after_debt={len(after_debt)};"
                    f"after_blockers={','.join(after_blockers)}"
                ),
            )
        )
    return records


def _cun4_mode(cars: list[dict[str, Any]], depot_assignment: Any) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    inbound: list[str] = []
    outbound: list[str] = []
    loads = physical.line_loads(cars)
    for car in cars:
        if car["Line"] != "存4线":
            continue
        target_line, _target_position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
        no = physical.car_no(car)
        if target_line in physical.DEPOT_TARGET_LINES or target_line in physical.DEPOT_LINES:
            inbound.append(no)
        elif target_line and target_line != "存4线":
            outbound.append(no)
    if inbound and outbound:
        return "MIXED_DIRTY", tuple(sorted(inbound)), tuple(sorted(outbound))
    if inbound:
        return "INBOUND_RELEASE", tuple(sorted(inbound)), ()
    if outbound:
        return "OUTBOUND_HOLD", (), tuple(sorted(outbound))

    refs = [ref for ref in build_car_refs(cars, depot_assignment) if not ref.satisfied]
    inbound_wait = any(ref.contract_family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT} for ref in refs)
    outbound_wait = any(ref.target_line == "存4线" for ref in refs)
    if inbound_wait and outbound_wait:
        return "FREE_CONTESTED", (), ()
    if inbound_wait:
        return "FREE_INBOUND_WAITING", (), ()
    if outbound_wait:
        return "FREE_OUTBOUND_WAITING", (), ()
    return "FREE", (), ()
