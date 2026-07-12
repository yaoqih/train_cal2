from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.stage2_simple.solve import Stage2Solver, case_id_from_path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "artifacts" / "fullflow_current"


def _case(truth: str, case_id: str) -> tuple[dict, dict]:
    truth_dir = ROOT / "data" / truth
    request_path = next(
        path for path in truth_dir.glob("*.json") if case_id_from_path(path) == case_id
    )
    stage1_path = BASELINE / truth / "stage1" / f"{case_id}_response.json"
    if not stage1_path.exists():
        pytest.skip(f"missing integration artifact: {stage1_path}")
    return (
        json.loads(request_path.read_text(encoding="utf-8-sig")),
        json.loads(stage1_path.read_text(encoding="utf-8-sig")),
    )


def _solve(truth: str, case_id: str) -> dict:
    request, stage1 = _case(truth, case_id)
    return Stage2Solver(
        case_id,
        request,
        stage1,
        time_budget_seconds=30.0,
    ).solve()


def _assert_verified(result: dict, max_operations: int) -> None:
    summary = result["summary"]
    assert summary["status"] == "complete"
    assert summary["operations"] <= max_operations
    assert summary["replay_physical_ok"]
    assert summary["combined_replay_physical_ok"]
    assert summary["replay_state_consistent"]
    assert summary["model_state_consistent"]
    assert summary["store4_put_rule_ok"]


def test_stayer_lease_is_part_of_the_episode_model() -> None:
    result = _solve("truth2", "0327Z")
    _assert_verified(result, 9)
    purposes = {row["purpose"] for row in result["trace"]["operations"]}
    assert "lease_stack_block" in purposes
    assert "restore_source_order" in purposes


def test_unwheel_tail_coalesces_different_business_tags() -> None:
    result = _solve("truth3", "0416Z")
    _assert_verified(result, 7)
    optional = set(result["summary"]["paint_to_unwheel_nos"])
    required = set(result["summary"]["required_unwheel_nos"])
    unwheel_puts = [
        set(row["move"])
        for row in result["trace"]["operations"]
        if row["action"] == "Put" and row["line"] == "卸轮线"
    ]
    assert any(required <= move and move & optional for move in unwheel_puts)


def test_scratch_recovery_absorbs_an_unprocessed_source() -> None:
    result = _solve("truth3", "0409Z")
    _assert_verified(result, 9)
    recovered = [
        row
        for row in result["trace"]["operations"]
        if row["purpose"] == "recover_lease"
    ]
    assert any(len(row["move"]) == 3 for row in recovered)
