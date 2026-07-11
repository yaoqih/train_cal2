from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from analyze_multi_get_capability import analyze_group, operation_contexts  # noqa: E402


def op(index: int, action: str, line: str, move: list[str], train: list[str]) -> dict:
    return {
        "Index": index,
        "Action": action,
        "Line": line,
        "MoveCars": move,
        "TrainCars": train,
        "PassbyPath": [line],
    }


def metrics(rows: list[dict], targets: dict[str, set[str]]) -> dict:
    contexts, diagnostics = operation_contexts(rows, fixed_phase="S4")
    assert not diagnostics
    return analyze_group(contexts, targets, {no: 1 for no in targets})


def test_two_independent_sources_are_strategic_multi_source() -> None:
    result = metrics(
        [
            op(1, "Get", "存2线", ["A"], ["A"]),
            op(2, "Get", "存3线", ["B"], ["A", "B"]),
            op(3, "Put", "油漆线", ["A", "B"], []),
        ],
        {"A": {"油漆线"}, "B": {"油漆线"}},
    )

    assert result["multi_source"] == 1
    assert result["strategic_multi_source"] == 1
    assert result["structural_multi_source"] == 0
    assert result["source_convergence"] == 1


def test_target_existing_cars_do_not_turn_rebuild_into_strategic_multi_source() -> None:
    result = metrics(
        [
            op(1, "Get", "油漆线", ["EXISTING"], ["EXISTING"]),
            op(2, "Get", "存3线", ["INBOUND"], ["EXISTING", "INBOUND"]),
            op(3, "Put", "油漆线", ["EXISTING", "INBOUND"], []),
        ],
        {"EXISTING": {"油漆线"}, "INBOUND": {"油漆线"}},
    )

    assert result["multi_source"] == 1
    assert result["strategic_multi_source"] == 0
    assert result["structural_multi_source"] == 1
    assert result["restored_cars"] == 1


def test_partial_put_then_fresh_get_and_anchor_span_are_detected() -> None:
    result = metrics(
        [
            op(1, "Get", "存2线", ["A", "B"], ["A", "B"]),
            op(2, "Put", "油漆线", ["B"], ["A"]),
            op(3, "Get", "存3线", ["C"], ["A", "C"]),
            op(4, "Put", "抛丸线", ["C"], ["A"]),
            op(5, "Put", "机走棚", ["A"], []),
        ],
        {"A": {"机走棚"}, "B": {"油漆线"}, "C": {"抛丸线"}},
    )

    assert result["partial_put_then_get"] == 1
    assert result["partial_put_hooks"] == 2
    assert result["retained_get_hooks"] == 1
    assert result["longest_carried_span"] == 5


def test_fresh_get_after_carry_closure_is_not_partial_put_continuation() -> None:
    result = metrics(
        [
            op(1, "Get", "存2线", ["A", "B"], ["A", "B"]),
            op(2, "Put", "油漆线", ["B"], ["A"]),
            op(3, "Put", "机走棚", ["A"], []),
            op(4, "Get", "存3线", ["C"], ["C"]),
            op(5, "Put", "抛丸线", ["C"], []),
        ],
        {"A": {"机走棚"}, "B": {"油漆线"}, "C": {"抛丸线"}},
    )

    assert result["partial_put_then_get"] == 0
