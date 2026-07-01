from __future__ import annotations

from typing import Any

from . import legacy_adapter as legacy
from . import serial
from .contracts import contract_debt
from .domain import CandidateEnvelope, ContractDelta, IntentKind


def simulate_candidate(candidate: Any, cars: list[dict[str, Any]], validation: Any) -> list[dict[str, Any]]:
    prospective = [dict(car) for car in cars]
    legacy.legacy.apply_candidate(candidate, prospective, validation)
    return prospective


def build_contract_delta(
    envelope: CandidateEnvelope,
    *,
    cars: list[dict[str, Any]],
    prospective_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> ContractDelta:
    before_unsatisfied = len(legacy.unsatisfied_cars(cars, depot_assignment))
    after_unsatisfied = len(legacy.unsatisfied_cars(prospective_cars, depot_assignment))
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
    if envelope.intent == IntentKind.BLOCKER_CLEAR:
        support_gain = max(1, before_contract_debt - after_contract_debt)
        reduced.append("source_front_blocker_cleared")
    if envelope.intent == IntentKind.SOURCE_CLEAR_RESTORE:
        if after_contract_debt < before_contract_debt:
            reduced.append("source_front_clear_restore_delivered")
        if envelope.resource_request.restored_borrowed_blockers:
            fulfilled.append("source_front_blocker_restored")
    if envelope.intent in {IntentKind.BORROWED_BLOCKER_CLEAR, IntentKind.DEPOT_OUTER_CLEAR}:
        support_gain = 1
        reduced.append("borrowed_source_front_blocker_cleared")
        if envelope.resource_request.borrowed_blockers:
            added.append("restore_borrowed_blocker")
        if envelope.resource_request.restored_borrowed_blockers:
            fulfilled.append("borrowed_blocker_restored")
    if serial_releases:
        support_gain = max(support_gain, len(serial_releases))
        reduced.append("serial_line_gate_released")
    if envelope.intent == IntentKind.DEPOT_REPACK:
        reduced.append("depot_repack_ordered")
        if after_contract_debt < before_contract_debt:
            reduced.append("depot_contract_debt_reduced")
        if envelope.resource_request.restored_borrowed_blockers:
            fulfilled.append("source_blocker_restored")
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
            legacy.car_no(car)
            for car in cars
            if car["Line"] == blocker_line
        }
        if not before_nos or not before_nos <= move_nos:
            continue
        after_nos = {
            legacy.car_no(car)
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
