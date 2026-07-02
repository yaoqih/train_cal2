from __future__ import annotations

from typing import Any

from . import physical
from .contracts import contract_debt
from .domain import ContractFamily, FlowContract, RemotePrefixLease


REMOTE_PREFIX_FAMILIES = {
    ContractFamily.REMOTE_SESSION,
    ContractFamily.REPAIR_INBOUND,
    ContractFamily.DEPOT_SLOT,
    ContractFamily.DEPOT_OUTBOUND,
}


def lease_key(source_line: str) -> str:
    return source_line


def eligible_contract(contract: FlowContract) -> bool:
    return contract.family in REMOTE_PREFIX_FAMILIES


def open_debt_nos(
    contract: FlowContract,
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    moving_nos: set[str],
) -> tuple[str, ...]:
    if not eligible_contract(contract):
        return ()
    by_no = {physical.car_no(car): car for car in cars}
    debt: list[str] = []
    for no in contract.subject_nos:
        car = by_no.get(no)
        if not car or no in moving_nos:
            continue
        if physical.car_is_satisfied(car, depot_assignment, cars):
            continue
        debt.append(no)
    return tuple(debt)


def build_lease(
    *,
    case_id: str,
    hook_index: int,
    contract: FlowContract,
    source_line: str,
    staging_line: str,
    blocker_nos: tuple[str, ...],
    restore_positions: dict[str, int],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> RemotePrefixLease | None:
    debt_nos = open_debt_nos(
        contract,
        cars=cars,
        depot_assignment=depot_assignment,
        moving_nos=set(blocker_nos),
    )
    if not debt_nos:
        return None
    return RemotePrefixLease(
        lease_id=f"{case_id}:{source_line}:{hook_index}",
        owner_contract_id=contract.contract_id,
        source_line=source_line,
        staging_line=staging_line,
        opened_hook=hook_index,
        blocker_nos=blocker_nos,
        debt_nos=debt_nos,
        restore_positions=tuple(sorted(restore_positions.items())),
    )


def opened_by_candidate(envelope: Any) -> bool:
    return envelope.intent.value == "REMOTE_PREFIX_LEASE"


def served_debt_count(
    lease: RemotePrefixLease,
    *,
    before_cars: list[dict[str, Any]],
    after_cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> int:
    before = _unsatisfied_subset(lease.debt_nos, before_cars, depot_assignment)
    after = _unsatisfied_subset(lease.debt_nos, after_cars, depot_assignment)
    return max(0, len(before) - len(after))


def remaining_debt_nos(
    lease: RemotePrefixLease,
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[str, ...]:
    return tuple(sorted(_unsatisfied_subset(lease.debt_nos, cars, depot_assignment)))


def blockers_on_staging(lease: RemotePrefixLease, cars: list[dict[str, Any]]) -> tuple[str, ...]:
    by_no = {physical.car_no(car): car for car in cars}
    return tuple(no for no in lease.blocker_nos if by_no.get(no, {}).get("Line") == lease.staging_line)


def blockers_on_source(lease: RemotePrefixLease, cars: list[dict[str, Any]]) -> tuple[str, ...]:
    by_no = {physical.car_no(car): car for car in cars}
    return tuple(no for no in lease.blocker_nos if by_no.get(no, {}).get("Line") == lease.source_line)


def source_is_reblocked(lease: RemotePrefixLease, cars: list[dict[str, Any]]) -> bool:
    return bool(blockers_on_source(lease, cars))


def lease_has_owner_debt(
    lease: RemotePrefixLease,
    contract: FlowContract,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> bool:
    if not eligible_contract(contract):
        return False
    if set(contract.subject_nos).isdisjoint(lease.debt_nos):
        return False
    return contract_debt(contract, cars, depot_assignment) > 0


def _unsatisfied_subset(
    nos: tuple[str, ...],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> set[str]:
    by_no = {physical.car_no(car): car for car in cars}
    return {
        no
        for no in nos
        if no in by_no and not physical.car_is_satisfied(by_no[no], depot_assignment, cars)
    }
