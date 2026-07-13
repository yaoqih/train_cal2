from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stage3_simple.transactions import (  # noqa: E402
    LegalTransition,
    SearchBudget,
    TransactionTermination,
    compute_stable_closure,
    enumerate_minimal_transactions,
)


def test_stable_closure_is_the_least_monotone_fixed_point() -> None:
    implications = {"A": "B", "B": "C"}

    closure = compute_stable_closure(
        "state",
        {"A"},
        lambda _state, committed: (
            implications[item]
            for item in committed
            if item in implications
        ),
    )

    assert closure == frozenset({"A", "B", "C"})


def test_transaction_search_protects_committed_projection_at_every_step() -> None:
    graph = {
        "start": (("move_committed", "bad"), ("prepare", "middle")),
        "middle": (("commit_b", "goal"),),
        "bad": (("commit_b", "bad_goal"),),
    }

    result = enumerate_minimal_transactions(
        start_state="start",
        committed={"A"},
        legal_neighbors=lambda state: (
            LegalTransition(action, next_state)
            for action, next_state in graph.get(state, ())
        ),
        stable_closure=lambda state, committed: (
            committed | {"B"}
            if state in {"goal", "bad_goal"}
            else committed
        ),
        committed_projection=lambda state, _committed: (
            "moved" if state in {"bad", "bad_goal"} else "fixed"
        ),
        action_cost=lambda _action: (1,),
        is_closed=lambda _state: True,
        state_key=lambda state: state,
        zero_cost=(0,),
        budget=SearchBudget(max_expansions=10),
    )

    assert result.termination is TransactionTermination.FOUND
    assert result.search_spec_evaluated
    assert result.committed_projection_pruned == 1
    assert len(result.transactions) == 1
    assert result.transactions[0].actions == ("prepare", "commit_b")
    assert result.transactions[0].newly_committed == frozenset({"B"})


def test_expansion_exhaustion_is_not_reported_as_structural_infeasibility() -> None:
    result = enumerate_minimal_transactions(
        start_state=0,
        committed=set(),
        legal_neighbors=lambda _state: (LegalTransition("step", 1),),
        stable_closure=lambda state, committed: committed | ({"A"} if state else set()),
        committed_projection=lambda _state, _committed: (),
        action_cost=lambda _action: (1,),
        is_closed=lambda _state: True,
        state_key=lambda state: state,
        zero_cost=(0,),
        budget=SearchBudget(max_expansions=0),
    )

    assert result.termination is TransactionTermination.EXPANSION_EXHAUSTED
    assert result.budget_exhausted
    assert not result.search_spec_evaluated
    assert result.transactions == ()


def test_transaction_branch_limit_returns_a_bounded_minimum_cost_prefix() -> None:
    result = enumerate_minimal_transactions(
        start_state="start",
        committed=set(),
        legal_neighbors=lambda state: (
            (
                LegalTransition(f"commit_{index}", f"goal_{index}")
                for index in range(10)
            )
            if state == "start"
            else ()
        ),
        stable_closure=lambda state, committed: (
            committed | {state}
            if state.startswith("goal_")
            else committed
        ),
        committed_projection=lambda _state, _committed: (),
        action_cost=lambda _action: (1,),
        is_closed=lambda _state: True,
        state_key=lambda state: state,
        zero_cost=(0,),
        budget=SearchBudget(max_expansions=100),
        max_transactions=3,
    )

    assert result.termination is TransactionTermination.FOUND
    assert len(result.transactions) == 3
    assert {item.cost for item in result.transactions} == {(1,)}
    assert not result.enumeration_complete
    assert not result.search_spec_evaluated
