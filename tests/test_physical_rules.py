from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical  # noqa: E402


def car(
    no: str,
    *,
    line: str = "存1线",
    position: int = 1,
    length: float = 14.3,
    target_lines: list[str] | None = None,
    closed: bool = False,
    heavy: bool = False,
    weigh: bool = False,
) -> dict:
    return physical.normalized_car(
        {
            "No": no,
            "Line": line,
            "Position": position,
            "RepairProcess": "段修",
            "Type": "棚车",
            "Length": length,
            "TargetLines": target_lines or ["存2线"],
            "IsClosedDoor": closed,
            "IsHeavy": heavy,
            "IsWeigh": weigh,
        }
    )


def test_action_family_is_owned_by_physical_model() -> None:
    assert physical.action_family("存1线", "机库线", False) == "LOCO_AREA_STAGING"
    assert physical.action_family("存1线", "洗罐站", False) == "FUNCTION_LINE_SERVICE"
    assert physical.action_family("修1库内", "存4线", False) == "DEPOT_OUTBOUND"


def test_pull_equivalent_counts_heavy_as_four() -> None:
    assert physical.pull_equivalent([car("H", heavy=True), car("N")]) == 5


def test_closed_door_first_car_is_rejected_for_heavy_consist() -> None:
    consist = [car("C", closed=True), car("H", heavy=True)]
    reasons = physical.closed_door_put_reasons(
        target_line="存2线",
        projected_cars=consist,
        moved_nos={physical.car_no(item) for item in consist},
        train_consist=consist,
    )
    assert any(reason.startswith("closed_door_full_consist_first_car_violation") for reason in reasons)


def test_store4_closed_door_front_position_is_rejected() -> None:
    projected = [
        car("C", line="存4线", position=1, target_lines=["存4线"], closed=True),
        car("N", line="存4线", position=4, target_lines=["存4线"]),
    ]
    reasons = physical.closed_door_put_reasons(
        target_line="存4线",
        projected_cars=projected,
        moved_nos={"C"},
        train_consist=projected,
    )
    assert "closed_door_cun4_put_position_violation:C:1" in reasons


def test_weigh_requires_pending_tail_car() -> None:
    batch = [car("W", weigh=True), car("N")]
    candidate = physical.hook_candidate(
        case_id="TEST",
        hook_index=1,
        source_line="存1线",
        target_line="存2线",
        batch=batch,
        planned_positions={"W": 1, "N": 2},
        generation_reason="test",
        candidate_kind="target_move",
        has_weigh_override=True,
    )
    reasons = physical.single_hook_weigh_reasons(candidate, batch)
    assert reasons[0].startswith("weigh_requires_pending_tail_car")


def test_weigh_line_must_be_empty() -> None:
    cars = [
        car("W", target_lines=["存2线"], weigh=True),
        car("B", line=physical.WEIGH_LINE, target_lines=[physical.WEIGH_LINE]),
    ]
    candidate = physical.hook_candidate(
        case_id="TEST",
        hook_index=2,
        source_line="存1线",
        target_line="存2线",
        batch=[cars[0]],
        planned_positions={"W": 1},
        generation_reason="test",
        candidate_kind="target_move",
    )
    result = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not result.accepted
    assert f"weigh_line_not_empty:{physical.WEIGH_LINE}:B" in result.reasons


def test_target_line_length_is_a_hard_limit() -> None:
    cars = [
        car(f"E{index}", line="预修线", position=index, target_lines=["预修线"])
        for index in range(1, 15)
    ]
    cars.append(car("M", line="存5线北", target_lines=["预修线"]))
    candidate = physical.hook_candidate(
        case_id="TEST",
        hook_index=3,
        source_line="存5线北",
        target_line="预修线",
        batch=[cars[-1]],
        planned_positions={"M": 15},
        generation_reason="test",
        candidate_kind="target_move",
    )
    result = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存5线北"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not result.accepted
    assert any(reason.startswith("target_line_length_violation:预修线:") for reason in result.reasons)


def test_get_and_put_follow_stack_ends() -> None:
    cars = [
        car("A", line="存2线", position=1),
        car("B", line="存2线", position=2),
        car("C", line="存2线", position=3),
    ]
    assert physical.line_access_order(cars, "存2线") == ["A", "B", "C"]
    assert not physical.inaccessible_get_reason(
        cars=cars,
        line="存2线",
        move_nos=("A", "B"),
        carried_nos=set(),
        step_index=1,
    )
    assert physical.inaccessible_get_reason(
        cars=cars,
        line="存2线",
        move_nos=("B", "C"),
        carried_nos=set(),
        step_index=1,
    ).startswith("line_end_get_order_violation:")


def test_track_graph_covers_all_operational_lines() -> None:
    graph = physical.TrackGraph()
    for line in physical.TRACK_SPECS:
        assert graph.route(line, line) == [line]
