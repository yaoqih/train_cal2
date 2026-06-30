from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import legacy_adapter as legacy
from .domain import CarRef, ContractFamily, FlowContract


FAMILY_PRIORITY = {
    ContractFamily.REMOTE_SESSION: 5,
    ContractFamily.SPECIAL_REPAIR_PROCESS: 10,
    ContractFamily.FUNCTION_LINE_SERVICE: 20,
    ContractFamily.DISPATCH_SHED_QUEUE: 30,
    ContractFamily.PRE_REPAIR_STAGING: 40,
    ContractFamily.YARD_REBALANCE: 50,
    ContractFamily.CUN4_PORT_STAGING: 60,
    ContractFamily.DEPOT_OUTBOUND: 70,
    ContractFamily.REPAIR_INBOUND: 80,
    ContractFamily.DEPOT_SLOT: 85,
    ContractFamily.LOCO_AREA_STAGING: 90,
    ContractFamily.TAIL_CLOSEOUT: 100,
    ContractFamily.RESIDUAL: 900,
}


def classify_family(source_line: str, target_line: str, is_weigh: bool) -> ContractFamily:
    if is_weigh:
        return ContractFamily.SPECIAL_REPAIR_PROCESS
    if source_line in legacy.DEPOT_LINES and target_line not in legacy.DEPOT_TARGET_LINES:
        return ContractFamily.DEPOT_OUTBOUND
    if target_line == "存4线":
        return ContractFamily.CUN4_PORT_STAGING
    if target_line in legacy.DEPOT_LINES:
        return ContractFamily.REPAIR_INBOUND
    if target_line in legacy.legacy.DEPOT_OUTSIDE_LINES:
        return ContractFamily.DEPOT_SLOT
    if target_line == "预修线":
        return ContractFamily.PRE_REPAIR_STAGING
    if target_line in {"调梁棚", "调梁线北"}:
        return ContractFamily.DISPATCH_SHED_QUEUE
    if target_line in {"洗罐站", "洗罐线北", "油漆线", "抛丸线", "卸轮线"}:
        return ContractFamily.FUNCTION_LINE_SERVICE
    if target_line in {"机走棚", "机库线", "机走北"}:
        return ContractFamily.LOCO_AREA_STAGING
    if target_line:
        return ContractFamily.YARD_REBALANCE
    return ContractFamily.RESIDUAL


def build_car_refs(cars: list[dict[str, Any]], depot_assignment: Any) -> list[CarRef]:
    loads = legacy.line_loads(cars)
    refs: list[CarRef] = []
    for car in cars:
        target_line, target_position, reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
        satisfied = legacy.car_is_satisfied(car, depot_assignment, cars)
        family = classify_family(car["Line"], target_line, bool(car.get("IsWeigh")))
        refs.append(
            CarRef(
                no=legacy.car_no(car),
                line=car["Line"],
                position=int(car.get("Position") or 0),
                target_line=target_line,
                target_position=target_position,
                target_reason=reason,
                contract_family=family,
                satisfied=satisfied,
                is_remote_source=car["Line"] in legacy.REMOTE_INTERACTION_LINES,
                is_remote_target=target_line in legacy.REMOTE_INTERACTION_LINES,
                is_weigh=bool(car.get("IsWeigh")),
                is_closed_door=bool(car.get("IsClosedDoor")),
                length_m=legacy.car_length(car),
                force_positions=legacy.force_positions(car),
            )
        )
    return refs


def build_contracts(cars: list[dict[str, Any]], depot_assignment: Any) -> list[FlowContract]:
    refs = [ref for ref in build_car_refs(cars, depot_assignment) if not ref.satisfied]
    grouped: dict[tuple[ContractFamily, str, str], list[CarRef]] = defaultdict(list)
    for ref in refs:
        grouped[(ref.contract_family, ref.line, ref.target_line)].append(ref)

    contracts: list[FlowContract] = []
    remote_refs = [
        ref
        for ref in refs
        if ref.is_remote_source or ref.is_remote_target or ref.contract_family in {
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
        }
    ]
    if remote_refs:
        ordered_remote_refs = sorted(remote_refs, key=lambda ref: (ref.line, ref.position, ref.target_line, ref.no))
        source_lines = tuple(dict.fromkeys(ref.line for ref in ordered_remote_refs))
        target_lines = tuple(dict.fromkeys(ref.target_line for ref in ordered_remote_refs if ref.target_line))
        contracts.append(
            FlowContract(
                contract_id="REMOTE_SESSION:" + ",".join(ref.no for ref in ordered_remote_refs),
                family=ContractFamily.REMOTE_SESSION,
                subject_nos=tuple(ref.no for ref in ordered_remote_refs),
                source_lines=source_lines,
                target_lines=target_lines,
                priority=FAMILY_PRIORITY[ContractFamily.REMOTE_SESSION],
                obligations=("remote_session_debt", "preserve_remote_session", "move_to_target"),
                protections=("remote_session_continuity", "cun4_port_owner", "depot_outer_inner_order"),
                reason="aggregate_remote_session_debt",
            )
        )
    for (family, source_line, target_line), items in grouped.items():
        items = sorted(items, key=lambda ref: (ref.position, ref.no))
        obligations = ["move_to_target"]
        protections: list[str] = []
        blockers: list[str] = []
        if family in {
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
        }:
            obligations.append("remote_depot_debt")
            protections.append("preserve_remote_session")
        if family == ContractFamily.SPECIAL_REPAIR_PROCESS:
            obligations.append("weigh_tail_only")
        if any(ref.force_positions for ref in items):
            obligations.append("force_position_window")
        if any(ref.is_closed_door for ref in items):
            protections.append("closed_door_order")
        contract_id = f"{family.value}:{source_line}->{target_line}:{','.join(ref.no for ref in items)}"
        contracts.append(
            FlowContract(
                contract_id=contract_id,
                family=family,
                subject_nos=tuple(ref.no for ref in items),
                source_lines=(source_line,),
                target_lines=(target_line,),
                priority=FAMILY_PRIORITY.get(family, 999),
                obligations=tuple(obligations),
                protections=tuple(protections),
                blockers=tuple(blockers),
                reason=f"classified_by_target:{target_line}",
            )
        )
    return sorted(contracts, key=lambda item: (item.priority, item.source_lines, item.target_lines, item.contract_id))


def contract_debt(contract: FlowContract, cars: list[dict[str, Any]], depot_assignment: Any) -> int:
    by_no = {legacy.car_no(car): car for car in cars}
    return sum(
        1
        for no in contract.subject_nos
        if no in by_no and not legacy.car_is_satisfied(by_no[no], depot_assignment, cars)
    )
