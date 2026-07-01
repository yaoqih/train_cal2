from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from . import plan_facts
from .domain import ContractDelta, ContractFamily, IntentKind, PhaseKind, PhaseState, ResourceDelta


PHASE_CODE = {
    PhaseKind.H1_FRONT_SERVICE: "H1",
    PhaseKind.H2_CUN4_PORT: "H2",
    PhaseKind.H3_RELEASE_ACCEPT: "H3",
    PhaseKind.H4_REMOTE_DEPOT: "H4",
    PhaseKind.H5_CLOSEOUT: "H5",
}

PHASE_RANK = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5}


@dataclass(frozen=True)
class PhasePermission:
    allowed: bool
    relation: str
    target_phase: str
    reason: str


@dataclass(frozen=True)
class PhaseGateRecord:
    case_id: str
    step_index: int
    from_phase: str
    to_phase: str
    transition_type: str
    active_variant: str
    predicate_values: str
    consumed_contract_ids: str
    created_contract_ids: str
    carried_obligation_ids: str
    blocked_contract_ids: str
    evidence_ids: str
    hook_count_in_phase: int
    manual_phase_hook_count: str
    reject_reason: str


class HumanPhaseGate:
    """Human-derived phase gate.

    This module is deliberately passive: it classifies phase state, records
    transitions, and reports whether an accepted action is primary/support/veto
    for the current phase.  Candidate generation and ranking stay outside it.
    """

    PRIMARY_FAMILIES = {
        "H1": {
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.YARD_REBALANCE,
            ContractFamily.LOCO_AREA_STAGING,
        },
        "H2": {ContractFamily.CUN4_PORT_STAGING, ContractFamily.YARD_REBALANCE},
        "H3": {ContractFamily.REMOTE_SESSION, ContractFamily.DEPOT_OUTBOUND, ContractFamily.REPAIR_INBOUND},
        "H4": {
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.SPECIAL_REPAIR_PROCESS,
        },
        "H5": {
            ContractFamily.TAIL_CLOSEOUT,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.YARD_REBALANCE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.DISPATCH_SHED_QUEUE,
        },
    }

    SUPPORT_FAMILIES = {
        "H1": {ContractFamily.CUN4_PORT_STAGING, ContractFamily.REMOTE_SESSION},
        "H2": {
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_OUTBOUND,
        },
        "H3": {ContractFamily.CUN4_PORT_STAGING, ContractFamily.DEPOT_SLOT},
        "H4": {ContractFamily.CUN4_PORT_STAGING, ContractFamily.LOCO_AREA_STAGING},
        "H5": {ContractFamily.SPECIAL_REPAIR_PROCESS},
    }

    def classify_state(
        self,
        *,
        front_debt: int,
        cun4_port_debt: int,
        remote_debt: int,
        closeout_debt: int,
        remote_session_open: bool,
    ) -> PhaseState:
        if remote_session_open and remote_debt:
            phase = PhaseKind.H4_REMOTE_DEPOT
            reason = "remote_session_continuation"
        elif front_debt > 6 and front_debt > int(remote_debt * 0.6):
            phase = PhaseKind.H1_FRONT_SERVICE
            reason = "front_service_debt_open"
        elif cun4_port_debt and remote_debt:
            phase = PhaseKind.H2_CUN4_PORT
            reason = "cun4_port_before_remote_session"
        elif remote_debt:
            phase = PhaseKind.H4_REMOTE_DEPOT
            reason = "remote_session_debt_open"
        else:
            phase = PhaseKind.H5_CLOSEOUT
            reason = "primary_debt_closed"
        return PhaseState(
            phase=phase,
            front_debt=front_debt,
            cun4_port_debt=cun4_port_debt,
            remote_debt=remote_debt,
            closeout_debt=closeout_debt,
            reason=reason,
        )

    def phase_code(self, phase: PhaseKind | str) -> str:
        if isinstance(phase, PhaseKind):
            return PHASE_CODE[phase]
        text = str(phase)
        for code in PHASE_RANK:
            if code in text:
                return code
        return text

    def phase_kind(self, phase_code: str) -> PhaseKind:
        for kind, code in PHASE_CODE.items():
            if code == phase_code:
                return kind
        return PhaseKind.H5_CLOSEOUT

    def active_phase(self, *, previous_phase: str, proposed: PhaseState) -> str:
        proposed_code = self.phase_code(proposed.phase)
        if not previous_phase:
            return proposed_code
        previous_rank = PHASE_RANK.get(previous_phase, 0)
        proposed_rank = PHASE_RANK.get(proposed_code, 0)
        if previous_phase == "H4" and proposed.remote_debt > 0:
            return "H4"
        if previous_phase in {"H3", "H4"} and proposed.remote_debt == 0 and proposed_rank < PHASE_RANK["H5"]:
            return "H5"
        if previous_phase == "H2" and proposed.cun4_port_debt > 0 and proposed.remote_debt > 0:
            return "H2"
        if proposed_rank and proposed_rank < previous_rank:
            return previous_phase
        return proposed_code

    def permission(
        self,
        *,
        phase_state: PhaseState,
        envelope: Any,
        contract_delta: ContractDelta,
        resource_delta: ResourceDelta,
        remote_session_open: bool,
    ) -> PhasePermission:
        phase = self.phase_code(phase_state.phase)
        target_phase = self.target_phase(
            envelope=envelope,
            resource_delta=resource_delta,
            remote_session_open=remote_session_open,
        )
        family = envelope.contract.family
        if family in self.PRIMARY_FAMILIES.get(phase, set()):
            return PhasePermission(True, "primary", target_phase, "primary_family_allowed")
        if self._is_structural_support(
            phase=phase,
            target_phase=target_phase,
            envelope=envelope,
            contract_delta=contract_delta,
            resource_delta=resource_delta,
        ):
            return PhasePermission(True, "support", target_phase, "support_contract_allowed")
        if resource_delta.request.intent == IntentKind.DEPOT_REPACK and contract_delta.effective_gain > 0:
            return PhasePermission(True, "support", target_phase, "positive_support_delta")
        return PhasePermission(False, "veto_candidate", target_phase, "phase_family_not_allowed")

    def target_phase(self, *, envelope: Any, resource_delta: ResourceDelta, remote_session_open: bool) -> str:
        family = envelope.contract.family
        request = resource_delta.request
        if (
            plan_facts.is_remote_outbound_session_release(envelope, request)
            and not remote_session_open
        ):
            return "H3"
        if family == ContractFamily.CUN4_PORT_STAGING or request.target_line == "存4线":
            return "H2"
        if family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT, ContractFamily.DEPOT_OUTBOUND}:
            return "H4"
        if family == ContractFamily.REMOTE_SESSION:
            return "H4" if remote_session_open else "H3"
        if family in {
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.YARD_REBALANCE,
            ContractFamily.LOCO_AREA_STAGING,
        }:
            return "H1"
        if family == ContractFamily.SPECIAL_REPAIR_PROCESS:
            return "H4"
        return "H5"

    def transition_type(self, previous_phase: str, current_phase: str) -> str:
        if not previous_phase:
            return "enter"
        if previous_phase == current_phase:
            return "stay"
        previous_rank = PHASE_RANK.get(previous_phase, 0)
        current_rank = PHASE_RANK.get(current_phase, 0)
        if current_rank <= previous_rank:
            return "fail"
        if current_rank > previous_rank + 1:
            return "skip"
        return "exit"

    def record(
        self,
        *,
        case_id: str,
        step_index: int,
        previous_phase: str,
        current_phase: str,
        phase_state: PhaseState,
        envelope: Any | None,
        contract_delta: ContractDelta | None,
        permission: PhasePermission | None,
        hook_count_in_phase: int,
        reject_reason: str = "",
        transition_override: str = "",
    ) -> PhaseGateRecord:
        transition = transition_override or self.transition_type(previous_phase, current_phase)
        if transition == "fail" and not reject_reason:
            reject_reason = f"phase_regression:{previous_phase}->{current_phase}"
        predicate_values = self._predicate_values(
            phase_state=phase_state,
            permission=permission,
            transition_type=transition,
        )
        contract_id = envelope.contract.contract_id if envelope else ""
        consumed = contract_id if contract_delta and contract_delta.contract_reduction > 0 else ""
        carried = "|".join(envelope.contract.obligations) if envelope else ""
        blocked = contract_id if permission and not permission.allowed else ""
        evidence = ""
        if envelope:
            evidence = f"{envelope.candidate.candidate_id}|{envelope.template_name}"
        return PhaseGateRecord(
            case_id=case_id,
            step_index=step_index,
            from_phase=previous_phase,
            to_phase=current_phase,
            transition_type=transition,
            active_variant=self._variant(phase_state),
            predicate_values=predicate_values,
            consumed_contract_ids=consumed,
            created_contract_ids="",
            carried_obligation_ids=carried,
            blocked_contract_ids=blocked,
            evidence_ids=evidence,
            hook_count_in_phase=hook_count_in_phase,
            manual_phase_hook_count="",
            reject_reason=reject_reason or (permission.reason if permission and not permission.allowed else ""),
        )

    def fail_record(
        self,
        *,
        case_id: str,
        step_index: int,
        phase_state: PhaseState,
        current_phase: str,
        blocked_reason: str,
        hook_count_in_phase: int,
    ) -> PhaseGateRecord:
        return self.record(
            case_id=case_id,
            step_index=step_index,
            previous_phase=current_phase,
            current_phase=current_phase,
            phase_state=phase_state,
            envelope=None,
            contract_delta=None,
            permission=None,
            hook_count_in_phase=hook_count_in_phase,
            reject_reason=blocked_reason,
            transition_override="fail",
        )

    def _is_structural_support(
        self,
        *,
        phase: str,
        target_phase: str,
        envelope: Any,
        contract_delta: ContractDelta,
        resource_delta: ResourceDelta,
    ) -> bool:
        if envelope.contract.family not in self.SUPPORT_FAMILIES.get(phase, set()):
            return False
        if contract_delta.effective_gain <= 0 and contract_delta.total_reduction <= 0:
            return False
        touched_remote = any(line in legacy.REMOTE_INTERACTION_LINES for line in resource_delta.request.touched_lines)
        if phase in {"H3", "H4"} and target_phase in {"H1", "H2"} and not touched_remote:
            return False
        return True

    def _predicate_values(
        self,
        *,
        phase_state: PhaseState,
        permission: PhasePermission | None,
        transition_type: str,
    ) -> str:
        values = {
            "front_debt": phase_state.front_debt,
            "cun4_port_debt": phase_state.cun4_port_debt,
            "remote_debt": phase_state.remote_debt,
            "closeout_debt": phase_state.closeout_debt,
            "phase_reason": phase_state.reason,
            "transition": transition_type,
        }
        if permission:
            values.update(
                {
                    "permission": permission.relation,
                    "target_phase": permission.target_phase,
                    "permission_reason": permission.reason,
                }
            )
        return ";".join(f"{key}={value}" for key, value in values.items())

    def _variant(self, phase_state: PhaseState) -> str:
        if phase_state.remote_debt and phase_state.cun4_port_debt:
            return "FULL_CHAIN_REPAIR"
        if phase_state.remote_debt:
            return "DIRECT_REPAIR_ENTRY"
        return "FRONT_OR_CLOSEOUT"
