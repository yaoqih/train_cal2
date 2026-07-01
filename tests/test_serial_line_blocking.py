from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from generate_physical_runtime_trace import (  # noqa: E402
    LOCO_LENGTH_M,
    PhysicalValidator,
    SERIAL_LINE_BLOCKERS,
    TrackGraph,
    build_depot_assignment,
    hook_candidate,
    line_access_order,
    line_entry_blocking_reasons,
    line_end_node,
    physical_positions_after_put,
    pre_repair_reversal_available_length,
    route_blocking_lines,
    LocoLocation,
)


def car(no: str, line: str, position: int, target: str = "存2线") -> dict[str, object]:
    return {
        "No": no,
        "Line": line,
        "Position": position,
        "RepairProcess": "",
        "Type": "",
        "Length": 14.0,
        "TargetLines": [target],
        "_TargetLineSet": {target},
    }


def candidate(source: str, target: str, cars: list[dict[str, object]], kind: str = "audit_direct"):
    batch = [cars[0]]
    return hook_candidate(
        case_id="SERIAL",
        hook_index=1,
        source_line=source,
        target_line=target,
        batch=batch,
        planned_positions={str(batch[0]["No"]): 1},
        generation_reason="serial_line_blocking_test",
        candidate_kind=kind,
    )


def validate(cand, cars: list[dict[str, object]]):
    return PhysicalValidator(TrackGraph()).validate(
        cand,
        cars,
        LocoLocation(line=cand.source_line, node=cand.source_line),
        build_depot_assignment([dict(item) for item in cars], {}),
    )


def test_all_serial_blockers_report_full_name_entry_reasons() -> None:
    cases = [
        ("修1库内", "修1库外", "修1库内", "修1库外"),
        ("修2库内", "修2库外", "修2库内", "修2库外"),
        ("修3库内", "修3库外", "修3库内", "修3库外"),
        ("修4库内", "修4库外", "修4库内", "修4库外"),
        ("机南", "机走棚", "机走线南", "机走棚"),
        ("机走棚", "机走北", "机走棚", "机走北"),
        ("洗油北", "机走棚", "洗罐油漆北", "机走棚"),
        ("洗罐线北", "洗油北", "洗罐线北", "洗罐油漆北"),
        ("油漆线", "洗油北", "油漆线", "洗罐油漆北"),
        ("洗罐站", "洗罐线北", "洗罐站", "洗罐线北"),
        ("调梁棚", "调梁线北", "调梁棚", "调梁线北"),
        ("存4南", "存4线", "存4线南", "存4线"),
        ("存4南", "存3线", "存4线南", "存3线"),
        ("存5线南", "存5线北", "存5线南", "存5线北"),
        ("存1线", "机北1", "存1线", "机走北1线"),
        ("机北2", "机北1", "机走北2线", "机走北1线"),
    ]

    for blocked_line, blocker_line, blocked_full, blocker_full in cases:
        reasons = line_entry_blocking_reasons(
            blocked_line,
            [car("M", blocked_line, 1), car("B", blocker_line, 1)],
            {"M"},
        )
        assert reasons == [f"serial_line_entry_blocked:{blocked_full}:{blocker_full}:B"]


def test_all_serial_blockers_block_physical_routes() -> None:
    graph = TrackGraph()

    for blocked_line, blocker_lines in SERIAL_LINE_BLOCKERS.items():
        for blocker_line in blocker_lines:
            static_path, available_path, route_blockers = route_blocking_lines(
                graph,
                [car("B", blocker_line, 1)],
                "存2线",
                blocked_line,
                set(),
            )
            entry_reasons = line_entry_blocking_reasons(blocked_line, [car("B", blocker_line, 1)], set())
            assert static_path or entry_reasons
            assert not available_path or entry_reasons
            assert blocker_line in route_blockers or entry_reasons


def test_direct_put_to_blocked_lines_is_rejected() -> None:
    cases = [
        ("修1库内", "修1库外", "serial_line_entry_blocked:修1库内:修1库外:B", "audit_direct"),
        ("机南", "机走棚", "机走线南", "blocker_relocation"),
        ("洗油北", "机走棚", "洗罐油漆北", "blocker_relocation"),
        ("洗罐线北", "洗油北", "洗罐线北", "audit_direct"),
        ("油漆线", "洗油北", "油漆线", "audit_direct"),
        ("洗罐站", "洗罐线北", "洗罐站", "audit_direct"),
        ("调梁棚", "调梁线北", "调梁棚", "audit_direct"),
        ("存4南", "存4线", "存4线南", "blocker_relocation"),
        ("存5线南", "存5线北", "存5线南", "audit_direct"),
        ("存1线", "机北1", "存1线", "audit_direct"),
        ("机北2", "机北1", "机走北2线", "blocker_relocation"),
    ]

    for target_line, blocker_line, expected_reason_part, kind in cases:
        cars = [car("M", "存2线", 1, target_line), car("B", blocker_line, 1, blocker_line)]
        result = validate(candidate("存2线", target_line, cars, kind), cars)
        assert not result.accepted
        assert any(expected_reason_part in reason for reason in result.reasons)


def test_route_to_wash_station_is_blocked_by_wash_north() -> None:
    cars = [car("M", "存2线", 1, "洗罐站"), car("B", "洗罐线北", 1, "洗罐线北")]
    result = validate(candidate("存2线", "洗罐站", cars), cars)
    assert not result.accepted
    assert any("洗罐线北:B" in reason or reason == "put_route_blocked_by_occupied_line" for reason in result.reasons)


def test_machine_shed_blocks_machine_south_and_wash_oil_north_routes() -> None:
    for target_line in ("机南", "洗油北", "洗罐站"):
        kind = "blocker_relocation" if target_line in {"机南", "洗油北"} else "audit_direct"
        cars = [car("M", "存2线", 1, target_line), car("B", "机走棚", 1, "机走棚")]
        result = validate(candidate("存2线", target_line, cars, kind), cars)
        assert not result.accepted


def test_serial_blockers_do_not_overblock_unrelated_detour_routes() -> None:
    graph = TrackGraph()
    path = graph.route_avoiding_occupied("存2线", "机库线", {"机北1"})
    assert path == ["存2线", "L4", "Z3", "Z2", "Z1", "L6", "L7", "机库线"]


def test_pre_repair_remaining_length_is_required_for_detour_reversal() -> None:
    moving = car("M", "存2线", 1, "机库线")
    blocker = car("B", "机北1", 1, "机北1")
    result = validate(candidate("存2线", "机库线", [moving, blocker]), [moving, blocker])
    assert result.accepted

    filler_length = pre_repair_reversal_available_length([moving, blocker], {"M"}) - LOCO_LENGTH_M
    filler = car("P", "预修线", 1, "预修线")
    filler["Length"] = filler_length + 0.1
    blocked = validate(candidate("存2线", "机库线", [moving, blocker, filler]), [moving, blocker, filler])
    assert not blocked.accepted
    assert any(reason.startswith("pre_repair_reversal_length_violation:") for reason in blocked.reasons)


def test_get_put_order_is_always_from_north_end() -> None:
    cars = [car("A", "机走棚", 1), car("B", "机走棚", 2)]
    assert line_access_order(cars, "机走棚", "L8") == ["A", "B"]

    projected_positions = physical_positions_after_put(
        cars,
        "机走棚",
        ["C"],
        line_end_node("机走棚", "South"),
    )
    assert projected_positions == {"C": 1, "A": 2, "B": 3}
