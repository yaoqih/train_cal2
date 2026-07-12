from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable

from .model import Duty, EpisodeChoice, SCRATCH_LINES, UNWHEEL, Stage2Problem
from .yard import Operation, Yard, YardState


LOCAL_STATE_LIMIT = 12_000


@dataclass(frozen=True)
class EpisodePlan:
    choice: EpisodeChoice
    state: YardState
    collector: tuple[str, ...]
    open_unload: tuple[str, ...]
    absorbed_choices: tuple[EpisodeChoice, ...]
    operations: tuple[Operation, ...]
    cost: tuple[int, int, int, int]
    scratch_line: str
    expansions: int


class EpisodePlanner:
    """Exact local stack transducer between canonical Stage 2 boundaries."""

    def __init__(
        self,
        problem: Stage2Problem,
        yard: Yard,
        deadline: Callable[[], bool],
    ) -> None:
        self.problem = problem
        self.yard = yard
        self.deadline = deadline
        self.cache: dict[
            tuple[
                YardState,
                tuple[str, ...],
                tuple[str, ...],
                EpisodeChoice,
                tuple[EpisodeChoice, ...],
            ],
            EpisodePlan | None,
        ] = {}
        self.rejections: Counter[str] = Counter()
        self.expansions = 0

    def plan(
        self,
        start: YardState,
        collector: tuple[str, ...],
        open_unload: tuple[str, ...],
        choice: EpisodeChoice,
        absorbable_choices: tuple[EpisodeChoice, ...] = (),
    ) -> EpisodePlan | None:
        key = (start, collector, open_unload, choice, absorbable_choices)
        if key in self.cache:
            return self.cache[key]
        if start.held != (*collector, *open_unload):
            raise ValueError("noncanonical_stage2_boundary")
        if choice.relocate:
            answer = self._plan_relocation(start, collector, open_unload, choice)
            self.cache[key] = answer
            return answer

        candidates: list[EpisodePlan] = []
        operation_lower_bound = int(bool(choice.selected))
        absorb_by_source = {item.source: item for item in absorbable_choices}
        scratch_options = ("",) + tuple(line for line in SCRATCH_LINES if line != choice.source)
        for scratch in scratch_options:
            absorb_choice = absorb_by_source.get(scratch)
            variants = (None, absorb_choice) if absorb_choice else (None,)
            for absorbed in variants:
                result = self._search(
                    start,
                    collector,
                    open_unload,
                    choice,
                    scratch,
                    absorbed,
                )
                if result is not None:
                    candidates.append(result)
                    # A useful scratch lease requires a source Get, a lease Put,
                    # a recovery Get, and at least one operation that resolves the
                    # boundary which made the lease necessary.  It therefore
                    # cannot tie a no-scratch episode that closes in <= 3 rows.
                    if result.cost[0] == operation_lower_bound or (
                        not scratch and result.cost[0] <= 3
                    ):
                        self.cache[key] = result
                        return result
                if self.deadline():
                    break
            if self.deadline():
                break
        answer = min(
            candidates,
            key=lambda item: (
                item.cost[0] - len(item.absorbed_choices),
                item.cost[1],
                item.cost[2],
                item.cost[3],
                -len(item.absorbed_choices),
                item.open_unload,
                item.collector,
                item.scratch_line,
            ),
            default=None,
        )
        self.cache[key] = answer
        return answer

    def _plan_relocation(
        self,
        start: YardState,
        collector: tuple[str, ...],
        open_unload: tuple[str, ...],
        choice: EpisodeChoice,
    ) -> EpisodePlan | None:
        candidates: list[EpisodePlan] = []
        for flush_first in (False, True):
            if flush_first and not open_unload:
                continue
            state = start
            operations: list[Operation] = []
            cost = (0, 0, 0, 0)
            if flush_first:
                flushed = self.yard.put(
                    state,
                    UNWHEEL,
                    open_unload,
                    purpose="flush_unwheel_for_corridor",
                    episode=choice.source,
                )
                if not flushed.accepted:
                    self.rejections[flushed.rejection] += 1
                    continue
                assert flushed.state is not None and flushed.operation is not None
                state = flushed.state
                operations.append(flushed.operation)
                cost = self.yard.add_cost(cost, self.yard.operation_cost(flushed.operation))
            picked = self.yard.get(
                state,
                choice.source,
                choice.relocate,
                purpose="release_depot_corridor",
                episode=choice.source,
            )
            if not picked.accepted:
                self.rejections[picked.rejection] += 1
                continue
            assert picked.state is not None and picked.operation is not None
            state = picked.state
            operations.append(picked.operation)
            cost = self.yard.add_cost(cost, self.yard.operation_cost(picked.operation))
            placed = self.yard.put(
                state,
                choice.relocate_to,
                choice.relocate,
                purpose="relocate_depot_corridor",
                episode=choice.source,
            )
            if not placed.accepted:
                self.rejections[placed.rejection] += 1
                continue
            assert placed.state is not None and placed.operation is not None
            state = placed.state
            operations.append(placed.operation)
            cost = self.yard.add_cost(cost, self.yard.operation_cost(placed.operation))
            next_open = () if flush_first else open_unload
            if state.held != (*collector, *next_open):
                raise AssertionError("corridor_episode_left_noncanonical_consist")
            candidates.append(
                EpisodePlan(
                    choice=choice,
                    state=state,
                    collector=collector,
                    open_unload=next_open,
                    absorbed_choices=(),
                    operations=tuple(operations),
                    cost=cost,
                    scratch_line=choice.relocate_to,
                    expansions=0,
                )
            )
        return min(
            candidates,
            key=lambda item: (item.cost, item.scratch_line),
            default=None,
        )

    def _search(
        self,
        start: YardState,
        collector: tuple[str, ...],
        open_unload: tuple[str, ...],
        choice: EpisodeChoice,
        scratch: str,
        absorbed_choice: EpisodeChoice | None,
    ) -> EpisodePlan | None:
        start_lines = self.yard.line_map(start)
        source_start = start_lines.get(choice.source, ())
        selected = choice.selected
        if not selected <= set(source_start):
            self.rejections["episode_selected_car_not_on_source"] += 1
            return None
        source_target = tuple(no for no in source_start if no not in selected)
        scratch_base = start_lines.get(scratch, ()) if scratch else ()
        scratch_target = () if absorbed_choice else scratch_base
        local_nos = frozenset((*source_start, *(scratch_base if absorbed_choice else ())))
        unload_nos = frozenset((*open_unload, *choice.unload))
        collector_nos = frozenset(
            (*collector, *choice.collect, *((absorbed_choice.collect) if absorbed_choice else ()))
        )

        queue: list[tuple[tuple[int, int, int, int], int, YardState]] = [
            ((0, 0, 0, 0), 0, start)
        ]
        best: dict[YardState, tuple[int, int, int, int]] = {
            start: (0, 0, 0, 0)
        }
        previous: dict[YardState, tuple[YardState, Operation]] = {}
        sequence = 1
        expansions = 0

        while queue:
            cost, _sequence, state = heapq.heappop(queue)
            if best.get(state) != cost:
                continue
            goal = self._goal_partition(
                state,
                collector,
                collector_nos,
                unload_nos,
                choice.source,
                source_target,
                scratch,
                scratch_target,
            )
            if goal is not None:
                next_collector, next_open = goal
                operations = self._reconstruct(previous, state)
                self.expansions += expansions
                return EpisodePlan(
                    choice=choice,
                    state=state,
                    collector=next_collector,
                    open_unload=next_open,
                    absorbed_choices=((absorbed_choice,) if absorbed_choice else ()),
                    operations=operations,
                    cost=cost,
                    scratch_line=scratch,
                    expansions=expansions,
                )
            if self.deadline() or expansions >= LOCAL_STATE_LIMIT:
                self.rejections[
                    "episode_time_budget_exhausted"
                    if self.deadline()
                    else "episode_state_limit_exhausted"
                ] += 1
                self.expansions += expansions
                return None
            expansions += 1

            for transition in self._neighbors(
                state=state,
                choice=choice,
                local_nos=local_nos,
                unload_nos=unload_nos,
                collector=collector,
                source_target=source_target,
                scratch=scratch,
                scratch_base=scratch_base,
                absorb_scratch=absorbed_choice is not None,
            ):
                if not transition.accepted:
                    self.rejections[transition.rejection or "episode_transition_rejected"] += 1
                    continue
                assert transition.state is not None
                assert transition.operation is not None
                delta = self.yard.operation_cost(transition.operation)
                next_cost = self.yard.add_cost(cost, delta)
                if next_cost >= best.get(
                    transition.state, (10**9, 10**9, 10**9, 10**9)
                ):
                    continue
                best[transition.state] = next_cost
                previous[transition.state] = (state, transition.operation)
                heapq.heappush(queue, (next_cost, sequence, transition.state))
                sequence += 1

        self.expansions += expansions
        self.rejections["episode_no_legal_transduction"] += 1
        return None

    def _neighbors(
        self,
        *,
        state: YardState,
        choice: EpisodeChoice,
        local_nos: frozenset[str],
        unload_nos: frozenset[str],
        collector: tuple[str, ...],
        source_target: tuple[str, ...],
        scratch: str,
        scratch_base: tuple[str, ...],
        absorb_scratch: bool,
    ) -> Iterable:
        lines = self.yard.line_map(state)
        source_now = lines.get(choice.source, ())
        selected_on_source = [no for no in source_now if no in choice.selected]
        if selected_on_source:
            deepest = max(source_now.index(no) for no in selected_on_source) + 1
            for length in range(deepest, 0, -1):
                move = source_now[:length]
                purpose = (
                    "extract_obligation"
                    if any(no in choice.selected for no in move)
                    else "lease_front_blocker"
                )
                yield self.yard.get(
                    state,
                    choice.source,
                    move,
                    purpose=purpose,
                    episode=choice.source,
                )

        if scratch:
            scratch_now = lines.get(scratch, ())
            temporary = (
                scratch_now
                if absorb_scratch
                else self._temporary_prefix(scratch_now, scratch_base)
            )
            for length in range(len(temporary), 0, -1):
                yield self.yard.get(
                    state,
                    scratch,
                    temporary[:length],
                    purpose="recover_lease",
                    episode=choice.source,
                )

        unload_tail = self._tail_in(state.held, unload_nos)
        if unload_tail:
            yield self.yard.put(
                state,
                UNWHEEL,
                unload_tail,
                purpose="complete_unwheel_group",
                episode=choice.source,
            )

        for target, move in self._defer_puts(state.held, local_nos):
            yield self.yard.put(
                state,
                target,
                move,
                purpose="complete_deferred_target",
                episode=choice.source,
            )

        for move in self._source_restore_suffixes(
            state.held,
            lines.get(choice.source, ()),
            source_target,
        ):
            yield self.yard.put(
                state,
                choice.source,
                move,
                purpose="restore_source_order",
                episode=choice.source,
            )

        if scratch:
            max_local_tail = 0
            for no in reversed(state.held):
                if no not in local_nos or no in collector:
                    break
                max_local_tail += 1
            for length in range(max_local_tail, 0, -1):
                move = state.held[-length:]
                yield self.yard.put(
                    state,
                    scratch,
                    move,
                    purpose="lease_stack_block",
                    episode=choice.source,
                )

    def _goal_partition(
        self,
        state: YardState,
        previous_collector: tuple[str, ...],
        collector_nos: frozenset[str],
        unload_nos: frozenset[str],
        source: str,
        source_target: tuple[str, ...],
        scratch: str,
        scratch_base: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        lines = self.yard.line_map(state)
        source_now = lines.get(source, ())
        source_business = (
            tuple(no for no in source_now if no not in unload_nos)
            if source == UNWHEEL
            else source_now
        )
        if not self._source_boundary_ok(
            lines,
            source,
            source_business,
            source_target,
        ):
            return None
        if scratch and lines.get(scratch, ()) != scratch_base:
            return None
        collector_count = len(collector_nos)
        if len(state.held) < collector_count:
            return None
        next_collector = state.held[:collector_count]
        next_open = state.held[collector_count:]
        if next_collector[: len(previous_collector)] != previous_collector:
            return None
        if frozenset(next_collector) != collector_nos:
            return None
        if not self.problem.collector_pattern_ok(next_collector):
            return None
        if any(no not in unload_nos for no in next_open):
            return None
        unwheel_now = set(lines.get(UNWHEEL, ()))
        if frozenset(next_open) | (unload_nos & unwheel_now) != unload_nos:
            return None
        if set(next_open) & unwheel_now:
            return None
        return next_collector, next_open

    def _source_boundary_ok(
        self,
        lines: dict[str, tuple[str, ...]],
        source: str,
        source_now: tuple[str, ...],
        source_target: tuple[str, ...],
    ) -> bool:
        expected_on_source: list[str] = []
        line_by_no = {
            no: line
            for line, nos in lines.items()
            for no in nos
        }
        for no in source_target:
            current_line = line_by_no.get(no, "")
            if current_line == source:
                expected_on_source.append(no)
                continue
            task = self.problem.tasks[no]
            targets = {
                str(value) for value in self.problem.meta[no].get("TargetLines") or ()
            }
            if task.duty is Duty.DEFER and current_line in targets:
                continue
            return False
        return tuple(expected_on_source) == source_now

    def _defer_puts(
        self,
        held: tuple[str, ...],
        local_nos: frozenset[str],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        tail_length = 0
        for no in reversed(held):
            if no not in local_nos or self.problem.tasks[no].duty is not Duty.DEFER:
                break
            tail_length += 1
        output: list[tuple[str, tuple[str, ...]]] = []
        for length in range(tail_length, 0, -1):
            move = held[-length:]
            targets = set(self.problem.meta[move[0]].get("TargetLines") or ())
            for no in move[1:]:
                targets &= set(self.problem.meta[no].get("TargetLines") or ())
            for target in sorted(targets):
                if target in SCRATCH_LINES:
                    output.append((target, move))
        return tuple(output)

    @staticmethod
    def _temporary_prefix(
        current: tuple[str, ...], base: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not base:
            return current
        if len(current) < len(base) or current[-len(base) :] != base:
            return ()
        return current[: -len(base)]

    @staticmethod
    def _tail_in(held: tuple[str, ...], allowed: frozenset[str]) -> tuple[str, ...]:
        start = len(held)
        while start > 0 and held[start - 1] in allowed:
            start -= 1
        return held[start:] if start < len(held) else ()

    @staticmethod
    def _source_restore_suffixes(
        held: tuple[str, ...],
        source_now: tuple[str, ...],
        source_target: tuple[str, ...],
    ) -> tuple[tuple[str, ...], ...]:
        output: list[tuple[str, ...]] = []
        for length in range(1, len(held) + 1):
            move = held[-length:]
            after = (*move, *source_now)
            if len(after) <= len(source_target) and source_target[-len(after) :] == after:
                output.append(move)
        return tuple(reversed(output))

    @staticmethod
    def _reconstruct(
        previous: dict[YardState, tuple[YardState, Operation]],
        state: YardState,
    ) -> tuple[Operation, ...]:
        operations: list[Operation] = []
        while state in previous:
            prior, operation = previous[state]
            operations.append(operation)
            state = prior
        operations.reverse()
        return tuple(operations)
