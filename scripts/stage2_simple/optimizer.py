from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .episodes import EpisodePlan, EpisodePlanner
from .model import DEPOT_OUT, EpisodeChoice, STORE4, Store4Kind, Stage2Problem, UNWHEEL
from .yard import Operation, Yard, YardState


GLOBAL_STATE_LIMIT = 100_000
INF_COST = (10**9,) * 7


@dataclass(frozen=True)
class BoundaryState:
    completed_mask: int
    yard: YardState
    collector: tuple[str, ...]
    open_unload: tuple[str, ...]


@dataclass(frozen=True)
class GlobalStep:
    source: str
    choices: tuple[EpisodeChoice, ...]
    operations: tuple[Operation, ...]


@dataclass(frozen=True)
class OptimizationResult:
    state: YardState | None
    operations: tuple[Operation, ...]
    decisions: tuple[EpisodeChoice, ...]
    cost: tuple[int, int, int, int, int, int, int] | None
    expansions: int
    episode_expansions: int
    reasons: tuple[str, ...]


class Stage2Optimizer:
    """One label-setting search on an acyclic line-completion graph."""

    def __init__(
        self,
        problem: Stage2Problem,
        yard: Yard,
        deadline: Callable[[], bool],
    ) -> None:
        self.problem = problem
        self.yard = yard
        self.deadline = deadline
        self.episodes = EpisodePlanner(problem, yard, deadline)
        self.lines = problem.source_lines
        self.all_mask = (1 << len(self.lines)) - 1
        self.rejections: Counter[str] = Counter()

    def solve(self) -> OptimizationResult:
        start = BoundaryState(0, self.yard.initial_state, (), ())
        queue: list[
            tuple[tuple[int, int, int, int, int, int, int], int, BoundaryState]
        ] = [((0, 0, 0, 0, 0, 0, 0), 0, start)]
        best: dict[BoundaryState, tuple[int, int, int, int, int, int, int]] = {
            start: (0, 0, 0, 0, 0, 0, 0)
        }
        previous: dict[BoundaryState, tuple[BoundaryState, GlobalStep]] = {}
        sequence = 1
        expansions = 0
        best_goal: tuple[
            tuple[int, int, int, int, int, int, int],
            BoundaryState,
            YardState,
            tuple[Operation, ...],
        ] | None = None

        while queue:
            cost, _sequence, state = heapq.heappop(queue)
            if best.get(state) != cost:
                continue
            if best_goal is not None and cost >= best_goal[0]:
                break
            if self.deadline():
                return self._failure(expansions, "stage2_time_budget_exhausted")
            if expansions >= GLOBAL_STATE_LIMIT:
                return self._failure(expansions, "stage2_state_limit_exhausted")
            expansions += 1

            if state.completed_mask == self.all_mask:
                terminal = self._finish(state)
                if terminal is None:
                    continue
                final_state, final_ops = terminal
                goal_cost = self._add_cost(cost, self._operation_delta(final_ops))
                goal_cost = (
                    *goal_cost[:6],
                    goal_cost[6] + self._collector_fragment_penalty(state.collector),
                )
                candidate = (goal_cost, state, final_state, final_ops)
                if best_goal is None or candidate[0] < best_goal[0]:
                    best_goal = candidate
                continue

            for index, source in enumerate(self.lines):
                bit = 1 << index
                if state.completed_mask & bit:
                    continue
                for choice in self.problem.choices(source):
                    if not self._choice_can_extend(state, choice, cost):
                        continue
                    absorbable = self._absorbable_choices(state, source)
                    episode = self.episodes.plan(
                        state.yard,
                        state.collector,
                        state.open_unload,
                        choice,
                        absorbable,
                    )
                    if episode is None:
                        self.rejections[f"episode_unavailable:{source}"] += 1
                        continue
                    completed_mask = state.completed_mask | bit
                    for absorbed in episode.absorbed_choices:
                        completed_mask |= 1 << self.lines.index(absorbed.source)
                    next_state = BoundaryState(
                        completed_mask=completed_mask,
                        yard=episode.state,
                        collector=episode.collector,
                        open_unload=episode.open_unload,
                    )
                    edge = self._episode_delta(episode)
                    next_cost = self._add_cost(cost, edge)
                    if next_cost >= best.get(next_state, INF_COST):
                        continue
                    best[next_state] = next_cost
                    previous[next_state] = (
                        state,
                        GlobalStep(
                            source,
                            (choice, *episode.absorbed_choices),
                            episode.operations,
                        ),
                    )
                    heapq.heappush(queue, (next_cost, sequence, next_state))
                    sequence += 1

        if best_goal is None:
            reason = "stage2_no_solution_in_episode_model"
            if self.rejections:
                reason = self.rejections.most_common(1)[0][0]
            return self._failure(expansions, reason)

        goal_cost, boundary, final_state, final_ops = best_goal
        steps = self._reconstruct(previous, boundary)
        operations = tuple(
            operation
            for step in steps
            for operation in step.operations
        ) + final_ops
        decisions = tuple(choice for step in steps for choice in step.choices)
        return OptimizationResult(
            state=final_state,
            operations=operations,
            decisions=decisions,
            cost=goal_cost,
            expansions=expansions,
            episode_expansions=self.episodes.expansions,
            reasons=(),
        )

    def _choice_can_extend(
        self,
        state: BoundaryState,
        choice: EpisodeChoice,
        cost: tuple[int, int, int, int, int, int, int],
    ) -> bool:
        if (
            cost[0] + len(choice.deferred_final)
            > self.problem.minimum_final_deferrals
        ):
            self.rejections["nonminimal_final_deferral"] += 1
            return False
        if (
            cost[1] + len(choice.deferred_optional)
            > self.problem.minimum_optional_deferrals
        ):
            self.rejections["nonminimal_optional_deferral"] += 1
            return False
        future_collector = (*state.collector, *choice.collect)
        if self.problem.pull_equivalent(future_collector) > 20:
            self.rejections["collector_pull_limit"] += 1
            return False
        if (
            any(
                self.problem.store4_kind(no) is Store4Kind.C4
                for no in state.collector
            )
            and any(
                self.problem.store4_kind(no) is Store4Kind.OFF
                for no in choice.collect
            )
        ):
            self.rejections["collector_dfa_rejects_off_after_c4"] += 1
            return False
        return True

    def _finish(
        self, boundary: BoundaryState
    ) -> tuple[YardState, tuple[Operation, ...]] | None:
        state = boundary.yard
        operations: list[Operation] = []
        if state.held != (*boundary.collector, *boundary.open_unload):
            self.rejections["terminal_noncanonical_held"] += 1
            return None
        if boundary.open_unload:
            transition = self.yard.put(
                state,
                UNWHEEL,
                boundary.open_unload,
                purpose="flush_unwheel_tail",
                episode="terminal",
            )
            if not transition.accepted:
                self.rejections[transition.rejection or "terminal_unwheel_flush"] += 1
                return None
            assert transition.state is not None and transition.operation is not None
            state = transition.state
            operations.append(transition.operation)
        if state.held != boundary.collector:
            self.rejections["terminal_collector_mismatch"] += 1
            return None
        if not boundary.collector:
            return state, tuple(operations)
        if not self.problem.collector_pattern_ok(boundary.collector):
            self.rejections["terminal_store4_pattern"] += 1
            return None
        if not self.problem.collector_closed_door_ok(boundary.collector):
            self.rejections["terminal_store4_closed_door"] += 1
            return None
        transition = self.yard.put(
            state,
            STORE4,
            boundary.collector,
            purpose="commit_store4_collector",
            episode="terminal",
            final_store4=True,
        )
        if not transition.accepted:
            self.rejections[transition.rejection or "terminal_store4_put"] += 1
            return None
        assert transition.state is not None and transition.operation is not None
        operations.append(transition.operation)
        return transition.state, tuple(operations)

    def _episode_delta(
        self, episode: EpisodePlan
    ) -> tuple[int, int, int, int, int, int, int]:
        choices = (episode.choice, *episode.absorbed_choices)
        return (
            sum(len(choice.deferred_final) for choice in choices),
            sum(len(choice.deferred_optional) for choice in choices),
            episode.cost[0],
            episode.cost[1],
            episode.cost[2],
            episode.cost[3],
            0,
        )

    def _absorbable_choices(
        self, state: BoundaryState, active_source: str
    ) -> tuple[EpisodeChoice, ...]:
        lines = self.yard.line_map(state.yard)
        output: list[EpisodeChoice] = []
        for index, source in enumerate(self.lines):
            if source == active_source or source not in DEPOT_OUT:
                continue
            if state.completed_mask & (1 << index):
                continue
            current = lines.get(source, ())
            if not current:
                continue
            for choice in self.problem.choices(source):
                if (
                    choice.collect == current
                    and not choice.unload
                    and not choice.deferred_final
                    and not choice.deferred_optional
                    and not choice.relocate
                ):
                    output.append(choice)
                    break
        return tuple(output)

    def _operation_delta(
        self, operations: tuple[Operation, ...]
    ) -> tuple[int, int, int, int, int, int, int]:
        hooks = defer_restore = lian7 = route = 0
        for operation in operations:
            delta = self.yard.operation_cost(operation)
            hooks += delta[0]
            defer_restore += delta[1]
            lian7 += delta[2]
            route += delta[3]
        return (0, 0, hooks, defer_restore, lian7, route, 0)

    def _collector_fragment_penalty(self, collector: tuple[str, ...]) -> int:
        keys = [
            self.problem.target_key(no)
            for no in collector
            if self.problem.store4_kind(no) is Store4Kind.OFF
        ]
        return sum(left != right for left, right in zip(keys, keys[1:]))

    @staticmethod
    def _add_cost(
        left: tuple[int, int, int, int, int, int, int],
        right: tuple[int, int, int, int, int, int, int],
    ) -> tuple[int, int, int, int, int, int, int]:
        return tuple(left[index] + right[index] for index in range(7))

    @staticmethod
    def _reconstruct(
        previous: dict[BoundaryState, tuple[BoundaryState, GlobalStep]],
        state: BoundaryState,
    ) -> tuple[GlobalStep, ...]:
        steps: list[GlobalStep] = []
        while state in previous:
            prior, step = previous[state]
            steps.append(step)
            state = prior
        steps.reverse()
        return tuple(steps)

    def _failure(self, expansions: int, reason: str) -> OptimizationResult:
        reasons = [reason]
        reasons.extend(
            f"{key}:{count}"
            for key, count in (self.rejections + self.episodes.rejections).most_common(10)
            if key != reason
        )
        return OptimizationResult(
            state=None,
            operations=(),
            decisions=(),
            cost=None,
            expansions=expansions,
            episode_expansions=self.episodes.expansions,
            reasons=tuple(reasons),
        )
