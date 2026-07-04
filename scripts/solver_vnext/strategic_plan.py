from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import depot_inbound_plan
from . import depot_outbound_plan
from . import physical
from . import release
from .contracts import build_car_refs
from .domain import ContractFamily, PhaseKind, RemoteSessionState


FRONT_TOPOLOGY_PRIORITY_LINES = (
    "洗罐站",
    "洗罐线北",
    "油漆线",
    "抛丸线",
    "卸轮线",
)
DEPOT_INBOUND_ASSEMBLY_OWNER_KINDS = {
    "vnext_depot_inbound_multisource_assembly_session",
    "vnext_depot_inbound_assembly_session",
    "vnext_depot_inbound_assembly_rebalance",
    "vnext_depot_inbound_route_blocker_digest",
    "vnext_depot_inbound_dirty_exchange_session",
    "vnext_depot_inbound_mixed_extraction_session",
    "vnext_depot_inbound_prefix_assembly_session",
    "vnext_cun4_release_group_assembly",
}
DEPOT_INBOUND_TEMPORARY_CLEANOUT_KINDS = {
    "temporary_cleanout",
}


@dataclass(frozen=True)
class FrontTopologyPlan:
    status: str
    reason: str
    priority_lines: tuple[str, ...]
    priority_nos: tuple[str, ...]
    blocked_risk_lines: tuple[str, ...]
    must_finish_before_remote: bool
    clear_for_remote: bool


@dataclass(frozen=True)
class Cun4ReleasePortPlan:
    status: str
    mode: str
    owner: str
    release_nos: tuple[str, ...]
    outbound_hold_nos: tuple[str, ...]
    dirty_nos: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RemoteSessionContinuityPlan:
    status: str
    reason: str
    should_continue_remote: bool
    remote_debt: int
    depot_outbound_debt: int
    depot_inbound_debt: int
    preferred_structures: tuple[str, ...]


@dataclass(frozen=True)
class PhaseCompletionPlan:
    status: str
    reason: str
    h1_can_exit: bool
    h4_can_close: bool


@dataclass(frozen=True)
class StrategicPlan:
    phase: PhaseKind
    front_topology: FrontTopologyPlan
    depot_inbound: depot_inbound_plan.DepotInboundAssemblyPlan
    depot_outbound: depot_outbound_plan.DepotOutboundAssemblyPlan
    cun4_release: Cun4ReleasePortPlan
    remote_session: RemoteSessionContinuityPlan
    completion: PhaseCompletionPlan
    depot_inbound_assembly_accepted: bool


def build_strategic_plan(
    *,
    phase: PhaseKind,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    remote_session: RemoteSessionState,
    remote_debt: int,
    depot_inbound_assembly_accepted: bool = False,
) -> StrategicPlan:
    front_topology = build_front_topology_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        remote_debt=remote_debt,
    )
    cun4_state = release.cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    depot_outbound = depot_outbound_plan.build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        cun4_released_nos=set(cun4_state.release_nos),
    )
    cun4_outbound_hold_nos = set(cun4_state.outbound_hold_nos) | {
        no
        for no, line in depot_outbound.temporary_line_by_no.items()
        if line == "存4线"
    }
    depot_inbound = depot_inbound_plan.build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        cun4_outbound_hold_nos=cun4_outbound_hold_nos,
        depot_outbound_nos=set(depot_outbound.outbound_nos),
        strict_cun4_unwheel_only=not depot_inbound_assembly_accepted,
    )
    depot_inbound_assembly_accepted = depot_inbound_assembly_accepted or depot_inbound.assembly_complete
    cun4_release = build_cun4_release_port_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        depot_outbound=depot_outbound,
        state=cun4_state,
    )
    remote_continuity = build_remote_session_continuity_plan(
        remote_session=remote_session,
        remote_debt=remote_debt,
        depot_inbound=depot_inbound,
        depot_outbound=depot_outbound,
    )
    completion = build_phase_completion_plan(
        front_topology=front_topology,
        depot_inbound=depot_inbound,
        depot_outbound=depot_outbound,
        remote_debt=remote_debt,
    )
    return StrategicPlan(
        phase=phase,
        front_topology=front_topology,
        depot_inbound=depot_inbound,
        depot_outbound=depot_outbound,
        cun4_release=cun4_release,
        remote_session=remote_continuity,
        completion=completion,
        depot_inbound_assembly_accepted=depot_inbound_assembly_accepted,
    )


def build_front_topology_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    remote_debt: int,
) -> FrontTopologyPlan:
    refs = [ref for ref in build_car_refs(cars, depot_assignment) if not ref.satisfied]
    priority_refs = [
        ref
        for ref in refs
        if ref.line in FRONT_TOPOLOGY_PRIORITY_LINES
    ]
    ordered_refs = sorted(
        priority_refs,
        key=lambda ref: (
            _front_line_rank(ref.line),
            ref.line,
            ref.position,
            ref.no,
        ),
    )
    priority_lines = tuple(
        dict.fromkeys(
            line
            for ref in ordered_refs
            for line in (ref.line, ref.target_line)
            if line == ref.line and line in FRONT_TOPOLOGY_PRIORITY_LINES
        )
    )
    priority_nos = tuple(ref.no for ref in ordered_refs)
    must_finish_before_remote = bool(priority_nos and remote_debt)
    if not priority_nos:
        status = "pass"
        reason = "front_topology_clear"
    elif must_finish_before_remote:
        status = "warn"
        reason = "front_topology_priority_before_remote"
    else:
        status = "pass"
        reason = "front_topology_priority_without_remote"
    return FrontTopologyPlan(
        status=status,
        reason=reason,
        priority_lines=priority_lines,
        priority_nos=priority_nos,
        blocked_risk_lines=priority_lines if must_finish_before_remote else (),
        must_finish_before_remote=must_finish_before_remote,
        clear_for_remote=not must_finish_before_remote,
    )


def build_cun4_release_port_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    depot_outbound: depot_outbound_plan.DepotOutboundAssemblyPlan,
    state: release.Cun4PortState | None = None,
) -> Cun4ReleasePortPlan:
    state = state or release.cun4_port_state(cars=cars, depot_assignment=depot_assignment)
    outbound_hold = tuple(dict.fromkeys((
        *state.outbound_hold_nos,
        *(
            no
            for no, line in depot_outbound.temporary_line_by_no.items()
            if line == "存4线"
        ),
    )))
    dirty_nos = tuple(state.dirty_nos)
    if state.release_nos and outbound_hold:
        status = "warn"
        owner = "mixed_release_and_outbound"
        reason = "cun4_release_and_outbound_share_port"
    elif dirty_nos:
        status = "fail"
        owner = "dirty"
        reason = state.reason
    elif state.release_nos:
        status = "pass"
        owner = "inbound_release"
        reason = state.reason
    elif outbound_hold:
        status = "pass"
        owner = "outbound_assembly"
        reason = "cun4_reserved_for_depot_outbound_assembly"
    else:
        status = "pass"
        owner = "free"
        reason = state.reason
    return Cun4ReleasePortPlan(
        status=status,
        mode=state.mode,
        owner=owner,
        release_nos=tuple(state.release_nos),
        outbound_hold_nos=outbound_hold,
        dirty_nos=dirty_nos,
        reason=reason,
    )


def build_remote_session_continuity_plan(
    *,
    remote_session: RemoteSessionState,
    remote_debt: int,
    depot_inbound: depot_inbound_plan.DepotInboundAssemblyPlan,
    depot_outbound: depot_outbound_plan.DepotOutboundAssemblyPlan,
) -> RemoteSessionContinuityPlan:
    should_continue = bool(remote_session.active and remote_debt)
    preferred: list[str] = []
    if depot_inbound.ungrouped_nos:
        preferred.append("depot_inbound_assembly")
    if depot_outbound.outbound_nos:
        preferred.append("depot_outbound_assembly")
    if remote_debt:
        preferred.extend(("cun4_release_accept", "depot_slot_swap", "remote_session_digest"))
    if should_continue:
        status = "warn"
        reason = "remote_session_should_continue"
    elif remote_debt:
        status = "pass"
        reason = "remote_debt_waiting_for_session"
    else:
        status = "pass"
        reason = "remote_session_clear"
    return RemoteSessionContinuityPlan(
        status=status,
        reason=reason,
        should_continue_remote=should_continue,
        remote_debt=remote_debt,
        depot_outbound_debt=len(depot_outbound.outbound_nos),
        depot_inbound_debt=len(depot_inbound.ungrouped_nos),
        preferred_structures=tuple(dict.fromkeys(preferred)),
    )


def build_phase_completion_plan(
    *,
    front_topology: FrontTopologyPlan,
    depot_inbound: depot_inbound_plan.DepotInboundAssemblyPlan,
    depot_outbound: depot_outbound_plan.DepotOutboundAssemblyPlan,
    remote_debt: int,
) -> PhaseCompletionPlan:
    h1_can_exit = front_topology.clear_for_remote
    h4_can_close = (
        remote_debt == 0
        and depot_inbound.assembly_complete
        and depot_outbound.assembly_complete
    )
    if not h1_can_exit:
        status = "warn"
        reason = "h1_front_topology_not_clear"
    elif not h4_can_close and remote_debt:
        status = "warn"
        reason = "h4_remote_debt_not_clear"
    elif not depot_inbound.assembly_complete:
        status = "warn"
        reason = "h4_depot_inbound_assembly_not_clear"
    elif not h4_can_close:
        status = "warn"
        reason = "h4_depot_outbound_assembly_not_clear"
    else:
        status = "pass"
        reason = "phase_completion_clear"
    return PhaseCompletionPlan(
        status=status,
        reason=reason,
        h1_can_exit=h1_can_exit,
        h4_can_close=h4_can_close,
    )


def planned_temporary_line(plan: StrategicPlan, no: str) -> str:
    mapping = plan.depot_outbound.temporary_line_by_no
    if no not in mapping:
        raise KeyError(f"not_in_depot_outbound_plan:{no}")
    return mapping[no]


def depot_outbound_plan_violations(
    *,
    plan: StrategicPlan,
    candidate: Any,
) -> tuple[str, ...]:
    planned_lines = plan.depot_outbound.temporary_line_by_no
    if not planned_lines:
        return ()
    violations: list[str] = []
    for line, nos in _put_nos_by_line(candidate).items():
        if line in physical.REMOTE_INTERACTION_LINES:
            continue
        for no in nos:
            if _depot_outbound_repack_staging_put_allowed(plan=plan, candidate=candidate, line=line, nos=nos):
                continue
            expected = planned_lines.get(no)
            if expected is None:
                continue
            if expected != line:
                violations.append(f"depot_outbound_plan_line_mismatch:{no}:{line}!={expected}")
    return tuple(violations)


def depot_inbound_plan_violations(
    *,
    plan: StrategicPlan,
    candidate: Any,
) -> tuple[str, ...]:
    inbound = plan.depot_inbound
    planned_lines = inbound.temporary_line_by_no
    if not inbound.inbound_nos:
        return ()
    violations: list[str] = []
    origin_line_by_no = _origin_lines_by_no(candidate)
    inbound_nos = set(inbound.inbound_nos)
    candidate_kind = getattr(candidate, "candidate_kind", "")
    is_assembly_candidate = candidate_kind in DEPOT_INBOUND_ASSEMBLY_OWNER_KINDS
    cleared_puts = _same_plan_cleared_put_nos_by_line(candidate)
    for line, nos in _put_nos_by_line(candidate).items():
        if line in depot_inbound_plan.ASSEMBLY_LINES:
            final_nos = tuple(no for no in nos if no not in cleared_puts.get(line, set()))
            if not final_nos:
                continue
            if _depot_cun4_exchange_outbound_put_allowed(plan=plan, candidate=candidate, line=line, nos=nos):
                continue
            if not is_assembly_candidate:
                if _depot_inbound_assembly_window_blocks(inbound=inbound, candidate_kind=candidate_kind):
                    for no in final_nos:
                        violations.append(f"depot_inbound_assembly_window_conflict:{no}:{line}")
                continue
            if line in _dirty_assembly_lines(plan):
                violations.append(f"depot_inbound_assembly_line_dirty:{line}")
            for no in final_nos:
                expected = planned_lines.get(no)
                if expected is None:
                    if no in inbound_nos:
                        violations.append(f"depot_inbound_plan_unassigned:{no}:{line}")
                        continue
                    violations.append(f"depot_inbound_assembly_purity_violation:{no}:{line}")
                    continue
                if _depot_inbound_assembly_owner_accepts(
                    candidate_kind=candidate_kind,
                    no=no,
                    line=line,
                    expected=expected,
                    inbound_nos=inbound_nos,
                ):
                    continue
                if expected != line:
                    violations.append(f"depot_inbound_plan_line_mismatch:{no}:{line}!={expected}")
            continue
        if line in physical.DEPOT_INBOUND_DESTINATION_LINES:
            for no in nos:
                expected = planned_lines.get(no)
                if expected is None:
                    if no in inbound_nos:
                        violations.append(f"depot_inbound_release_unassigned:{no}:{line}")
                    continue
                origin_line = origin_line_by_no.get(no, "")
                if origin_line not in depot_inbound_plan.ASSEMBLY_LINES:
                    violations.append(f"depot_inbound_release_without_assembly:{no}:{origin_line}->{line}")
                elif not inbound.assembly_complete:
                    violations.append(f"depot_inbound_release_before_assembly_complete:{no}:{line}")
            continue
    return tuple(violations)


def depot_inbound_support_gain(
    *,
    plan: StrategicPlan,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    candidate: Any,
) -> int:
    planned_lines = plan.depot_inbound.temporary_line_by_no
    if not planned_lines:
        return 0
    before_by_no = {physical.car_no(car): car for car in cars}
    after_by_no = {physical.car_no(car): car for car in prospective_cars}
    moved_nos = set(getattr(candidate, "move_car_nos", ()) or ())
    gain = 0
    for no, target_line in planned_lines.items():
        if no not in moved_nos:
            continue
        before = before_by_no.get(no)
        after = after_by_no.get(no)
        if before is None or after is None:
            continue
        if before["Line"] == target_line:
            continue
        if after["Line"] == target_line:
            gain += 1
    dirty_lines = set(plan.depot_inbound.purity_violation_lines)
    for no in set(plan.depot_inbound.purity_violation_nos) & moved_nos:
        before = before_by_no.get(no)
        after = after_by_no.get(no)
        if before is None or after is None:
            continue
        if before["Line"] not in dirty_lines:
            continue
        if after["Line"] not in depot_inbound_plan.ASSEMBLY_LINES:
            gain += 1
    return gain


def depot_outbound_support_gain(
    *,
    plan: StrategicPlan,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    candidate: Any,
) -> int:
    planned_lines = plan.depot_outbound.temporary_line_by_no
    if not planned_lines:
        return 0
    before_by_no = {physical.car_no(car): car for car in cars}
    after_by_no = {physical.car_no(car): car for car in prospective_cars}
    moved_nos = set(getattr(candidate, "move_car_nos", ()) or ())
    gain = 0
    for no, target_line in planned_lines.items():
        if no not in moved_nos:
            continue
        before = before_by_no.get(no)
        after = after_by_no.get(no)
        if before is None or after is None:
            continue
        if before["Line"] not in physical.REMOTE_INTERACTION_LINES:
            continue
        if after["Line"] == target_line:
            gain += 1
    return gain


def _put_nos_by_line(candidate: Any) -> dict[str, tuple[str, ...]]:
    by_line: dict[str, list[str]] = {}
    for step in physical.candidate_plan_steps(candidate):
        if step.action != "Put":
            continue
        by_line.setdefault(step.line, []).extend(step.move_car_nos)
    return {line: tuple(nos) for line, nos in by_line.items()}


def _same_plan_cleared_put_nos_by_line(candidate: Any) -> dict[str, set[str]]:
    steps = tuple(physical.candidate_plan_steps(candidate))
    later_get_index: dict[tuple[str, str], int] = {}
    for index, step in enumerate(steps):
        if step.action != "Get":
            continue
        for no in step.move_car_nos:
            later_get_index[(step.line, no)] = index

    cleared: dict[str, set[str]] = {}
    for index, step in enumerate(steps):
        if step.action != "Put":
            continue
        for no in step.move_car_nos:
            if later_get_index.get((step.line, no), -1) > index:
                cleared.setdefault(step.line, set()).add(no)
    return cleared


def _origin_lines_by_no(candidate: Any) -> dict[str, str]:
    origins: dict[str, str] = {}
    for step in physical.candidate_plan_steps(candidate):
        if step.action != "Get":
            continue
        for no in step.move_car_nos:
            origins.setdefault(no, step.line)
    return origins


def _dirty_assembly_lines(plan: StrategicPlan) -> set[str]:
    return set(plan.depot_inbound.purity_violation_lines)


def _depot_inbound_assembly_window_blocks(
    *,
    inbound: depot_inbound_plan.DepotInboundAssemblyPlan,
    candidate_kind: str,
) -> bool:
    if not inbound.inbound_nos:
        return False
    if candidate_kind in DEPOT_INBOUND_ASSEMBLY_OWNER_KINDS:
        return False
    if inbound.assembly_complete:
        return False
    if candidate_kind in DEPOT_INBOUND_TEMPORARY_CLEANOUT_KINDS:
        return False
    return True


def _depot_inbound_assembly_owner_accepts(
    *,
    candidate_kind: str,
    no: str,
    line: str,
    expected: str,
    inbound_nos: set[str],
) -> bool:
    if no not in inbound_nos:
        return False
    if (
        candidate_kind == "vnext_cun4_release_group_assembly"
        and line == "存4线"
        and expected == "存4线"
    ):
        return True
    return candidate_kind == "vnext_depot_inbound_assembly_rebalance" and line in depot_inbound_plan.ASSEMBLY_LINES


def _depot_cun4_exchange_outbound_put_allowed(
    *,
    plan: StrategicPlan,
    candidate: Any,
    line: str,
    nos: tuple[str, ...],
) -> bool:
    if getattr(candidate, "candidate_kind", "") not in {
        "vnext_depot_cun4_inbound_outbound_exchange",
        "vnext_depot_cun4_source_repack_exchange",
    }:
        return False
    if line != "存4线" or not nos:
        return False
    outbound_nos = set(plan.depot_outbound.cun4_nos)
    if not set(nos) <= outbound_nos:
        return False
    release_nos = set(plan.cun4_release.release_nos)
    if not release_nos:
        return False
    put_by_line = _put_nos_by_line(candidate)
    released_to_depot = {
        no
        for put_line, put_nos in put_by_line.items()
        if put_line in physical.DEPOT_INBOUND_DESTINATION_LINES
        for no in put_nos
    }
    return release_nos <= released_to_depot


def _depot_outbound_repack_staging_put_allowed(
    *,
    plan: StrategicPlan,
    candidate: Any,
    line: str,
    nos: tuple[str, ...],
) -> bool:
    if getattr(candidate, "candidate_kind", "") != "vnext_depot_cun4_source_repack_exchange":
        return False
    if line == "存4线" or not nos:
        return False
    outbound_nos = set(plan.depot_outbound.cun4_nos)
    if not set(nos) <= outbound_nos:
        return False
    final_cun4_nos = set(_put_nos_by_line(candidate).get("存4线", ()))
    return outbound_nos <= final_cun4_nos


def _front_line_rank(source_line: str) -> int:
    for index, line in enumerate(FRONT_TOPOLOGY_PRIORITY_LINES):
        if source_line == line:
            return index
    raise ValueError(f"line_not_in_front_topology_plan:{source_line}")
