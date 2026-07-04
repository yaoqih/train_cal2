from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from analyze_depot_inbound_stage import _analyze_case


def test_stage_completion_freezes_at_first_clean_acceptance_moment() -> None:
    result = _analyze_case(
        case_id="T",
        case={
            "debt": {"A": {"source_line": "存1线", "target_line": "卸轮线", "initial_grouped": False}},
            "line_by_no": {"A": "存1线", "X": "存2线"},
            "target_by_no": {"A": "卸轮线", "X": "存3线"},
        },
        operations={
            1: [_op("Put", "存4线", "A")],
            2: [_op("Put", "机南", "X")],
            3: [_op("Get", "存4线", "A"), _op("Put", "卸轮线", "A")],
        },
    )

    assert result["grouped_at_checkpoint_count"] == 1
    assert result["first_completion_hook"] == 1
    assert result["stage_complete"] == 1
    assert result["complete_then_contaminated"] == 0
    assert result["release_before_complete"] == 0
    assert result["contamination_nos"] == ()


def test_first_stage_stops_on_any_assembly_line_depot_release() -> None:
    result = _analyze_case(
        case_id="T",
        case={
            "debt": {"A": {"source_line": "存1线", "target_line": "修1库内", "initial_grouped": False}},
            "line_by_no": {"A": "存1线"},
            "target_by_no": {"A": "修1库内"},
        },
        operations={
            1: [_op("Put", "机南", "A")],
            2: [_op("Get", "机南", "A"), _op("Put", "修1库内", "A")],
            3: [_op("Put", "存4线", "A")],
        },
    )

    assert result["stage_hook_count"] == 1
    assert result["release_hook"] == 2
    assert result["cun4_release_hook"] == 0
    assert result["stage_complete"] == 1
    assert result["grouped_by_line_counts"]["机南"] == 1


def _op(action: str, line: str, *nos: str) -> dict[str, object]:
    return {
        "action": action,
        "line": line,
        "move_nos": tuple(nos),
    }
