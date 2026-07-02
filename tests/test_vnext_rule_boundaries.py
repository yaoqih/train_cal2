from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical
from solver_vnext.placement import planned_positions_for_batch
from solver_vnext.contracts import classify_family
from solver_vnext.domain import ContractFamily


def car(
    no: str,
    *,
    line: str = "存1线",
    position: int = 1,
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
            "Length": 14.3,
            "TargetLines": target_lines or ["存2线"],
            "IsClosedDoor": closed,
            "IsHeavy": heavy,
            "IsWeigh": weigh,
        }
    )


def test_family_classification_is_single_source() -> None:
    assert classify_family("存1线", "机库线", False) == ContractFamily.LOCO_AREA_STAGING
    assert physical.action_family("存1线", "机库线", False) == ContractFamily.LOCO_AREA_STAGING.value
    assert physical.action_family("存1线", "洗罐站", False) == ContractFamily.FUNCTION_LINE_SERVICE.value
    assert physical.action_family("修1库内", "存4线", False) == ContractFamily.DEPOT_OUTBOUND.value


def test_closed_door_non_cun4_heavy_first_rejected() -> None:
    consist = [
        car("C1", closed=True, heavy=False),
        car("C2", heavy=True),
    ]
    reasons = physical.closed_door_put_reasons(
        target_line="存2线",
        projected_cars=consist,
        moved_nos={physical.car_no(item) for item in consist},
        train_consist=consist,
    )
    assert any(reason.startswith("closed_door_full_consist_first_car_violation") for reason in reasons)


def test_closed_door_non_cun4_over_ten_first_rejected() -> None:
    consist = [car("C01", closed=True)] + [car(f"C{index:02d}") for index in range(2, 12)]
    reasons = physical.closed_door_put_reasons(
        target_line="存2线",
        projected_cars=consist,
        moved_nos={physical.car_no(item) for item in consist},
        train_consist=consist,
    )
    assert any(reason.startswith("closed_door_full_consist_first_car_violation") for reason in reasons)


def test_closed_door_cun4_put_position_rejected_for_moved_car() -> None:
    projected = [
        car("C1", line="存4线", position=1, target_lines=["存4线"], closed=True),
        car("C2", line="存4线", position=4, target_lines=["存4线"]),
    ]
    reasons = physical.closed_door_put_reasons(
        target_line="存4线",
        projected_cars=projected,
        moved_nos={"C1"},
        train_consist=projected,
    )
    assert "closed_door_cun4_put_position_violation:C1:1" in reasons


def test_closed_door_cun4_put_position_ignores_unmoved_car_for_step_check() -> None:
    projected = [
        car("C1", line="存4线", position=1, target_lines=["存4线"], closed=True),
        car("C2", line="存4线", position=4, target_lines=["存4线"]),
    ]
    assert physical.closed_door_put_reasons(
        target_line="存4线",
        projected_cars=projected,
        moved_nos={"C2"},
        train_consist=projected,
    ) == []


def test_pull_equivalent_counts_heavy_as_four() -> None:
    assert physical.pull_equivalent([car("C1", heavy=True), car("C2")]) == 5


def test_weigh_requires_pending_tail_car() -> None:
    batch = [car("C1", weigh=True), car("C2")]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="存2线",
        batch=batch,
        planned_positions={"C1": 1, "C2": 2},
        generation_reason="test",
        candidate_kind="target_move",
        has_weigh_override=True,
    )
    reasons = physical.single_hook_weigh_reasons(candidate, batch)
    assert reasons and reasons[0].startswith("weigh_requires_pending_tail_car")


def test_reversal_triplet_rule_uses_loco_plus_train_length() -> None:
    static_cars = [car("B1", line="机北2", position=1)]
    reasons = physical.pre_repair_reversal_reasons(
        ["调梁线北", "渡4", "机库线"],
        static_cars,
        moving_nos=set(),
        train_length_m=30.0,
    )
    assert any(reason.startswith("route_reversal_length_violation") for reason in reasons)


def test_reversal_triplet_rule_does_not_fire_without_blocker() -> None:
    reasons = physical.pre_repair_reversal_reasons(
        ["调梁线北", "渡4", "机库线"],
        [],
        moving_nos=set(),
        train_length_m=300.0,
    )
    assert reasons == []


def test_reversal_triplet_rule_does_not_fire_on_unlisted_path() -> None:
    static_cars = [car("B1", line="机北2", position=1)]
    reasons = physical.pre_repair_reversal_reasons(
        ["存1线", "存2线", "存3线"],
        static_cars,
        moving_nos=set(),
        train_length_m=300.0,
    )
    assert reasons == []


def test_line_graph_covers_known_lines_and_is_connected() -> None:
    graph = physical.TrackGraph()
    known_lines = set(physical.TRACK_SPECS) | physical.RUNNING_LINES
    missing = [line for line in known_lines if line not in graph._adjacency]
    assert missing == []

    start = "机库线"
    unreachable = [
        line
        for line in sorted(known_lines)
        if not graph.route(start, line)
    ]
    assert unreachable == []


def test_occupied_pre_repair_put_uses_allowed_line_approach() -> None:
    graph = physical.TrackGraph()
    cars = [
        car("P1", line="预修线", position=1, target_lines=["预修线"]),
        car("M1", line="存5线北", position=1, target_lines=["预修线"]),
    ]
    moving_nos = {"M1"}
    path = graph.route_avoiding_occupied(
        "存5线北",
        "预修线",
        physical.occupied_lines_for_route(cars, moving_nos),
        target_approach_lines=physical.route_approach_lines_for_put("预修线", cars, moving_nos),
    )
    assert path
    assert path[-2] in {"渡7", "存2线"}
    assert "预修线" not in path[:-1]


def test_empty_pre_repair_put_does_not_force_approach_line() -> None:
    graph = physical.TrackGraph()
    cars = [car("M1", line="存5线北", position=1, target_lines=["预修线"])]
    moving_nos = {"M1"}
    path = graph.route_avoiding_occupied(
        "存5线北",
        "预修线",
        physical.occupied_lines_for_route(cars, moving_nos),
        target_approach_lines=physical.route_approach_lines_for_put("预修线", cars, moving_nos),
    )
    assert path
    assert path[-2] == "渡9"


def test_spotting_placement_uses_relaxed_business_window_with_fixed_capacity() -> None:
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    batch = [
        car(f"F{index}", line="存5线北", position=index, target_lines=["调梁棚"])
        for index in range(1, 5)
    ]
    for item in batch:
        item["_ForcePositions"] = (6, 7, 8, 9)
        item["ForceTargetPosition"] = [6, 7, 8, 9]
    placed = planned_positions_for_batch(
        batch=batch,
        target_line="调梁棚",
        cars=batch,
        depot_assignment=depot_assignment,
        batch_nos={physical.car_no(item) for item in batch},
    )
    assert set(placed.values()) <= {6, 7, 8, 9, 10, 11}
    assert len(set(placed.values())) == 4

    overflow = [
        car(f"O{index}", line="存5线北", position=index, target_lines=["调梁棚"])
        for index in range(1, 6)
    ]
    for item in overflow:
        item["_ForcePositions"] = (6, 7, 8, 9)
        item["ForceTargetPosition"] = [6, 7, 8, 9]
    assert planned_positions_for_batch(
        batch=overflow,
        target_line="调梁棚",
        cars=overflow,
        depot_assignment=depot_assignment,
        batch_nos={physical.car_no(item) for item in overflow},
    ) == {}


def test_spotting_line_source_positions_are_not_compacted_after_remove() -> None:
    cars = [
        car("N1", line="抛丸线", position=1, target_lines=["存1线"]),
        car("F1", line="抛丸线", position=2, target_lines=["抛丸线"]),
        car("F2", line="抛丸线", position=3, target_lines=["抛丸线"]),
    ]
    for item in cars[1:]:
        item["_ForcePositions"] = (2, 3)
        item["ForceTargetPosition"] = [2, 3]
    physical.compact_source_positions(cars, "抛丸线", {"N1"})
    assert {physical.car_no(item): item["Position"] for item in cars} == {
        "N1": 1,
        "F1": 2,
        "F2": 3,
    }


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
