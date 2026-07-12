from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from solver_vnext import physical

from .domain import OwnedStack, RecoveryLease, segment_for
from .search import OperationTransitions, SearchNode, Stage4Problem
from .topology import RESOURCE_GATES, resource_gate_closure
ROUTE_DEFER_REASON_PREFIXES = (
    "put_route_blocked_by_occupied_line",
    "put_route_missing",
    "route_line_length_violation",
    "route_reversal_length_violation",
    "route_reversal_with_blocker_length_violation",
)


def monotone_stack_prepend_allowed(
    move_ranks: list[int],
    existing_ranks: list[int],
) -> bool:
    return bool(
        move_ranks
        and existing_ranks
        and all(right == left + 1 for left, right in zip(move_ranks, move_ranks[1:]))
        and all(
            right == left + 1
            for left, right in zip(existing_ranks, existing_ranks[1:])
        )
        and move_ranks[-1] + 1 == existing_ranks[0]
    )


@dataclass(frozen=True)
class SourceWindowResult:
    node: SearchNode
    closed_node: SearchNode
    complete: bool
    reason: str
    trace: tuple[dict, ...]
    decisions: tuple["LabelDecision", ...]
    leases: tuple[RecoveryLease, ...]
    closed_leases: tuple[RecoveryLease, ...]
    stacks: tuple[tuple[str, OwnedStack], ...]


@dataclass(frozen=True)
class LabelDecision:
    label: str
    selected: int
    alternatives: int


class SourceWindowGenerator:
    """Expand one source-window label into physical flow transitions."""

    def __init__(
        self,
        problem: Stage4Problem,
        transitions: OperationTransitions,
        choices: tuple[int, ...] = (),
    ) -> None:
        self.problem = problem
        self.transitions = transitions
        self.node = SearchNode(physical.initial_planlet_state(
            problem.cars,
            problem.loco_location,
        ))
        self.closed_node = self.node
        self.stacks: dict[str, OwnedStack] = {}
        self.leases: list[RecoveryLease] = []
        self.closed_leases: tuple[RecoveryLease, ...] = ()
        self.staged_nos: set[str] = set()
        self.trace: list[dict] = []
        self.failure = ""
        self.choices = choices
        self.decisions: list[LabelDecision] = []
        self.rank_by_no = dict(problem.final_rank_by_no)

    def restore(
        self,
        node: SearchNode,
        stacks: tuple[tuple[str, OwnedStack], ...],
        leases: tuple[RecoveryLease, ...],
    ) -> None:
        self.node = node
        self.closed_node = node
        self.stacks = dict(stacks)
        self.leases = list(leases)
        self.closed_leases = leases
        self.staged_nos = {
            no for stack in self.stacks.values() for no in stack.nos
        }

    def advance(self) -> SourceWindowResult:
        started_hooks = self.node.cost.hooks
        if not self.close_direct_targets():
            return self.result(False)
        if self.node.cost.hooks > started_hooks:
            return self._closed_result()

        remaining = self.source_pending_nos()
        if remaining:
            line = self.next_source_line(remaining)
            before = self.problem.physical_signature(self.node.state)
            if not self.digest_line(line, self.line_must_be_empty(line, remaining)):
                return self.result(False)
            if not self.close_ready_staged_targets():
                return self.result(False)
            if self.problem.physical_signature(self.node.state) == before:
                self.failure = f"source_digest_no_progress:{line}"
                return self.result(False)
            return self._closed_result()

        targets = sorted(
            ({
                self.problem.target_by_no.get(no, "")
                for no in self.problem.unsatisfied_active(self.node.state)
                if self.problem.target_by_no.get(no)
            } | {
                stack.owner
                for stack in self.stacks.values()
                if not stack.owner.startswith(("restore:", "lease:"))
            }),
            key=self.target_order,
        )
        for target in targets:
            if physical.is_spotting_line(target):
                if not self.close_ranked_target(target):
                    return self.result(False)
            elif self.owner_lines(target):
                if not self.close_simple_target(target):
                    return self.result(False)

        if not self.restore_temporary_stacks():
            return self.result(False)
        return self._closed_result()

    def _closed_result(self) -> SourceWindowResult:
        complete = (
            self.problem.complete(self.node)
            and not self.stacks
            and not self.leases
        )
        if self.node.state.carried_order:
            self.failure = "source_window_left_open_carry"
            return self.result(False)
        if not complete and not self.failure:
            self.failure = "session_closed"
        return self.result(complete)

    def result(self, complete: bool) -> SourceWindowResult:
        return SourceWindowResult(
            node=self.node,
            closed_node=self.closed_node,
            complete=complete,
            reason="complete" if complete else self.failure or "construction_failed",
            trace=tuple(self.trace),
            decisions=tuple(self.decisions),
            leases=tuple(self.leases),
            closed_leases=self.closed_leases,
            stacks=tuple(sorted(self.stacks.items())),
        )

    def choose(self, options: list, label: str):
        if not options:
            raise ValueError(f"empty choice set:{label}")
        cursor = len(self.decisions)
        selected = self.choices[cursor] if cursor < len(self.choices) else 0
        if selected >= len(options):
            raise ValueError(
                f"choice out of range:{label}:{selected}>={len(options)}"
            )
        self.decisions.append(LabelDecision(label, selected, len(options)))
        return options[selected]

    def source_pending_nos(self) -> frozenset[str]:
        unsatisfied = self.problem.unsatisfied_active(self.node.state)
        by_no = {physical.car_no(car): car for car in self.node.state.cars}
        return frozenset(
            no
            for no in unsatisfied
            if no not in self.staged_nos
            and by_no[no].get("Line")
            and not (
                physical.is_spotting_line(self.problem.target_by_no.get(no, ""))
                and by_no[no].get("Line") == self.problem.target_by_no.get(no)
            )
        )

    def next_source_line(self, pending: frozenset[str]) -> str:
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        lines = {str(by_no[no]["Line"]) for no in pending}
        inbound_targets = {
            self.problem.target_by_no.get(no, "")
            for no in pending
            if self.problem.target_by_no.get(no)
        }

        def rank(line: str) -> tuple:
            downstream = sum(
                physical.operation_approach_lines(other) == {line}
                for other in lines
                if other != line
            )
            dirty_target = int(
                line in inbound_targets
                and any(
                    by_no[no].get("Line") == line
                    and self.problem.target_by_no.get(no) != line
                    for no in pending
                )
            )
            dirty_operation = int(
                dirty_target
                and physical.TRACK_SPECS.get(line) is not None
                and physical.TRACK_SPECS[line].track_type == "operation"
            )
            order = physical.line_access_order(cars, line)
            fully_releasable = int(bool(order) and all(no in pending for no in order))
            first = min(
                (
                    index
                    for index, no in enumerate(
                        physical.line_access_order(cars, line), start=1
                    )
                    if no in pending
                ),
                default=999,
            )
            return (
                -downstream,
                -dirty_operation,
                -fully_releasable,
                -dirty_target,
                first,
                line,
            )

        ranked = sorted(lines, key=rank)
        return self.choose(ranked[:4], "source_line")

    def line_must_be_empty(self, line: str, pending: frozenset[str]) -> bool:
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        source_lines = {str(by_no[no]["Line"]) for no in pending}
        return any(
            physical.operation_approach_lines(other) == {line}
            for other in source_lines
            if other != line
        )

    def close_direct_targets(self) -> bool:
        while True:
            unsatisfied = self.problem.unsatisfied_active(self.node.state)
            candidates = sorted(
                {
                    self.problem.target_by_no.get(no, "")
                    for no in unsatisfied
                    if physical.is_spotting_line(self.problem.target_by_no.get(no, ""))
                    and self.problem.target_exposed(
                        self.problem.target_by_no.get(no, ""),
                        self.node.state,
                        unsatisfied,
                    )
                },
                key=self.target_order,
            )
            closed = False
            for target in candidates:
                if self.try_direct_target_merge(target):
                    closed = True
                    break
            if not closed:
                return True

    def try_direct_target_merge(self, target: str) -> bool:
        desired = tuple(
            no
            for no, _rank in sorted(
                (
                    (no, rank)
                    for no, rank in self.rank_by_no.items()
                    if self.problem.target_by_no.get(no) == target
                    or (
                        no in self.problem.protected_nos
                        and target in self.problem.target_options(no)
                    )
                ),
                key=lambda item: (item[1], item[0]),
            )
        )
        if not desired:
            return False
        cars = list(self.node.state.cars)
        desired_set = set(desired)
        sequences: dict[str, tuple[str, ...]] = {}
        for line in sorted({car.get("Line") or "" for car in cars if car.get("Line")}):
            order = physical.line_access_order(cars, line)
            members = tuple(no for no in order if no in desired_set)
            if not members:
                continue
            if tuple(order[: len(members)]) != members:
                return False
            ranks = [self.rank_by_no.get(no, 0) for no in members]
            if any(right != left + 1 for left, right in zip(ranks, ranks[1:])):
                return False
            sequences[line] = members
        assembled: list[str] = []
        get_steps: list[physical.PlanStep] = []
        while len(assembled) < len(desired):
            next_no = desired[len(assembled)]
            match = next(
                (
                    (line, members)
                    for line, members in sequences.items()
                    if members and members[0] == next_no
                ),
                None,
            )
            if match is None:
                return False
            line, members = match
            get_steps.append(physical.plan_step("Get", line, members))
            assembled.extend(members)
            del sequences[line]
        by_no = {physical.car_no(car): car for car in cars}
        if physical.pull_equivalent([by_no[no] for no in desired]) > physical.PULL_LIMIT_EQUIVALENT:
            return False

        original = self.node
        trial = self.node
        for step in get_steps:
            successor = self.transitions.apply_step(trial, step)
            if successor is None:
                return False
            trial = successor
        positions = self.transitions.planned_positions(trial.state, target, desired)
        if positions is None:
            return False
        successor = self.transitions.apply_step(
            trial,
            physical.plan_step("Put", target, desired, positions),
        )
        if successor is None:
            return False
        self.node = original
        for step in get_steps:
            if not self.apply(step):
                raise AssertionError("validated direct target prefix became invalid")
        if not self.apply(physical.plan_step("Put", target, desired, positions)):
            raise AssertionError("validated direct target put became invalid")
        return True

    def digest_line(self, line: str, clear_all: bool) -> bool:
        if not self.acquire_target_resources(line):
            return False
        if not self.digest_line_body(line, clear_all):
            return False
        return self.release_target_resources(line)

    def digest_line_body(self, line: str, clear_all: bool) -> bool:
        for _round in range(30):
            cars = list(self.node.state.cars)
            order = physical.line_access_order(cars, line)
            unsatisfied = self.problem.unsatisfied_active(self.node.state)
            pending_indexes = [index for index, no in enumerate(order) if no in unsatisfied]
            if clear_all:
                required_size = len(order)
            elif pending_indexes:
                required_size = max(pending_indexes) + 1
            else:
                return True
            if required_size == 0:
                return True
            by_no = {physical.car_no(car): car for car in cars}
            equivalent = 0
            max_take = 0
            for no in order[:required_size]:
                next_equivalent = equivalent + physical.pull_equivalent([by_no[no]])
                if next_equivalent > physical.PULL_LIMIT_EQUIVALENT:
                    break
                equivalent = next_equivalent
                max_take += 1
            if max_take == 0:
                self.failure = f"source_prefix_over_pull_limit:{line}"
                return False
            boundaries: list[int] = []
            previous_key: tuple | None = None
            previous_rank = 0
            for index, no in enumerate(order[:max_take], start=1):
                if no in unsatisfied:
                    owner = self.problem.target_by_no.get(no, "")
                    key = ("active", owner)
                    rank = self.rank_by_no.get(no, 0)
                else:
                    owner = line
                    key = ("owned", owner)
                    rank = 0
                next_boundary = key != previous_key and previous_key is not None
                if (
                    not next_boundary
                    and physical.is_spotting_line(owner)
                    and previous_rank
                    and rank != previous_rank + 1
                ):
                    next_boundary = True
                if next_boundary:
                    boundaries.append(index - 1)
                previous_key = key
                previous_rank = rank
            boundaries.append(max_take)
            take_options = sorted(set(boundaries), reverse=True)[:8]
            take = self.choose(take_options, f"pull_prefix:{line}")
            if not self.apply(physical.plan_step("Get", line, tuple(order[:take]))):
                return False
            while self.node.state.carried_order:
                move, owner, temporary = self.tail_intent(line, clear_all)
                if not move:
                    self.failure = f"tail_intent_missing:{line}"
                    return False
                move = self.prepare_tail_move(move)
                if not move:
                    return False
                if (
                    not temporary
                    and self.try_join_same_target_source(owner, line, move)
                ):
                    continue
                if not temporary:
                    joined = self.try_join_productive_source(owner, line, move)
                    if joined is False:
                        return False
                    if joined is True:
                        continue
                if not self.place_tail(move, owner, temporary):
                    return False
            if not physical.line_access_order(list(self.node.state.cars), line):
                return True
        self.failure = f"source_digest_round_limit:{line}"
        return False

    def tail_intent(
        self,
        source_line: str,
        clear_all: bool,
    ) -> tuple[tuple[str, ...], str, bool]:
        carry = self.node.state.carried_order
        unsatisfied = self.problem.unsatisfied_active(self.node.state)
        origins = dict(self.node.carry_origins)
        tail = carry[-1]
        if tail in unsatisfied:
            owner = self.problem.target_by_no.get(tail, "")
            temporary = False
        else:
            origin = origins.get(tail, source_line)
            owner = f"restore:{origin}" if clear_all else origin
            temporary = clear_all
        start = len(carry) - 1
        right_rank = self.rank_by_no.get(tail, 0)
        while start > 0:
            no = carry[start - 1]
            if no in unsatisfied:
                candidate_owner = self.problem.target_by_no.get(no, "")
                candidate_temporary = False
            else:
                origin = origins.get(no, source_line)
                candidate_owner = f"restore:{origin}" if clear_all else origin
                candidate_temporary = clear_all
            if (candidate_owner, candidate_temporary) != (owner, temporary):
                break
            left_rank = self.rank_by_no.get(no, 0)
            if physical.is_spotting_line(owner) and (
                not left_rank or not right_rank or left_rank + 1 != right_rank
            ):
                break
            start -= 1
            right_rank = left_rank
        return tuple(carry[start:]), owner, temporary

    def try_join_same_target_source(
        self,
        owner: str,
        current_line: str,
        retained_move: tuple[str, ...],
    ) -> bool:
        if not owner:
            return False
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        unsatisfied = self.problem.unsatisfied_active(self.node.state)
        carry_equivalent = physical.pull_equivalent([
            by_no[no]
            for no in self.node.state.carried_order
        ])
        remaining = physical.PULL_LIMIT_EQUIVALENT - carry_equivalent
        if remaining <= 0:
            return False
        expected_rank = 0
        if physical.is_spotting_line(owner):
            retained_ranks = [
                self.rank_by_no.get(no, 0)
                for no in retained_move
            ]
            if (
                not retained_ranks
                or any(not rank for rank in retained_ranks)
                or any(
                    right != left + 1
                    for left, right in zip(retained_ranks, retained_ranks[1:])
                )
            ):
                return False
            expected_rank = retained_ranks[-1] + 1

        accepted: list[tuple[tuple, SearchNode, str, tuple[str, ...]]] = []
        lines = {
            by_no[no].get("Line") or ""
            for no in self.source_pending_nos()
        }
        for line in sorted(lines - {current_line, owner, ""}):
            order = physical.line_access_order(cars, line)
            move: list[str] = []
            equivalent = 0
            for no in order:
                if (
                    no not in unsatisfied
                    or self.problem.target_by_no.get(no) != owner
                ):
                    break
                next_equivalent = equivalent + physical.pull_equivalent([by_no[no]])
                if next_equivalent > remaining:
                    break
                equivalent = next_equivalent
                move.append(no)
            if not move:
                continue
            if expected_rank:
                ranks = [
                    self.rank_by_no.get(no, 0)
                    for no in move
                ]
                if (
                    ranks[0] != expected_rank
                    or any(
                        right != left + 1
                        for left, right in zip(ranks, ranks[1:])
                    )
                ):
                    continue
            step = physical.plan_step("Get", line, tuple(move))
            successor = self.transitions.apply_step(self.node, step)
            if successor is None:
                continue
            accepted.append((
                (
                    -len(move),
                    len(successor.state.operation_paths[-1]),
                    line,
                    tuple(move),
                ),
                successor,
                line,
                tuple(move),
            ))
        if not accepted:
            return False

        options: list[tuple[tuple, SearchNode | None, str, tuple[str, ...]]] = [
            *sorted(accepted)[:4],
            ((1, 0, "", ()), None, "", ()),
        ]
        _rank, successor, source, move = self.choose(
            options,
            f"same_target_join:{owner}",
        )
        if successor is None:
            return False
        self.commit(
            successor,
            "same_target_join",
            owner=owner,
            joined_source=source,
            joined_move=list(move),
        )
        return True

    def prepare_tail_move(self, move: tuple[str, ...]) -> tuple[str, ...]:
        by_no = {physical.car_no(car): car for car in self.node.state.cars}
        tail = self.node.state.carried_order[-1]
        tail_car = by_no[tail]
        if tail_car.get("IsWeigh") and not tail_car.get("_Weighed"):
            if not self.apply(physical.plan_step("Weigh", physical.WEIGH_LINE, (tail,))):
                return ()
            by_no = {physical.car_no(car): car for car in self.node.state.cars}
        pending_indexes = [
            index
            for index, no in enumerate(move)
            if by_no[no].get("IsWeigh") and not by_no[no].get("_Weighed")
        ]
        if not pending_indexes:
            return move
        suffix = move[max(pending_indexes) + 1 :]
        if not suffix:
            self.failure = f"weigh_tail_not_exposed:{move[max(pending_indexes)]}"
        return suffix

    def place_tail(
        self,
        move: tuple[str, ...],
        owner: str,
        temporary: bool,
    ) -> bool:
        stage_reason = self.tail_staging_reason(move, owner, temporary)
        if stage_reason:
            return self.stage(move, owner, reason=stage_reason)

        positions = self.transitions.planned_positions(
            self.node.state,
            owner,
            move,
        )
        if positions is None:
            self.failure = f"direct_target_positions_missing:{owner}:{','.join(move)}"
            return False
        step = physical.plan_step("Put", owner, move, positions)
        transition = self.transitions.transition_step(self.node, step)
        if transition.accepted:
            successor = self.transitions.apply_step(self.node, step)
            if successor is None:
                raise AssertionError("accepted direct Put became invalid")
            self.commit(successor, "operation")
            return True
        if transition.reasons and all(
            reason.startswith(ROUTE_DEFER_REASON_PREFIXES)
            for reason in transition.reasons
        ):
            return self.stage(
                move,
                owner,
                reason="direct_route_unavailable:" + ",".join(transition.reasons),
            )
        self.failure = (
            f"direct_target_put_rejected:{owner}:{','.join(move)}:"
            + ",".join(transition.reasons)
        )
        return False

    def tail_staging_reason(
        self,
        move: tuple[str, ...],
        owner: str,
        temporary: bool,
    ) -> str:
        if temporary:
            return "restore_obligation"
        if self.target_dirty(owner):
            return "target_has_outbound_debt"
        if self.resource_reserved(owner):
            return "topology_resource_lease"
        if self.target_requires_rebuild(owner, move):
            return "target_window_rebuild"
        return ""

    def target_requires_rebuild(
        self,
        target: str,
        move: tuple[str, ...],
    ) -> bool:
        if not physical.is_spotting_line(target):
            return False
        existing = physical.line_access_order(list(self.node.state.cars), target)
        return bool(existing) or not self.complete_target_batch(target, move)

    def try_join_productive_source(
        self,
        owner: str,
        current_line: str,
        retained_move: tuple[str, ...],
    ) -> bool | None:
        if not self.tail_staging_reason(retained_move, owner, False):
            return None
        pending = self.source_pending_nos()
        if not pending:
            return None
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        carry_equivalent = physical.pull_equivalent([
            by_no[no]
            for no in self.node.state.carried_order
        ])
        remaining = physical.PULL_LIMIT_EQUIVALENT - carry_equivalent
        if remaining <= 0:
            return None
        carried_targets = {
            self.problem.target_by_no.get(no, "")
            for no in self.node.state.carried_order
            if no in self.problem.unsatisfied_active(self.node.state)
        }
        accepted: list[tuple[tuple, str, tuple[str, ...]]] = []
        source_lines = {
            str(by_no[no].get("Line") or "")
            for no in pending
        }
        for line in sorted(source_lines - {current_line, ""}):
            spec = physical.TRACK_SPECS.get(line)
            if (
                spec is None
                or spec.track_type != "storage"
                or line not in carried_targets
            ):
                continue
            order = physical.line_access_order(cars, line)
            members = [no for no in pending if by_no[no].get("Line") == line]
            if not members:
                continue
            deepest = max(order.index(no) for no in members)
            move = tuple(order[: deepest + 1])
            if (
                self.line_must_be_empty(line, pending)
                and len(move) != len(order)
            ):
                continue
            if physical.pull_equivalent([by_no[no] for no in move]) > remaining:
                continue
            successor = self.transitions.apply_step(
                self.node,
                physical.plan_step("Get", line, move),
            )
            if successor is None:
                continue
            accepted.append((
                (
                    0 if line in carried_targets else 1,
                    -sum(no in pending for no in move),
                    len(successor.state.operation_paths[-1]),
                    line,
                ),
                line,
                move,
            ))
        if not accepted:
            return None
        ranked = sorted(accepted)[:4]
        skip = ((2, 0, 0, ""), "", ())
        direct_owner = [item for item in ranked if item[1] == owner]
        current_spec = physical.TRACK_SPECS.get(current_line)
        options: list[tuple[tuple, str, tuple[str, ...]]] = (
            [*direct_owner, skip]
            if direct_owner
            and current_spec is not None
            and current_spec.track_type == "operation"
            else [skip, *ranked]
        )
        _rank, line, move = self.choose(options, f"cross_flow_join:{owner}")
        if not line:
            return None
        return self.join_source(line, move, owner)

    def join_source(
        self,
        line: str,
        move: tuple[str, ...],
        retained_owner: str,
    ) -> bool:
        boundary = len(self.node.state.carried_order)
        tail_owner = self.problem.target_by_no.get(
            self.node.state.carried_order[-1], ""
        ) if self.node.state.carried_order else ""
        retained = self.node.state.carried_order[:boundary]
        retained_ranks = [
            self.rank_by_no.get(no, 0)
            for no in retained
        ]
        retained_closes_target = bool(
            retained
            and all(self.problem.target_by_no.get(no) == line for no in retained)
            and (
                not physical.is_spotting_line(line)
                or (
                    all(retained_ranks)
                    and all(
                        right == left + 1
                        for left, right in zip(retained_ranks, retained_ranks[1:])
                    )
                )
            )
        )
        anchor_floor = (
            0
            if retained_closes_target
            else boundary - 1
            if tail_owner == line
            else boundary
        )
        successor = self.transitions.apply_step(
            self.node,
            physical.plan_step("Get", line, move),
        )
        if successor is None:
            return False
        self.commit(
            successor,
            "cross_flow_join",
            owner=retained_owner,
            joined_source=line,
            joined_move=list(move),
        )
        while len(self.node.state.carried_order) > anchor_floor:
            tail, owner, temporary = self.tail_intent(line, False)
            if not tail:
                self.failure = f"joined_tail_intent_missing:{line}"
                return False
            tail = self.prepare_tail_move(tail)
            if not tail:
                return False
            if len(self.node.state.carried_order) - len(tail) < anchor_floor:
                tail = tuple(self.node.state.carried_order[anchor_floor:])
                owners = {
                    self.problem.target_by_no.get(no, "")
                    if no in self.problem.unsatisfied_active(self.node.state)
                    else dict(self.node.carry_origins).get(no, line)
                    for no in tail
                }
                if len(owners) != 1:
                    self.failure = f"joined_anchor_owner_mismatch:{line}"
                    return False
                owner = next(iter(owners))
            if not self.place_tail(tail, owner, temporary):
                return False
        return True

    def target_dirty(self, target: str) -> bool:
        if not target:
            return True
        unsatisfied = self.problem.unsatisfied_active(self.node.state)
        by_no = {physical.car_no(car): car for car in self.node.state.cars}
        return any(
            by_no[no].get("Line") == target
            and self.problem.target_by_no.get(no) != target
            for no in unsatisfied
        )

    def resource_reserved(self, line: str) -> bool:
        pending_targets = {
            self.problem.target_by_no.get(no, "")
            for no in self.problem.unsatisfied_active(self.node.state)
            if self.problem.target_by_no.get(no)
        }
        return any(
            line in resource_gate_closure(target)
            for target in pending_targets
            if target != line
        )

    def complete_target_batch(self, target: str, move: tuple[str, ...]) -> bool:
        pending = {
            no
            for no in self.problem.unsatisfied_active(self.node.state)
            if self.problem.target_by_no.get(no) == target
        }
        return bool(pending) and pending <= set(move)

    def stage(
        self,
        move: tuple[str, ...],
        owner: str,
        *,
        reason: str = "target_window_assembly",
    ) -> bool:
        candidates = self.staging_candidates(move, owner)
        accepted: list[tuple[tuple, SearchNode, str, bool]] = []
        for rank, line in candidates:
            positions = self.transitions.planned_positions(self.node.state, line, move)
            if positions is None:
                continue
            successor = self.transitions.apply_step(
                self.node,
                physical.plan_step("Put", line, move, positions),
            )
            if successor is None:
                continue
            recovery = self.transitions.apply_step(
                successor,
                physical.plan_step("Get", line, move),
            )
            if recovery is None:
                continue
            accepted.append((rank, successor, line, False))
        if owner in physical.TRACK_SPECS:
            positions = self.transitions.planned_positions(self.node.state, owner, move)
            if positions is not None:
                successor = self.transitions.apply_step(
                    self.node,
                    physical.plan_step("Put", owner, move, positions),
                )
                if successor is not None:
                    recovery = self.transitions.apply_step(
                        successor,
                        physical.plan_step("Get", owner, move),
                    )
                    if recovery is not None:
                        accepted.append(((3, len(move), owner), successor, owner, True))
        if not accepted:
            self.failure = f"staging_assignment_infeasible:{owner}:{','.join(move)}"
            return False
        ranked_accepted = sorted(accepted, key=lambda item: item[0])[:6]
        _rank, successor, line, target_stack = self.choose(
            ranked_accepted,
            f"staging:{owner}",
        )
        if target_stack:
            self.commit(
                successor,
                "target_stack",
                owner=owner,
                staging_reason=reason,
            )
            return True
        existing = self.stacks.get(line)
        stack_rank_by_no = self.owner_stack_rank_by_no(owner)
        segment = segment_for(
            move,
            owner=owner,
            rank_by_no=stack_rank_by_no,
            protected=owner.startswith(("restore:", "lease:")),
        )
        stack = existing.prepend(segment) if existing else OwnedStack(line, (segment,))
        if stack is None:
            self.failure = f"staging_stack_prepend_rejected:{owner}:{line}"
            return False
        self.stacks[line] = stack
        self.staged_nos.update(move)
        self.leases.append(RecoveryLease(
            line=line,
            owner=owner,
            nos=move,
            gate_footprint=frozenset(physical.operation_approach_lines(line)),
        ))
        self.commit(
            successor,
            "stage",
            owner=owner,
            staging_line=line,
            staging_reason=reason,
        )
        return True

    def staging_candidates(
        self,
        move: tuple[str, ...],
        owner: str,
    ) -> list[tuple[tuple, str]]:
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        move_length = sum(physical.car_length(by_no[no]) for no in move)
        pending_sources = {
            by_no[no].get("Line") or ""
            for no in self.source_pending_nos()
        }
        required_approaches = set().union(*(
            approaches
            for source in pending_sources | set(self.stacks)
            for approaches in [physical.operation_approach_lines(source)]
            if len(approaches) == 1
        )) if pending_sources or self.stacks else set()
        pending_targets = {
            self.problem.target_by_no.get(no, "")
            for no in self.problem.unsatisfied_active(self.node.state)
            if self.problem.target_by_no.get(no)
        }
        reserved_resources = set().union(*(
            resource_gate_closure(target)
            for target in pending_targets
        )) if pending_targets else set()
        leased_gates = set().union(*(
            lease.gate_footprint for lease in self.leases
        )) if self.leases else set()
        carry_origins = set(dict(self.node.carry_origins).values())
        stack_rank_by_no = self.owner_stack_rank_by_no(owner)
        move_ranks = [stack_rank_by_no.get(no, 0) for no in move]
        candidates: list[tuple[tuple, str]] = []
        for line, spec in physical.TRACK_SPECS.items():
            if line in physical.DEPOT_TARGET_LINES or line == "卸轮线":
                continue
            if spec.track_type == "temporary" and pending_targets - {owner}:
                continue
            if spec.track_type not in {"storage", "temporary"}:
                continue
            if (
                line in pending_sources
                or line in reserved_resources
                or line in required_approaches
                or line in leased_gates
            ):
                continue
            if line in carry_origins or line == owner:
                continue
            stack = self.stacks.get(line)
            if stack and stack.owner != owner:
                continue
            if stack and physical.is_spotting_line(owner):
                existing_ranks = [
                    rank
                    for segment in stack.segments
                    for rank in segment.ranks
                ]
                if not monotone_stack_prepend_allowed(move_ranks, existing_ranks):
                    continue
            used = sum(
                physical.car_length(car)
                for car in cars
                if car.get("Line") == line
            )
            if used + move_length > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                continue
            route = self.problem.graph.route(self.node.state.loco_location.line, line)
            rank = (
                0 if stack else 1,
                1 if line in pending_targets else 0,
                len(route),
                -round(spec.length_m - used - move_length, 3),
                line,
            )
            candidates.append((rank, line))
        return sorted(candidates)

    def owner_stack_rank_by_no(self, owner: str) -> dict[str, int]:
        if not physical.is_spotting_line(owner):
            return self.rank_by_no
        participants = self.problem.owner_order(owner)
        if participants and all(
            self.rank_by_no.get(no, 0) > 0
            for no in participants
        ):
            participants = tuple(sorted(
                participants,
                key=lambda no: (self.rank_by_no[no], no),
            ))
        return {
            no: index
            for index, no in enumerate(participants, start=1)
        }

    def close_ranked_target(self, target: str) -> bool:
        self.refresh_target_ranks(target)
        desired = tuple(
            no
            for no, _rank in sorted(
                (
                    (no, rank)
                    for no, rank in self.rank_by_no.items()
                    if self.problem.target_by_no.get(no) == target
                    or (
                        no in self.problem.protected_nos
                        and target in self.problem.target_options(no)
                    )
                ),
                key=lambda item: (item[1], item[0]),
            )
        )
        if not desired:
            return self.close_simple_target(target)
        missing_staging = [
            no
            for no in desired
            if no in self.problem.unsatisfied_active(self.node.state)
            and no not in self.staged_nos
            and next(
                car.get("Line") for car in self.node.state.cars
                if physical.car_no(car) == no
            ) != target
        ]
        if missing_staging:
            self.failure = f"target_inbound_not_staged:{target}:{','.join(missing_staging)}"
            return False
        if not self.acquire_target_resources(target):
            return False
        existing = tuple(physical.line_access_order(list(self.node.state.cars), target))
        if existing and not self.apply(physical.plan_step("Get", target, existing)):
            return False
        while self.node.state.carried_order:
            carry = self.node.state.carried_order
            prefix = 0
            for actual, expected in zip(carry, desired):
                if actual != expected:
                    break
                prefix += 1
            if prefix == len(carry):
                break
            start = len(carry) - 1
            right_rank = self.rank_by_no.get(carry[-1], 0)
            while start > max(prefix, 0):
                left_rank = self.rank_by_no.get(carry[start - 1], 0)
                if not left_rank or left_rank + 1 != right_rank:
                    break
                start -= 1
                right_rank = left_rank
            if not self.stage(tuple(carry[start:]), target):
                return False

        while len(self.node.state.carried_order) < len(desired):
            next_no = desired[len(self.node.state.carried_order)]
            stack = next(
                (stack for stack in self.stacks.values() if stack.owner == target and stack.nos and stack.nos[0] == next_no),
                None,
            )
            if stack is None:
                self.failure = f"target_next_rank_inaccessible:{target}:{next_no}"
                return False
            remaining = desired[len(self.node.state.carried_order):]
            take = 0
            for actual, expected in zip(stack.nos, remaining):
                if actual != expected:
                    break
                take += 1
            move = stack.nos[:take]
            if not self.apply(physical.plan_step("Get", stack.line, move)):
                return False
            self.consume_stack(stack.line, move)
        if self.node.state.carried_order != desired:
            self.failure = f"target_consist_order_mismatch:{target}"
            return False
        positions = self.transitions.planned_positions(self.node.state, target, desired)
        if positions is None:
            self.failure = f"target_positions_missing:{target}"
            return False
        if not self.apply(physical.plan_step("Put", target, desired, positions)):
            return False
        return self.release_target_resources(target)

    def refresh_target_ranks(self, target: str) -> None:
        cars = list(self.node.state.cars)
        by_no = {physical.car_no(car): car for car in cars}
        existing = [
            no
            for no in physical.line_access_order(cars, target)
            if target in self.problem.target_options(no)
            and not (
                no in self.problem.active_nos
                and self.problem.target_by_no.get(no) != target
            )
        ]
        staged = [
            no
            for line in self.owner_lines(target)
            for no in self.stacks[line].nos
        ]
        participants = tuple(dict.fromkeys((*existing, *staged)))
        missing = [
            no for no in participants
            if not self.rank_by_no.get(no, 0)
        ]
        if not participants or not missing:
            return
        planning = [dict(car) for car in cars]
        participant_set = set(participants)
        target_order = tuple(physical.line_access_order(planning, target))
        if target_order:
            physical.apply_physical_get_order(planning, target, target_order)
        for line in sorted({
            by_no[no].get("Line") or ""
            for no in participants
            if (by_no[no].get("Line") or "") != target
        }):
            group = tuple(
                no
                for no in physical.line_access_order(planning, line)
                if no in participant_set
            )
            if group:
                physical.apply_physical_get_order(planning, line, group)
        positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in participants],
            target_line=target,
            cars=planning,
            depot_assignment=self.problem.depot_assignment,
            batch_nos=participant_set,
        )
        if len(positions) == len(participants):
            self.rank_by_no.update(positions)

    def acquire_target_resources(self, target: str) -> bool:
        resources = self.minimum_resource_release(target)
        if resources is None:
            self.failure = f"target_access_blocked_outside_lease:{target}"
            return False
        for resource in resources:
            order = tuple(physical.line_access_order(list(self.node.state.cars), resource))
            if not order:
                continue
            by_no = {physical.car_no(car): car for car in self.node.state.cars}
            if physical.pull_equivalent([by_no[no] for no in order]) > physical.PULL_LIMIT_EQUIVALENT:
                self.failure = f"resource_over_pull_limit:{target}:{resource}"
                return False
            if not self.apply(physical.plan_step("Get", resource, order)):
                return False
            transfer_owner = self.transferable_resource_owner(order)
            if transfer_owner:
                if not self.stage(
                    order,
                    transfer_owner,
                    reason="topology_resource_owner_transfer",
                ):
                    return False
                continue
            lease_owner = f"lease:{target}:{resource}"
            if self.owner_lines(resource):
                if not self.merge_resource_lease(order, resource, lease_owner):
                    self.failure = f"resource_owner_merge_rejected:{target}:{resource}"
                    return False
            elif not self.stage(
                order,
                lease_owner,
                reason="topology_resource_release",
            ):
                return False
        return True

    def transferable_resource_owner(self, order: tuple[str, ...]) -> str:
        unsatisfied = self.problem.unsatisfied_active(self.node.state)
        if not order or any(no not in unsatisfied for no in order):
            return ""
        owners = {
            self.problem.target_by_no.get(no, "")
            for no in order
        }
        return next(iter(owners)) if len(owners) == 1 and "" not in owners else ""

    def minimum_resource_release(self, target: str) -> tuple[str, ...] | None:
        """Return the smallest occupied lease subset that opens target access."""

        candidates = tuple(
            resource
            for resource in sorted(RESOURCE_GATES.get(target, ()))
            if physical.line_access_order(list(self.node.state.cars), resource)
        )
        if self.target_access_available(target, frozenset()):
            return ()
        for size in range(1, len(candidates) + 1):
            feasible = [
                subset
                for subset in combinations(candidates, size)
                if self.target_access_available(target, frozenset(subset))
            ]
            if feasible:
                return min(
                    feasible,
                    key=lambda subset: (
                        sum(
                            len(physical.line_access_order(
                                list(self.node.state.cars),
                                resource,
                            ))
                            for resource in subset
                        ),
                        subset,
                    ),
                )

        # An empty destination is reached later from its owner stack. Its route
        # cannot be judged from the current locomotive position yet.
        if not physical.line_access_order(list(self.node.state.cars), target):
            return ()
        return None

    def target_access_available(
        self,
        target: str,
        released_resources: frozenset[str],
    ) -> bool:
        cars = list(self.node.state.cars)
        target_order = tuple(physical.line_access_order(cars, target))
        released_nos = {
            physical.car_no(car)
            for car in cars
            if car.get("Line") in released_resources
        }
        carried = set(self.node.state.carried_order)
        moving = carried | set(target_order) | released_nos
        if target_order:
            occupied = physical.occupied_lines_for_get_route(cars, moving, target)
            approaches = physical.route_approach_lines_for_get(target)
        else:
            occupied = physical.occupied_lines_for_route(cars, moving)
            approaches = physical.route_approach_lines_for_put(target, cars, moving)
        route = self.problem.graph.route_avoiding_occupied(
            self.node.state.loco_location.line,
            target,
            occupied,
            source_departure_lines=physical.route_departure_lines_for_source(
                self.node.state.loco_location.line,
                cars,
                moving,
            ),
            target_approach_lines=approaches,
            cars=cars,
            moving_nos=moving,
            train_length_m=physical.train_length_for_nos(cars, carried),
        )
        return bool(route)

    def merge_resource_lease(
        self,
        blockers: tuple[str, ...],
        resource: str,
        lease_owner: str,
    ) -> bool:
        for line in self.owner_lines(resource):
            stack = self.stacks[line]
            positions = self.transitions.planned_positions(self.node.state, line, blockers)
            if positions is None:
                continue
            successor = self.transitions.apply_step(
                self.node,
                physical.plan_step("Put", line, blockers, positions),
            )
            if successor is None:
                continue
            combined = (*blockers, *stack.nos)
            self.stacks[line] = OwnedStack(line, (segment_for(
                combined,
                owner=lease_owner,
                rank_by_no=self.rank_by_no,
                protected=True,
            ),))
            self.staged_nos.update(blockers)
            self.leases.append(RecoveryLease(
                line=line,
                owner=lease_owner,
                nos=blockers,
                gate_footprint=frozenset(physical.operation_approach_lines(line)),
            ))
            self.commit(
                successor,
                "resource_owner_merge",
                resource=resource,
                staging_line=line,
            )
            return True
        return False

    def release_target_resources(self, target: str) -> bool:
        prefix = f"lease:{target}:"
        owners = sorted({
            stack.owner
            for stack in self.stacks.values()
            if stack.owner.startswith(prefix)
        })
        for owner in owners:
            resource = owner[len(prefix):]
            while self.owner_lines(owner):
                stack = self.stacks[self.owner_lines(owner)[0]]
                move = stack.nos
                if not self.apply(physical.plan_step("Get", stack.line, move)):
                    return False
                self.consume_stack(stack.line, move)
                while self.owner_lines(resource):
                    inbound = self.stacks[self.owner_lines(resource)[0]]
                    by_no = {physical.car_no(car): car for car in self.node.state.cars}
                    combined = (*self.node.state.carried_order, *inbound.nos)
                    if physical.pull_equivalent([by_no[no] for no in combined]) > physical.PULL_LIMIT_EQUIVALENT:
                        break
                    if not self.apply(physical.plan_step("Get", inbound.line, inbound.nos)):
                        return False
                    self.consume_stack(inbound.line, inbound.nos)
                combined_move = self.node.state.carried_order
                if not self.put(resource, combined_move):
                    self.failure = f"resource_restore_rejected:{target}:{resource}"
                    return False
        return True

    def close_ready_staged_targets(self) -> bool:
        while True:
            unsatisfied = self.problem.unsatisfied_active(self.node.state)
            by_no = {physical.car_no(car): car for car in self.node.state.cars}
            targets = {
                self.problem.target_by_no.get(no, "")
                for no in unsatisfied
                if self.problem.target_by_no.get(no)
            }
            ready = [
                target
                for target in targets
                if all(
                    no in self.staged_nos or by_no[no].get("Line") == target
                    for no in unsatisfied
                    if self.problem.target_by_no.get(no) == target
                )
                and self.owner_lines(target)
                and not self.target_dirty(target)
                and not self.resource_reserved(target)
            ]
            if not ready:
                return True
            ranked_ready = sorted(
                ready,
                key=lambda item: (
                    sum(
                        self.problem.target_by_no.get(no) == item
                        for no in unsatisfied
                    ),
                    -len(self.owner_lines(item)),
                    self.target_order(item),
                ),
            )
            target = self.choose(ranked_ready[:4], "ready_target")
            if physical.is_spotting_line(target):
                if not self.close_ranked_target(target):
                    return False
            elif not self.close_simple_target(target):
                return False

    def close_simple_target(self, target: str) -> bool:
        if not self.acquire_target_resources(target):
            return False
        for _round in range(30):
            if not self.owner_lines(target):
                return self.release_target_resources(target)
            stack = self.stacks[self.owner_lines(target)[0]]
            move = stack.nos
            if stack.line != target and not self.acquire_target_resources(stack.line):
                return False
            if not self.apply(physical.plan_step("Get", stack.line, move)):
                return False
            self.consume_stack(stack.line, move)
            if not self.put(target, move):
                self.failure = f"simple_target_put_rejected:{target}:{','.join(move)}"
                return False
            if stack.line != target and not self.release_target_resources(stack.line):
                return False
        self.failure = f"simple_target_direction_round_limit:{target}"
        return False

    def restore_temporary_stacks(self) -> bool:
        owners = sorted({
            stack.owner
            for stack in self.stacks.values()
            if stack.owner.startswith(("restore:", "lease:"))
        })
        for owner in owners:
            target = (
                owner.rsplit(":", 1)[1]
                if owner.startswith("lease:")
                else owner.split(":", 1)[1]
            )
            if owner.startswith("lease:"):
                for line in self.owner_lines(owner):
                    stack = self.stacks[line]
                    self.stacks[line] = OwnedStack(line, (segment_for(
                        stack.nos,
                        owner=target,
                        rank_by_no=self.rank_by_no,
                    ),))
                if not self.close_simple_target(target):
                    return False
                continue
            while self.owner_lines(owner):
                stack = self.stacks[self.owner_lines(owner)[0]]
                move = stack.nos
                if not self.apply(physical.plan_step("Get", stack.line, move)):
                    return False
                self.consume_stack(stack.line, move)
                if not self.put(target, move):
                    return False
        return True

    def target_order(self, target: str) -> tuple:
        pending = {
            self.problem.target_by_no.get(no, "")
            for no in self.problem.unsatisfied_active(self.node.state)
        }
        downstream = sum(resource in pending for resource in RESOURCE_GATES.get(target, ()))
        return (-downstream, target)

    def owner_lines(self, owner: str) -> list[str]:
        return sorted(line for line, stack in self.stacks.items() if stack.owner == owner and stack.nos)

    def consume_stack(self, line: str, move: tuple[str, ...]) -> None:
        stack = self.stacks[line]
        if stack.nos[: len(move)] != move:
            raise AssertionError(f"staging prefix mismatch:{line}")
        for no in move:
            self.staged_nos.discard(no)
        remaining = stack.consume(move)
        if remaining:
            self.stacks[line] = remaining
        else:
            del self.stacks[line]
        moved = set(move)
        updated_leases: list[RecoveryLease] = []
        for lease in self.leases:
            if lease.line != line or moved.isdisjoint(lease.nos):
                updated_leases.append(lease)
                continue
            lease_nos = tuple(no for no in lease.nos if no not in moved)
            if lease_nos:
                updated_leases.append(RecoveryLease(
                    line=lease.line,
                    owner=lease.owner,
                    nos=lease_nos,
                    gate_footprint=lease.gate_footprint,
                ))
        self.leases = updated_leases

    def put(self, line: str, move: tuple[str, ...]) -> bool:
        positions = self.transitions.planned_positions(self.node.state, line, move)
        if positions is None:
            return False
        return self.apply(physical.plan_step("Put", line, move, positions), quiet=True)

    def apply(self, step: physical.PlanStep, *, quiet: bool = False) -> bool:
        successor = self.transitions.apply_step(self.node, step)
        if successor is None:
            if not quiet:
                self.failure = (
                    f"physical_step_rejected:{len(self.node.steps) + 1}:"
                    f"{step.action}:{step.line}:{','.join(step.move_car_nos)}"
                )
            return False
        self.commit(successor, "operation")
        return True

    def commit(self, successor: SearchNode, event: str, **extra: object) -> None:
        step = successor.steps[-1]
        self.node = successor
        if (
            not successor.state.carried_order
            and not self.problem.protected_damage(successor.state)
        ):
            self.closed_node = successor
            self.closed_leases = tuple(self.leases)
        self.trace.append({
            "event": event,
            "hook": successor.cost.hooks,
            "action": step.action,
            "line": step.line,
            "move": list(step.move_car_nos),
            "carry": list(successor.state.carried_order),
            "unsatisfied": len(self.problem.unsatisfied_active(successor.state)),
            **extra,
        })
