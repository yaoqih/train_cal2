from __future__ import annotations

import time
from dataclasses import dataclass

from solver_vnext import physical

from .contracts import (
    DEPOT_REHOOK_ID,
    SERVICE_TARGETS,
    build_contract_graph,
    classify_depot_rehook,
)
from .domain import (
    CarrySegment,
    ContractGraph,
    DepotRehookMode,
    OwnedStack,
    RecoveryLease,
    lease_length,
    segment_carry,
    segment_for,
)
from .search import OperationTransitions, SearchNode, Stage4Problem
from .topology import resource_gate_closure


STAGING_TYPES = frozenset({"storage", "temporary"})
STAGING_EXCLUDED = physical.DEPOT_TARGET_LINES | {"卸轮线", "存4南"}
STAGING_RISK = {
    "洗油北": 4,
    "机北1": 3,
    "机北2": 3,
    "机走北": 2,
    "调梁线北": 2,
    "洗罐线北": 2,
    "机南": 1,
}
STAGING_EXIT_RISK = {
    "存1线": 0,
    "存2线": 1,
    "存3线": 2,
    "存4线": 3,
    "存5线南": 4,
    "存5线北": 6,
}
@dataclass(frozen=True)
class PlanningConfig:
    time_budget_seconds: float = 300.0
    max_expansions: int = 20_000


@dataclass(frozen=True)
class TargetSegment:
    target: str
    nos: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryIntent:
    next_line: str
    projected_nos: tuple[str, ...]


@dataclass(frozen=True)
class PlanningResult:
    node: SearchNode
    complete: bool
    reason: str
    trace: tuple[dict, ...]
    contracts: ContractGraph
    leases: tuple[RecoveryLease, ...]
    expansions: int
    elapsed_seconds: float


@dataclass(frozen=True)
class PlanningCheckpoint:
    node: SearchNode
    contracts: ContractGraph
    stacks: tuple[tuple[str, OwnedStack], ...]
    leases: tuple[RecoveryLease, ...]
    trace: tuple[dict, ...]
    expansions: int
    goal_owners: tuple[tuple[str, str], ...]


class PlanningFailure(RuntimeError):
    pass


class PlanBuilder:
    def __init__(
        self,
        problem: Stage4Problem,
        transitions: OperationTransitions,
        contracts: ContractGraph,
        *,
        config: PlanningConfig,
    ) -> None:
        self.problem = problem
        self.transitions = transitions
        self.contracts = contracts
        self.config = config
        self.node = SearchNode(physical.initial_planlet_state(
            problem.cars,
            problem.loco_location,
        ))
        self.initial_line_by_no = {
            physical.car_no(car): str(car.get("Line") or "")
            for car in problem.cars
        }
        self.stacks: dict[str, OwnedStack] = {}
        self.leases: list[RecoveryLease] = []
        self.trace: list[dict] = []
        self.expansions = 0
        self.started = time.monotonic()
        self.goal_owner_by_no: dict[str, str] = {}
        self.holdout_parking = self._reserve_holdout_parking()

    @property
    def cars(self) -> list[dict]:
        return list(self.node.state.cars)

    @property
    def by_no(self) -> dict[str, dict]:
        return {physical.car_no(car): car for car in self.node.state.cars}

    @property
    def carry(self) -> tuple[str, ...]:
        return self.node.state.carried_order

    def check_budget(self) -> None:
        if self.expansions >= self.config.max_expansions:
            raise PlanningFailure("search_incomplete:expansion_budget")
        if time.monotonic() - self.started >= self.config.time_budget_seconds:
            raise PlanningFailure("search_incomplete:time_budget")

    def owner(self, no: str) -> str:
        if no in self.goal_owner_by_no:
            return self.goal_owner_by_no[no]
        target = self.problem.target_by_no.get(no, "")
        if no in self.problem.active_nos and target:
            return target
        return f"restore:{self.initial_line_by_no.get(no, '')}"

    def source_line(self, no: str) -> str:
        current = str(self.by_no[no].get("Line") or "")
        if current:
            return current
        return dict(self.node.carry_origins).get(
            no,
            self.initial_line_by_no.get(no, ""),
        )

    def _reserve_holdout_parking(self) -> dict[str, str]:
        if not self.problem.capacity_holdout_nos:
            return {}
        cars = list(self.problem.cars)
        by_no = self.problem.by_no
        pending_sources = {
            str(by_no[no].get("Line") or "") for no in self.problem.active_nos
            if by_no[no].get("Line")
        }
        pending_targets = {
            self.problem.target_by_no.get(no, "") for no in self.problem.active_nos
            if self.problem.target_by_no.get(no)
        }
        reserved_gates = set().union(*(
            resource_gate_closure(target) for target in pending_targets
        )) if pending_targets else set()
        reservations: dict[str, str] = {}
        reserved_length: dict[str, float] = {}
        for no in sorted(self.problem.capacity_holdout_nos):
            candidates = []
            for line, spec in physical.TRACK_SPECS.items():
                if spec.track_type != "storage" or line in STAGING_EXCLUDED:
                    continue
                if line in pending_sources or line in pending_targets or line in reserved_gates:
                    continue
                if line in {by_no[no].get("Line"), self.problem.target_by_no.get(no)}:
                    continue
                used = sum(
                    physical.car_length(car) for car in cars
                    if car.get("Line") == line
                ) + reserved_length.get(line, 0.0)
                remaining = spec.length_m - used - physical.car_length(by_no[no])
                if remaining + physical.LINE_LENGTH_TOLERANCE_M < 0:
                    continue
                candidates.append((-remaining, line))
            if not candidates:
                raise ValueError(f"capacity_holdout_parking_missing:{no}")
            _remaining, line = min(candidates)
            reservations[no] = line
            reserved_length[line] = (
                reserved_length.get(line, 0.0) + physical.car_length(by_no[no])
            )
        return reservations

    def carry_segments(self) -> tuple[CarrySegment, ...]:
        return segment_carry(
            self.carry,
            {no: self.owner(no) for no in self.carry},
            self.problem.final_rank_by_no,
            self.problem.protected_nos,
        )

    def checkpoint(self) -> PlanningCheckpoint:
        return PlanningCheckpoint(
            node=self.node,
            contracts=self.contracts,
            stacks=tuple(sorted(self.stacks.items())),
            leases=tuple(self.leases),
            trace=tuple(self.trace),
            expansions=self.expansions,
            goal_owners=tuple(sorted(self.goal_owner_by_no.items())),
        )

    def restore(self, checkpoint: PlanningCheckpoint) -> None:
        self.node = checkpoint.node
        self.contracts = checkpoint.contracts
        self.stacks = dict(checkpoint.stacks)
        self.leases = list(checkpoint.leases)
        self.trace = list(checkpoint.trace)
        self.expansions = checkpoint.expansions
        self.goal_owner_by_no = dict(checkpoint.goal_owners)

    def positions(self, line: str, move: tuple[str, ...]) -> dict[str, int] | None:
        return self.transitions.planned_positions(self.node.state, line, move)

    def probe(self, action: str, line: str, move: tuple[str, ...]) -> SearchNode | None:
        self.check_budget()
        positions = self.positions(line, move) if action == "Put" else {}
        if positions is None:
            return None
        self.expansions += 1
        return self.transitions.apply_step(
            self.node,
            physical.plan_step(action, line, move, positions),
        )

    def apply(
        self,
        action: str,
        line: str,
        move: tuple[str, ...],
        *,
        event: str,
        successor: SearchNode | None = None,
        **extra: object,
    ) -> None:
        successor = successor or self.probe(action, line, move)
        if successor is None:
            raise PlanningFailure(
                f"physical_edge_missing:{action}:{line}:{','.join(move)}"
            )
        self.node = successor
        if action == "Get":
            self._consume_stack(line, move)
        self.trace.append({
            "event": event,
            "action": action,
            "line": line,
            "move": list(move),
            "carry": list(self.carry),
            "hooks": self.node.cost.hooks,
            **extra,
        })

    def get(self, line: str, move: tuple[str, ...], *, event: str) -> None:
        self.apply("Get", line, move, event=event)

    def put_target(self, line: str, move: tuple[str, ...], *, event: str) -> None:
        self.apply("Put", line, move, event=event)

    def stage(
        self,
        segment: CarrySegment,
        *,
        forbidden_lines: frozenset[str] = frozenset(),
        recovery: RecoveryIntent | None = None,
    ) -> None:
        candidates = self.staging_candidates(segment, forbidden_lines, recovery)
        if not candidates:
            raise PlanningFailure(
                f"staging_edge_missing:{segment.owner}:{','.join(segment.nos)}"
            )
        _rank, line, successor = candidates[0]
        previous = self.stacks.get(line)
        if previous:
            stack = previous.prepend(segment)
            if stack is None:
                raise AssertionError("ranked staging candidate changed")
        else:
            stack = OwnedStack(line, (segment,))
        self.apply(
            "Put",
            line,
            segment.nos,
            event="stage_owner_block",
            successor=successor,
            owner=segment.owner,
        )
        self.stacks[line] = stack
        self.leases.append(RecoveryLease(
            line=line,
            owner=segment.owner,
            nos=segment.nos,
            gate_footprint=frozenset(physical.operation_approach_lines(line)),
        ))

    def staging_candidates(
        self,
        segment: CarrySegment,
        forbidden_lines: frozenset[str],
        recovery: RecoveryIntent | None,
    ) -> list[tuple[tuple, str, SearchNode]]:
        cars = self.cars
        by_no = self.by_no
        pending = self.problem.unsatisfied_active(self.node.state)
        pending_sources = {
            str(by_no[no].get("Line") or "")
            for no in pending
            if by_no[no].get("Line")
        }
        pending_targets = {
            self.problem.target_by_no.get(no, "")
            for no in pending
            if self.problem.target_by_no.get(no)
        }
        reserved = set().union(*(
            resource_gate_closure(target)
            for target in pending_targets
        )) if pending_targets else set()
        leased_gates = set().union(*(
            lease.gate_footprint for lease in self.leases
        )) if self.leases else set()
        candidates: list[tuple[tuple, str, SearchNode]] = []
        for line, spec in physical.TRACK_SPECS.items():
            if line in STAGING_EXCLUDED or line in forbidden_lines:
                continue
            if (
                line in set(self.holdout_parking.values())
                and not set(segment.nos) <= set(self.holdout_parking)
                and segment.owner not in SERVICE_TARGETS
            ):
                continue
            if spec.track_type not in STAGING_TYPES:
                continue
            if line == segment.owner:
                continue
            stack = self.stacks.get(line)
            if stack and stack.prepend(segment) is None:
                continue
            order = tuple(physical.line_access_order(cars, line))
            if line == "存5线南" and physical.line_access_order(cars, "存5线北"):
                continue
            if line in leased_gates:
                continue
            if (
                recovery
                and recovery.next_line == segment.owner
                and segment.owner in SERVICE_TARGETS
                and line in resource_gate_closure(segment.owner)
            ):
                continue
            same_owner = bool(stack)
            origins = {self.source_line(no) for no in segment.nos}
            reusable_origin = bool(
                line in origins
                and not order
                and not any(
                    approach in physical.TRACK_SPECS
                    for approach in physical.operation_approach_lines(line)
                )
            )
            owner_ranks = [
                self.problem.final_rank_by_no.get(no, 0)
                for no, owner in self.goal_owner_by_no.items()
                if owner == segment.owner
            ]
            final_owner_gate = bool(
                segment.ranks
                and owner_ranks
                and segment.ranks[-1] == max(owner_ranks)
                and line in resource_gate_closure(segment.owner)
                and segment.owner not in SERVICE_TARGETS
            )
            gate_reserved = (
                line in reserved
                and line not in resource_gate_closure(segment.owner)
            )
            if gate_reserved:
                continue
            successor = self.probe("Put", line, segment.nos)
            if successor is None:
                continue
            route = successor.state.operation_paths[-1]
            exit_violation = 0
            exit_distance = 0
            if recovery and recovery.next_line:
                exit_route = self.problem.graph.route(line, recovery.next_line)
                projected_length = physical.train_length_for_nos(
                    cars,
                    recovery.projected_nos,
                )
                exit_violation = int(bool(physical.route_line_length_reasons(
                    exit_route,
                    projected_length,
                )))
                exit_distance = len(exit_route)
            rank = (
                0 if same_owner else 1,
                0 if reusable_origin else 1,
                exit_violation,
                0 if final_owner_gate else 1,
                (
                    STAGING_RISK.get(line, 0)
                    + 2 * int(line in pending_targets)
                    + 3 * int(line in pending_sources)
                ),
                exit_distance,
                STAGING_EXIT_RISK.get(line, 3),
                len(route),
                -round(spec.length_m - lease_length(by_no, (*order, *segment.nos)), 3),
                line,
            )
            candidates.append((rank, line, successor))
        return sorted(candidates, key=lambda item: item[0])

    def _consume_stack(self, line: str, move: tuple[str, ...]) -> None:
        stack = self.stacks.get(line)
        if not stack or stack.nos[: len(move)] != move:
            return
        remaining = stack.consume(move)
        if remaining:
            self.stacks[line] = remaining
        else:
            del self.stacks[line]
        moved = set(move)
        self.leases = [
            lease for lease in self.leases
            if not (lease.line == line and set(lease.nos) <= moved)
        ]

    def direct_target_successor(
        self,
        segment: CarrySegment,
    ) -> tuple[str, SearchNode] | None:
        target = segment.owner
        if target.startswith("restore:"):
            target = target.split(":", 1)[1]
        if target not in physical.TRACK_SPECS:
            return None
        stack = self.stacks.get(target)
        if stack and stack.owner != segment.owner:
            return None
        if target in SERVICE_TARGETS and self.target_dirty(target):
            return None
        successor = self.probe("Put", target, segment.nos)
        return (target, successor) if successor is not None else None

    def target_dirty(self, target: str) -> bool:
        return any(
            no in self.problem.active_nos
            and self.problem.target_by_no.get(no) != target
            for no in physical.line_access_order(self.cars, target)
        )

    def place_tail(
        self,
        segment: CarrySegment,
        *,
        desired_nos: frozenset[str],
        forbidden_lines: frozenset[str] = frozenset(),
        recovery: RecoveryIntent | None = None,
    ) -> None:
        if set(segment.nos) <= set(self.holdout_parking):
            lines = {self.holdout_parking[no] for no in segment.nos}
            if len(lines) != 1:
                raise PlanningFailure("capacity_holdout_split_segment")
            line = next(iter(lines))
            self.put_target(line, segment.nos, event="park_capacity_holdout")
            return
        if segment.owner.startswith("restore:"):
            pending = self.problem.unsatisfied_active(self.node.state)
            forbidden_lines = forbidden_lines | frozenset(
                str(self.by_no[no].get("Line") or "")
                for no in pending
                if self.by_no[no].get("Line")
            )
        if desired_nos.intersection(segment.nos):
            self.stage(
                segment,
                forbidden_lines=forbidden_lines,
                recovery=recovery,
            )
            return
        if self.complete_from_owner_stack(segment):
            return
        target = segment.owner.split(":", 1)[-1]
        pending_owner = {
            no for no in self.problem.unsatisfied_active(self.node.state)
            if self.problem.target_by_no.get(no) == target
        }
        if physical.is_spotting_line(target) and not pending_owner.issubset(segment.nos):
            self.stage(segment, forbidden_lines=forbidden_lines, recovery=recovery)
            return
        direct_target = segment.owner.split(":", 1)[-1]
        direct = (
            None
            if direct_target in forbidden_lines
            else self.direct_target_successor(segment)
        )
        if direct is not None:
            target, successor = direct
            self.apply(
                "Put",
                target,
                segment.nos,
                event="fanout_true_target",
                successor=successor,
                owner=segment.owner,
            )
            return
        self.stage(segment, forbidden_lines=forbidden_lines, recovery=recovery)

    def complete_from_owner_stack(self, segment: CarrySegment) -> bool:
        if not segment.ranks or segment.ranks[-1] <= 0:
            return False
        candidates = []
        for line, stack in self.stacks.items():
            if stack.owner != segment.owner or not stack.segments[0].ranks:
                continue
            if stack.segments[0].ranks[0] <= segment.ranks[-1]:
                continue
            gap_ranks = set(range(
                segment.ranks[-1] + 1,
                stack.segments[0].ranks[0],
            ))
            if any(
                self.problem.target_by_no.get(no) == segment.owner
                and self.problem.final_rank_by_no.get(no, 0) in gap_ranks
                for no in self.problem.active_nos
            ):
                continue
            combined = (*segment.nos, *stack.nos)
            equivalent = physical.pull_equivalent([
                self.by_no[no] for no in (*self.carry, *stack.nos)
            ])
            if equivalent > physical.PULL_LIMIT_EQUIVALENT:
                continue
            get_successor = self.probe("Get", line, stack.nos)
            if get_successor is None:
                continue
            positions = self.transitions.planned_positions(
                get_successor.state,
                segment.owner,
                combined,
            )
            if positions is None:
                continue
            put_successor = self.transitions.apply_step(
                get_successor,
                physical.plan_step("Put", segment.owner, combined, positions),
            )
            if put_successor is None:
                continue
            candidates.append((len(stack.nos), line, stack, get_successor, put_successor))
        if not candidates:
            return False
        _size, line, stack, get_successor, put_successor = min(
            candidates,
            key=lambda item: (-item[0], item[1]),
        )
        combined = (*segment.nos, *stack.nos)
        self.apply(
            "Get",
            line,
            stack.nos,
            event="continue_owner_stack",
            successor=get_successor,
            owner=segment.owner,
        )
        self.apply(
            "Put",
            segment.owner,
            combined,
            event="close_continued_owner_stack",
            successor=put_successor,
            owner=segment.owner,
        )
        self.close_contract_for_target(segment.owner)
        return True

    def close_contract_for_target(self, target: str) -> None:
        contract_id = f"TARGET_WINDOW:{target}"
        if contract_id not in self.contracts.by_id():
            return
        if any(
            self.problem.target_by_no.get(no) == target
            for no in self.problem.unsatisfied_active(self.node.state)
        ):
            return
        self.contracts = self.contracts.close(contract_id)


class ConsistAssembler:
    def __init__(self, builder: PlanBuilder) -> None:
        self.builder = builder
        self.target_move_provider = None
        self._active_targets: set[str] = set()

    def assemble(
        self,
        segments: tuple[TargetSegment, ...],
        *,
        anchor: tuple[str, ...] = (),
        allow_interrupts: bool = True,
    ) -> None:
        desired = tuple(no for segment in segments for no in segment.nos)
        expected = (*anchor, *desired)
        desired_set = frozenset(desired)
        if self.builder.carry[: len(anchor)] != anchor:
            raise PlanningFailure("anchor_prefix_missing")

        targets = {segment.target for segment in segments}
        self.builder.goal_owner_by_no.update(
            (no, segment.target)
            for segment in segments
            for no in segment.nos
        )
        self._active_targets.update(targets)
        self._open_outbound_only_targets(segments, desired_set)

        rounds = 0
        seen: set[tuple] = set()
        while self.builder.carry != expected:
            rounds += 1
            if rounds > len(self.builder.problem.cars) * 8 + 40:
                raise PlanningFailure("consist_assembly_round_limit")
            signature = self.builder.problem.physical_signature(self.builder.node.state)
            if signature in seen:
                raise PlanningFailure("consist_assembly_cycle")
            seen.add(signature)
            matched = self._matched_prefix(expected)
            if len(self.builder.carry) > matched:
                segment = self.builder.carry_segments()[-1]
                if set(segment.nos) <= set(anchor):
                    raise PlanningFailure("anchor_would_be_staged")
                recovery = self._recovery_intent(expected, segment)
                missing_sources = self._missing_predecessor_sources(
                    expected,
                    matched,
                    segment,
                )
                dependent_origins = frozenset(
                    line
                    for no in segment.nos
                    for line in [self.builder.source_line(no)]
                    if line and any(
                        approach in physical.TRACK_SPECS
                        for approach in physical.operation_approach_lines(line)
                    )
                )
                blocked_predecessor_line = ""
                if matched < len(expected):
                    next_no = expected[matched]
                    if next_no not in self.builder.carry:
                        blocked_predecessor_line = self.builder.source_line(next_no)
                self.builder.place_tail(
                    segment,
                    desired_nos=frozenset(self.builder.goal_owner_by_no),
                    forbidden_lines=(
                        frozenset(item.target for item in segments)
                        | missing_sources
                        | dependent_origins
                        | frozenset({blocked_predecessor_line} if blocked_predecessor_line else ())
                    ),
                    recovery=recovery,
                )
                continue

            if (
                allow_interrupts
                and
                matched > len(anchor)
                and self._close_dirty_service(anchor=self.builder.carry)
            ):
                seen.clear()
                continue

            next_no = expected[matched]
            line = str(self.builder.by_no[next_no].get("Line") or "")
            if not line:
                raise PlanningFailure(f"desired_car_not_on_line:{next_no}")
            move = self._pull_prefix(line, next_no, desired_set)
            successor = self.builder.probe("Get", line, move)
            if successor is None:
                self._release_single_gate(line, desired_set)
                successor = self.builder.probe("Get", line, move)
            if successor is None:
                raise PlanningFailure(f"desired_prefix_inaccessible:{line}:{next_no}")
            self.builder.apply(
                "Get",
                line,
                move,
                event="acquire_desired_prefix",
                successor=successor,
                next_desired=next_no,
            )

        for segment in reversed(segments):
            self.builder.put_target(
                segment.target,
                segment.nos,
                event="close_target_segment",
            )
            self.builder.close_contract_for_target(segment.target)
        if self.builder.carry != anchor:
            raise AssertionError("target fanout changed anchor")
        self._active_targets.difference_update(targets)

    def _close_dirty_service(self, *, anchor: tuple[str, ...]) -> bool:
        if self.target_move_provider is None:
            return False
        pending = self.builder.problem.unsatisfied_active(self.builder.node.state)
        targets = sorted({
            self.builder.problem.target_by_no.get(no, "")
            for no in pending
            if self.builder.problem.target_by_no.get(no) in SERVICE_TARGETS
            and self.builder.problem.target_by_no.get(no) not in self._active_targets
        })
        dirty = [target for target in targets if self.builder.target_dirty(target)]
        if len(dirty) >= 2 and self._prepare_coupled_windows(dirty, anchor):
            return True
        for target in targets:
            if self.builder.target_dirty(target):
                continue
            move = self.target_move_provider(target)
            if not move:
                continue
            if not self._all_sources_expose(move):
                continue
            equivalent = physical.pull_equivalent([
                self.builder.by_no[no] for no in (*anchor, *move)
            ])
            if equivalent > physical.PULL_LIMIT_EQUIVALENT:
                continue
            self.assemble(
                (TargetSegment(target, move),),
                anchor=anchor,
                allow_interrupts=False,
            )
            return True
        for target in targets:
            if self.builder.target_dirty(target):
                if self._prepare_target_window(target, anchor):
                    return True
        return False

    def _prepare_coupled_windows(
        self,
        targets: list[str],
        anchor: tuple[str, ...],
    ) -> bool:
        windows = []
        common_owner = ""
        for target in targets:
            order = tuple(physical.line_access_order(self.builder.cars, target))
            outbound = tuple(
                no for no in order
                if no in self.builder.problem.active_nos
                and self.builder.problem.target_by_no.get(no) != target
            )
            owners = {self.builder.problem.target_by_no.get(no, "") for no in outbound}
            if not outbound or len(owners) != 1:
                continue
            owner = next(iter(owners))
            if common_owner and owner != common_owner:
                continue
            common_owner = owner
            windows.append((target, order, set(order) - set(outbound)))
        if len(windows) < 2 or not common_owner:
            return False
        total = (*anchor, *(no for _target, order, _members in windows for no in order))
        if physical.pull_equivalent([self.builder.by_no[no] for no in total]) > physical.PULL_LIMIT_EQUIVALENT:
            return False

        for target, order, target_members in windows:
            successor = self.builder.probe("Get", target, order)
            if successor is None:
                raise PlanningFailure("coupled_window_get_missing")
            self.builder.apply(
                "Get",
                target,
                order,
                event="prepare_coupled_service_window",
                successor=successor,
            )
            self.builder.goal_owner_by_no.update(
                (no, target) for no in target_members
            )
            pending_sources = frozenset(
                str(self.builder.by_no[no].get("Line") or "")
                for no in self.builder.problem.unsatisfied_active(self.builder.node.state)
                if self.builder.by_no[no].get("Line")
            )
            temporary = frozenset(
                line for line, spec in physical.TRACK_SPECS.items()
                if spec.track_type == "temporary"
            )
            while (
                self.builder.carry != anchor
                and self.builder.carry[-1] in target_members
            ):
                tail = self.builder.carry_segments()[-1]
                self.builder.place_tail(
                    tail,
                    desired_nos=frozenset(target_members),
                    forbidden_lines=(
                        frozenset({target}) | pending_sources | temporary
                    ),
                )
        suffix = self.builder.carry[len(anchor):]
        if not suffix or any(
            self.builder.problem.target_by_no.get(no) != common_owner
            for no in suffix
        ):
            raise PlanningFailure("coupled_window_owner_mismatch")
        self.builder.put_target(
            common_owner,
            suffix,
            event="close_coupled_service_outbound",
        )
        return True

    def _prepare_target_window(
        self,
        target: str,
        anchor: tuple[str, ...],
    ) -> bool:
        order = tuple(physical.line_access_order(self.builder.cars, target))
        if not order:
            return False
        equivalent = physical.pull_equivalent([
            self.builder.by_no[no] for no in (*anchor, *order)
        ])
        if equivalent > physical.PULL_LIMIT_EQUIVALENT:
            return False
        successor = self.builder.probe("Get", target, order)
        if successor is None:
            return False
        self.builder.apply(
            "Get",
            target,
            order,
            event="prepare_service_window",
            successor=successor,
        )
        target_members = {
            no for no in order
            if target in self.builder.problem.target_options(no)
            and not (
                no in self.builder.problem.active_nos
                and self.builder.problem.target_by_no.get(no) != target
            )
        }
        self.builder.goal_owner_by_no.update((no, target) for no in target_members)
        pending_sources = frozenset(
            str(self.builder.by_no[no].get("Line") or "")
            for no in self.builder.problem.unsatisfied_active(self.builder.node.state)
            if self.builder.by_no[no].get("Line")
        )
        temporary_lines = frozenset(
            line for line, spec in physical.TRACK_SPECS.items()
            if spec.track_type == "temporary"
        )
        while self.builder.carry != anchor:
            tail = self.builder.carry_segments()[-1]
            self.builder.place_tail(
                tail,
                desired_nos=frozenset(target_members),
                forbidden_lines=(
                    frozenset({target})
                    | pending_sources
                    | temporary_lines
                ),
            )
        return True

    def _all_sources_expose(self, move: tuple[str, ...]) -> bool:
        move_set = set(move)
        by_line: dict[str, set[str]] = {}
        for no in move:
            line = str(self.builder.by_no[no].get("Line") or "")
            if not line:
                return False
            by_line.setdefault(line, set()).add(no)
        for line, members in by_line.items():
            order = tuple(physical.line_access_order(self.builder.cars, line))
            last = max(order.index(no) for no in members)
            if any(no not in move_set for no in order[: last + 1]):
                return False
        return True

    def _matched_prefix(self, expected: tuple[str, ...]) -> int:
        matched = 0
        for actual, wanted in zip(self.builder.carry, expected):
            if actual != wanted:
                break
            matched += 1
        return matched

    def _recovery_intent(
        self,
        expected: tuple[str, ...],
        segment: CarrySegment,
    ) -> RecoveryIntent | None:
        indexes = [
            expected.index(no) for no in segment.nos
            if no in expected
        ]
        if not indexes:
            return None
        end = max(indexes)
        next_line = ""
        if end + 1 < len(expected):
            next_no = expected[end + 1]
            next_line = self.builder.source_line(next_no)
        elif segment.owner in physical.TRACK_SPECS:
            next_line = segment.owner
        return RecoveryIntent(
            next_line=next_line,
            projected_nos=expected[: end + 1],
        )

    def _missing_predecessor_sources(
        self,
        expected: tuple[str, ...],
        matched: int,
        segment: CarrySegment,
    ) -> frozenset[str]:
        indexes = [expected.index(no) for no in segment.nos if no in expected]
        if not indexes:
            return frozenset()
        predecessors = expected[matched:min(indexes)]
        sources = {
            str(self.builder.by_no[no].get("Line") or "")
            for no in predecessors
            if self.builder.by_no[no].get("Line")
        }
        if any(no not in self.builder.carry for no in predecessors):
            sources.update(
                self.builder.source_line(no) for no in segment.nos
                if self.builder.source_line(no)
            )
        return frozenset(sources)

    def _pull_prefix(
        self,
        line: str,
        subject: str,
        desired_nos: frozenset[str],
    ) -> tuple[str, ...]:
        order = tuple(physical.line_access_order(self.builder.cars, line))
        if subject not in order:
            raise PlanningFailure(f"subject_not_in_access_order:{line}:{subject}")
        remaining = physical.PULL_LIMIT_EQUIVALENT - physical.pull_equivalent([
            self.builder.by_no[no] for no in self.builder.carry
        ])
        stack = self.builder.stacks.get(line)
        stack_limit = len(stack.nos) if stack and subject in stack.nos else len(order)
        deepest_desired = max(
            (index for index, no in enumerate(order) if no in desired_nos),
            default=order.index(subject),
        )
        useful_limit = max(order.index(subject), deepest_desired) + 1
        equivalent = 0
        take = 0
        for no in order[:stack_limit]:
            equivalent += physical.pull_equivalent([self.builder.by_no[no]])
            if equivalent > remaining:
                break
            take += 1
            if take >= useful_limit and take < len(order):
                next_no = order[take]
                if (
                    next_no in self.builder.problem.protected_nos
                    and next_no not in desired_nos
                ):
                    break
        subject_index = order.index(subject)
        if take <= subject_index:
            take = min(take, max(1, subject_index))
        if take == 0:
            raise PlanningFailure(f"pull_capacity_exhausted:{line}:{subject}")
        owner = self.builder.goal_owner_by_no.get(
            subject,
            self.builder.problem.target_by_no.get(subject, ""),
        )
        if owner in SERVICE_TARGETS and subject_index == 0:
            service_take = 0
            for no in order[:take]:
                no_owner = self.builder.goal_owner_by_no.get(
                    no,
                    self.builder.problem.target_by_no.get(no, ""),
                )
                if no_owner != owner:
                    break
                service_take += 1
            if service_take:
                take = service_take
        return order[:take]

    def _open_outbound_only_targets(
        self,
        segments: tuple[TargetSegment, ...],
        desired_nos: frozenset[str],
    ) -> None:
        for segment in segments:
            order = tuple(physical.line_access_order(
                self.builder.cars,
                segment.target,
            ))
            outbound = tuple(
                no for no in order
                if no in self.builder.problem.active_nos
                and self.builder.problem.target_by_no.get(no) != segment.target
            )
            desired_existing = set(order).intersection(segment.nos)
            if (
                not outbound
                or desired_existing
                or set(outbound).intersection(desired_nos)
            ):
                continue
            equivalent = physical.pull_equivalent([
                self.builder.by_no[no]
                for no in (*self.builder.carry, *order)
            ])
            if equivalent > physical.PULL_LIMIT_EQUIVALENT:
                continue
            successor = self.builder.probe("Get", segment.target, order)
            if successor is None:
                continue
            self.builder.apply(
                "Get",
                segment.target,
                order,
                event="open_outbound_target",
                successor=successor,
            )
            while self.builder.carry and self.builder.carry[-1] in set(order):
                tail = self.builder.carry_segments()[-1]
                self.builder.place_tail(tail, desired_nos=desired_nos)

    def _release_single_gate(
        self,
        source: str,
        desired_nos: frozenset[str],
    ) -> None:
        occupied = [
            line for line in physical.operation_approach_lines(source)
            if physical.line_access_order(self.builder.cars, line)
        ]
        if len(occupied) != 1:
            return
        line = occupied[0]
        order = tuple(physical.line_access_order(self.builder.cars, line))
        if not order:
            return
        equivalent = physical.pull_equivalent([
            self.builder.by_no[no] for no in (*self.builder.carry, *order)
        ])
        if equivalent > physical.PULL_LIMIT_EQUIVALENT:
            return
        successor = self.builder.probe("Get", line, order)
        if successor is None:
            return
        self.builder.apply(
            "Get",
            line,
            order,
            event="release_route_gate",
            successor=successor,
        )
        while self.builder.carry and self.builder.carry[-1] in set(order):
            segment = self.builder.carry_segments()[-1]
            self.builder.stage(
                segment,
                forbidden_lines=frozenset({line, source}),
            )


class ContractPlanner:
    def __init__(self, problem: Stage4Problem, config: PlanningConfig) -> None:
        self.problem = problem
        self.config = config
        self.transitions = OperationTransitions(problem)
        self.builder = PlanBuilder(
            problem,
            self.transitions,
            build_contract_graph(problem),
            config=config,
        )
        self.assembler = ConsistAssembler(self.builder)
        self.assembler.target_move_provider = self.target_move

    def advance_remaining(self, checkpoint: PlanningCheckpoint) -> PlanningResult:
        self.builder.restore(checkpoint)
        reason = "session_closed"
        try:
            progressed = self._advance_remaining_contract()
            complete = (
                self.problem.complete(self.builder.node)
                and not self.builder.stacks
                and not self.builder.leases
            )
            if self.builder.carry:
                raise PlanningFailure("target_window_left_open_carry")
            if not progressed and not complete:
                raise PlanningFailure("target_window_edge_missing")
            if complete:
                reason = "complete"
        except PlanningFailure as exc:
            complete = False
            reason = str(exc)
        return PlanningResult(
            node=self.builder.node,
            complete=complete,
            reason=reason,
            trace=tuple(self.builder.trace),
            contracts=self.builder.contracts,
            leases=tuple(self.builder.leases),
            expansions=self.builder.expansions,
            elapsed_seconds=round(time.monotonic() - self.builder.started, 3),
        )

    def resolve_depot_rehook(self) -> None:
        contract = classify_depot_rehook(self.problem)
        self.builder.contracts = self.builder.contracts.activate(DEPOT_REHOOK_ID)
        if contract.mode == DepotRehookMode.NOT_REQUIRED:
            self._close_rehook()
            return
        c4 = contract.c4_backbone
        if c4:
            self.builder.get("存4线", c4, event="rehook_c4_backbone")

        if contract.mode == DepotRehookMode.C4_ONLY:
            if c4:
                self.builder.put_target("存4线", c4, event="restore_c4_backbone")
            self._close_rehook()
            return

        unload_move = contract.unload_prefix
        capacity = physical.PULL_LIMIT_EQUIVALENT - physical.pull_equivalent([
            self.builder.by_no[no] for no in self.builder.carry
        ])
        unload_equivalent = physical.pull_equivalent([
            self.builder.by_no[no] for no in unload_move
        ])
        if unload_equivalent > capacity:
            release: list[str] = []
            released = 0
            for no in reversed(c4):
                release.insert(0, no)
                released += physical.pull_equivalent([self.builder.by_no[no]])
                if unload_equivalent <= capacity + released:
                    break
            self.builder.put_target(
                "存4线",
                tuple(release),
                event="rehook_capacity_batch",
            )
            c4 = c4[: -len(release)]

        self.builder.get("卸轮线", unload_move, event="rehook_paint_tail")
        paint_existing = tuple(
            physical.line_access_order(self.builder.cars, "油漆线")
        )
        paint_segment = segment_for(
            contract.paint_tail,
            owner="油漆线",
            rank_by_no=self.problem.final_rank_by_no,
        )
        paint_staged = False
        blockers = tuple(no for no in unload_move if no not in set(contract.paint_tail))
        if blockers:
            if self.builder.carry[-len(contract.paint_tail):] != contract.paint_tail:
                raise PlanningFailure("unload_prefix_paint_not_tail")
            if paint_existing:
                self._stage_on_c4(paint_segment)
                paint_staged = True
            else:
                self._close_paint_direct(contract.paint_tail)
            blocker_segment = segment_for(
                blockers,
                owner="restore:卸轮线",
                rank_by_no=self.problem.final_rank_by_no,
                protected=True,
            )
            direct = self.builder.direct_target_successor(blocker_segment)
            if direct is None:
                raise PlanningFailure("unload_prefix_restore_inaccessible")
            line, successor = direct
            self.builder.apply(
                "Put",
                line,
                blockers,
                event="restore_unload_prefix",
                successor=successor,
            )
            if not paint_existing:
                if c4:
                    self.builder.put_target("存4线", c4, event="restore_c4_backbone")
                self._close_rehook()
                return

        if (
            contract.mode in {DepotRehookMode.DIRECT, DepotRehookMode.BATCHED}
            and not paint_existing
        ):
            self._close_paint_direct(contract.paint_tail)
            if c4:
                self.builder.put_target("存4线", c4, event="restore_c4_backbone")
            self._close_rehook()
            return

        if not paint_staged:
            self._stage_on_c4(paint_segment)
        c4_lease_owner = ""
        window_equivalent = physical.pull_equivalent([
            self.builder.by_no[no] for no in (*c4, *paint_existing)
        ])
        if window_equivalent > physical.PULL_LIMIT_EQUIVALENT:
            release_count = window_equivalent - physical.PULL_LIMIT_EQUIVALENT
            release = c4[-release_count:]
            c4_segment = segment_for(
                release,
                owner="rehook:c4",
                rank_by_no=self.problem.final_rank_by_no,
                protected=True,
            )
            self.builder.stage(
                c4_segment,
                forbidden_lines=frozenset({"存4线"}),
            )
            c4 = c4[:-release_count]
            c4_lease_owner = c4_segment.owner
        self.builder.get("油漆线", paint_existing, event="open_paint_window")

        self._resolve_outbound_paint_window(
            contract=contract,
            c4=c4,
            c4_lease_owner=c4_lease_owner,
            paint_existing=paint_existing,
        )

    def _close_paint_direct(self, paint: tuple[str, ...]) -> None:
        self.builder.put_target("油漆线", paint, event="close_paint_tail")
        self.builder.close_contract_for_target("油漆线")

    def _resolve_outbound_paint_window(
        self,
        *,
        contract,
        c4: tuple[str, ...],
        c4_lease_owner: str,
        paint_existing: tuple[str, ...],
    ) -> None:
        outbound = set(contract.paint_outbound)
        stayers = tuple(no for no in paint_existing if no not in outbound)
        target_segments = self._segments_for_outbound(paint_existing)
        anchor = (*c4, *stayers)
        if self.builder.carry[: len(anchor)] != anchor:
            raise PlanningFailure("paint_window_stayer_prefix_mismatch")

        deferred = bool(
            any(segment.target == "存4线" for segment in target_segments)
            or physical.pull_equivalent([
                self.builder.by_no[no]
                for no in (*anchor, *(no for item in target_segments for no in item.nos))
            ]) > physical.PULL_LIMIT_EQUIVALENT
            or not self._segments_fit_anchor_window(anchor, target_segments)
        )
        if target_segments and not deferred:
            self.assembler.assemble(
                target_segments,
                anchor=anchor,
                allow_interrupts=False,
            )
        elif target_segments:
            while self.builder.carry != anchor:
                segment = self.builder.carry_segments()[-1]
                self.builder.stage(
                    segment,
                    forbidden_lines=frozenset({"存4线", "油漆线"}),
                )

        paint_move = tuple(sorted(
            (*stayers, *contract.paint_tail),
            key=lambda no: (
                self.problem.final_rank_by_no.get(no, 10_000),
                no,
            ),
        ))
        self.assembler.assemble(
            (TargetSegment("油漆线", paint_move),),
            anchor=c4,
            allow_interrupts=False,
        )

        if c4_lease_owner:
            lines = [
                line for line, stack in self.builder.stacks.items()
                if stack.owner == c4_lease_owner
            ]
            if len(lines) != 1:
                raise PlanningFailure("rehook_c4_batch_lease_missing")
            stack = self.builder.stacks[lines[0]]
            self.builder.get(
                stack.line,
                stack.nos,
                event="recover_rehook_c4_batch",
            )
            c4 = self.builder.carry
        if c4:
            self.builder.put_target("存4线", c4, event="restore_c4_backbone")

        if target_segments and deferred:
            self.assembler.assemble(
                target_segments,
                allow_interrupts=False,
            )
        self._close_exposed_owner_stacks()
        self._close_rehook()

    def _segments_fit_anchor_window(
        self,
        anchor: tuple[str, ...],
        segments: tuple[TargetSegment, ...],
    ) -> bool:
        by_no = self.builder.by_no
        accumulated = list(anchor)
        carried = set(self.builder.carry)
        for segment in segments:
            for no in segment.nos:
                line = str(by_no[no].get("Line") or "")
                if line and no not in carried:
                    order = physical.line_access_order(self.builder.cars, line)
                    prefix = order[: order.index(no) + 1]
                    if physical.pull_equivalent([
                        by_no[item] for item in (*accumulated, *prefix)
                    ]) > physical.PULL_LIMIT_EQUIVALENT:
                        return False
                accumulated.append(no)
        return True

    def _close_exposed_owner_stacks(self) -> None:
        for _round in range(len(self.builder.stacks) + 5):
            owners = sorted({
                stack.owner for stack in self.builder.stacks.values()
                if stack.owner in physical.TRACK_SPECS
                and stack.owner != "油漆线"
            })
            if not owners:
                return
            target = owners[0]
            move = self.target_move(target)
            if not move:
                raise PlanningFailure(f"owner_stack_without_debt:{target}")
            self.assembler.assemble(
                (TargetSegment(target, move),),
                allow_interrupts=False,
            )
        raise PlanningFailure("owner_stack_closure_limit")

    def _stage_on_c4(self, segment: CarrySegment) -> None:
        successor = self.builder.probe("Put", "存4线", segment.nos)
        if successor is None:
            raise PlanningFailure("paint_tail_c4_lease_missing")
        self.builder.apply(
            "Put",
            "存4线",
            segment.nos,
            event="lease_paint_tail_on_c4",
            successor=successor,
        )
        self.builder.stacks["存4线"] = OwnedStack("存4线", (segment,))
        self.builder.leases.append(RecoveryLease(
            line="存4线",
            owner="油漆线",
            nos=segment.nos,
            gate_footprint=frozenset(),
        ))

    def _close_rehook(self) -> None:
        if self.builder.carry:
            raise PlanningFailure("rehook_contract_left_carry")
        self.builder.contracts = self.builder.contracts.close(DEPOT_REHOOK_ID)

    def _segments_for_outbound(
        self,
        outbound: tuple[str, ...],
    ) -> tuple[TargetSegment, ...]:
        targets: list[str] = []
        for no in outbound:
            target = self.problem.target_by_no.get(no, "")
            if target and target != "油漆线" and target not in targets:
                targets.append(target)
        segments: list[TargetSegment] = []
        for target in targets:
            move = self.target_move(target)
            if move:
                trial = self.builder.probe("Put", target, move)
                if trial is None:
                    existing = tuple(
                        no for no in physical.line_access_order(
                            self.builder.cars,
                            target,
                        )
                        if target in self.problem.target_options(no)
                        and not (
                            no in self.problem.active_nos
                            and self.problem.target_by_no.get(no) != target
                        )
                    )
                    if existing:
                        move = tuple(sorted(
                            dict.fromkeys((*existing, *move)),
                            key=lambda no: (
                                self.problem.final_rank_by_no.get(no, 10_000),
                                no,
                            ),
                        ))
                segments.append(TargetSegment(target, move))
        return tuple(segments)

    def _advance_remaining_contract(self) -> bool:
        unsatisfied = self.problem.unsatisfied_active(self.builder.node.state)
        if not unsatisfied:
            return False
        targets = sorted({
            self.problem.target_by_no.get(no, "")
            for no in unsatisfied
            if self.problem.target_by_no.get(no)
        })
        if not targets:
            raise PlanningFailure("search_incomplete:targetless_active_debt")
        if not any(self._target_is_complex(target) for target in targets):
            if self.sweep_source():
                return True
        primary = min(targets, key=self.target_priority)
        chain = self.target_chain(primary)
        before = self.problem.physical_signature(self.builder.node.state)
        self.assembler.assemble(chain)
        if self.problem.physical_signature(self.builder.node.state) == before:
            raise PlanningFailure(f"target_contract_no_progress:{primary}")
        return True

    def _target_is_complex(self, target: str) -> bool:
        move = self.target_move(target)
        active_move = set(move).intersection(self.problem.active_nos)
        return bool(
            any(physical.force_positions(self.builder.by_no[no]) for no in move)
            or set(move) - active_move
            or self.builder.target_dirty(target)
        )

    def sweep_source(self) -> bool:
        unsatisfied = self.problem.unsatisfied_active(self.builder.node.state)
        by_no = self.builder.by_no
        candidates = []
        for line in sorted({
            str(by_no[no].get("Line") or "") for no in unsatisfied
            if by_no[no].get("Line")
        }):
            order = tuple(physical.line_access_order(self.builder.cars, line))
            if not order or any(
                no not in unsatisfied
                and no not in self.problem.capacity_holdout_nos
                and no not in self.builder.goal_owner_by_no
                for no in order
            ):
                continue
            equivalent = physical.pull_equivalent([by_no[no] for no in order])
            if equivalent > physical.PULL_LIMIT_EQUIVALENT:
                continue
            runs = 1 + sum(
                self.builder.owner(left) != self.builder.owner(right)
                for left, right in zip(order, order[1:])
            )
            candidates.append((-len(set(order) & set(unsatisfied)), -runs, line, order))
        if not candidates:
            return False
        _count, _runs, line, order = min(candidates)
        successor = self.builder.probe("Get", line, order)
        if successor is None:
            return False
        self.builder.apply(
            "Get",
            line,
            order,
            event="terminal_source_sweep",
            successor=successor,
        )
        while self.builder.carry:
            segment = self.builder.carry_segments()[-1]
            if set(segment.nos) <= set(self.builder.holdout_parking):
                self.builder.place_tail(segment, desired_nos=frozenset())
                continue
            if self.builder.complete_from_owner_stack(segment):
                continue
            target = segment.owner
            pending = {
                no for no in self.problem.unsatisfied_active(self.builder.node.state)
                if self.problem.target_by_no.get(no) == target
            }
            segment_ranks = [
                self.problem.final_rank_by_no.get(no, 0) for no in segment.nos
            ]
            pending_ranks = [
                self.problem.final_rank_by_no.get(no, 0) for no in pending - set(segment.nos)
            ]
            monotone_prepend = bool(
                segment_ranks
                and all(rank > 0 for rank in segment_ranks)
                and all(rank > 0 for rank in pending_ranks)
                and (not pending_ranks or max(pending_ranks) < min(segment_ranks))
            )
            direct = self.builder.direct_target_successor(segment)
            if direct is not None and (not pending or monotone_prepend):
                target_line, put_successor = direct
                self.builder.apply(
                    "Put",
                    target_line,
                    segment.nos,
                    event="terminal_source_fanout",
                    successor=put_successor,
                )
                self.builder.close_contract_for_target(target_line)
                continue
            self.builder.stage(segment)
        return True

    def target_priority(self, target: str) -> tuple:
        move = self.target_move(target)
        forced = sum(
            bool(physical.force_positions(self.builder.by_no[no]))
            for no in move
        )
        dirty = int(self.builder.target_dirty(target))
        service = int(target in SERVICE_TARGETS)
        sources = len({
            self.builder.by_no[no].get("Line") or ""
            for no in move
        })
        return (
            -forced,
            -dirty,
            -service,
            -len(move),
            -sources,
            target,
        )

    def target_chain(self, primary: str) -> tuple[TargetSegment, ...]:
        primary_move = self.target_move(primary)
        if not primary_move:
            raise PlanningFailure(f"empty_target_contract:{primary}")
        segments: list[TargetSegment] = []
        primary_sources = {
            self.builder.by_no[no].get("Line") or ""
            for no in primary_move
        }
        blocked_south = (
            "存5线南" in primary_sources
            and bool(physical.line_access_order(self.builder.cars, "存5线北"))
        )
        if blocked_south:
            candidates = []
            for target in sorted({
                self.problem.target_by_no.get(no, "")
                for no in self.problem.unsatisfied_active(self.builder.node.state)
                if self.problem.target_by_no.get(no) not in {"", primary}
            }):
                move = self.target_move(target)
                if not move or len(move) > 3 or not self.is_suffix_target(target, move):
                    continue
                sources = {self.builder.by_no[no].get("Line") or "" for no in move}
                if not sources.intersection({"存5线北", "存5线南"}):
                    continue
                if not self.builder.target_dirty(target):
                    continue
                candidates.append(TargetSegment(target, move))
            segments.extend(sorted(candidates, key=lambda item: item.target))
        segments.append(TargetSegment(primary, primary_move))
        while physical.pull_equivalent([
            self.builder.by_no[no]
            for segment in segments
            for no in segment.nos
        ]) > physical.PULL_LIMIT_EQUIVALENT:
            if len(segments) == 1:
                raise PlanningFailure(f"target_consist_over_limit:{primary}")
            segments.pop(0)
        return tuple(segments)

    def target_move(self, target: str) -> tuple[str, ...]:
        unsatisfied = self.problem.unsatisfied_active(self.builder.node.state)
        inbound = {
            no for no in unsatisfied
            if self.problem.target_by_no.get(no) == target
        }
        if not inbound:
            return ()
        existing = tuple(physical.line_access_order(self.builder.cars, target))
        existing_target = tuple(
            no for no in existing
            if target in self.problem.target_options(no)
            and not (
                no in self.problem.active_nos
                and self.problem.target_by_no.get(no) != target
            )
        )
        inbound_ranks = [self.problem.final_rank_by_no.get(no, 0) for no in inbound]
        existing_ranks = [
            self.problem.final_rank_by_no.get(no, 0) for no in existing_target
        ]
        rebuild = bool(
            inbound_ranks
            and existing_ranks
            and min(inbound_ranks) <= max(existing_ranks)
        )
        participants = set(inbound)
        if rebuild:
            participants.update(existing_target)
        return tuple(sorted(
            participants,
            key=lambda no: (
                self.problem.final_rank_by_no.get(no, 10_000),
                no,
            ),
        ))

    def is_suffix_target(self, target: str, move: tuple[str, ...]) -> bool:
        existing = tuple(physical.line_access_order(self.builder.cars, target))
        existing_ranks = [
            self.problem.final_rank_by_no.get(no, 0)
            for no in existing
            if target in self.problem.target_options(no)
        ]
        move_ranks = [self.problem.final_rank_by_no.get(no, 0) for no in move]
        return bool(
            move_ranks
            and all(rank > 0 for rank in move_ranks)
            and all(right == left + 1 for left, right in zip(move_ranks, move_ranks[1:]))
            and (not existing_ranks or min(move_ranks) > max(existing_ranks))
        )
