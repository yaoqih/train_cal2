from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from . import release
from .contracts import build_car_refs
from .domain import ContractFamily


FULL_CHAIN_REPAIR = "FULL_CHAIN_REPAIR"
LATE_CUN4_REPAIR = "LATE_CUN4_REPAIR"
DIRECT_REPAIR_ENTRY = "DIRECT_REPAIR_ENTRY"
MIXED_SIGNAL_REPAIR = "MIXED_SIGNAL_REPAIR"
DEPOT_DIGEST_ONLY = "DEPOT_DIGEST_ONLY"
FRONT_OR_CLOSEOUT = "FRONT_OR_CLOSEOUT"


@dataclass(frozen=True)
class FlowEdgeRecord:
    case_id: str
    hook_index: int
    edge_key: str
    family: str
    status: str
    variant: str
    subject_count: int
    subject_nos: str
    source_lines: str
    target_lines: str
    obligations: str
    protections: str
    evidence: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class FlowFacts:
    active_variant: str
    repair_debt: int
    depot_outbound_debt: int
    cun4_port_debt: int
    remote_debt: int
    front_debt: int
    closeout_debt: int
    cun4_release_ready: bool
    cun4_port_mode: str
    cun4_release_count: int
    cun4_prefix_hold_count: int


FRONT_FAMILIES = {
    ContractFamily.FUNCTION_LINE_SERVICE,
    ContractFamily.DISPATCH_SHED_QUEUE,
    ContractFamily.PRE_REPAIR_STAGING,
    ContractFamily.YARD_REBALANCE,
    ContractFamily.LOCO_AREA_STAGING,
}


def classify_flow_facts(cars: list[dict[str, Any]], depot_assignment: Any) -> FlowFacts:
    refs = [ref for ref in build_car_refs(cars, depot_assignment) if not ref.satisfied]
    repair_debt = sum(
        ref.contract_family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT}
        for ref in refs
    )
    depot_outbound_debt = sum(ref.contract_family == ContractFamily.DEPOT_OUTBOUND for ref in refs)
    cun4_port_debt = sum(
        ref.contract_family == ContractFamily.CUN4_PORT_STAGING
        or (
            ref.contract_family != ContractFamily.DEPOT_OUTBOUND
            and (ref.target_line == "存4线" or ref.line == "存4线")
        )
        for ref in refs
    )
    remote_debt = sum(
        ref.is_remote_source
        or ref.is_remote_target
        or ref.contract_family
        in {
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
        }
        for ref in refs
    )
    front_debt = sum(ref.contract_family in FRONT_FAMILIES for ref in refs)
    closeout_debt = sum(
        ref.contract_family not in FRONT_FAMILIES
        and ref.contract_family
        not in {
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
            ContractFamily.SPECIAL_REPAIR_PROCESS,
        }
        for ref in refs
    )
    cun4_state = release.cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    return FlowFacts(
        active_variant=_repair_variant(
            refs=refs,
            repair_debt=repair_debt,
            depot_outbound_debt=depot_outbound_debt,
            cun4_port_debt=cun4_port_debt,
            remote_debt=remote_debt,
            front_debt=front_debt,
        ),
        repair_debt=repair_debt,
        depot_outbound_debt=depot_outbound_debt,
        cun4_port_debt=cun4_port_debt,
        remote_debt=remote_debt,
        front_debt=front_debt,
        closeout_debt=closeout_debt,
        cun4_release_ready=cun4_state.standard_ready,
        cun4_port_mode=cun4_state.mode,
        cun4_release_count=cun4_state.release_count,
        cun4_prefix_hold_count=len(cun4_state.prefix_hold_nos),
    )


def build_flow_edge_records(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> list[FlowEdgeRecord]:
    refs = [ref for ref in build_car_refs(cars, depot_assignment) if not ref.satisfied]
    facts = classify_flow_facts(cars, depot_assignment)
    grouped: dict[ContractFamily, list[Any]] = {}
    for ref in refs:
        grouped.setdefault(ref.contract_family, []).append(ref)

    records: list[FlowEdgeRecord] = []
    for family, items in sorted(grouped.items(), key=lambda item: item[0].value):
        ordered = sorted(items, key=lambda ref: (ref.line, ref.position, ref.no))
        subject_nos = tuple(ref.no for ref in ordered)
        source_lines = tuple(dict.fromkeys(ref.line for ref in ordered))
        target_lines = tuple(dict.fromkeys(ref.target_line for ref in ordered if ref.target_line))
        variant = facts.active_variant if family in {
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.CUN4_PORT_STAGING,
        } else FRONT_OR_CLOSEOUT
        records.append(
            FlowEdgeRecord(
                case_id=case_id,
                hook_index=hook_index,
                edge_key=f"{family.value}:{','.join(source_lines)}->{','.join(target_lines)}",
                family=family.value,
                status=_edge_status(family, ordered),
                variant=variant,
                subject_count=len(subject_nos),
                subject_nos="|".join(subject_nos),
                source_lines="|".join(source_lines),
                target_lines="|".join(target_lines),
                obligations="|".join(_obligations(family, variant)),
                protections="|".join(_protections(family, variant)),
                evidence="|".join(
                    f"{ref.no}:{ref.line}->{ref.target_line or '?'}:{ref.target_reason}"
                    for ref in ordered[:12]
                ),
                confidence=_confidence(variant),
                reason=_reason(family, variant),
            )
        )
    return records


def _repair_variant(
    *,
    refs: list[Any],
    repair_debt: int,
    depot_outbound_debt: int,
    cun4_port_debt: int,
    remote_debt: int,
    front_debt: int,
) -> str:
    if repair_debt == 0 and depot_outbound_debt == 0 and cun4_port_debt == 0:
        return FRONT_OR_CLOSEOUT
    if repair_debt and not cun4_port_debt and not depot_outbound_debt:
        return DEPOT_DIGEST_ONLY if remote_debt >= 10 else DIRECT_REPAIR_ENTRY
    if repair_debt and cun4_port_debt and remote_debt:
        has_south_storage_source = any(ref.line in {"存5线南", "存4南"} for ref in refs)
        if has_south_storage_source and front_debt <= int(remote_debt * 0.7):
            return MIXED_SIGNAL_REPAIR
        if any(ref.line == "存4线" for ref in refs):
            return LATE_CUN4_REPAIR
        return FULL_CHAIN_REPAIR
    if repair_debt:
        return DIRECT_REPAIR_ENTRY
    return FRONT_OR_CLOSEOUT


def _edge_status(family: ContractFamily, refs: list[Any]) -> str:
    if family == ContractFamily.CUN4_PORT_STAGING:
        return "FORMING"
    if family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT}:
        if any(ref.line in physical.DEPOT_LINES or ref.line in physical.DEPOT_OUTSIDE_LINES for ref in refs):
            return "DIGESTING"
        if any(ref.line == "存4线" or ref.target_line == "存4线" for ref in refs):
            return "PORT_READY"
        return "SEED"
    if family == ContractFamily.DEPOT_OUTBOUND:
        return "DIGESTING"
    if family in FRONT_FAMILIES:
        return "FORMING"
    return "SEED"


def _obligations(family: ContractFamily, variant: str) -> tuple[str, ...]:
    if family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT}:
        if variant == DEPOT_DIGEST_ONLY:
            return ("depot_digest_complete_before_closeout",)
        if variant == MIXED_SIGNAL_REPAIR:
            return ("conservative_repair_digest", "no_low_confidence_machine_accept")
        return ("form_release_group", "machine_accept_or_legal_skip", "depot_digest")
    if family == ContractFamily.DEPOT_OUTBOUND:
        return ("release_depot_slot", "preserve_cun4_direction")
    if family == ContractFamily.CUN4_PORT_STAGING:
        return ("shape_cun4_release_port",)
    if family == ContractFamily.SPECIAL_REPAIR_PROCESS:
        return ("special_process_hard_rule",)
    return ("move_to_target",)


def _protections(family: ContractFamily, variant: str) -> tuple[str, ...]:
    protections: list[str] = []
    if family in {
        ContractFamily.REPAIR_INBOUND,
        ContractFamily.DEPOT_SLOT,
        ContractFamily.DEPOT_OUTBOUND,
        ContractFamily.CUN4_PORT_STAGING,
    }:
        protections.append("cun4_north_buffer_owner")
    if variant in {FULL_CHAIN_REPAIR, LATE_CUN4_REPAIR}:
        protections.append("machine_accept_main_consist")
    if variant == DEPOT_DIGEST_ONLY:
        protections.append("no_tail_closeout_before_depot_digest")
    return tuple(protections)


def _confidence(variant: str) -> str:
    if variant in {FULL_CHAIN_REPAIR, DEPOT_DIGEST_ONLY}:
        return "high"
    if variant in {LATE_CUN4_REPAIR, DIRECT_REPAIR_ENTRY}:
        return "medium"
    if variant == MIXED_SIGNAL_REPAIR:
        return "low"
    return "none"


def _reason(family: ContractFamily, variant: str) -> str:
    if family in {
        ContractFamily.REPAIR_INBOUND,
        ContractFamily.DEPOT_SLOT,
        ContractFamily.DEPOT_OUTBOUND,
        ContractFamily.CUN4_PORT_STAGING,
    }:
        return f"manual_repair_structure_variant:{variant}"
    return f"classified_family:{family.value}"
