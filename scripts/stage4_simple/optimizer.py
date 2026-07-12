from __future__ import annotations

import heapq
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import count

from .construct import SourceWindowGenerator, SourceWindowResult
from .domain import ContractStatus
from .planner import (
    ContractPlanner,
    PlanningCheckpoint,
    PlanningConfig,
    PlanningFailure,
    PlanningResult,
)
from .search import OperationTransitions, Stage4Problem


@dataclass(frozen=True)
class OptimizationConfig:
    time_budget_seconds: float = 300.0
    max_labels: int = 64
    max_expansions: int = 30_000


@dataclass(frozen=True)
class OptimizationResult:
    plan: PlanningResult
    evaluated_labels: int
    feasible_labels: int
    elapsed_seconds: float
    stop_reason: str


class BlockFlowOptimizer:
    """Shortest-path search over closed target/source-window sessions."""

    MAX_SOURCE_EDGES = 4
    FAST_SOURCE_ATTEMPTS = 6
    MAX_SOURCE_ATTEMPTS = 64
    MAX_TERMINAL_SOURCE_ATTEMPTS = 128

    def __init__(self, problem: Stage4Problem, config: OptimizationConfig) -> None:
        self.problem = problem
        self.config = config
        self.transitions = OperationTransitions(problem)

    def solve(self) -> OptimizationResult:
        started = time.monotonic()
        deadline = started + self.config.time_budget_seconds
        head = ContractPlanner(self.problem, self._planning_config(deadline))
        head.resolve_depot_rehook()
        initial = head.builder.checkpoint()
        if initial.node.state.carried_order or initial.stacks or initial.leases:
            raise PlanningFailure("depot_rehook_checkpoint_not_closed")

        serial = count()
        frontier: list[tuple[tuple, int, PlanningCheckpoint]] = []
        heapq.heappush(frontier, (self._priority(initial), next(serial), initial))
        best_cost = {self._signature(initial): initial.node.cost}
        best_complete: PlanningCheckpoint | None = None
        best_partial = initial
        feasible_labels = 0
        evaluated_labels = 0
        rejected = Counter()
        stop_reason = "label_frontier_exhausted"

        while frontier:
            if time.monotonic() >= deadline:
                stop_reason = "time_budget_exhausted"
                break
            if evaluated_labels >= self.config.max_labels:
                stop_reason = "label_budget_exhausted"
                break
            _rank, _serial, checkpoint = heapq.heappop(frontier)
            signature = self._signature(checkpoint)
            if checkpoint.node.cost != best_cost.get(signature):
                continue
            evaluated_labels += 1

            if self._complete(checkpoint):
                feasible_labels += 1
                if (
                    best_complete is None
                    or checkpoint.node.cost < best_complete.node.cost
                ):
                    best_complete = checkpoint
                continue
            if self._partial_rank(checkpoint) < self._partial_rank(best_partial):
                best_partial = checkpoint

            successors: list[PlanningCheckpoint] = []
            target, reason = self._target_edge(checkpoint, deadline)
            if target is not None:
                successors.append(target)
            elif reason:
                rejected[reason] += 1
            source_edges, source_rejections = self._source_edges(checkpoint)
            successors.extend(source_edges)
            rejected.update(source_rejections)

            for successor in successors:
                if successor.node.state.carried_order:
                    rejected["session_left_open_carry"] += 1
                    continue
                if self._uncovered_protected_damage(successor):
                    rejected["session_left_protected_damage"] += 1
                    continue
                successor = self._close_satisfied_contracts(successor)
                successor_signature = self._signature(successor)
                previous = best_cost.get(successor_signature)
                if previous is not None and previous <= successor.node.cost:
                    continue
                best_cost[successor_signature] = successor.node.cost
                if self._complete(successor):
                    feasible_labels += 1
                    if (
                        best_complete is None
                        or successor.node.cost < best_complete.node.cost
                    ):
                        best_complete = successor
                heapq.heappush(frontier, (
                    self._priority(successor),
                    next(serial),
                    successor,
                ))

        chosen = best_complete or best_partial
        reason = "complete" if best_complete is not None else self._failure_reason(rejected)
        return OptimizationResult(
            plan=self._planning_result(chosen, reason, started),
            evaluated_labels=evaluated_labels,
            feasible_labels=feasible_labels,
            elapsed_seconds=round(time.monotonic() - started, 3),
            stop_reason=stop_reason,
        )

    def _target_edge(
        self,
        checkpoint: PlanningCheckpoint,
        deadline: float,
    ) -> tuple[PlanningCheckpoint | None, str]:
        planner = ContractPlanner(self.problem, self._planning_config(deadline))
        result = planner.advance_remaining(checkpoint)
        if result.reason not in {"session_closed", "complete"}:
            return None, result.reason
        successor = planner.builder.checkpoint()
        if self._signature(successor) == self._signature(checkpoint):
            return None, "target_window_no_progress"
        return successor, ""

    def _source_edges(
        self,
        checkpoint: PlanningCheckpoint,
    ) -> tuple[list[PlanningCheckpoint], Counter[str]]:
        accepted: dict[tuple, PlanningCheckpoint] = {}
        rejected: Counter[str] = Counter()
        queued = {()}
        evaluated: set[tuple[int, ...]] = set()
        frontier: list[tuple[tuple, tuple[int, ...]]] = [
            (self._choice_priority(()), ())
        ]
        while (
            frontier
            and len(evaluated) < self._source_attempt_limit(accepted.values())
        ):
            _rank, choices = heapq.heappop(frontier)
            if choices in evaluated:
                continue
            evaluated.add(choices)
            result = self._run_source_edge(checkpoint, choices)
            for child in self._source_choice_vectors(result):
                if child in queued:
                    continue
                queued.add(child)
                heapq.heappush(frontier, (self._choice_priority(child), child))
            if result.reason not in {"session_closed", "complete"}:
                rejected[result.reason] += 1
                continue
            successor = self._source_checkpoint(checkpoint, result)
            signature = self._signature(successor)
            if signature == self._signature(checkpoint):
                continue
            previous = accepted.get(signature)
            if previous is not None and previous.node.cost <= successor.node.cost:
                continue
            accepted[signature] = successor
        ranked = sorted(
            accepted.values(),
            key=self._priority,
        )
        return ranked[: self.MAX_SOURCE_EDGES], rejected

    def _source_attempt_limit(
        self,
        accepted: Iterable[PlanningCheckpoint],
    ) -> int:
        closed = tuple(accepted)
        if any(self._terminal_recovery_reachable(edge) for edge in closed):
            return self.MAX_TERMINAL_SOURCE_ATTEMPTS
        if closed:
            return self.FAST_SOURCE_ATTEMPTS
        return self.MAX_SOURCE_ATTEMPTS

    def _terminal_recovery_reachable(
        self,
        checkpoint: PlanningCheckpoint,
    ) -> bool:
        pending = self.problem.unsatisfied_active(checkpoint.node.state)
        staged = {
            no
            for _line, stack in checkpoint.stacks
            for no in stack.nos
        }
        return pending <= staged

    def _run_source_edge(
        self,
        checkpoint: PlanningCheckpoint,
        choices: tuple[int, ...],
    ) -> SourceWindowResult:
        generator = SourceWindowGenerator(
            self.problem,
            self.transitions,
            choices,
        )
        generator.restore(
            checkpoint.node,
            checkpoint.stacks,
            checkpoint.leases,
        )
        return generator.advance()

    @staticmethod
    def _source_choice_vectors(
        result: SourceWindowResult,
    ) -> list[tuple[int, ...]]:
        selected = tuple(decision.selected for decision in result.decisions)
        vectors: set[tuple[int, ...]] = set()
        for index, decision in enumerate(result.decisions):
            for alternative in range(decision.selected + 1, decision.alternatives):
                vectors.add((*selected[:index], alternative))
        return sorted(vectors, key=BlockFlowOptimizer._choice_priority)

    @staticmethod
    def _choice_priority(value: tuple[int, ...]) -> tuple:
        return (
            sum(choice != 0 for choice in value),
            0 if len(value) == 1 else 1,
            sum(value),
            len(value),
            value,
        )

    def _source_checkpoint(
        self,
        parent: PlanningCheckpoint,
        result: SourceWindowResult,
    ) -> PlanningCheckpoint:
        return PlanningCheckpoint(
            node=result.node,
            contracts=parent.contracts,
            stacks=result.stacks,
            leases=result.leases,
            trace=(*parent.trace, *result.trace),
            expansions=(
                parent.expansions
                + len(result.node.steps)
                - len(parent.node.steps)
            ),
            goal_owners=parent.goal_owners,
        )

    def _close_satisfied_contracts(
        self,
        checkpoint: PlanningCheckpoint,
    ) -> PlanningCheckpoint:
        pending = self.problem.unsatisfied_active(checkpoint.node.state)
        contracts = checkpoint.contracts
        for contract in contracts.contracts:
            if contract.status == ContractStatus.CLOSED:
                continue
            if not any(
                no in pending
                and self.problem.target_by_no.get(no) == contract.target
                for no in contract.subjects
            ):
                contracts = contracts.close(contract.contract_id)
        if contracts == checkpoint.contracts:
            return checkpoint
        return PlanningCheckpoint(
            node=checkpoint.node,
            contracts=contracts,
            stacks=checkpoint.stacks,
            leases=checkpoint.leases,
            trace=checkpoint.trace,
            expansions=checkpoint.expansions,
            goal_owners=checkpoint.goal_owners,
        )

    def _planning_config(self, deadline: float) -> PlanningConfig:
        return PlanningConfig(
            time_budget_seconds=max(0.001, deadline - time.monotonic()),
            max_expansions=self.config.max_expansions,
        )

    def _priority(self, checkpoint: PlanningCheckpoint) -> tuple:
        state = checkpoint.node.state
        return (
            len(self.problem.unsatisfied_active(state)),
            checkpoint.node.cost.hooks + self.problem.hook_lower_bound(state),
            len(checkpoint.leases),
            checkpoint.node.cost,
        )

    def _partial_rank(self, checkpoint: PlanningCheckpoint) -> tuple:
        return (
            len(self.problem.unsatisfied_active(checkpoint.node.state)),
            len(self.problem.protected_damage(checkpoint.node.state)),
            len(checkpoint.leases),
            checkpoint.node.cost,
        )

    def _signature(self, checkpoint: PlanningCheckpoint) -> tuple:
        return (
            self.problem.physical_signature(checkpoint.node.state),
            tuple(
                (line, stack.owner, stack.nos)
                for line, stack in checkpoint.stacks
            ),
            tuple(
                (lease.line, lease.owner, lease.nos)
                for lease in checkpoint.leases
            ),
            tuple(
                (contract.contract_id, contract.status.value)
                for contract in checkpoint.contracts.contracts
            ),
            checkpoint.goal_owners,
        )

    def _complete(self, checkpoint: PlanningCheckpoint) -> bool:
        return bool(
            self.problem.complete(checkpoint.node)
            and not checkpoint.stacks
            and not checkpoint.leases
        )

    def _uncovered_protected_damage(
        self,
        checkpoint: PlanningCheckpoint,
    ) -> frozenset[str]:
        covered = {
            no
            for _line, stack in checkpoint.stacks
            for no in stack.nos
        } | {
            no
            for lease in checkpoint.leases
            for no in lease.nos
        }
        return self.problem.protected_damage(checkpoint.node.state) - covered

    def _planning_result(
        self,
        checkpoint: PlanningCheckpoint,
        reason: str,
        started: float,
    ) -> PlanningResult:
        return PlanningResult(
            node=checkpoint.node,
            complete=self._complete(checkpoint),
            reason=reason,
            trace=checkpoint.trace,
            contracts=checkpoint.contracts,
            leases=checkpoint.leases,
            expansions=checkpoint.expansions,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    @staticmethod
    def _failure_reason(rejected: Counter[str]) -> str:
        if not rejected:
            return "search_incomplete:frontier_exhausted"
        reason, count_value = rejected.most_common(1)[0]
        return f"search_incomplete:{reason}:rejected={count_value}"
