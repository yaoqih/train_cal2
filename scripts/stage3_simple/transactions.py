from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar


StateT = TypeVar("StateT")
ActionT = TypeVar("ActionT")
CommitT = TypeVar("CommitT", bound=Hashable)

Cost = tuple[int, ...]


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
