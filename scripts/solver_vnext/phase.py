from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical
from . import plan_facts
from .domain import ContractDelta, ContractFamily, IntentKind, PhaseKind, PhaseState, RemoteSessionState, ResourceDelta


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

    This module classifies phase state and decides whether a candidate is
    primary/support/veto for that phase. It does not rank candidates or generate
    moves; the engine treats veto as a hard gate before policy tie-breaking.
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
        "H1": {ContractFamily.CUN4_PORT_STAGING},
        "H2": {
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.REMOTE_SESSION,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_OUTBOUND,
            ContractFamily.SPECIAL_REPAIR_PROCESS,
        },
        "H3": {ContractFamily.CUN4_PORT_STAGING, ContractFamily.DEPOT_SLOT},
        "H4": {
            ContractFamily.CUN4_PORT_STAGING,
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.YARD_REBALANCE,
            ContractFamily.LOCO_AREA_STAGING,
        },
        "H5": {ContractFamily.SPECIAL_REPAIR_PROCESS},
    }

    def classify_state(
        self,
        *,
        front_debt: int,
        cun4_port_debt: int,
        remote_debt: int,
        closeout_debt: int,
        remote_session: RemoteSessionState,
        cun4_release_ready: bool = False,
        cun4_port_mode: str = "",
        cun4_release_count: int = 0,
        cun4_prefix_hold_count: int = 0,
        active_variant: str = "",
    ) -> PhaseState:
        if remote_session.active and remote_debt:
            phase = PhaseKind.H4_REMOTE_DEPOT
            reason = f"remote_session_continuation:{remote_session.session_id or remote_session.owner_contract_id}"
        elif cun4_release_ready and remote_debt:
            phase = PhaseKind.H3_RELEASE_ACCEPT
            reason = f"cun4_release_ready:{cun4_port_mode}:{cun4_release_count}"
        elif self._h1_front_work_still_material(
            front_debt=front_debt,
            remote_debt=remote_debt,
            active_variant=active_variant,
        ):
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
            cun4_release_ready=cun4_release_ready,
            cun4_port_mode=cun4_port_mode,
            cun4_release_count=cun4_release_count,
            cun4_prefix_hold_count=cun4_prefix_hold_count,
            active_variant=active_variant,
        )

    def _h1_front_work_still_material(self, *, front_debt: int, remote_debt: int, active_variant: str) -> bool:
        if front_debt <= 6:
            return False
        if not remote_debt:
            return True
        if active_variant == "DEPOT_DIGEST_ONLY":
            return front_debt > max(10, int(remote_debt * 0.45))
        if active_variant in {"FULL_CHAIN_REPAIR", "LATE_CUN4_REPAIR", "MIXED_SIGNAL_REPAIR"}:
            return front_debt > max(8, int(remote_debt * 0.35))
        return front_debt > int(remote_debt * 0.5)

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
        if previous_phase == "H3" and proposed.remote_debt > 0 and proposed_code != "H4":
            return "H3"
        if previous_phase in {"H3", "H4"} and proposed.remote_debt == 0 and proposed_rank < PHASE_RANK["H5"]:
            return "H5"
        if previous_phase == "H2" and proposed_code != "H3" and proposed.cun4_port_debt > 0 and proposed.remote_debt > 0:
            return "H2"
        if previous_phase == "H2" and proposed.cun4_release_ready and proposed.remote_debt > 0:
            return "H3"
        if proposed_rank and proposed_rank < previous_rank:
            return previous_phase
        return proposed_code

    def next_phase_after_exhaustion(self, *, phase_state: PhaseState, current_phase: str) -> str:
        if current_phase == "H1":
            if phase_state.cun4_port_debt and phase_state.remote_debt:
                return "H2"
            if phase_state.remote_debt:
                return "H4"
            if phase_state.closeout_debt:
                return "H5"
        if current_phase == "H2":
            if phase_state.cun4_release_ready and phase_state.remote_debt:
                return "H3"
            if phase_state.remote_debt:
                return "H4"
        if current_phase == "H3" and phase_state.remote_debt:
            return "H4"
        return ""

    def permission(
        self,
        *,
        phase_state: PhaseState,
        envelope: Any,
        contract_delta: ContractDelta,
        resource_delta: ResourceDelta,
        remote_session: RemoteSessionState,
    ) -> PhasePermission:
        phase = self.phase_code(phase_state.phase)
        target_phase = self.target_phase(
            envelope=envelope,
            resource_delta=resource_delta,
        )
        family = envelope.contract.family
        hard_veto = self._hard_boundary_veto(
            phase=phase,
            phase_state=phase_state,
            target_phase=target_phase,
            contract_delta=contract_delta,
            resource_delta=resource_delta,
            remote_session=remote_session,
        )
        if hard_veto:
            return PhasePermission(False, "veto_candidate", target_phase, hard_veto)
        if family in self.PRIMARY_FAMILIES.get(phase, set()):
            return PhasePermission(True, "primary", target_phase, "primary_family_allowed")
        if resource_delta.request.intent == IntentKind.CUN4_RELEASE_ACCEPT and phase in {"H2", "H3", "H4"}:
            return PhasePermission(True, "primary", "H3", "cun4_release_accept_boundary")
        if resource_delta.request.intent == IntentKind.CUN4_OUTBOUND_HOLD and phase in {"H2", "H3", "H4"}:
            return PhasePermission(True, "primary", "H4", "depot_outbound_h4_release")
        if (
            resource_delta.request.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY
            and phase in {"H1", "H2", "H4"}
            and contract_delta.effective_gain > 0
        ):
            relation = "primary" if phase == "H4" else "support"
            return PhasePermission(True, relation, "H4", "depot_inbound_assembly_boundary")
        if self._is_structural_support(
            phase=phase,
            target_phase=target_phase,
            envelope=envelope,
            contract_delta=contract_delta,
            resource_delta=resource_delta,
        ):
            return PhasePermission(True, "support", target_phase, "support_contract_allowed")
        if resource_delta.request.intent == IntentKind.DEPOT_SLOT_SWAP and contract_delta.effective_gain > 0:
            return PhasePermission(True, "support", target_phase, "positive_support_delta")
        return PhasePermission(False, "veto_candidate", target_phase, "phase_family_not_allowed")

    def target_phase(self, *, envelope: Any, resource_delta: ResourceDelta) -> str:
        family = envelope.contract.family
        request = resource_delta.request
        if request.intent == IntentKind.CUN4_RELEASE_GROUP:
            return "H2"
        if request.intent == IntentKind.CUN4_OUTBOUND_HOLD:
            return "H4"
        if request.intent == IntentKind.CUN4_RELEASE_ACCEPT:
            return "H3"
        if request.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
            return "H4"
        if plan_facts.is_remote_outbound_session_release(envelope, request):
            return "H3"
        if family == ContractFamily.CUN4_PORT_STAGING:
            return "H2"
        if family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT, ContractFamily.DEPOT_OUTBOUND}:
            return "H4"
        if family == ContractFamily.REMOTE_SESSION:
            return "H4"
        if request.source_line in physical.REMOTE_INTERACTION_LINES or request.source_line == "存4线":
            return "H4"
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
        if previous_phase == "H4" and current_phase == "H3":
            return "release"
        previous_rank = PHASE_RANK.get(previous_phase, 0)
        current_rank = PHASE_RANK.get(current_phase, 0)
        if current_rank <= previous_rank:
            return "fail"
        if current_rank > previous_rank + 1:
            return "skip"
        return "exit"

    def execution_phase(self, *, current_phase: str, permission: PhasePermission | None) -> str:
        """Phase consumed by the accepted move, not just the pre-move state."""
        if not permission or not permission.allowed:
            return current_phase
        if permission.target_phase == "H3":
            return "H3"
        current_rank = PHASE_RANK.get(current_phase, 0)
        target_rank = PHASE_RANK.get(permission.target_phase, 0)
        if permission.relation == "support" and permission.target_phase != "H3":
            return current_phase
        if target_rank > current_rank:
            return permission.target_phase
        return current_phase

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
        if phase == "H4" and envelope.contract.family == ContractFamily.CUN4_PORT_STAGING:
            return True
        if (
            phase == "H4"
            and target_phase == "H1"
            and envelope.contract.family
            in {
                ContractFamily.FUNCTION_LINE_SERVICE,
                ContractFamily.DISPATCH_SHED_QUEUE,
                ContractFamily.PRE_REPAIR_STAGING,
                ContractFamily.YARD_REBALANCE,
                ContractFamily.LOCO_AREA_STAGING,
            }
        ):
            return True
        touched_remote = any(line in physical.REMOTE_INTERACTION_LINES for line in resource_delta.request.touched_lines)
        if phase in {"H3", "H4"} and target_phase in {"H1", "H2"} and not touched_remote:
            return False
        return True

    def _hard_boundary_veto(
        self,
        *,
        phase: str,
        phase_state: PhaseState,
        target_phase: str,
        contract_delta: ContractDelta,
        resource_delta: ResourceDelta,
        remote_session: RemoteSessionState,
    ) -> str:
        request = resource_delta.request
        touched_remote = any(line in physical.REMOTE_INTERACTION_LINES for line in request.touched_lines)
        if phase == "H2" and phase_state.cun4_release_ready:
            if target_phase == "H3":
                return ""
            if target_phase == "H4" and remote_session.active:
                return ""
            return "h2_release_ready_requires_h3_release_accept"
        if phase == "H2" and not phase_state.cun4_release_ready:
            if target_phase == "H4" and request.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
                return ""
            if target_phase in {"H3", "H4"} and "存4线" not in request.put_lines:
                if self._h2_port_can_exit_without_release(phase_state):
                    return ""
                return "h2_requires_release_group_before_remote_digest"
        if phase == "H3":
            if target_phase == "H3":
                return ""
            if target_phase == "H4" and (remote_session.active or touched_remote):
                return ""
            return "h3_release_accept_atomic_boundary"
        if phase == "H4" and phase_state.remote_debt > 0:
            if target_phase == "H5":
                return "h4_remote_debt_before_closeout"
            if target_phase == "H1":
                if not phase_state.depot_inbound_assembly_complete:
                    return ""
                return "h4_blocks_front_work_until_remote_debt_clear"
            if target_phase == "H2":
                if request.family == ContractFamily.CUN4_PORT_STAGING and request.target_line == "存4线":
                    return ""
                return "h4_blocks_front_work_until_remote_debt_clear"
        if phase == "H4" and not phase_state.depot_outbound_assembly_complete and target_phase == "H5":
            return "h4_depot_outbound_assembly_before_closeout"
        if phase == "H4" and not phase_state.depot_inbound_assembly_complete and target_phase == "H5":
            return "h4_depot_inbound_assembly_before_closeout"
        if phase == "H1" and not phase_state.depot_inbound_assembly_complete:
            if target_phase in {"H3", "H4"}:
                if request.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
                    return ""
                return "h1_depot_inbound_assembly_before_remote"
        return ""

    def _h2_port_can_exit_without_release(self, phase_state: PhaseState) -> bool:
        """H2 can finish as a protected outbound hold, not only as release-ready.

        Human plans use CUN4 as a port resource in two shapes:
        release-ready inbound group, or a clean outbound hold that has already
        consumed the CUN4 staging debt.  The latter should enter H4 directly;
        forcing a release group would invent work that the business case does
        not require.
        """
        return (
            phase_state.cun4_port_debt == 0
            and phase_state.cun4_release_count == 0
            and phase_state.cun4_port_mode in {"FREE", "OUTBOUND_HOLD"}
        )

    def _is_opportunistic_support(
        self,
        *,
        phase_state: PhaseState,
        target_phase: str,
        contract_delta: ContractDelta,
    ) -> bool:
        if contract_delta.effective_gain <= 0 and contract_delta.total_reduction <= 0:
            return False
        if target_phase == "H5" and (
            phase_state.front_debt > 0 or phase_state.cun4_port_debt > 0 or phase_state.remote_debt > 0
        ):
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
            "cun4_release_ready": phase_state.cun4_release_ready,
            "cun4_port_mode": phase_state.cun4_port_mode,
            "cun4_release_count": phase_state.cun4_release_count,
            "cun4_prefix_hold_count": phase_state.cun4_prefix_hold_count,
            "phase_reason": phase_state.reason,
            "front_topology_clear_for_remote": phase_state.front_topology_clear_for_remote,
            "depot_inbound_assembly_complete": phase_state.depot_inbound_assembly_complete,
            "depot_outbound_assembly_complete": phase_state.depot_outbound_assembly_complete,
            "strategic_plan_reason": phase_state.strategic_plan_reason,
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
        if phase_state.active_variant:
            return phase_state.active_variant
        if phase_state.remote_debt and phase_state.cun4_port_debt:
            return "FULL_CHAIN_REPAIR"
        if phase_state.remote_debt:
            return "DIRECT_REPAIR_ENTRY"
        return "FRONT_OR_CLOSEOUT"
