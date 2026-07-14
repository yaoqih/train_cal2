from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stage3_simple.transactions import (  # noqa: E402
    compute_stable_closure,
    strict_progress_potential,
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


def test_strict_progress_potential_counts_only_uncommitted_items() -> None:
    assert strict_progress_potential({"A", "B", "C"}, {"A"}) == 2
