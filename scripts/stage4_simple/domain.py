from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping

from solver_vnext import physical


class ContractStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"


class DepotRehookMode(str, Enum):
    NOT_REQUIRED = "not_required"
    C4_ONLY = "c4_only"
    DIRECT = "direct"
    TARGET_REBUILD = "target_rebuild"
    OUTBOUND_DEPENDENCY = "outbound_dependency"
    PREFIX_DEPENDENCY = "prefix_dependency"
    BATCHED = "batched"


@dataclass(frozen=True)
class CarrySegment:
    owner: str
    nos: tuple[str, ...]
    ranks: tuple[int, ...] = ()
    protected: bool = False

    @property
    def rank_interval(self) -> tuple[int, int] | None:
        if not self.ranks or any(rank <= 0 for rank in self.ranks):
            return None
        return self.ranks[0], self.ranks[-1]

    def can_prepend(self, other: "CarrySegment") -> bool:
        if self.owner != other.owner or self.protected != other.protected:
            return False
        left = other.rank_interval
        right = self.rank_interval
        if left is None or right is None:
            return False
        return left[1] + 1 == right[0]


@dataclass(frozen=True)
class OwnedStack:
    line: str
    segments: tuple[CarrySegment, ...]

    @property
    def nos(self) -> tuple[str, ...]:
        return tuple(no for segment in self.segments for no in segment.nos)

    @property
    def owner(self) -> str:
        owners = {segment.owner for segment in self.segments}
        return next(iter(owners)) if len(owners) == 1 else ""

    def prepend(self, segment: CarrySegment) -> "OwnedStack" | None:
        if not self.segments:
            return OwnedStack(self.line, (segment,))
        first = self.segments[0]
        if first.owner != segment.owner:
            return None
        first_interval = first.rank_interval
        segment_interval = segment.rank_interval
        if (first_interval is None) != (segment_interval is None):
            return None
        if first_interval is not None and not first.can_prepend(segment):
            return None
        return OwnedStack(self.line, (segment, *self.segments))

    def consume(self, move: tuple[str, ...]) -> "OwnedStack" | None:
        if self.nos[: len(move)] != move:
            raise ValueError(f"stack prefix mismatch:{self.line}")
        remaining = len(move)
        segments = list(self.segments)
        while remaining:
            segment = segments.pop(0)
            if remaining < len(segment.nos):
                segments.insert(0, replace(
                    segment,
                    nos=segment.nos[remaining:],
                    ranks=segment.ranks[remaining:] if segment.ranks else (),
                ))
                remaining = 0
            else:
                remaining -= len(segment.nos)
        return OwnedStack(self.line, tuple(segments)) if segments else None


@dataclass(frozen=True)
class RecoveryLease:
    line: str
    owner: str
    nos: tuple[str, ...]
    gate_footprint: frozenset[str]


@dataclass(frozen=True)
class FlowContract:
    contract_id: str
    subjects: tuple[str, ...]
    target: str
    predecessors: frozenset[str] = frozenset()
    status: ContractStatus = ContractStatus.PENDING


@dataclass(frozen=True)
class DepotRehookContract:
    mode: DepotRehookMode
    c4_backbone: tuple[str, ...]
    paint_tail: tuple[str, ...]
    unload_prefix: tuple[str, ...]
    paint_outbound: tuple[str, ...]


@dataclass(frozen=True)
class ContractGraph:
    contracts: tuple[FlowContract, ...]

    def by_id(self) -> Mapping[str, FlowContract]:
        return {contract.contract_id: contract for contract in self.contracts}

    def ready(self) -> tuple[FlowContract, ...]:
        by_id = self.by_id()
        return tuple(
            contract
            for contract in self.contracts
            if contract.status == ContractStatus.PENDING
            and all(
                by_id[predecessor].status == ContractStatus.CLOSED
                for predecessor in contract.predecessors
            )
        )

    def activate(self, contract_id: str) -> "ContractGraph":
        return self._set_status(contract_id, ContractStatus.ACTIVE)

    def close(self, contract_id: str) -> "ContractGraph":
        return self._set_status(contract_id, ContractStatus.CLOSED)

    def _set_status(
        self,
        contract_id: str,
        status: ContractStatus,
    ) -> "ContractGraph":
        if contract_id not in self.by_id():
            raise KeyError(contract_id)
        return ContractGraph(tuple(
            replace(contract, status=status)
            if contract.contract_id == contract_id
            else contract
            for contract in self.contracts
        ))


def segment_for(
    nos: Iterable[str],
    *,
    owner: str,
    rank_by_no: Mapping[str, int],
    protected: bool = False,
) -> CarrySegment:
    ordered = tuple(nos)
    return CarrySegment(
        owner=owner,
        nos=ordered,
        ranks=tuple(rank_by_no.get(no, 0) for no in ordered),
        protected=protected,
    )


def segment_carry(
    carried_order: tuple[str, ...],
    owner_by_no: Mapping[str, str],
    rank_by_no: Mapping[str, int],
    protected_nos: frozenset[str],
) -> tuple[CarrySegment, ...]:
    segments: list[CarrySegment] = []
    for no in carried_order:
        owner = owner_by_no[no]
        rank = rank_by_no.get(no, 0)
        protected = no in protected_nos
        if segments:
            previous = segments[-1]
            contiguous = (
                not previous.ranks
                or not rank
                or previous.ranks[-1] + 1 == rank
            )
            if (
                previous.owner == owner
                and previous.protected == protected
                and contiguous
            ):
                segments[-1] = replace(
                    previous,
                    nos=(*previous.nos, no),
                    ranks=(*previous.ranks, rank),
                )
                continue
        segments.append(CarrySegment(owner, (no,), (rank,), protected))
    return tuple(segments)


def lease_length(cars_by_no: Mapping[str, dict], nos: Iterable[str]) -> float:
    return round(sum(physical.car_length(cars_by_no[no]) for no in nos), 3)
