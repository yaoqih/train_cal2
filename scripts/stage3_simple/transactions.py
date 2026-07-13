from __future__ import annotations

import heapq
import time
from collections.abc import Callable, Collection, Hashable, Iterable
from dataclasses import dataclass
from enum import Enum
from itertools import count
from typing import Generic, TypeVar


StateT = TypeVar("StateT")
ActionT = TypeVar("ActionT")
CommitT = TypeVar("CommitT", bound=Hashable)

Cost = tuple[int, ...]


class TransactionTermination(str, Enum):
    FOUND = "minimal_transactions_found"
    NO_TRANSACTION = "no_strict_progress_transaction"
    DEADLINE_EXHAUSTED = "transaction_deadline_exhausted"
    EXPANSION_EXHAUSTED = "transaction_expansion_budget_exhausted"


@dataclass(frozen=True)
class SearchBudget:
    """Absolute deadline and expansion limit for one transaction search."""

    deadline: float | None = None
    max_expansions: int | None = None

    def validate(self) -> None:
        if self.max_expansions is not None and self.max_expansions < 0:
            raise ValueError("transaction_expansion_budget_must_be_nonnegative")


@dataclass(frozen=True)
class LegalTransition(Generic[StateT, ActionT]):
    action: ActionT
    state: StateT


@dataclass(frozen=True)
class Transaction(Generic[StateT, ActionT, CommitT]):
    start_state: StateT
    end_state: StateT
    actions: tuple[ActionT, ...]
    committed_before: frozenset[CommitT]
    committed_after: frozenset[CommitT]
    cost: Cost

    @property
    def newly_committed(self) -> frozenset[CommitT]:
        return self.committed_after - self.committed_before


@dataclass(frozen=True)
class TransactionSearchResult(Generic[StateT, ActionT, CommitT]):
    transactions: tuple[Transaction[StateT, ActionT, CommitT], ...]
    termination: TransactionTermination
    minimal_cost: Cost | None
    minimal_cost_proven: bool
    enumeration_complete: bool
    search_spec_evaluated: bool
    expansions: int
    generated: int
    dominated_pruned: int
    committed_projection_pruned: int
    frontier_size: int
    elapsed_seconds: float

    @property
    def found(self) -> bool:
        return bool(self.transactions)

    @property
    def budget_exhausted(self) -> bool:
        return self.termination in {
            TransactionTermination.DEADLINE_EXHAUSTED,
            TransactionTermination.EXPANSION_EXHAUSTED,
        }


def compute_stable_closure(
    state: StateT,
    committed: Collection[CommitT],
    discover: Callable[[StateT, frozenset[CommitT]], Iterable[CommitT]],
    *,
    max_rounds: int | None = None,
) -> frozenset[CommitT]:
    """Compute the least monotone fixed point above ``committed``."""

    if max_rounds is not None and max_rounds < 0:
        raise ValueError("stable_closure_max_rounds_must_be_nonnegative")
    closure = frozenset(committed)
    rounds = 0
    while True:
        discovered = frozenset(discover(state, closure))
        next_closure = closure | discovered
        if next_closure == closure:
            return closure
        rounds += 1
        if max_rounds is not None and rounds > max_rounds:
            raise RuntimeError("stable_closure_round_budget_exhausted")
        closure = next_closure


def strict_progress_potential(
    active_items: Collection[CommitT],
    committed: Collection[CommitT],
) -> int:
    """Return the number of active items not yet stably committed."""

    active = frozenset(active_items)
    stable = frozenset(committed)
    unknown = stable - active
    if unknown:
        raise ValueError(
            "stable_closure_contains_non_active_items:"
            + ",".join(sorted(map(str, unknown)))
        )
    return len(active - stable)


def enumerate_minimal_transactions(
    *,
    start_state: StateT,
    committed: Collection[CommitT],
    legal_neighbors: Callable[[StateT], Iterable[LegalTransition[StateT, ActionT]]],
    stable_closure: Callable[
        [StateT, frozenset[CommitT]], Collection[CommitT]
    ],
    committed_projection: Callable[[StateT, frozenset[CommitT]], object],
    action_cost: Callable[[ActionT], Cost],
    is_closed: Callable[[StateT], bool],
    state_key: Callable[[StateT], Hashable],
    zero_cost: Cost,
    budget: SearchBudget = SearchBudget(),
    max_transactions: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TransactionSearchResult[StateT, ActionT, CommitT]:
    """Enumerate all minimum-cost closed transactions with strict progress.

    A legal transaction must preserve the projection of every item committed at
    the start at every intermediate state. Its closed end state must strictly
    enlarge the stable closure. Equal or more expensive paths to the same state
    key are dominated and never expanded.
    """

    _validate_zero_cost(zero_cost)
    budget.validate()
    if max_transactions is not None and max_transactions <= 0:
        raise ValueError("max_transactions_must_be_positive")
    started = clock()
    committed_before = frozenset(stable_closure(start_state, frozenset(committed)))
    if not frozenset(committed).issubset(committed_before):
        raise ValueError("initial_stable_closure_dropped_committed_items")
    protected_projection = committed_projection(start_state, committed_before)
    start_key = state_key(start_state)
    _require_hashable(start_key)

    serial = count()
    records: list[tuple[StateT, int | None, ActionT | None, Cost]] = [
        (start_state, None, None, zero_cost)
    ]
    frontier: list[tuple[Cost, int, int]] = [(zero_cost, next(serial), 0)]
    best: dict[Hashable, Cost] = {start_key: zero_cost}
    goals: list[Transaction[StateT, ActionT, CommitT]] = []
    minimal_cost: Cost | None = None
    expansions = 0
    generated = 0
    dominated_pruned = 0
    projection_pruned = 0
    goal_keys: set[tuple[frozenset[CommitT], Hashable]] = set()

    def finish(
        termination: TransactionTermination,
        *,
        enumeration_complete: bool,
        evaluated: bool,
    ) -> TransactionSearchResult[StateT, ActionT, CommitT]:
        return TransactionSearchResult(
            transactions=tuple(goals),
            termination=termination,
            minimal_cost=minimal_cost,
            minimal_cost_proven=minimal_cost is not None,
            enumeration_complete=enumeration_complete,
            search_spec_evaluated=evaluated,
            expansions=expansions,
            generated=generated,
            dominated_pruned=dominated_pruned,
            committed_projection_pruned=projection_pruned,
            frontier_size=len(frontier),
            elapsed_seconds=round(clock() - started, 6),
        )

    while frontier:
        if budget.deadline is not None and clock() >= budget.deadline:
            return finish(
                TransactionTermination.DEADLINE_EXHAUSTED,
                enumeration_complete=False,
                evaluated=False,
            )

        cost, _serial, record_index = heapq.heappop(frontier)
        state, _parent, _action, record_cost = records[record_index]
        if cost != record_cost or best.get(state_key(state)) != cost:
            continue
        if max_transactions is None and minimal_cost is not None and cost > minimal_cost:
            return finish(
                TransactionTermination.FOUND,
                enumeration_complete=True,
                evaluated=True,
            )

        closure = frozenset(stable_closure(state, committed_before))
        if not committed_before.issubset(closure):
            raise ValueError("stable_closure_dropped_committed_items")
        if record_index != 0 and is_closed(state) and committed_before < closure:
            if minimal_cost is None:
                minimal_cost = cost
            goal_key = (closure, state_key(state))
            if goal_key not in goal_keys:
                goal_keys.add(goal_key)
                goals.append(
                    Transaction(
                        start_state=start_state,
                        end_state=state,
                        actions=_reconstruct_actions(records, record_index),
                        committed_before=committed_before,
                        committed_after=closure,
                        cost=cost,
                    )
                )
            if max_transactions is not None and len(goals) >= max_transactions:
                return finish(
                    TransactionTermination.FOUND,
                    enumeration_complete=False,
                    evaluated=False,
                )
            continue

        if budget.max_expansions is not None and expansions >= budget.max_expansions:
            return finish(
                TransactionTermination.EXPANSION_EXHAUSTED,
                enumeration_complete=False,
                evaluated=False,
            )
        expansions += 1

        for transition in legal_neighbors(state):
            generated += 1
            next_state = transition.state
            if committed_projection(next_state, committed_before) != protected_projection:
                projection_pruned += 1
                continue
            delta = action_cost(transition.action)
            _validate_delta(delta, zero_cost)
            next_cost = tuple(cost[index] + delta[index] for index in range(len(cost)))
            if max_transactions is None and minimal_cost is not None and next_cost > minimal_cost:
                dominated_pruned += 1
                continue
            next_key = state_key(next_state)
            _require_hashable(next_key)
            incumbent = best.get(next_key)
            if incumbent is not None and incumbent <= next_cost:
                dominated_pruned += 1
                continue
            best[next_key] = next_cost
            next_record = len(records)
            records.append((next_state, record_index, transition.action, next_cost))
            heapq.heappush(frontier, (next_cost, next(serial), next_record))

    if goals:
        return finish(
            TransactionTermination.FOUND,
            enumeration_complete=True,
            evaluated=True,
        )
    return finish(
        TransactionTermination.NO_TRANSACTION,
        enumeration_complete=True,
        evaluated=True,
    )


def _reconstruct_actions(
    records: list[tuple[StateT, int | None, ActionT | None, Cost]],
    record_index: int,
) -> tuple[ActionT, ...]:
    actions: list[ActionT] = []
    while True:
        _state, parent, action, _cost = records[record_index]
        if parent is None:
            break
        if action is None:
            raise RuntimeError("transaction_parent_action_missing")
        actions.append(action)
        record_index = parent
    actions.reverse()
    return tuple(actions)


def _validate_zero_cost(cost: Cost) -> None:
    if not cost:
        raise ValueError("transaction_zero_cost_must_not_be_empty")
    if any(value != 0 for value in cost):
        raise ValueError("transaction_zero_cost_must_contain_only_zeroes")


def _validate_delta(delta: Cost, zero_cost: Cost) -> None:
    if len(delta) != len(zero_cost):
        raise ValueError("transaction_action_cost_dimension_mismatch")
    if any(value < 0 for value in delta):
        raise ValueError("transaction_action_cost_must_be_nonnegative")


def _require_hashable(value: Hashable) -> None:
    try:
        hash(value)
    except TypeError as exc:
        raise ValueError("transaction_state_key_must_be_hashable") from exc
