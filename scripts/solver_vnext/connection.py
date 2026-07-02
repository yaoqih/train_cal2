from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import CandidateEnvelope, ContractDelta, ResourceDelta
from .phase import PhasePermission


@dataclass(frozen=True)
class ConnectionMetricRecord:
    case_id: str
    hook_index: int
    connection: str
    input_id: str
    output_id: str
    owner_contract_id: str
    status: str
    reason: str


def records_for_selected(
    *,
    case_id: str,
    hook_index: int,
    envelope: CandidateEnvelope,
    contract_delta: ContractDelta,
    resource_delta: ResourceDelta,
    phase_permission: PhasePermission,
) -> list[ConnectionMetricRecord]:
    return [
        _record(
            case_id,
            hook_index,
            "FlowClassify->FlowContract",
            "|".join(envelope.contract.subject_nos),
            envelope.contract.contract_id,
            envelope.contract.contract_id,
            bool(envelope.contract.subject_nos),
            envelope.contract.reason or "contract_has_owner",
        ),
        _record(
            case_id,
            hook_index,
            "FlowContract->StructuralIntent",
            envelope.contract.contract_id,
            envelope.intent.value,
            envelope.contract.contract_id,
            bool(envelope.intent),
            "intent_bound_to_contract",
        ),
        _record(
            case_id,
            hook_index,
            "Candidate->ResourceRequest",
            envelope.candidate.candidate_id,
            resource_delta.request.candidate_id,
            envelope.contract.contract_id,
            bool(resource_delta.request.resources),
            "|".join(resource.value for resource in resource_delta.request.resources),
        ),
        _record(
            case_id,
            hook_index,
            "Candidate->ContractDelta",
            envelope.candidate.candidate_id,
            contract_delta.contract_id,
            envelope.contract.contract_id,
            contract_delta.contract_id == envelope.contract.contract_id,
            f"before={contract_delta.before_contract_debt};after={contract_delta.after_contract_debt};gain={contract_delta.effective_gain}",
        ),
        _record(
            case_id,
            hook_index,
            "ResourceRequest->ResourceDelta",
            resource_delta.request.candidate_id,
            "|".join(resource.value for resource in resource_delta.acquired),
            envelope.contract.contract_id,
            not resource_delta.violations,
            "|".join(resource_delta.violations) or "resource_delta_legal",
        ),
        _record(
            case_id,
            hook_index,
            "PhaseGate->AcceptRejectGate",
            phase_permission.target_phase,
            phase_permission.relation,
            envelope.contract.contract_id,
            phase_permission.allowed,
            phase_permission.reason,
        ),
    ]


def _record(
    case_id: str,
    hook_index: int,
    connection: str,
    input_id: str,
    output_id: str,
    owner_contract_id: str,
    ok: bool,
    reason: str,
) -> ConnectionMetricRecord:
    return ConnectionMetricRecord(
        case_id=case_id,
        hook_index=hook_index,
        connection=connection,
        input_id=input_id,
        output_id=output_id,
        owner_contract_id=owner_contract_id,
        status="pass" if ok else "fail",
        reason=reason,
    )
