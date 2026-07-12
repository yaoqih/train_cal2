from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Iterable, Mapping

from solver_vnext import physical


@dataclass(frozen=True)
class Block:
    line: str
    nos: tuple[str, ...]
    target: str
    active: bool
    protected: bool
    forced_positions: tuple[int, ...]
    equivalent: int
    length_m: float


@dataclass(frozen=True)
class AccessAlternative:
    subject: Block
    preceding: tuple[Block, ...]


@dataclass(frozen=True)
class TargetWindow:
    line: str
    existing: tuple[str, ...]
    inbound: tuple[str, ...]
    outbound: tuple[str, ...]
    source_runs: tuple[tuple[str, tuple[str, ...]], ...]
    source_inversions: int
    capacity_rounds: int
    estimated_hooks: int

    @property
    def dirty(self) -> bool:
        return bool(self.outbound)

@dataclass(frozen=True)
class FlowDiagnostics:
    blocks: tuple[Block, ...]
    alternatives: tuple[AccessAlternative, ...]
    windows: tuple[TargetWindow, ...]
    relaxed_hook_estimate: int


class FlowModel:
    """State-derived block and target-window relaxation.

    Cars remain the source of truth. Blocks are rebuilt after every transition,
    so grouping never hides a changed access order or target assignment.
    """

    def __init__(
        self,
        cars: list[dict],
        target_by_no: Mapping[str, str],
        active_nos: Iterable[str],
        protected_nos: Iterable[str],
        depot_assignment: physical.DepotAssignment,
    ) -> None:
        self.cars = cars
        self.target_by_no = dict(target_by_no)
        self.active_nos = frozenset(active_nos)
        self.protected_nos = frozenset(protected_nos)
        self.depot_assignment = depot_assignment
        self.by_no = {physical.car_no(car): car for car in cars}
        self.unsatisfied = frozenset(
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, depot_assignment)
        )
        self.pending = self.unsatisfied & self.active_nos

    def block_key(self, no: str, line: str) -> tuple:
        car = self.by_no[no]
        active = no in self.pending
        protected = no in self.protected_nos
        target = self.target_by_no.get(no, line) if active else line
        return (
            target,
            active,
            protected,
            physical.force_positions(car),
            bool(car.get("IsHeavy")),
            bool(car.get("IsWeigh") and not car.get("_Weighed")),
        )

    def blocks_on(self, line: str) -> tuple[Block, ...]:
        order = physical.line_access_order(self.cars, line)
        groups: list[list[str]] = []
        keys: list[tuple] = []
        for no in order:
            key = self.block_key(no, line)
            if keys and keys[-1] == key:
                groups[-1].append(no)
            else:
                keys.append(key)
                groups.append([no])
        return tuple(
            Block(
                line=line,
                nos=tuple(group),
                target=key[0],
                active=bool(key[1]),
                protected=bool(key[2]),
                forced_positions=tuple(key[3]),
                equivalent=physical.pull_equivalent([self.by_no[no] for no in group]),
                length_m=round(sum(physical.car_length(self.by_no[no]) for no in group), 3),
            )
            for group, key in zip(groups, keys)
        )

    def blocks(self) -> tuple[Block, ...]:
        lines = sorted({car.get("Line") or "" for car in self.cars if car.get("Line")})
        return tuple(block for line in lines for block in self.blocks_on(line))

    def access_alternatives(self) -> tuple[AccessAlternative, ...]:
        alternatives: list[AccessAlternative] = []
        for line in sorted({self.by_no[no].get("Line") or "" for no in self.pending}):
            preceding: list[Block] = []
            for block in self.blocks_on(line):
                if block.active:
                    alternatives.append(AccessAlternative(block, tuple(preceding)))
                preceding.append(block)
        return tuple(alternatives)

    def target_positions(self, target: str, participants: tuple[str, ...]) -> dict[str, int]:
        planning = [dict(car) for car in self.cars]
        participant_set = set(participants)
        target_order = tuple(physical.line_access_order(planning, target))
        if target_order:
            physical.apply_physical_get_order(planning, target, target_order)
        for line in sorted({
            self.by_no[no].get("Line") or ""
            for no in participants
            if (self.by_no[no].get("Line") or "") != target
        }):
            group = tuple(
                no
                for no in physical.line_access_order(planning, line)
                if no in participant_set
            )
            if group:
                physical.apply_physical_get_order(planning, line, group)
        return physical.planned_positions_for_batch(
            batch=[self.by_no[no] for no in participants],
            target_line=target,
            cars=planning,
            depot_assignment=self.depot_assignment,
            batch_nos=participant_set,
        )

    def target_window(self, target: str) -> TargetWindow:
        existing = tuple(physical.line_access_order(self.cars, target))
        inbound = tuple(sorted(
            (
                no
                for no in self.pending
                if self.target_by_no.get(no) == target
                and (self.by_no.get(no) or {}).get("Line") != target
            ),
            key=lambda no: (
                (self.by_no.get(no) or {}).get("Line") or "",
                int((self.by_no.get(no) or {}).get("Position") or 0),
                no,
            ),
        ))
        outbound = tuple(
            no
            for no in existing
            if no in self.pending and self.target_by_no.get(no) != target
        )
        by_source: dict[str, list[str]] = defaultdict(list)
        for no in inbound:
            by_source[self.by_no[no]["Line"]].append(no)

        participants = tuple(dict.fromkeys((*existing, *inbound)))
        positions = self.target_positions(target, participants) if participants else {}
        source_runs: list[tuple[str, tuple[str, ...]]] = []
        inversions = 0
        for source, nos in sorted(by_source.items()):
            ordered = tuple(
                no
                for no in physical.line_access_order(self.cars, source)
                if no in set(nos)
            )
            source_runs.append((source, ordered))
            desired = [positions.get(no, 0) for no in ordered]
            inversions += sum(current <= previous for previous, current in zip(desired, desired[1:]))

        total_equivalent = physical.pull_equivalent(
            [self.by_no[no] for no in participants if no in self.by_no]
        )
        rounds = max(1, ceil(total_equivalent / physical.PULL_LIMIT_EQUIVALENT)) if inbound else 0
        source_gets = len(by_source)
        target_get = int(bool(existing) and bool(inbound))
        target_put = int(bool(inbound) or bool(outbound))
        lower_bound = source_gets + target_get + target_put + inversions
        return TargetWindow(
            line=target,
            existing=existing,
            inbound=inbound,
            outbound=outbound,
            source_runs=tuple(source_runs),
            source_inversions=inversions,
            capacity_rounds=rounds,
            estimated_hooks=lower_bound,
        )

    def windows(self) -> tuple[TargetWindow, ...]:
        targets = sorted({self.target_by_no.get(no, "") for no in self.pending if self.target_by_no.get(no)})
        return tuple(self.target_window(target) for target in targets)

    def relaxed_hook_estimate(self) -> int:
        """Estimate remaining work while preserving block order and target windows.

        This value ranks and explains constructions. It is deliberately not used
        as an admissible optimality bound because route and retained-consist
        interactions can share operations across windows.
        """
        if not self.pending:
            return 0
        source_lines = {
            self.by_no[no].get("Line") or ""
            for no in self.pending
            if (self.by_no[no].get("Line") or "") != self.target_by_no.get(no, "")
        }
        target_lines = {
            self.target_by_no.get(no, "")
            for no in self.pending
            if self.target_by_no.get(no, "")
            and (self.by_no[no].get("Line") or "") != self.target_by_no.get(no, "")
        }
        basic = len(source_lines) + len(target_lines)
        window_bound = sum(
            int(window.dirty) + window.source_inversions
            for window in self.windows()
        )
        return basic + window_bound

    def diagnostics(self) -> FlowDiagnostics:
        return FlowDiagnostics(
            blocks=self.blocks(),
            alternatives=self.access_alternatives(),
            windows=self.windows(),
            relaxed_hook_estimate=self.relaxed_hook_estimate(),
        )
