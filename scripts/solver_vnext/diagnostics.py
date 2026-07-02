from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import physical


@dataclass
class CandidateRoundStats:
    generated_candidate_count: int = 0
    accepted_candidate_count: int = 0
    physical_reject_count: int = 0
    resource_violation_count: int = 0
    contract_reject_count: int = 0
    phase_veto_count: int = 0
    loop_reject_count: int = 0
    other_reject_count: int = 0
    reject_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def rejected_candidate_count(self) -> int:
        return (
            self.physical_reject_count
            + self.resource_violation_count
            + self.contract_reject_count
            + self.phase_veto_count
            + self.loop_reject_count
            + self.other_reject_count
        )

    def generated(self) -> None:
        self.generated_candidate_count += 1

    def accepted(self) -> None:
        self.accepted_candidate_count += 1

    def rejected(self, reason: str) -> None:
        self.reject_reasons[reason] += 1
        if reason == "physical_reject":
            self.physical_reject_count += 1
        elif reason.startswith("resource_violation:"):
            self.resource_violation_count += 1
        elif reason.startswith("contract_broken:") or reason == "nonpositive_contract_and_global_delta":
            self.contract_reject_count += 1
        elif reason.startswith("phase_veto:"):
            self.phase_veto_count += 1
        elif reason == "state_signature_loop":
            self.loop_reject_count += 1
        else:
            self.other_reject_count += 1

    def top_reject_reasons(self, limit: int = 6) -> str:
        return "|".join(f"{reason}:{count}" for reason, count in self.reject_reasons.most_common(limit))


@dataclass(frozen=True)
class StructureNodeMetricRecord:
    case_id: str
    hook_index: int
    phase: str
    phase_reason: str
    active_variant: str
    front_debt: int
    cun4_port_debt: int
    remote_debt: int
    closeout_debt: int
    cun4_release_ready: bool
    remote_session_open: bool
    flow_edge_count: int
    flow_edge_families: str
    contract_count: int
    contract_families: str
    generated_candidate_count: int
    accepted_candidate_count: int
    rejected_candidate_count: int
    physical_reject_count: int
    resource_violation_count: int
    contract_reject_count: int
    phase_veto_count: int
    loop_reject_count: int
    other_reject_count: int
    top_reject_reasons: str
    selected_candidate_id: str
    selected_contract_id: str
    selected_family: str
    selected_intent: str
    selected_template_name: str
    selected_source_line: str
    selected_target_line: str
    selected_move_count: int
    selected_contract_reduction: int
    selected_effective_gain: int
    selected_total_reduction: int
    blocked_reason: str


@dataclass(frozen=True)
class GenerationGapRecord:
    case_id: str
    hook_index: int
    phase: str
    phase_reason: str
    contract_id: str
    family: str
    source_lines: str
    target_lines: str
    subject_count: int
    applicable_template_count: int
    applicable_templates: str
    source_reachable: str
    target_reachable: str
    source_route_blocked: str
    target_route_blocked: str
    source_prefixes: str
    serial_blocker_sources: str
    reason: str


def build_structure_node_record(
    *,
    state: Any,
    policy_context: Any,
    flow_edges: list[Any],
    contracts: list[Any],
    stats: CandidateRoundStats,
    selected: Any | None,
    blocked_reason: str = "",
) -> StructureNodeMetricRecord:
    phase_state = policy_context.phase_state
    edge_families = Counter(edge.family for edge in flow_edges)
    contract_families = Counter(contract.family.value for contract in contracts)
    selected_envelope = selected.envelope if selected else None
    selected_request = selected.resource_delta.request if selected else None
    return StructureNodeMetricRecord(
        case_id=state.case_id,
        hook_index=state.hook_index,
        phase=phase_state.phase.value,
        phase_reason=phase_state.reason,
        active_variant=phase_state.active_variant,
        front_debt=phase_state.front_debt,
        cun4_port_debt=phase_state.cun4_port_debt,
        remote_debt=phase_state.remote_debt,
        closeout_debt=phase_state.closeout_debt,
        cun4_release_ready=phase_state.cun4_release_ready,
        remote_session_open=policy_context.remote_session_open,
        flow_edge_count=len(flow_edges),
        flow_edge_families=_counter_text(edge_families),
        contract_count=len(contracts),
        contract_families=_counter_text(contract_families),
        generated_candidate_count=stats.generated_candidate_count,
        accepted_candidate_count=stats.accepted_candidate_count,
        rejected_candidate_count=stats.rejected_candidate_count,
        physical_reject_count=stats.physical_reject_count,
        resource_violation_count=stats.resource_violation_count,
        contract_reject_count=stats.contract_reject_count,
        phase_veto_count=stats.phase_veto_count,
        loop_reject_count=stats.loop_reject_count,
        other_reject_count=stats.other_reject_count,
        top_reject_reasons=stats.top_reject_reasons(),
        selected_candidate_id=selected_envelope.candidate.candidate_id if selected_envelope else "",
        selected_contract_id=selected_envelope.contract.contract_id if selected_envelope else "",
        selected_family=selected_envelope.contract.family.value if selected_envelope else "",
        selected_intent=selected_envelope.intent.value if selected_envelope else "",
        selected_template_name=selected_envelope.template_name if selected_envelope else "",
        selected_source_line=selected_envelope.candidate.source_line if selected_envelope else "",
        selected_target_line=selected_request.target_line if selected_request else "",
        selected_move_count=len(selected_envelope.candidate.move_car_nos) if selected_envelope else 0,
        selected_contract_reduction=selected.contract_delta.contract_reduction if selected else 0,
        selected_effective_gain=selected.contract_delta.effective_gain if selected else 0,
        selected_total_reduction=selected.contract_delta.total_reduction if selected else 0,
        blocked_reason=blocked_reason,
    )


def build_generation_gap_records(
    *,
    state: Any,
    policy_context: Any,
    contracts: list[Any],
    episodes: tuple[Any, ...],
    frontier_record: Any,
) -> list[GenerationGapRecord]:
    reachable = _split_set(frontier_record.reachable_lines)
    route_blocked = _split_set(frontier_record.route_blocked_lines)
    prefixes = _prefix_by_line(frontier_record.accessible_prefixes)
    serial_blockers = _serial_blocker_lines(frontier_record.serial_blocker_lines)
    phase_state = policy_context.phase_state
    rows: list[GenerationGapRecord] = []
    for contract in contracts:
        applicable = tuple(
            episode.template_name
            for episode in episodes
            if episode.applies(contract)
        )
        sources = tuple(contract.source_lines)
        targets = tuple(line for line in contract.target_lines if line)
        source_reachable = tuple(line for line in sources if line in reachable)
        target_reachable = tuple(line for line in targets if line in reachable)
        source_blocked = tuple(line for line in sources if line in route_blocked)
        target_blocked = tuple(line for line in targets if line in route_blocked)
        source_prefixes = tuple(
            f"{line}:{','.join(prefixes[line])}"
            for line in sources
            if prefixes.get(line)
        )
        serial_sources = tuple(line for line in sources if line in serial_blockers)
        rows.append(
            GenerationGapRecord(
                case_id=state.case_id,
                hook_index=state.hook_index,
                phase=phase_state.phase.value,
                phase_reason=phase_state.reason,
                contract_id=contract.contract_id,
                family=contract.family.value,
                source_lines="|".join(sources),
                target_lines="|".join(targets),
                subject_count=len(contract.subject_nos),
                applicable_template_count=len(applicable),
                applicable_templates="|".join(applicable),
                source_reachable="|".join(source_reachable),
                target_reachable="|".join(target_reachable),
                source_route_blocked="|".join(source_blocked),
                target_route_blocked="|".join(target_blocked),
                source_prefixes="|".join(source_prefixes),
                serial_blocker_sources="|".join(serial_sources),
                reason=_generation_gap_reason(
                    contract=contract,
                    applicable=applicable,
                    sources=sources,
                    targets=targets,
                    source_reachable=source_reachable,
                    target_reachable=target_reachable,
                    source_blocked=source_blocked,
                    target_blocked=target_blocked,
                    source_prefixes=source_prefixes,
                    serial_sources=serial_sources,
                ),
            )
        )
    return rows


def _generation_gap_reason(
    *,
    contract: Any,
    applicable: tuple[str, ...],
    sources: tuple[str, ...],
    targets: tuple[str, ...],
    source_reachable: tuple[str, ...],
    target_reachable: tuple[str, ...],
    source_blocked: tuple[str, ...],
    target_blocked: tuple[str, ...],
    source_prefixes: tuple[str, ...],
    serial_sources: tuple[str, ...],
) -> str:
    if not applicable:
        return "no_applicable_episode"
    if sources and not source_reachable:
        return "source_not_reachable"
    if targets and not target_reachable:
        return "target_not_reachable"
    if source_blocked:
        return "source_route_blocked"
    if target_blocked:
        return "target_route_blocked"
    if not source_prefixes:
        return "source_prefix_empty"
    if serial_sources:
        return "serial_blocker_source_open"
    if _subject_is_blocked_inside_prefix(contract, source_prefixes):
        return "source_prefix_blocker_requires_lease"
    if any(physical.is_spotting_line(line) for line in targets):
        return "spotting_target_repack_required"
    return "episode_generated_zero_after_prefilter"


def _subject_is_blocked_inside_prefix(contract: Any, source_prefixes: tuple[str, ...]) -> bool:
    subject_nos = set(getattr(contract, "subject_nos", ()) or ())
    if not subject_nos:
        return False
    for item in source_prefixes:
        if ":" not in item:
            continue
        _line, nos_text = item.split(":", 1)
        nos = [no for no in nos_text.split(",") if no]
        for index, no in enumerate(nos):
            if no in subject_nos:
                return index > 0
    return False


def _split_set(text: str) -> set[str]:
    return {item for item in (text or "").split("|") if item}


def _prefix_by_line(text: str) -> dict[str, tuple[str, ...]]:
    prefixes: dict[str, tuple[str, ...]] = {}
    for item in (text or "").split("|"):
        if not item or ":" not in item:
            continue
        line, nos = item.split(":", 1)
        prefixes[line] = tuple(no for no in nos.split(",") if no)
    return prefixes


def _serial_blocker_lines(text: str) -> set[str]:
    lines: set[str] = set()
    for item in (text or "").split("|"):
        if not item:
            continue
        line = item.split(":", 1)[0]
        if line:
            lines.add(line)
    return lines


def _counter_text(counter: Counter[str]) -> str:
    return "|".join(f"{key}:{count}" for key, count in sorted(counter.items()))
