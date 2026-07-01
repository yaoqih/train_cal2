from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from .domain import CandidateEnvelope, ResourceDelta
from .phase import PhasePermission


@dataclass(frozen=True)
class StagingIntentRecord:
    case_id: str
    hook_index: int
    event_type: str
    active_key: str
    owner_contract_id: str
    family: str
    phase: str
    target_phase: str
    intent: str
    template_name: str
    source_line: str
    staging_line: str
    vehicle_order: str
    accessible_end: str
    expiry_condition: str
    release_condition: str
    forbidden_pollution: str
    reason: str


class StagingIntentBuilder:
    """Extract explicit temporary-assembly facts from selected planlets."""

    def records_for_selected(
        self,
        *,
        case_id: str,
        hook_index: int,
        phase: str,
        envelope: CandidateEnvelope,
        resource_delta: ResourceDelta,
        phase_permission: PhasePermission,
    ) -> list[StagingIntentRecord]:
        records: list[StagingIntentRecord] = []
        request = resource_delta.request
        target_phase = phase_permission.target_phase
        if request.same_plan_source_return_nos:
            records.append(
                self._record(
                    case_id=case_id,
                    hook_index=hook_index,
                    event_type="instant",
                    phase=phase,
                    target_phase=target_phase,
                    envelope=envelope,
                    staging_line=request.source_line,
                    vehicle_order=request.same_plan_source_return_nos,
                    expiry_condition="same_planlet_source_return_required",
                    release_condition="same_planlet_returns_unconsumed_prefix",
                    reason="same_planlet_temporary_source_return",
                )
            )
        records.extend(
            self._same_plan_temporary_puts(
                case_id=case_id,
                hook_index=hook_index,
                phase=phase,
                target_phase=target_phase,
                envelope=envelope,
            )
        )
        return records

    def _same_plan_temporary_puts(
        self,
        *,
        case_id: str,
        hook_index: int,
        phase: str,
        target_phase: str,
        envelope: CandidateEnvelope,
    ) -> list[StagingIntentRecord]:
        steps = legacy.candidate_plan_steps(envelope.candidate)
        later_gets: dict[tuple[str, str], int] = {}
        for index, step in enumerate(steps):
            if step.action != "Get":
                continue
            for no in self._step_nos(step):
                later_gets[(step.line, no)] = index

        records: list[StagingIntentRecord] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for index, step in enumerate(steps):
            if step.action != "Put":
                continue
            staged = tuple(
                no
                for no in self._step_nos(step)
                if later_gets.get((step.line, no), -1) > index
            )
            if not staged:
                continue
            key = (step.line, staged)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                self._record(
                    case_id=case_id,
                    hook_index=hook_index,
                    event_type="instant",
                    phase=phase,
                    target_phase=target_phase,
                    envelope=envelope,
                    staging_line=step.line,
                    vehicle_order=staged,
                    expiry_condition="same_planlet_get_required",
                    release_condition="same_planlet_retrieves_staged_segment",
                    reason="same_planlet_temporary_put_get",
                )
            )
        return records

    def _step_nos(self, step: Any) -> tuple[str, ...]:
        return tuple(getattr(step, "move_car_nos", ()) or getattr(step, "move_nos", ()) or ())

    def _record(
        self,
        *,
        case_id: str,
        hook_index: int,
        event_type: str,
        phase: str,
        target_phase: str,
        envelope: CandidateEnvelope,
        staging_line: str,
        vehicle_order: tuple[str, ...],
        expiry_condition: str,
        release_condition: str,
        reason: str,
    ) -> StagingIntentRecord:
        active_key = self._active_key(
            owner_contract_id=envelope.contract.contract_id,
            staging_line=staging_line,
            vehicle_order=vehicle_order,
        )
        return StagingIntentRecord(
            case_id=case_id,
            hook_index=hook_index,
            event_type=event_type,
            active_key=active_key,
            owner_contract_id=envelope.contract.contract_id,
            family=envelope.contract.family.value,
            phase=phase,
            target_phase=target_phase,
            intent=envelope.intent.value,
            template_name=envelope.template_name,
            source_line=envelope.candidate.source_line,
            staging_line=staging_line,
            vehicle_order="|".join(vehicle_order),
            accessible_end="current_access_order",
            expiry_condition=expiry_condition,
            release_condition=release_condition,
            forbidden_pollution=self._forbidden_pollution(staging_line),
            reason=reason,
        )

    def _active_key(
        self,
        *,
        owner_contract_id: str,
        staging_line: str,
        vehicle_order: tuple[str, ...],
    ) -> str:
        return f"{owner_contract_id}:{staging_line}:{','.join(vehicle_order)}"

    def _forbidden_pollution(self, staging_line: str) -> str:
        flags: list[str] = []
        if staging_line == "存4线":
            flags.append("do_not_pollute_cun4_release_port")
        if staging_line in legacy.REMOTE_INTERACTION_LINES:
            flags.append("do_not_block_remote_session_corridor")
        if staging_line in legacy.RUNNING_LINES:
            flags.append("running_line_storage_forbidden")
        return "|".join(flags)
