from __future__ import annotations

from typing import Any

from . import physical
from .domain import CandidateEnvelope, ContractFamily, IntentKind, ResourceRequest


def touches_remote(request: ResourceRequest) -> bool:
    return any(line in physical.REMOTE_INTERACTION_LINES for line in request.touched_lines)


def remote_transition_cost(candidate: Any, last_business_remote: bool | None) -> int:
    lines = [
        step.line
        for step in physical.candidate_plan_steps(candidate)
        if step.action in {"Get", "Put"}
    ]
    if not lines:
        return 0
    remote_flags = [line in physical.REMOTE_INTERACTION_LINES for line in lines]
    cost = sum(1 for left, right in zip(remote_flags, remote_flags[1:]) if left != right)
    if last_business_remote is not None and remote_flags[0] != last_business_remote:
        cost += 1
    return cost


def put_count(candidate: Any) -> int:
    return sum(1 for step in physical.candidate_plan_steps(candidate) if step.action == "Put")


def hook_count(candidate: Any) -> int:
    steps = physical.candidate_plan_steps(candidate)
    if not steps:
        return 2
    return sum(1 for step in steps if step.action in {"Get", "Put"})


def depot_put_line_count(request: ResourceRequest) -> int:
    return sum(1 for line in request.put_lines if line in physical.DEPOT_LINES)


def is_remote_release_to_cun4(envelope: CandidateEnvelope, request: ResourceRequest) -> bool:
    return (
        envelope.intent == IntentKind.CUN4_RELEASE_ACCEPT
        and envelope.contract.family == ContractFamily.REMOTE_SESSION
        and request.source_line in physical.REMOTE_INTERACTION_LINES
        and request.target_line == "存4线"
    )


def is_remote_outbound_session_release(envelope: CandidateEnvelope, request: ResourceRequest) -> bool:
    if not is_remote_release_to_cun4(envelope, request):
        return False
    if request.same_plan_source_return_nos:
        return False
    steps = physical.candidate_plan_steps(envelope.candidate)
    gets = [step for step in steps if step.action == "Get"]
    puts = [step for step in steps if step.action == "Put"]
    return (
        len(gets) >= 2
        and len(puts) == 1
        and puts[0].line == "存4线"
        and all(step.line in physical.REMOTE_INTERACTION_LINES for step in gets)
    )
