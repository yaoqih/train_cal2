from __future__ import annotations

from typing import Any

from . import physical
from . import release
from . import serial
from .contracts import contract_debt
from .domain import CandidateEnvelope, ContractDelta, IntentKind


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
    opens_serial_gate_lease = envelope.intent == IntentKind.SERIAL_GATE_CLEAR and bool(serial_releases)
    if after_contract_debt > before_contract_debt and not opens_serial_gate_lease:
        broken.append("contract_debt_increased")
    if after_unsatisfied > before_unsatisfied and not opens_serial_gate_lease:
        broken.append("global_unsatisfied_increased")
    if serial_releases:
        support_gain = max(support_gain, len(serial_releases))
        reduced.append("serial_line_gate_released")
    if opens_serial_gate_lease:
        fulfilled.append("serial_gate_lease_opened")
        support_gain = max(support_gain, len(serial_releases) + max(0, after_unsatisfied - before_unsatisfied))
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
    if envelope.intent in {IntentKind.DEPOT_REPACK, IntentKind.DEPOT_SLOT_SWAP}:
        reduced.append("depot_repack_ordered" if envelope.intent == IntentKind.DEPOT_REPACK else "depot_slot_swap_ordered")
        if after_contract_debt < before_contract_debt:
            reduced.append("depot_contract_debt_reduced")
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
