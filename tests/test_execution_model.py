from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stage1_simple.solve import case_files as stage1_case_files  # noqa: E402
from stage2_simple.solve import aggregate as stage2_aggregate  # noqa: E402
from stage2_simple.solve import case_files as stage2_case_files  # noqa: E402
from stage3_simple.solve import aggregate as stage3_aggregate  # noqa: E402
from stage3_simple.solve import case_files as stage3_case_files  # noqa: E402
from stage4_simple.solve import case_files as stage4_case_files  # noqa: E402


CASE_DISCOVERERS = (
    stage1_case_files,
    stage2_case_files,
    stage3_case_files,
    stage4_case_files,
)


def test_all_stage_clis_share_file_or_directory_input_semantics(tmp_path: Path) -> None:
    first = tmp_path / "validation_case_0101Z.json"
    second = tmp_path / "validation_case_0102W.json"
    unrelated = tmp_path / "aggregate_summary.json"
    for path in (first, second, unrelated):
        path.write_text("{}", encoding="utf-8")

    for discover in CASE_DISCOVERERS:
        assert discover(first) == [first]
        assert discover(tmp_path) == [first, second]


def test_all_stage_clis_reject_missing_or_empty_input(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.mkdir()

    for discover in CASE_DISCOVERERS:
        with pytest.raises(FileNotFoundError):
            discover(missing)
        with pytest.raises(ValueError, match=r"validation_\*\.json"):
            discover(empty)


@pytest.mark.parametrize("aggregate", [stage2_aggregate, stage3_aggregate])
def test_downstream_aggregates_keep_noncomplete_statuses_distinct(aggregate) -> None:
    result = aggregate(
        [
            {"case_id": "0101Z", "status": "complete", "operations": 2},
            {"case_id": "0102Z", "status": "partial", "operations": 1},
            {"case_id": "0103Z", "status": "unavailable", "operations": 0},
            {"case_id": "0104Z", "status": "error", "operations": 0},
        ]
    )

    assert result["complete"] == 1
    assert result["partial"] == 1
    assert result["unavailable"] == 1
    assert result["error"] == 1
