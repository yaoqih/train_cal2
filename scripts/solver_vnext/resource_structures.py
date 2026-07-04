from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import physical
from . import depot_inbound_plan
from . import depot_outbound_plan
from . import release
from . import serial
from .contracts import build_car_refs
from .domain import ContractFamily, SerialGateLease


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
    strategic_plan: Any,
    serial_gate_leases: dict[str, SerialGateLease] | None = None,
) -> list[ResourceStructureRecord]:
    serial_gate_leases = serial_gate_leases or {}
    return [
        _front_topology_plan_record(
            case_id=case_id,
            hook_index=hook_index,
            strategic_plan=strategic_plan,
        ),
        _cun4_record(case_id=case_id, hook_index=hook_index, cars=cars, depot_assignment=depot_assignment),
        _cun4_release_port_plan_record(
            case_id=case_id,
            hook_index=hook_index,
            strategic_plan=strategic_plan,
        ),
        _depot_slot_record(case_id=case_id, hook_index=hook_index, cars=cars, depot_assignment=depot_assignment),
        _depot_inbound_assembly_record(
            case_id=case_id,
            hook_index=hook_index,
            strategic_plan=strategic_plan,
        ),
        _depot_outbound_assembly_record(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            strategic_plan=strategic_plan,
        ),
        _remote_session_continuity_plan_record(
            case_id=case_id,
            hook_index=hook_index,
            strategic_plan=strategic_plan,
        ),
        _phase_completion_plan_record(
            case_id=case_id,
            hook_index=hook_index,
            strategic_plan=strategic_plan,
        ),
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


def _front_topology_plan_record(
    *,
    case_id: str,
    hook_index: int,
    strategic_plan: Any,
) -> ResourceStructureRecord:
    plan = strategic_plan.front_topology
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="FRONT_TOPOLOGY_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.reason,
        resource_key="|".join(plan.priority_lines),
        subject_nos="|".join(plan.priority_nos),
        violation="",
        detail=(
            f"priority_lines={','.join(plan.priority_lines)};"
            f"blocked_risk_lines={','.join(plan.blocked_risk_lines)};"
            f"must_finish_before_remote={plan.must_finish_before_remote};"
            f"clear_for_remote={plan.clear_for_remote};"
            f"reason={plan.reason}"
        ),
    )


def _cun4_release_port_plan_record(
    *,
    case_id: str,
    hook_index: int,
    strategic_plan: Any,
) -> ResourceStructureRecord:
    plan = strategic_plan.cun4_release
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="CUN4_RELEASE_PORT_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.owner,
        resource_key="存4线",
        subject_nos="|".join((*plan.release_nos, *plan.outbound_hold_nos, *plan.dirty_nos)),
        violation="cun4_release_port_dirty" if plan.status == "fail" else "",
        detail=(
            f"mode={plan.mode};owner={plan.owner};"
            f"release={','.join(plan.release_nos)};"
            f"outbound={','.join(plan.outbound_hold_nos)};"
            f"dirty={','.join(plan.dirty_nos)};"
            f"reason={plan.reason}"
        ),
    )


def _depot_outbound_assembly_record(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    strategic_plan: Any | None = None,
) -> ResourceStructureRecord:
    plan = (
        strategic_plan.depot_outbound
        if strategic_plan is not None
        else depot_outbound_plan.build_depot_outbound_assembly_plan(
            cars=cars,
            depot_assignment=depot_assignment,
        )
    )
    groups = "|".join(
        f"{group.line}:{','.join(group.vehicle_nos)}:{group.length_m:.1f}/{group.capacity_m:.1f}"
        for group in plan.groups
        if group.vehicle_nos
    )
    detail = ";".join(
        (
            f"reason={plan.reason}",
            f"sources={','.join(plan.source_lines)}",
            f"targets={','.join(plan.target_lines)}",
            f"route_blocker={','.join(plan.route_blocker_nos)}",
            f"non_cun4={','.join(plan.non_cun4_nos)}",
            f"cun4_target={','.join(plan.cun4_target_nos)}",
            f"pull_order={','.join(plan.pull_order_nos)}",
            f"cun4_prefix_unsafe={','.join(plan.cun4_prefix_unsafe_nos)}",
            f"cun4_outbound_hold={','.join(plan.cun4_reserved_by_outbound_hold_nos)}",
            f"cun4_budget_m={plan.cun4_budget_m:.1f}",
            f"cun4_free_m={plan.cun4_free_m:.1f}",
            f"pullout_required_m={plan.pullout_required_m:.1f}",
            f"depot_inner_free_after_pull_m={plan.depot_inner_free_after_pull_m:.1f}",
            f"depot_outer_free_after_pull_m={plan.depot_outer_free_after_pull_m:.1f}",
            f"remote_surplus_after_pull_m={plan.remote_surplus_after_pull_m:.1f}",
            f"pull_equivalent={plan.pull_equivalent}",
            f"groups={groups}",
            f"unplaced={','.join(plan.unplaced_nos)}",
        )
    )
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="DEPOT_OUTBOUND_ASSEMBLY_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.reason,
        resource_key="存4线|" + "|".join(depot_outbound_plan.OVERFLOW_ASSEMBLY_LINES),
        subject_nos="|".join(plan.cun4_nos),
        violation="overflow_assembly_capacity_insufficient" if plan.unplaced_nos else "",
        detail=detail,
    )


def _depot_inbound_assembly_record(
    *,
    case_id: str,
    hook_index: int,
    strategic_plan: Any,
) -> ResourceStructureRecord:
    plan = strategic_plan.depot_inbound
    groups = "|".join(
        f"{group.line}:{','.join(group.vehicle_nos)}:{group.length_m:.1f}/{group.capacity_m:.1f}:free={group.free_m:.1f}"
        for group in plan.groups
        if group.vehicle_nos
    )
    detail = ";".join(
        (
            f"reason={plan.reason}",
            f"sources={','.join(plan.source_lines)}",
            f"targets={','.join(plan.target_lines)}",
            f"grouped={','.join(plan.grouped_nos)}",
            f"ungrouped={','.join(plan.ungrouped_nos)}",
            f"unassigned={','.join(plan.unassigned_nos)}",
            f"purity_nos={','.join(plan.purity_violation_nos)}",
            f"purity_lines={','.join(plan.purity_violation_lines)}",
            f"purity_exempt={','.join(plan.purity_exempt_nos)}",
            f"total_length_m={plan.total_length_m:.1f}",
            f"pullout_required_m={plan.pullout_required_m:.1f}",
            f"depot_free_m={plan.depot_free_m:.1f}",
            f"depot_surplus_after_pull_m={plan.depot_surplus_after_pull_m:.1f}",
            f"cun4_budget_m={plan.cun4_budget_m:.1f}",
            f"cun4_vehicle_budget={plan.cun4_vehicle_budget}",
            f"assembly_capacity_m={plan.assembly_capacity_m:.1f}",
            f"assembly_free_m={plan.assembly_free_m:.1f}",
            f"groups={groups}",
        )
    )
    if plan.purity_violation_nos:
        violation = "inbound_assembly_line_purity_violation"
    elif plan.unassigned_nos:
        violation = "inbound_assembly_capacity_insufficient"
    else:
        violation = ""
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="DEPOT_INBOUND_ASSEMBLY_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.reason,
        resource_key="|".join(depot_inbound_plan.ASSEMBLY_LINES),
        subject_nos="|".join(plan.inbound_nos),
        violation=violation,
        detail=detail,
    )


def _remote_session_continuity_plan_record(
    *,
    case_id: str,
    hook_index: int,
    strategic_plan: Any,
) -> ResourceStructureRecord:
    plan = strategic_plan.remote_session
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="REMOTE_SESSION_CONTINUITY_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.reason,
        resource_key="remote_session",
        subject_nos="",
        violation="",
        detail=(
            f"should_continue_remote={plan.should_continue_remote};"
            f"remote_debt={plan.remote_debt};"
            f"depot_inbound_debt={plan.depot_inbound_debt};"
            f"depot_outbound_debt={plan.depot_outbound_debt};"
            f"preferred={','.join(plan.preferred_structures)};"
            f"reason={plan.reason}"
        ),
    )


def _phase_completion_plan_record(
    *,
    case_id: str,
    hook_index: int,
    strategic_plan: Any,
) -> ResourceStructureRecord:
    plan = strategic_plan.completion
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="PHASE_COMPLETION_PLAN",
        status=plan.status,
        owner_contract_id="",
        candidate_id="",
        mode=plan.reason,
        resource_key=strategic_plan.phase.value,
        subject_nos="",
        violation="",
        detail=(
            f"h1_can_exit={plan.h1_can_exit};"
            f"h4_can_close={plan.h4_can_close};"
            f"depot_inbound_complete={strategic_plan.depot_inbound.assembly_complete};"
            f"depot_outbound_complete={strategic_plan.depot_outbound.assembly_complete};"
            f"reason={plan.reason}"
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
    leased_serving: list[str] = []
    leased_polluted: list[str] = []
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
                    pollution_nos = serial.lease_pollution_nos(lease, blockers)
                    if pollution_nos:
                        leased_polluted.append(f"{blocker_line}:{lease.lease_id}:{','.join(pollution_nos)}")
                    else:
                        leased_serving.append(f"{blocker_line}:{lease.lease_id}:{','.join(blockers)}")
    if leased_polluted:
        mode = "leased_polluted"
    elif leased_serving:
        mode = "leased_serving"
    elif blocked:
        mode = "blocked_needs_lease"
    else:
        mode = "clear"
    return ResourceStructureRecord(
        case_id=case_id,
        hook_index=hook_index,
        structure="SERIAL_GATE_LEASE",
        status="fail" if leased_polluted else "pass",
        owner_contract_id="",
        candidate_id="",
        mode=mode,
        resource_key="serial_lines",
        subject_nos="",
        violation="serial_gate_lease_polluted_before_downstream_clear" if leased_polluted else "",
        detail="|".join(leased_polluted or leased_serving or blocked),
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
        pollution_nos = serial.lease_pollution_nos(lease, after_blockers)
        service_nos = serial.lease_service_nos(lease, after_blockers)
        if after_debt and pollution_nos:
            mode = "polluted_before_served"
            status = "fail"
            violation = "serial_gate_lease_polluted_before_downstream_clear"
        elif not after_debt:
            mode = "closed"
            status = "pass"
            violation = ""
        elif service_nos:
            mode = "serving"
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
                    f"after_blockers={','.join(after_blockers)};"
                    f"service={','.join(service_nos)};pollution={','.join(pollution_nos)}"
                ),
            )
        )

    return records


def _cun4_mode(cars: list[dict[str, Any]], depot_assignment: Any) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    state = release.cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    if state.release_nos or state.outbound_hold_nos or state.dirty_nos:
        return state.mode, tuple(sorted(state.release_nos)), tuple(sorted((*state.outbound_hold_nos, *state.dirty_nos)))

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
