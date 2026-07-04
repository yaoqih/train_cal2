from __future__ import annotations

from typing import Any

from . import physical
from . import release
from . import serial
from . import strategic_plan as strategic
from .contracts import contract_debt
from .domain import CandidateEnvelope, ContractDelta, IntentKind


_INNER_TARGET_SEGMENT_GAIN = 1


def simulate_candidate(candidate: Any, cars: list[dict[str, Any]], validation: Any) -> list[dict[str, Any]]:
    prospective = [dict(car) for car in cars]
    physical.apply_candidate(candidate, prospective, validation)
    return prospective


def build_contract_delta(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
    strategic_plan: Any | None = None,
) -> ContractDelta:
    before_unsatisfied = len(physical.unsatisfied_cars(cars, depot_assignment))
    after_unsatisfied = len(physical.unsatisfied_cars(prospective_cars, depot_assignment))
    before_contract_debt = contract_debt(envelope.contract, cars, depot_assignment)
    after_contract_debt = contract_debt(envelope.contract, prospective_cars, depot_assignment)
    fulfilled: list[str] = []
    reduced: list[str] = []
    broken: list[str] = []
    added: list[str] = []
    support_gain = 0
    if after_contract_debt == 0 and before_contract_debt > 0:
        fulfilled.append("contract_debt_complete")
    if after_contract_debt < before_contract_debt:
        reduced.append("contract_debt_reduced")
    serial_releases = _serial_blocker_releases(
        envelope,
        cars=cars,
        prospective_cars=prospective_cars,
        depot_assignment=depot_assignment,
    )
    if after_contract_debt > before_contract_debt:
        broken.append("contract_debt_increased")
    if after_unsatisfied > before_unsatisfied:
        broken.append("global_unsatisfied_increased")
    if serial_releases:
        support_gain = max(support_gain, len(serial_releases))
        reduced.append("serial_line_gate_released")
    side_target_gain = _side_target_completion_gain(
        envelope,
        cars=cars,
        prospective_cars=prospective_cars,
        depot_assignment=depot_assignment,
    )
    inner_segment_gain = _inner_target_segment_gain(
        envelope,
        cars=cars,
        prospective_cars=prospective_cars,
        depot_assignment=depot_assignment,
    )
    if side_target_gain:
        support_gain = max(support_gain, side_target_gain)
        reduced.append("side_target_completion")
    if inner_segment_gain:
        support_gain = max(support_gain, _INNER_TARGET_SEGMENT_GAIN)
        reduced.append("inner_target_segment_extended")
    if envelope.resource_request.same_plan_source_return_nos:
        fulfilled.append("same_plan_prefix_returned_to_source")
    if envelope.intent == IntentKind.CUN4_RELEASE_GROUP:
        before_release_count = release.cun4_release_group_count(cars, depot_assignment)
        after_release_count = release.cun4_release_group_count(prospective_cars, depot_assignment)
        if after_release_count > before_release_count:
            support_gain = max(support_gain, after_release_count - before_release_count)
            reduced.append("cun4_release_group_formed")
            fulfilled.append("temporary_cun4_release_group_owner_bound")
    if envelope.intent == IntentKind.CUN4_RELEASE_ACCEPT:
        before_release_count = release.cun4_release_group_count(cars, depot_assignment)
        after_release_count = release.cun4_release_group_count(prospective_cars, depot_assignment)
        if before_release_count > after_release_count:
            reduced.append("cun4_release_group_released")
    if envelope.intent == IntentKind.DEPOT_SLOT_SWAP:
        reduced.append("depot_slot_swap_ordered")
        if after_contract_debt < before_contract_debt:
            reduced.append("depot_contract_debt_reduced")
    if strategic_plan is not None and envelope.intent == IntentKind.CUN4_OUTBOUND_HOLD:
        assembly_gain = strategic.depot_outbound_support_gain(
            plan=strategic_plan,
            cars=cars,
            prospective_cars=prospective_cars,
            candidate=envelope.candidate,
        )
        if assembly_gain:
            support_gain = max(support_gain, assembly_gain)
            reduced.append("depot_outbound_temporary_group_formed")
            fulfilled.append("depot_outbound_assembly_plan_owner_bound")
    if strategic_plan is not None and envelope.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY:
        assembly_gain = strategic.depot_inbound_support_gain(
            plan=strategic_plan,
            cars=cars,
            prospective_cars=prospective_cars,
            candidate=envelope.candidate,
        )
        if assembly_gain:
            support_gain = max(support_gain, assembly_gain)
            reduced.append("depot_inbound_temporary_group_formed")
            fulfilled.append("depot_inbound_assembly_plan_owner_bound")
    if strategic_plan is not None:
        cun4_open_gain = _cun4_open_gain(envelope, cars=cars, prospective_cars=prospective_cars)
        if cun4_open_gain:
            support_gain = max(support_gain, cun4_open_gain)
            reduced.append("cun4_opened_for_depot_outbound")
    return ContractDelta(
        contract_id=envelope.contract.contract_id,
        family=envelope.contract.family,
        before_unsatisfied=before_unsatisfied,
        after_unsatisfied=after_unsatisfied,
        before_contract_debt=before_contract_debt,
        after_contract_debt=after_contract_debt,
        fulfilled=tuple(fulfilled),
        reduced=tuple(reduced),
        broken=tuple(broken),
        added=tuple(added),
        support_gain=support_gain,
    )


def _cun4_open_gain(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
) -> int:
    if getattr(envelope.candidate, "candidate_kind", "") != "vnext_depot_inbound_cun4_open_release":
        return 0
    moved = set(envelope.candidate.move_car_nos)
    before_by_no = {physical.car_no(car): car for car in cars}
    after_by_no = {physical.car_no(car): car for car in prospective_cars}
    if not moved:
        return 0
    if not moved <= set(before_by_no) or not moved <= set(after_by_no):
        return 0
    return sum(
        1
        for no in moved
        if before_by_no[no]["Line"] == "存4线" and after_by_no[no]["Line"] != "存4线"
    )


def _side_target_completion_gain(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> int:
    moved = set(envelope.candidate.move_car_nos)
    contract_nos = set(envelope.contract.subject_nos)
    side_nos = moved - contract_nos
    if not side_nos:
        return 0
    before_by_no = {physical.car_no(car): car for car in cars}
    after_by_no = {physical.car_no(car): car for car in prospective_cars}
    return sum(
        1
        for no in side_nos
        if no in before_by_no
        and no in after_by_no
        and not physical.car_is_satisfied(before_by_no[no], depot_assignment, cars)
        and physical.car_is_satisfied(after_by_no[no], depot_assignment, prospective_cars)
    )


def _inner_target_segment_gain(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> int:
    """Return a light signal when a non-temporary line's south-end target segment grows."""
    moved = set(envelope.candidate.move_car_nos)
    if not moved:
        return 0
    touched_lines = {
        step.line
        for step in physical.candidate_plan_steps(envelope.candidate)
        if step.action in {"Get", "Put"}
    }
    for line in sorted(touched_lines):
        spec = physical.TRACK_SPECS.get(line)
        if spec is None or spec.track_type == "temporary" or line in physical.RUNNING_LINES:
            continue
        before_count = _inner_target_segment_length(
            cars=cars,
            line=line,
            depot_assignment=depot_assignment,
        )
        after_count = _inner_target_segment_length(
            cars=prospective_cars,
            line=line,
            depot_assignment=depot_assignment,
        )
        if after_count > before_count:
            return 1
    return 0


def _inner_target_segment_length(
    *,
    cars: list[dict[str, Any]],
    line: str,
    depot_assignment: Any,
) -> int:
    # Position 1 is the north access end.  Scan from the south/inner end so a
    # held north prefix does not hide a correctly formed inner target segment.
    line_cars = sorted(
        (car for car in cars if car["Line"] == line),
        key=lambda car: (int(car.get("Position") or 0), physical.car_no(car)),
        reverse=True,
    )
    if not line_cars:
        return 0
    loads = physical.line_loads(cars)
    count = 0
    for car in line_cars:
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if target_line != line:
            break
        count += 1
    return count


def _serial_blocker_releases(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[str, ...]:
    move_nos = set(envelope.resource_request.move_nos)
    releases: list[str] = []
    for blocker_line in serial.serial_blocker_lines():
        before_nos = {
            physical.car_no(car)
            for car in cars
            if car["Line"] == blocker_line
        }
        if not before_nos or not before_nos <= move_nos:
            continue
        after_nos = {
            physical.car_no(car)
            for car in prospective_cars
            if car["Line"] == blocker_line
        }
        if after_nos:
            continue
        debt_nos = serial.downstream_debt_nos(
            blocker_line=blocker_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos=move_nos,
        )
        if debt_nos:
            releases.append(blocker_line)
    return tuple(releases)
