from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical
from solver_vnext import serial
from solver_vnext.depot_inbound_plan import build_depot_inbound_assembly_plan
from solver_vnext.depot_outbound_plan import build_depot_outbound_assembly_plan
from solver_vnext.delta import build_contract_delta, simulate_candidate
from solver_vnext.episodes import (
    Cun4OutboundAssemblyReleaseEpisode,
    Cun4UnwheelReleaseEpisode,
    DepotInboundAssemblyReleaseEpisode,
    DepotInboundAssemblyRebalanceEpisode,
    DepotInboundDirtyCleanoutEpisode,
    DepotInboundRouteBlockerDigestEpisode,
    DepotInboundDirtyExchangeSessionEpisode,
    DepotInboundMixedExtractionSessionEpisode,
    DepotInboundPrefixAssemblySessionEpisode,
    DepotInboundAssemblySessionEpisode,
    RemoteDirectCloseoutEpisode,
    SourcePrefixReleaseEpisode,
    SpottingRepackEpisode,
    Stage4LinearSweepEpisode,
    DepotCun4SourceRepackExchangeEpisode,
    DepotCun4InboundOutboundExchangeEpisode,
    DirectMoveEpisode,
    DepotOutboundSessionEpisode,
    EPISODES,
    _depot_inbound_multisource_stepwise_put_plan,
    _depot_inbound_target_clearance_plan,
)
from solver_vnext.frontier import AccessFrontier
from solver_vnext.gate import AcceptRejectGate
from solver_vnext.placement import planned_positions_for_batch
from solver_vnext.strategic_plan import build_strategic_plan
from solver_vnext.contracts import classify_family
from solver_vnext.flow import classify_flow_facts
from solver_vnext.domain import (
    CandidateEnvelope,
    ContractDelta,
    ContractFamily,
    FlowContract,
    IntentKind,
    PhaseKind,
    PhaseState,
    RemoteSessionState,
    ResourceDelta,
    ResourceKind,
    ResourceRequest,
    SerialGateLease,
    SolverState,
)
from solver_vnext.phase import HumanPhaseGate
from solver_vnext.policy import BaselinePolicy, PolicyContext
from solver_vnext.resources import StationResourceGraph


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


def test_single_hook_weigh_requires_empty_weigh_line() -> None:
    cars = [
        car("W1", line="存1线", position=1, target_lines=["存2线"], weigh=True),
        car("B1", line=physical.WEIGH_LINE, position=1, target_lines=[physical.WEIGH_LINE]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=2,
        source_line="存1线",
        target_line="存2线",
        batch=[cars[0]],
        planned_positions={"W1": 1},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert f"weigh_line_not_empty:{physical.WEIGH_LINE}:B1" in validation.reasons


def test_planlet_weigh_requires_empty_weigh_line() -> None:
    cars = [
        car("W1", line="存1线", position=1, target_lines=["存2线"], weigh=True),
        car("B1", line=physical.WEIGH_LINE, position=1, target_lines=[physical.WEIGH_LINE]),
    ]
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=3,
        source_line="存1线",
        target_line="存2线",
        batch=[cars[0]],
        steps=(
            physical.plan_step("Get", "存1线", ("W1",)),
            physical.plan_step("Weigh", physical.WEIGH_LINE, ("W1",)),
            physical.plan_step("Put", "存2线", ("W1",), {"W1": 1}),
        ),
        reason="test",
        candidate_kind="test_planlet",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert f"weigh_line_not_empty:{physical.WEIGH_LINE}:B1" in validation.reasons


def test_final_target_put_still_rejects_line_length_overflow() -> None:
    cars = [
        car(f"E{index}", line="预修线", position=index, target_lines=["预修线"])
        for index in range(1, 15)
    ]
    cars.append(car("M1", line="存5线北", position=1, target_lines=["预修线"]))
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=4,
        source_line="存5线北",
        target_line="预修线",
        batch=[cars[-1]],
        planned_positions={"M1": 15},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存5线北"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert any(
        reason.startswith("target_line_length_violation:预修线:")
        for reason in validation.reasons
    )


def test_depot_line_length_capacity_is_hard_limit() -> None:
    cars = [
        car(f"D{index}", line="修1库内", position=index, target_lines=["修1库内"])
        for index in range(1, 12)
    ]
    mover = car("M1", line="存1线", position=1, target_lines=["修1库内"])
    batch = [mover]
    cars.append(mover)
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=5,
        source_line="存1线",
        target_line="修1库内",
        batch=batch,
        planned_positions={"M1": 12},
        generation_reason="test",
        candidate_kind="target_move",
    )
    projected = physical.projected_after_physical_put(
        cars,
        "修1库内",
        ["M1"],
        {"M1": 12},
    )
    assert not physical.line_has_length_capacity(
        "修1库内",
        cars,
        batch,
        {"M1"},
        grouped=physical.cars_by_line(cars),
    )
    reasons = physical.validate_target_positions(
        candidate,
        projected,
        batch,
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert any(
        reason.startswith("target_line_length_violation:修1库内:")
        for reason in reasons
    )


def test_all_track_specs_enforce_length_capacity() -> None:
    for target_line, spec in physical.TRACK_SPECS.items():
        source_line = "存1线" if target_line != "存1线" else "存2线"
        existing = car(
            "E1",
            line=target_line,
            position=1,
            length=spec.length_m - 0.1,
            target_lines=[target_line],
        )
        mover = car(
            "M1",
            line=source_line,
            position=1,
            length=1.0,
            target_lines=[target_line],
        )
        cars = [existing, mover]
        batch = [mover]
        candidate = physical.hook_candidate(
            case_id="T",
            hook_index=6,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            planned_positions={"M1": 2},
            generation_reason="test",
            candidate_kind="target_move",
        )
        projected = physical.projected_after_physical_put(
            cars,
            target_line,
            ["M1"],
            {"M1": 2},
        )

        assert not physical.line_has_length_capacity(
            target_line,
            cars,
            batch,
            {"M1"},
            grouped=physical.cars_by_line(cars),
        ), target_line
        reasons = physical.validate_target_positions(
            candidate,
            projected,
            batch,
            physical.DepotAssignment(slots={}, failures={}),
        )
        assert any(
            reason.startswith(f"target_line_length_violation:{target_line}:")
            for reason in reasons
        ), (target_line, reasons)


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


def test_route_search_skips_reversal_length_violating_short_path() -> None:
    graph = physical.TrackGraph()
    cars = [car("B1", line="机北1", position=1)]
    path = graph.route_avoiding_occupied(
        "存1线",
        "机走北",
        physical.occupied_lines_for_route(cars, set()),
        cars=cars,
        moving_nos=set(),
        train_length_m=90.0,
    )
    windows = set(zip(path, path[1:], path[2:]))
    assert path
    assert ("渡6", "渡5", "机走北") not in windows
    assert physical.pre_repair_reversal_reasons(path, cars, set(), 90.0) == []


def test_singleton_put_to_machine_south_leaves_loco_at_access_end() -> None:
    assert physical.post_put_loco_location(["机南"], "机南") == physical.LocoLocation("机走棚")


def test_route_rejects_long_train_through_link6_cache() -> None:
    graph = physical.TrackGraph()
    cars = [
        car("ALT1", line="机走棚", position=1, target_lines=["机走棚"]),
        car("ALT2", line="预修线", position=1, target_lines=["预修线"]),
    ]
    train_length = 262.9
    static_path = graph.route("机走北", "存5线北")

    path = graph.route_avoiding_occupied(
        "机走北",
        "存5线北",
        physical.occupied_lines_for_route(cars, set()),
        cars=cars,
        moving_nos=set(),
        train_length_m=train_length,
    )

    assert "联6" in static_path
    assert path
    assert "联6" not in path
    assert physical.route_line_length_reasons(static_path, train_length) == [
        "route_line_length_violation:联6:277.9>192.0"
    ]


def test_reversal_middle_blocker_can_be_used_when_length_fits() -> None:
    graph = physical.TrackGraph()
    cars = [car("P1", line="预修线", position=1, length=20.0)]
    path = graph.route_avoiding_occupied(
        "存2线",
        "渡7",
        physical.occupied_lines_for_route(cars, set()),
        cars=cars,
        moving_nos=set(),
        train_length_m=30.0,
    )
    assert path == ["存2线", "预修线", "渡7"]


def test_reversal_middle_blocker_uses_other_path_when_length_overflows() -> None:
    graph = physical.TrackGraph()
    cars = [car("P1", line="预修线", position=1, length=20.0)]
    path = graph.route_avoiding_occupied(
        "存2线",
        "渡7",
        physical.occupied_lines_for_route(cars, set()),
        cars=cars,
        moving_nos=set(),
        train_length_m=190.0,
    )
    assert path
    assert path != ["存2线", "预修线", "渡7"]
    assert physical.pre_repair_reversal_reasons(path, cars, set(), 190.0) == []


def test_planlet_get_route_uses_current_carry_length_not_future_pickup() -> None:
    cars = [
        car("B1", line="机北1", position=1, target_lines=["机北1"]),
        car("M1", line="机走北", position=1, length=90.0, target_lines=["机走棚"]),
    ]
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=8,
        source_line="机走北",
        target_line="机走棚",
        batch=[cars[1]],
        steps=(
            physical.plan_step("Get", "机走北", ("M1",)),
            physical.plan_step("Put", "机走棚", ("M1",), {"M1": 1}),
        ),
        reason="test",
        candidate_kind="test_planlet_get_length",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert validation.accepted


def test_loco_stands_on_put_approach_node_after_put_to_jinan() -> None:
    cars = [
        car("A", line="抛丸线", position=1, target_lines=["修1库内"]),
        car("X", line="机走棚", position=1, target_lines=["调梁棚"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=1,
        source_line="抛丸线",
        target_line="机南",
        batch=[cars[0]],
        planned_positions={"A": 1},
        generation_reason="test",
        candidate_kind="vnext_depot_inbound_assembly_session",
    )
    graph = physical.TrackGraph()
    validation = physical.validate_candidate(
        graph,
        candidate,
        cars,
        physical.LocoLocation("机库线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    next_loco = physical.next_loco_location(candidate, validation)
    assert next_loco == physical.LocoLocation("渡10")

    physical.apply_candidate(candidate, cars, validation)
    occupied = physical.occupied_lines_for_get_route(cars, {"X"}, "机走棚")
    path = graph.route_avoiding_occupied(
        next_loco.line,
        "机走棚",
        occupied,
        source_departure_lines=physical.route_departure_lines_for_source(next_loco.line, cars, {"X"}),
        target_approach_lines=physical.route_approach_lines_for_get("机走棚"),
        cars=cars,
        moving_nos={"X"},
        train_length_m=0.0,
    )
    assert path
    assert path[-2:] == ["机走北", "机走棚"]


def test_depot_outbound_session_to_cun4_leaves_loco_at_north_end() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="修1库内",
        target_line="存4线",
        batch=cars,
        cars=cars,
        depot_assignment=depot_assignment,
        reason="test",
        candidate_kind="vnext_depot_outbound_session",
        planned_positions={"A": 1},
    )
    assert candidate is not None
    graph = physical.TrackGraph()
    validation = physical.validate_candidate(
        graph,
        candidate,
        cars,
        physical.LocoLocation("修1库内"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    assert validation.put_path[-2:] == ("存4南", "存4线")
    assert physical.next_loco_location(candidate, validation) == physical.LocoLocation("存4线")


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


def test_spotting_repack_empty_target_uses_contiguous_source_put_order() -> None:
    cars = [
        car("B1", line="存1线", position=1, target_lines=["存1线"]),
        car("B2", line="存1线", position=2, target_lines=["存1线"]),
        car("B3", line="存1线", position=3, target_lines=["存1线"]),
        car("T1", line="存1线", position=4, target_lines=["抛丸线"]),
        car("T2", line="存1线", position=5, target_lines=["抛丸线"]),
        car("E1", line="存1线", position=6, target_lines=["存1线"]),
    ]
    cars[4]["_ForcePositions"] = (2, 3)
    cars[4]["ForceTargetPosition"] = [2, 3]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="FUNCTION_LINE_SERVICE:存1线->抛丸线:T1,T2",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        subject_nos=("T1", "T2"),
        source_lines=("存1线",),
        target_lines=("抛丸线",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SpottingRepackEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    put = next(
        step
        for step in envelopes[0].candidate.plan_steps
        if step.action == "Put" and step.line == "抛丸线"
    )
    assert put.planned_positions == {"T1": 1, "T2": 2}
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_source_prefix_release_returns_satisfied_source_blocker() -> None:
    cars = [
        car("B1", line="存5线北", position=1, target_lines=["存5线北"]),
        car("T1", line="存5线北", position=2, target_lines=["存5线南"]),
        car("T2", line="存5线北", position=3, target_lines=["存5线南"]),
        car("E1", line="存5线南", position=1, target_lines=["存5线南"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="YARD_REBALANCE:存5线北->存5线南:T1,T2",
        family=ContractFamily.YARD_REBALANCE,
        subject_nos=("T1", "T2"),
        source_lines=("存5线北",),
        target_lines=("存5线南",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SourcePrefixReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存5线北"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    steps = envelopes[0].candidate.plan_steps
    assert [(step.action, step.line, step.move_car_nos) for step in steps] == [
        ("Get", "存5线北", ("B1", "T1", "T2")),
        ("Put", "存5线南", ("T1", "T2")),
        ("Put", "存5线北", ("B1",)),
    ]
    assert envelopes[0].resource_request.same_plan_source_return_nos == ("B1",)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存5线北"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_direct_move_handles_same_line_pending_weigh() -> None:
    cars = [
        car("W1", line="存4线", position=1, target_lines=["存4线"], weigh=True),
        car("S1", line="存4线", position=2, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="SPECIAL_REPAIR_PROCESS:存4线->存4线:W1",
        family=ContractFamily.SPECIAL_REPAIR_PROCESS,
        subject_nos=("W1",),
        source_lines=("存4线",),
        target_lines=("存4线",),
        priority=1,
        obligations=("move_to_target", "weigh_tail_only"),
    )
    envelopes = list(
        DirectMoveEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "存4线", ("W1",)),
        ("Weigh", "机库线", ("W1",)),
        ("Put", "存4线", ("W1",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存4线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelopes[0].candidate, cars, validation)
    assert next(item for item in prospective if physical.car_no(item) == "W1")["_Weighed"]


def test_source_prefix_release_allows_completed_weigh_blocker() -> None:
    cars = [
        car("W1", line="存4线", position=1, target_lines=["存4线"], weigh=True),
        car("T1", line="存4线", position=2, target_lines=["油漆线"]),
    ]
    cars[0]["_Weighed"] = True
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="FUNCTION_LINE_SERVICE:存4线->油漆线:T1",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        subject_nos=("T1",),
        source_lines=("存4线",),
        target_lines=("油漆线",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SourcePrefixReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "存4线", ("W1", "T1")),
        ("Put", "油漆线", ("T1",)),
        ("Put", "存4线", ("W1",)),
    ]


def test_source_prefix_release_marks_cun4_to_unwheel_as_release_accept() -> None:
    cars = [
        car("B1", line="存4线", position=1, target_lines=["存4线"]),
        car("T1", line="存4线", position=2, target_lines=["卸轮线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="FUNCTION_LINE_SERVICE:存4线->卸轮线:T1",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        subject_nos=("T1",),
        source_lines=("存4线",),
        target_lines=("卸轮线",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SourcePrefixReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    assert envelopes[0].intent == IntentKind.CUN4_RELEASE_ACCEPT
    assert envelopes[0].resource_request.intent == IntentKind.CUN4_RELEASE_ACCEPT


def test_spotting_repack_restores_existing_target_in_contiguous_chunks() -> None:
    cars = [
        car("S1", line="存5线北", position=1, target_lines=["调梁棚"]),
        car("S2", line="存5线北", position=2, target_lines=["调梁棚"]),
        car("S3", line="存5线北", position=3, target_lines=["调梁棚"]),
        car("S4", line="存5线北", position=4, target_lines=["调梁棚"]),
        car("F1", line="调梁棚", position=5, target_lines=["调梁棚"]),
        car("F2", line="调梁棚", position=6, target_lines=["调梁棚"]),
        car("F3", line="调梁棚", position=7, target_lines=["调梁棚"]),
        car("F4", line="调梁棚", position=8, target_lines=["调梁棚"]),
        car("E1", line="调梁棚", position=9, target_lines=["调梁棚"]),
    ]
    for item in cars[4:8]:
        item["_ForcePositions"] = (6, 7, 8, 9)
        item["ForceTargetPosition"] = [6, 7, 8, 9]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="DISPATCH_SHED_QUEUE:存5线北->调梁棚:S1,S2,S3,S4",
        family=ContractFamily.DISPATCH_SHED_QUEUE,
        subject_nos=("S1", "S2", "S3", "S4"),
        source_lines=("存5线北",),
        target_lines=("调梁棚",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SpottingRepackEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存5线北"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    repair_puts = [
        step
        for step in envelopes[0].candidate.plan_steps
        if step.action == "Put" and step.line == "调梁棚"
    ]
    assert [step.move_car_nos for step in repair_puts] == [
        ("F1", "F2", "F3", "F4"),
        ("S1", "S2", "S3", "S4"),
        ("E1",),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存5线北"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_spotting_same_line_repack_gets_current_order_then_chunk_puts_final_order() -> None:
    cars = [
        car("N1", line="调梁棚", position=1, target_lines=["调梁棚"]),
        car("N2", line="调梁棚", position=2, target_lines=["调梁棚"]),
        car("N3", line="调梁棚", position=3, target_lines=["调梁棚"]),
        car("N4", line="调梁棚", position=4, target_lines=["调梁棚"]),
        car("F1", line="调梁棚", position=5, target_lines=["调梁棚"]),
        car("F2", line="调梁棚", position=6, target_lines=["调梁棚"]),
        car("F3", line="调梁棚", position=7, target_lines=["调梁棚"]),
        car("F4", line="调梁棚", position=8, target_lines=["调梁棚"]),
        car("S1", line="调梁棚", position=9, target_lines=["调梁棚"]),
        car("S2", line="调梁棚", position=16, target_lines=["调梁棚"]),
    ]
    for item in cars[4:8]:
        item["_ForcePositions"] = (6, 7, 8, 9)
        item["ForceTargetPosition"] = [6, 7, 8, 9]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="DISPATCH_SHED_QUEUE:调梁棚->调梁棚:F1,F2,F3,F4",
        family=ContractFamily.DISPATCH_SHED_QUEUE,
        subject_nos=("F1", "F2", "F3", "F4"),
        source_lines=("调梁棚",),
        target_lines=("调梁棚",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SpottingRepackEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("调梁棚"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    steps = envelopes[0].candidate.plan_steps
    assert steps[0] == physical.plan_step(
        "Get",
        "调梁棚",
        ("N1", "N2", "N3", "N4", "F1", "F2", "F3", "F4", "S1", "S2"),
    )
    assert [step.move_car_nos for step in steps if step.action == "Put" and step.line == "调梁棚"] == [
        ("F1", "F2", "F3", "F4", "S1", "S2"),
        ("N1", "N2", "N3", "N4"),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("调梁棚"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelopes[0].candidate, cars, validation)
    assert physical.spotting_group_is_acceptable(
        prospective,
        "调梁棚",
        (6, 7, 8, 9),
        depot_assignment,
    )


def test_spotting_same_line_repack_stages_satisfied_route_blocker() -> None:
    cars = [
        car("B", line="调梁线北", position=1, target_lines=["调梁线北"]),
        car("N1", line="调梁棚", position=1, target_lines=["调梁棚"]),
        car("N2", line="调梁棚", position=2, target_lines=["调梁棚"]),
        car("N3", line="调梁棚", position=3, target_lines=["调梁棚"]),
        car("N4", line="调梁棚", position=4, target_lines=["调梁棚"]),
        car("F1", line="调梁棚", position=5, target_lines=["调梁棚"]),
        car("F2", line="调梁棚", position=6, target_lines=["调梁棚"]),
        car("F3", line="调梁棚", position=7, target_lines=["调梁棚"]),
        car("F4", line="调梁棚", position=8, target_lines=["调梁棚"]),
        car("S1", line="调梁棚", position=9, target_lines=["调梁棚"]),
        car("S2", line="调梁棚", position=16, target_lines=["调梁棚"]),
    ]
    for item in cars[5:9]:
        item["_ForcePositions"] = (6, 7, 8, 9)
        item["ForceTargetPosition"] = [6, 7, 8, 9]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="DISPATCH_SHED_QUEUE:调梁棚->调梁棚:F1,F2,F3,F4",
        family=ContractFamily.DISPATCH_SHED_QUEUE,
        subject_nos=("F1", "F2", "F3", "F4"),
        source_lines=("调梁棚",),
        target_lines=("调梁棚",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        SpottingRepackEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    steps = envelopes[0].candidate.plan_steps
    assert steps[0].action == "Get" and steps[0].line == "调梁线北" and steps[0].move_car_nos == ("B",)
    assert steps[1].action == "Put" and steps[1].line != "调梁线北" and steps[1].move_car_nos == ("B",)
    assert steps[-2].action == "Get" and steps[-2].line == steps[1].line and steps[-2].move_car_nos == ("B",)
    assert steps[-1] == physical.plan_step("Put", "调梁线北", ("B",), {"B": 1})
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存4线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_plain_line_source_positions_compact_after_north_end_remove() -> None:
    cars = [
        car("N1", line="存1线", position=1, target_lines=["存2线"]),
        car("E1", line="存1线", position=2, target_lines=["存1线"]),
        car("E2", line="存1线", position=5, target_lines=["存1线"]),
    ]
    physical.apply_physical_get_order(cars, "存1线", ("N1",))
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "N1": ("", 0),
        "E1": ("存1线", 1),
        "E2": ("存1线", 2),
    }


def test_spotting_line_source_positions_preserve_after_north_end_remove() -> None:
    cars = [
        car("N1", line="抛丸线", position=1, target_lines=["存1线"]),
        car("F1", line="抛丸线", position=2, target_lines=["抛丸线"]),
        car("F2", line="抛丸线", position=3, target_lines=["抛丸线"]),
    ]
    for item in cars[1:]:
        item["_ForcePositions"] = (2, 3)
        item["ForceTargetPosition"] = [2, 3]
    physical.apply_physical_get_order(cars, "抛丸线", ("N1",))
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "N1": ("", 0),
        "F1": ("抛丸线", 2),
        "F2": ("抛丸线", 3),
    }


def test_depot_source_positions_preserve_after_north_end_remove() -> None:
    cars = [
        car("N1", line="修3库内", position=1, target_lines=["存4线"]),
        car("D1", line="修3库内", position=2, target_lines=["修3库内"]),
        car("D2", line="修3库内", position=4, target_lines=["修3库内"]),
    ]
    physical.apply_physical_get_order(cars, "修3库内", ("N1",))
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "N1": ("", 0),
        "D1": ("修3库内", 2),
        "D2": ("修3库内", 4),
    }


def test_get_order_allows_only_north_end_prefix() -> None:
    cars = [
        car("A", line="存1线", position=1),
        car("B", line="存1线", position=2),
        car("C", line="存1线", position=3),
    ]
    assert physical.inaccessible_get_reason(
        cars=cars,
        line="存1线",
        move_nos=("A", "B"),
        carried_nos=set(),
        step_index=1,
    ) == ""
    reason = physical.inaccessible_get_reason(
        cars=cars,
        line="存1线",
        move_nos=("B",),
        carried_nos=set(),
        step_index=1,
    )
    assert reason.startswith("line_end_get_order_violation")
    reason = physical.inaccessible_get_reason(
        cars=cars,
        line="存1线",
        move_nos=("A", "C"),
        carried_nos=set(),
        step_index=1,
    )
    assert "reachable=A,B" in reason


def test_put_order_allows_only_train_tail_suffix() -> None:
    carried_order = ["A", "B", "C"]
    assert physical.inaccessible_put_reason(carried_order, ("C",), 2) == ""
    assert physical.inaccessible_put_reason(carried_order, ("B", "C"), 2) == ""
    assert physical.inaccessible_put_reason(carried_order, ("A",), 2).startswith(
        "train_tail_put_order_violation"
    )
    assert physical.inaccessible_put_reason(carried_order, ("C", "B"), 2).startswith(
        "train_tail_put_order_violation"
    )


def test_put_inserts_from_north_end_and_shifts_existing_on_plain_lines() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["存2线"]),
        car("M2", line="存1线", position=2, target_lines=["存2线"]),
        car("E1", line="存2线", position=4, target_lines=["存2线"]),
        car("E2", line="存2线", position=8, target_lines=["存2线"]),
    ]
    physical.apply_physical_put_order(cars, "存2线", ["M1", "M2"])
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "M1": ("存2线", 1),
        "M2": ("存2线", 2),
        "E1": ("存2线", 3),
        "E2": ("存2线", 4),
    }


def test_business_position_put_rejects_depot_slot_behind_existing_access_car() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["修1库内"]),
        car("E1", line="修1库内", position=1, target_lines=["修1库内"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=6,
        source_line="存1线",
        target_line="修1库内",
        batch=cars[:1],
        planned_positions={"M1": 2},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert any(
        reason.startswith("business_position_put_blocked_by_access_end:修1库内:")
        for reason in validation.reasons
    )


def test_business_planned_positions_must_preserve_put_order() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["修1库内"]),
        car("M2", line="存1线", position=2, target_lines=["修1库内"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=7,
        source_line="存1线",
        target_line="修1库内",
        batch=cars,
        planned_positions={"M1": 2, "M2": 1},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert any(reason.startswith("target_put_order_violation:修1库内:") for reason in validation.reasons)


def test_business_planned_positions_must_be_contiguous_translation() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["修1库内"]),
        car("M2", line="存1线", position=2, target_lines=["修1库内"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=8,
        source_line="存1线",
        target_line="修1库内",
        batch=cars,
        planned_positions={"M1": 2, "M2": 4},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert not validation.accepted
    assert any(reason.startswith("target_put_order_violation:修1库内:") for reason in validation.reasons)


def test_business_planned_positions_allow_offset_before_existing_access_car() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["存2线"]),
        car("M2", line="存1线", position=2, target_lines=["存2线"]),
        car("E1", line="存2线", position=5, target_lines=["存2线"]),
    ]
    cars[0]["_ForcePositions"] = (3,)
    cars[0]["ForceTargetPosition"] = [3]
    cars[1]["_ForcePositions"] = (4,)
    cars[1]["ForceTargetPosition"] = [4]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=9,
        source_line="存1线",
        target_line="存2线",
        batch=cars[:2],
        planned_positions={"M1": 3, "M2": 4},
        generation_reason="test",
        candidate_kind="target_move",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert validation.accepted


def test_forced_position_put_uses_business_position_on_plain_line() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["存2线"]),
        car("E1", line="存2线", position=1, target_lines=["存2线"]),
    ]
    cars[0]["_ForcePositions"] = (3,)
    cars[0]["ForceTargetPosition"] = [3]
    physical.apply_physical_put_order(
        cars,
        "存2线",
        ["M1"],
        {"M1": 3},
    )
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "M1": ("存2线", 3),
        "E1": ("存2线", 1),
    }


def test_existing_forced_position_on_plain_target_line_is_not_shifted_by_put() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["存2线"]),
        car("F1", line="存2线", position=3, target_lines=["存2线"]),
    ]
    cars[1]["_ForcePositions"] = (3,)
    cars[1]["ForceTargetPosition"] = [3]
    physical.apply_physical_put_order(
        cars,
        "存2线",
        ["M1"],
        {"M1": 1},
    )
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "M1": ("存2线", 1),
        "F1": ("存2线", 3),
    }


def route_after_get_from_source(
    source_line: str,
    target_line: str,
    *,
    target_occupied: bool = False,
) -> list[str]:
    cars = [
        car("M1", line=source_line, position=1, target_lines=[target_line]),
        car("S1", line=source_line, position=2, target_lines=[source_line]),
    ]
    if target_occupied:
        cars.append(car("T1", line=target_line, position=1, target_lines=[target_line]))
    moving_nos = {"M1"}
    return physical.TrackGraph().route_avoiding_occupied(
        source_line,
        target_line,
        physical.occupied_lines_for_route(cars, moving_nos),
        source_departure_lines=physical.route_departure_lines_for_source(source_line, cars, moving_nos),
        target_approach_lines=physical.route_approach_lines_for_put(target_line, cars, moving_nos),
    )


def test_occupied_source_line_departure_uses_configured_operation_end() -> None:
    cases = [
        ("存5线南", "预修线", True, {"存5线北"}, {"渡7", "存2线"}),
        ("预修线", "存5线南", True, {"渡7", "存2线"}, {"存5线北"}),
        ("存3线", "洗罐站", False, {"渡3"}, None),
        ("存4线", "修3库内", False, {"渡1"}, None),
    ]
    for source_line, target_line, target_occupied, allowed_first, allowed_before_target in cases:
        path = route_after_get_from_source(
            source_line,
            target_line,
            target_occupied=target_occupied,
        )
        assert path, (source_line, target_line)
        assert path[1] in allowed_first, path
        if allowed_before_target:
            assert path[-2] in allowed_before_target, path


def test_unoccupied_source_line_can_depart_from_other_end_when_target_requires_it() -> None:
    cars = [
        car("M1", line="存5线南", position=1, target_lines=["预修线"]),
        car("T1", line="预修线", position=1, target_lines=["预修线"]),
    ]
    moving_nos = {"M1"}
    path = physical.TrackGraph().route_avoiding_occupied(
        "存5线南",
        "预修线",
        physical.occupied_lines_for_route(cars, moving_nos),
        source_departure_lines=physical.route_departure_lines_for_source("存5线南", cars, moving_nos),
        target_approach_lines=physical.route_approach_lines_for_put("预修线", cars, moving_nos),
    )
    assert path[1] == "渡8"
    assert path[-2] in {"渡7", "存2线"}


def test_serial_gate_lease_allows_only_downstream_debt_service() -> None:
    lease = SerialGateLease(
        lease_id="T:机走棚:1",
        owner_contract_id="C",
        blocker_line="机走棚",
        opened_hook=1,
        blocker_nos=("B1",),
        debt_nos=("D1", "D2"),
    )
    assert serial.lease_allows_put(lease, ("D1",))
    assert not serial.lease_allows_put(lease, ("B1",))
    assert not serial.lease_allows_put(lease, ("X1",))
    assert serial.lease_pollution_nos(lease, ("D1", "B1", "X1")) == ("B1", "X1")


def test_serial_gate_resource_uses_put_step_nos_not_whole_candidate_batch() -> None:
    graph = StationResourceGraph()
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    cars = [
        car("D1", line="洗罐站", position=1, target_lines=["存2线"]),
        car("D2", line="洗罐站", position=2, target_lines=["存2线"]),
        car("B1", line="机走棚", position=1, target_lines=["存1线"]),
    ]
    lease = SerialGateLease(
        lease_id="T:机走棚:1",
        owner_contract_id="C",
        blocker_line="机走棚",
        opened_hook=1,
        blocker_nos=("B1",),
        debt_nos=("D1", "D2"),
    )
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=2,
        source_line="洗罐站",
        target_line="机走棚",
        batch=[cars[0], cars[2]],
        planned_positions={},
        generation_reason="test",
        candidate_kind="test_serial_lease_multidrop",
        plan_steps=(
            physical.plan_step("Get", "洗罐站", ("D1", "B1")),
            physical.plan_step("Put", "机走棚", ("D1",)),
            physical.plan_step("Put", "存1线", ("B1",)),
        ),
    )
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(ResourceKind.SERIAL_LINE_GATE,),
        source_line="洗罐站",
        target_line="机走棚",
        move_nos=("D1", "B1"),
        touched_lines=("洗罐站", "机走棚", "存1线"),
        put_lines=("机走棚", "存1线"),
        intent=IntentKind.BLOCKER_STAGING,
    )
    assert graph._serial_blocker_storage_violations(
        request,
        candidate=candidate,
        cars=cars,
        depot_assignment=depot_assignment,
        serial_gate_leases={"机走棚": lease},
    ) == []
    other_owner_request = ResourceRequest(
        contract_id="OTHER",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(ResourceKind.SERIAL_LINE_GATE,),
        source_line="洗罐站",
        target_line="机走棚",
        move_nos=("D1", "B1"),
        touched_lines=("洗罐站", "机走棚", "存1线"),
        put_lines=("机走棚", "存1线"),
        intent=IntentKind.BLOCKER_STAGING,
    )
    assert graph._serial_blocker_storage_violations(
        other_owner_request,
        candidate=candidate,
        cars=cars,
        depot_assignment=depot_assignment,
        serial_gate_leases={"机走棚": lease},
    )[0].startswith("serial_blocker_storage_before_downstream_clear")


def test_serial_gate_allows_same_plan_temporary_staging_clear() -> None:
    graph = StationResourceGraph()
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    cars = [
        car("S", line="存1线", position=1, target_lines=["调梁棚"]),
        car("D", line="存5线南", position=1, target_lines=["预修线"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="调梁棚",
        batch=[cars[0]],
        planned_positions={},
        generation_reason="test",
        candidate_kind="vnext_spotting_repack",
        plan_steps=(
            physical.plan_step("Get", "存1线", ("S",)),
            physical.plan_step("Put", "存5线北", ("S",), {"S": 1}),
            physical.plan_step("Get", "存5线北", ("S",)),
            physical.plan_step("Put", "调梁棚", ("S",), {"S": 1}),
        ),
    )
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(ResourceKind.SERIAL_LINE_GATE,),
        source_line="存1线",
        target_line="调梁棚",
        move_nos=("S",),
        touched_lines=("存1线", "存5线北", "调梁棚"),
        put_lines=("存5线北", "调梁棚"),
        intent=IntentKind.FRONT_PREP,
    )

    assert graph._serial_blocker_storage_violations(
        request,
        candidate=candidate,
        cars=cars,
        depot_assignment=depot_assignment,
        serial_gate_leases={},
    ) == []


def test_serial_gate_rejects_undeclared_same_plan_temporary_staging_clear() -> None:
    graph = StationResourceGraph()
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    cars = [
        car("S", line="存1线", position=1, target_lines=["调梁棚"]),
        car("D", line="存5线南", position=1, target_lines=["调梁棚"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="调梁棚",
        batch=[cars[0]],
        planned_positions={},
        generation_reason="test",
        candidate_kind="test_same_plan_temporary_staging",
        plan_steps=(
            physical.plan_step("Get", "存1线", ("S",)),
            physical.plan_step("Put", "存5线北", ("S",), {"S": 1}),
            physical.plan_step("Get", "存5线北", ("S",)),
            physical.plan_step("Put", "调梁棚", ("S",), {"S": 1}),
        ),
    )
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(ResourceKind.SERIAL_LINE_GATE,),
        source_line="存1线",
        target_line="调梁棚",
        move_nos=("S",),
        touched_lines=("存1线", "存5线北", "调梁棚"),
        put_lines=("存5线北", "调梁棚"),
        intent=IntentKind.FRONT_PREP,
    )

    assert graph._serial_blocker_storage_violations(
        request,
        candidate=candidate,
        cars=cars,
        depot_assignment=depot_assignment,
        serial_gate_leases={},
    ) == ["serial_blocker_storage_before_downstream_clear:存5线北:存5线南:D"]


def test_cun4_same_plan_source_return_does_not_require_buffer_owner() -> None:
    graph = StationResourceGraph()
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    cars = [
        car("R1", line="存4线", position=1, target_lines=["存4线"]),
        car("T1", line="存4线", position=2, target_lines=["油漆线"]),
    ]
    candidate = physical.hook_candidate(
        case_id="T",
        hook_index=1,
        source_line="存4线",
        target_line="存4线",
        batch=cars,
        planned_positions={},
        generation_reason="test",
        candidate_kind="vnext_spotting_repack",
        plan_steps=(
            physical.plan_step("Get", "存4线", ("R1", "T1")),
            physical.plan_step("Put", "油漆线", ("T1",), {"T1": 1}),
            physical.plan_step("Put", "存4线", ("R1",), {"R1": 1}),
        ),
    )
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(ResourceKind.CUN4_NORTH_BUFFER,),
        source_line="油漆线",
        target_line="油漆线",
        move_nos=("R1", "T1"),
        touched_lines=("存4线", "油漆线"),
        put_lines=("油漆线", "存4线"),
        intent=IntentKind.FRONT_PREP,
        same_plan_source_return_nos=("R1",),
    )
    delta = graph.acquire(
        request,
        candidate=candidate,
        validation=SimpleNamespace(reasons=()),
        cars=cars,
        depot_assignment=depot_assignment,
    )
    assert "cun4_buffer_requires_owner" not in delta.violations


def test_h4_blocks_front_work_even_when_it_touches_remote_line() -> None:
    gate = HumanPhaseGate()
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id="candidate",
        resources=(),
        source_line="存5线北",
        target_line="卸轮线",
        move_nos=("D1",),
        touched_lines=("存5线北", "卸轮线"),
        put_lines=("卸轮线",),
        intent=IntentKind.FRONT_PREP,
    )
    contract = FlowContract(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        subject_nos=("D1",),
        source_lines=("存5线北",),
        target_lines=("卸轮线",),
        priority=1,
        obligations=("move_to_target",),
    )
    permission = gate.permission(
        phase_state=PhaseState(
            phase=PhaseKind.H4_REMOTE_DEPOT,
            front_debt=1,
            cun4_port_debt=0,
            remote_debt=5,
            closeout_debt=0,
            reason="remote_session_continuation",
        ),
        envelope=CandidateEnvelope(
            candidate=object(),
            contract=contract,
            intent=IntentKind.FRONT_PREP,
            resource_request=request,
            template_name="direct_accessible_prefix",
        ),
        contract_delta=ContractDelta(
            contract_id="C",
            family=ContractFamily.FUNCTION_LINE_SERVICE,
            before_unsatisfied=5,
            after_unsatisfied=4,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        resource_delta=ResourceDelta(request=request, acquired=(), released_lines=()),
        remote_session=RemoteSessionState(active=True),
    )
    assert not permission.allowed
    assert permission.reason == "h4_blocks_front_work_until_remote_debt_clear"


def test_remote_source_front_family_is_h4_remote_work() -> None:
    gate = HumanPhaseGate()
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id="candidate",
        resources=(),
        source_line="存4线",
        target_line="油漆线",
        move_nos=("D1",),
        touched_lines=("存4线", "油漆线"),
        put_lines=("油漆线",),
        intent=IntentKind.FRONT_PREP,
    )
    contract = FlowContract(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        subject_nos=("D1",),
        source_lines=("存4线",),
        target_lines=("油漆线",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelope = CandidateEnvelope(
        candidate=object(),
        contract=contract,
        intent=IntentKind.FRONT_PREP,
        resource_request=request,
        template_name="source_prefix_release",
    )
    resource_delta = ResourceDelta(request=request, acquired=(), released_lines=())
    assert gate.target_phase(envelope=envelope, resource_delta=resource_delta) == "H4"
    permission = gate.permission(
        phase_state=PhaseState(
            phase=PhaseKind.H4_REMOTE_DEPOT,
            front_debt=1,
            cun4_port_debt=0,
            remote_debt=1,
            closeout_debt=0,
            reason="remote_session_continuation",
            depot_inbound_assembly_complete=True,
        ),
        envelope=envelope,
        contract_delta=ContractDelta(
            contract_id="C",
            family=ContractFamily.FUNCTION_LINE_SERVICE,
            before_unsatisfied=1,
            after_unsatisfied=0,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        resource_delta=resource_delta,
        remote_session=RemoteSessionState(active=True),
    )
    assert permission.allowed


def test_target_line_staging_to_cun4_does_not_force_h2_by_itself() -> None:
    gate = HumanPhaseGate()
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.DEPOT_OUTBOUND,
        candidate_id="candidate",
        resources=(),
        source_line="修2库内",
        target_line="存4线",
        move_nos=("D1",),
        touched_lines=("修2库内", "存4线"),
        put_lines=("存4线",),
        intent=IntentKind.REMOTE_DEPOT,
    )
    contract = FlowContract(
        contract_id="C",
        family=ContractFamily.DEPOT_OUTBOUND,
        subject_nos=("D1",),
        source_lines=("修2库内",),
        target_lines=("存4线",),
        priority=1,
        obligations=("move_to_target",),
    )
    target_phase = gate.target_phase(
        envelope=CandidateEnvelope(
            candidate=object(),
            contract=contract,
            intent=IntentKind.REMOTE_DEPOT,
            resource_request=request,
            template_name="remote_depot_direct_accessible_prefix",
        ),
        resource_delta=ResourceDelta(request=request, acquired=(), released_lines=()),
    )
    assert target_phase == "H4"


def test_h4_allows_cun4_port_support_for_remote_debt() -> None:
    gate = HumanPhaseGate()
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.CUN4_PORT_STAGING,
        candidate_id="candidate",
        resources=(),
        source_line="存2线",
        target_line="存4线",
        move_nos=("D1", "D2"),
        touched_lines=("存2线", "存4线"),
        put_lines=("存4线",),
        intent=IntentKind.FRONT_PREP,
    )
    contract = FlowContract(
        contract_id="C",
        family=ContractFamily.CUN4_PORT_STAGING,
        subject_nos=("D1", "D2"),
        source_lines=("存2线",),
        target_lines=("存4线",),
        priority=1,
        obligations=("shape_cun4_release_port",),
    )
    permission = gate.permission(
        phase_state=PhaseState(
            phase=PhaseKind.H4_REMOTE_DEPOT,
            front_debt=0,
            cun4_port_debt=2,
            remote_debt=5,
            closeout_debt=0,
            reason="remote_session_continuation",
        ),
        envelope=CandidateEnvelope(
            candidate=object(),
            contract=contract,
            intent=IntentKind.FRONT_PREP,
            resource_request=request,
            template_name="direct_accessible_prefix",
        ),
        contract_delta=ContractDelta(
            contract_id="C",
            family=ContractFamily.CUN4_PORT_STAGING,
            before_unsatisfied=5,
            after_unsatisfied=4,
            before_contract_debt=2,
            after_contract_debt=1,
        ),
        resource_delta=ResourceDelta(request=request, acquired=(), released_lines=()),
        remote_session=RemoteSessionState(active=True),
    )
    assert permission.allowed
    assert permission.relation == "support"


def test_stage4_linear_sweep_empties_cun5_north_before_putting_cun5_south() -> None:
    episode = Stage4LinearSweepEpisode()
    cars = [
        car("A", line="存5线北", position=1, target_lines=["预修线"]),
        car("B", line="存5线北", position=2, target_lines=["存3线"]),
        car("C", line="存5线北", position=3, target_lines=["存5线南"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="YARD_REBALANCE:存5线北->存5线南:C",
        family=ContractFamily.YARD_REBALANCE,
        subject_nos=("C",),
        source_lines=("存5线北",),
        target_lines=("存5线南",),
        priority=1,
        obligations=("move_to_target",),
    )
    candidates = list(
        episode.generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存5线北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=SimpleNamespace(phase=PhaseKind.H5_CLOSEOUT),
        )
    )
    assert len(candidates) == 1
    steps = physical.candidate_plan_steps(candidates[0].candidate)
    assert steps[0] == physical.plan_step("Get", "存5线北", ("A", "B", "C"))
    assert [step.line for step in steps if step.action == "Put"] == ["存5线南", "存3线", "预修线"]


def test_stage4_linear_sweep_allows_repeated_target_segments() -> None:
    episode = Stage4LinearSweepEpisode()
    cars = [
        car("A", line="存5线北", position=1, target_lines=["预修线"]),
        car("B", line="存5线北", position=2, target_lines=["存3线"]),
        car("C", line="存5线北", position=3, target_lines=["预修线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="PRE_REPAIR_STAGING:存5线北->预修线:A,C",
        family=ContractFamily.PRE_REPAIR_STAGING,
        subject_nos=("A", "C"),
        source_lines=("存5线北",),
        target_lines=("预修线",),
        priority=1,
        obligations=("move_to_target",),
    )
    candidates = list(
        episode.generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存5线北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=SimpleNamespace(phase=PhaseKind.H5_CLOSEOUT),
        )
    )
    assert len(candidates) == 1
    steps = physical.candidate_plan_steps(candidates[0].candidate)
    assert [step.line for step in steps if step.action == "Put"] == ["预修线", "存3线", "预修线"]


def test_stage4_linear_sweep_leaves_satisfied_source_suffix() -> None:
    episode = Stage4LinearSweepEpisode()
    cars = [
        car("A", line="调梁棚", position=1, target_lines=["机走棚"]),
        car("B", line="调梁棚", position=2, target_lines=["机走棚"]),
        car("C", line="调梁棚", position=3, target_lines=["调梁棚"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="DISPATCH_SHED_QUEUE:调梁棚->机走棚:A,B",
        family=ContractFamily.DISPATCH_SHED_QUEUE,
        subject_nos=("A", "B"),
        source_lines=("调梁棚",),
        target_lines=("机走棚",),
        priority=1,
        obligations=("move_to_target",),
    )
    candidates = list(
        episode.generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("调梁棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=SimpleNamespace(phase=PhaseKind.H5_CLOSEOUT),
        )
    )
    assert len(candidates) == 1
    steps = physical.candidate_plan_steps(candidates[0].candidate)
    assert steps[0] == physical.plan_step("Get", "调梁棚", ("A", "B"))
    assert [step.line for step in steps if step.action == "Put"] == ["机走棚"]


def test_stage4_linear_sweep_splits_unordered_target_positions_from_tail() -> None:
    episode = Stage4LinearSweepEpisode()
    assert episode._tail_ordered_segment_start(
        drop=("A", "B", "C"),
        positions={"A": 1, "B": 2, "C": 3},
    ) == 0
    assert episode._tail_ordered_segment_start(
        drop=("A", "B", "C"),
        positions={"A": 1, "B": 3, "C": 2},
    ) == 2
    assert episode._tail_ordered_segment_start(
        drop=("A", "B", "C"),
        positions={},
    ) == 2


def test_stage4_linear_sweep_does_not_run_before_h5() -> None:
    episode = Stage4LinearSweepEpisode()
    cars = [car("C", line="存5线北", position=1, target_lines=["存5线南"])]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="YARD_REBALANCE:存5线北->存5线南:C",
        family=ContractFamily.YARD_REBALANCE,
        subject_nos=("C",),
        source_lines=("存5线北",),
        target_lines=("存5线南",),
        priority=1,
        obligations=("move_to_target",),
    )
    candidates = list(
        episode.generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存5线北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=SimpleNamespace(phase=PhaseKind.H4_REMOTE_DEPOT),
        )
    )
    assert candidates == []


def test_planlet_rejects_non_tail_put_and_accepts_tail_put_order() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["存3线"]),
        car("B", line="存1线", position=2, target_lines=["存2线"]),
        car("C", line="存1线", position=3, target_lines=["存2线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    graph = physical.TrackGraph()
    invalid = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="存2线",
        batch=cars,
        steps=(
            physical.plan_step("Get", "存1线", ("A", "B", "C")),
            physical.plan_step("Put", "存2线", ("A",)),
        ),
        reason="test",
        candidate_kind="test_planlet",
    )
    invalid_validation = physical.validate_candidate(
        graph,
        invalid,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert any(
        reason.startswith("train_tail_put_order_violation")
        for reason in invalid_validation.reasons
    )

    valid = physical.build_planlet_candidate(
        case_id="T",
        hook_index=2,
        source_line="存1线",
        target_line="存3线",
        batch=cars,
        steps=(
            physical.plan_step("Get", "存1线", ("A", "B", "C")),
            physical.plan_step("Put", "存2线", ("B", "C")),
            physical.plan_step("Put", "存3线", ("A",)),
        ),
        reason="test",
        candidate_kind="test_planlet",
    )
    valid_validation = physical.validate_candidate(
        graph,
        valid,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert valid_validation.accepted, valid_validation.reasons


def test_planlet_source_compaction_excludes_cars_still_on_loco() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["存3线"]),
        car("B", line="存1线", position=2, target_lines=["存2线"]),
        car("C", line="存1线", position=3, target_lines=["存2线"]),
        car("S", line="存1线", position=4, target_lines=["存1线"]),
    ]
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=3,
        source_line="存1线",
        target_line="存3线",
        batch=cars[:3],
        steps=(
            physical.plan_step("Get", "存1线", ("A", "B", "C")),
            physical.plan_step("Put", "存2线", ("B", "C")),
            physical.plan_step("Put", "存3线", ("A",)),
        ),
        reason="test",
        candidate_kind="test_planlet",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert validation.accepted, validation.reasons
    physical.apply_candidate(candidate, cars, validation)
    assert {physical.car_no(item): item["Position"] for item in cars}["S"] == 1


def test_planlet_partial_return_to_source_does_not_count_carried_cars_on_line() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["存3线"]),
        car("B", line="存1线", position=2, target_lines=["存1线"]),
        car("S", line="存1线", position=3, target_lines=["存1线"]),
    ]
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=4,
        source_line="存1线",
        target_line="存3线",
        batch=cars[:2],
        steps=(
            physical.plan_step("Get", "存1线", ("A", "B")),
            physical.plan_step("Put", "存1线", ("B",)),
            physical.plan_step("Put", "存3线", ("A",)),
        ),
        reason="test",
        candidate_kind="test_planlet",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存1线"),
        physical.DepotAssignment(slots={}, failures={}),
    )
    assert validation.accepted, validation.reasons
    physical.apply_candidate(candidate, cars, validation)
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "A": ("存3线", 1),
        "B": ("存1线", 1),
        "S": ("存1线", 2),
    }


def test_access_frontier_requires_real_depot_assignment() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["存2线"]),
    ]
    with pytest.raises(TypeError):
        AccessFrontier().direct_move_is_reachable(
            source_line="存1线",
            target_line="存2线",
            batch=cars,
            cars=cars,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            planned_positions={"A": 1},
        )


def test_depot_inbound_assembly_plan_keeps_cun4_clear_for_unwheel_and_reports_purity() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"], length=30.0),
        car("B", line="存1线", position=2, target_lines=["修2库内"], length=30.0),
        car("C", line="存1线", position=3, target_lines=["卸轮线"], length=30.0),
        car("G", line="机南", position=1, target_lines=["修2库内"], length=10.0),
        car("X", line="机走棚", position=1, target_lines=["存1线"], length=10.0),
    ]
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos=set(),
    )
    assert plan.purity_violation_nos == ("X",)
    assert plan.purity_violation_lines == ("机走棚",)
    assert plan.temporary_line_by_no["G"] == "机南"
    assert plan.temporary_line_by_no["C"] == "存4线"
    assert plan.temporary_line_by_no["A"] != "存4线"
    assert plan.temporary_line_by_no["B"] != "存4线"
    assert "机走棚" not in {plan.temporary_line_by_no[no] for no in ("A", "B", "C")}
    assert not plan.unassigned_nos
    assert plan.status == "fail"


def test_depot_inbound_assembly_plan_reports_capacity_without_cun4_repair_overflow() -> None:
    cars = [
        car(f"I{index}", line="存1线", position=index, target_lines=["修1库内"], length=20.0)
        for index in range(1, 17)
    ]
    cars.append(car("O", line="修1库内", position=1, target_lines=["存4线"], length=20.0))
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos={"O"},
    )
    line_counts = Counter(plan.temporary_line_by_no.values())
    assert line_counts["存4线"] == 0
    assert sum(line_counts[line] for line in ("机南", "机走棚", "机走北", "洗油北")) == 15
    assert plan.unassigned_nos == ("I16",)
    assert plan.status == "fail"


def test_depot_inbound_assembly_session_forms_strategic_group() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
        car("B", line="存1线", position=2, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.temporary_line_by_no == {"A": "机南", "B": "机南"}
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("存1线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblySessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.intent == IntentKind.DEPOT_INBOUND_ASSEMBLY
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "存1线", ("A", "B")),
        ("Put", "机南", ("A", "B")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert "depot_inbound_temporary_group_formed" in delta.reduced
    assert delta.support_gain == 2


def test_depot_inbound_assembly_session_prefers_larger_source_window() -> None:
    cars = [
        car("A", line="调梁棚", position=1, target_lines=["修1库内"]),
        car("B", line="预修线", position=1, target_lines=["修2库内"]),
        car("C", line="预修线", position=2, target_lines=["修3库内"]),
        car("D", line="预修线", position=3, target_lines=["修4库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=4,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B,C,D",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B", "C", "D"),
        source_lines=("调梁棚", "预修线"),
        target_lines=("修1库内", "修2库内", "修3库内", "修4库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblySessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机库线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 2
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "预修线", ("B", "C", "D")),
        ("Put", "机南", ("B", "C", "D")),
    ]


def test_depot_inbound_assembly_rebalance_moves_route_blocking_group_to_cun4() -> None:
    cars = [
        car("B1", line="机走棚", position=1, target_lines=["卸轮线"]),
        car("B2", line="机走棚", position=2, target_lines=["卸轮线"]),
        car("A", line="油漆线", position=1, target_lines=["修3库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=3,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B1,B2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B1", "B2"),
        source_lines=("油漆线", "机走棚"),
        target_lines=("卸轮线", "修3库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblyRebalanceEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机南"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "机走棚", ("B1", "B2")),
        ("Put", "存4线", ("B1", "B2")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机南"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_depot_inbound_assembly_rebalance_skips_stable_grouped_line() -> None:
    cars = [
        car("A", line="洗油北", position=1, target_lines=["修1库内"]),
        car("B", line="洗油北", position=2, target_lines=["修2库内"]),
        car("X", line="洗罐线北", position=1, target_lines=["存1线"]),
        car("C", line="存1线", position=1, target_lines=["修3库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=4,
    )
    assert "C" in strategic_plan.depot_inbound.ungrouped_nos
    assert "A" not in strategic_plan.depot_inbound.ungrouped_nos
    assert "B" not in strategic_plan.depot_inbound.ungrouped_nos
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B,C",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B", "C"),
        source_lines=("洗油北", "洗罐线北", "存1线"),
        target_lines=("修1库内", "修2库内", "修3库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblyRebalanceEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("洗油北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_inbound_prefix_assembly_session_restores_source_blocker() -> None:
    cars = [
        car("X", line="存1线", position=1, target_lines=["存2线"]),
        car("A", line="存1线", position=2, target_lines=["修1库内"]),
        car("B", line="存1线", position=3, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("存1线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundPrefixAssemblySessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "存1线", ("X", "A", "B")),
        ("Put", "机南", ("A", "B")),
        ("Put", "存1线", ("X",)),
    ]
    assert envelope.resource_request.same_plan_source_return_nos == ("X",)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.support_gain == 2


def test_depot_inbound_route_blocker_digest_clears_serial_blocker_line() -> None:
    cars = [
        car("X", line="调梁线北", position=1, target_lines=["洗罐站"]),
        car("A", line="调梁棚", position=1, target_lines=["修1库内"]),
        car("B", line="调梁棚", position=2, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("调梁棚",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundRouteBlockerDigestEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机库线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "调梁线北", ("X",)),
        ("Put", "洗罐站", ("X",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机库线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.support_gain == 1
    assert "serial_line_gate_released" in delta.reduced


def test_side_target_completion_lightly_rewards_inner_target_segment_growth() -> None:
    cars = [
        car("T", line="存2线", position=1, target_lines=["存2线"]),
        car("X", line="存2线", position=2, target_lines=["存3线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="存2线",
        target_line="存2线",
        batch=cars,
        steps=(
            physical.plan_step("Get", "存2线", ("T", "X")),
            physical.plan_step("Put", "存3线", ("X",), {"X": 1}),
            physical.plan_step("Put", "存2线", ("T",), {"T": 1}),
        ),
        reason="test",
        candidate_kind="test_inner_target_segment",
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:T",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("T",),
        source_lines=("存2线",),
        target_lines=("存2线",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelope = CandidateEnvelope(
        candidate=candidate,
        contract=contract,
        intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        resource_request=ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line="存2线",
            target_line="存2线",
            move_nos=tuple(candidate.move_car_nos),
            intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        ),
        template_name="test_inner_target_segment",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存2线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
    )
    assert "side_target_completion" in delta.reduced
    assert "inner_target_segment_extended" in delta.reduced
    assert delta.support_gain == 1


def test_inner_target_segment_allows_north_temporary_prefix_on_non_temporary_line() -> None:
    cars = [
        car("N", line="存2线", position=1, target_lines=["存3线"]),
        car("T", line="存2线", position=2, target_lines=["存2线"]),
        car("X", line="存2线", position=3, target_lines=["存3线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="存2线",
        target_line="存2线",
        batch=cars,
        steps=(
            physical.plan_step("Get", "存2线", ("N", "T", "X")),
            physical.plan_step("Put", "存3线", ("X",), {"X": 1}),
            physical.plan_step("Put", "存2线", ("N", "T"), {"N": 1, "T": 2}),
        ),
        reason="test",
        candidate_kind="test_inner_target_segment",
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:T",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("T",),
        source_lines=("存2线",),
        target_lines=("存2线",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelope = CandidateEnvelope(
        candidate=candidate,
        contract=contract,
        intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        resource_request=ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line="存2线",
            target_line="存2线",
            move_nos=tuple(candidate.move_car_nos),
            intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        ),
        template_name="test_inner_target_segment",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("存2线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(candidate, cars, validation)
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in prospective
    } == {
        "N": ("存2线", 1),
        "T": ("存2线", 2),
        "X": ("存3线", 1),
    }
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
    )
    assert "inner_target_segment_extended" in delta.reduced
    assert delta.support_gain == 1


def test_inner_target_segment_reward_ignores_temporary_lines() -> None:
    cars = [
        car("N", line="机南", position=1, target_lines=["机北1"]),
        car("T", line="机南", position=2, target_lines=["机南"]),
        car("X", line="机南", position=3, target_lines=["机北1"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="机南",
        target_line="机南",
        batch=cars,
        steps=(
            physical.plan_step("Get", "机南", ("N", "T", "X")),
            physical.plan_step("Put", "机北1", ("X",), {"X": 1}),
            physical.plan_step("Put", "机南", ("N", "T"), {"N": 1, "T": 2}),
        ),
        reason="test",
        candidate_kind="vnext_depot_inbound_assembly_session",
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:T",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("T",),
        source_lines=("机南",),
        target_lines=("机南",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelope = CandidateEnvelope(
        candidate=candidate,
        contract=contract,
        intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        resource_request=ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line="机南",
            target_line="机南",
            move_nos=tuple(candidate.move_car_nos),
            intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
        ),
        template_name="test_inner_target_segment",
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        candidate,
        cars,
        physical.LocoLocation("机南"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
    )
    assert "inner_target_segment_extended" not in delta.reduced


def test_depot_inbound_mixed_extraction_returns_prefix_to_source() -> None:
    cars = [
        car("X", line="存1线", position=1, target_lines=["存2线"]),
        car("A", line="存1线", position=2, target_lines=["修1库内"]),
        car("B", line="存1线", position=3, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("存1线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundMixedExtractionSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 2
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "存1线", ("X", "A", "B")),
        ("Put", "机南", ("A", "B")),
        ("Put", "存2线", ("X",)),
    ]
    assert envelope.resource_request.same_plan_source_return_nos == ()
    assert any(item.resource_request.same_plan_source_return_nos == ("X",) for item in envelopes)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    assert {physical.car_no(car): car["Line"] for car in prospective} == {
        "X": "存2线",
        "A": "机南",
        "B": "机南",
    }


def test_depot_inbound_mixed_extraction_allows_valid_repeated_destinations() -> None:
    cars = [
        car("X", line="存1线", position=1, target_lines=["存2线"]),
        car("Y", line="存1线", position=2, target_lines=["存3线"]),
        car("Z", line="存1线", position=3, target_lines=["存2线"]),
        car("A", line="存1线", position=4, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A",),
        source_lines=("存1线",),
        target_lines=("修1库内",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundMixedExtractionSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 2
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "存1线", ("X", "Y", "Z", "A")),
        ("Put", "机南", ("A",)),
        ("Put", "存2线", ("Z",)),
        ("Put", "存3线", ("Y",)),
        ("Put", "存2线", ("X",)),
    ]
    assert envelope.resource_request.same_plan_source_return_nos == ()
    assert any(item.resource_request.same_plan_source_return_nos == ("X", "Y", "Z") for item in envelopes)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    assert {physical.car_no(car): car["Line"] for car in prospective} == {
        "X": "存2线",
        "Y": "存3线",
        "Z": "存2线",
        "A": "机南",
    }


def test_depot_inbound_mixed_extraction_keeps_valid_larger_repeated_window() -> None:
    cars = [
        car("X", line="存1线", position=1, target_lines=["存2线"]),
        car("A", line="存1线", position=2, target_lines=["修1库内"]),
        car("B", line="存1线", position=3, target_lines=["修2库内"]),
        car("Y", line="存1线", position=4, target_lines=["存2线"]),
        car("C", line="存1线", position=5, target_lines=["修3库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=3,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B,C",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B", "C"),
        source_lines=("存1线",),
        target_lines=("修1库内", "修2库内", "修3库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundMixedExtractionSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存1线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 4
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "存1线", ("X", "A", "B", "Y", "C")),
        ("Put", "机南", ("C",)),
        ("Put", "存2线", ("Y",)),
        ("Put", "机南", ("A", "B")),
        ("Put", "存2线", ("X",)),
    ]
    assert envelope.resource_request.same_plan_source_return_nos == ()
    assert any(item.resource_request.same_plan_source_return_nos == ("Y", "X") for item in envelopes)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("存1线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    assert {physical.car_no(car): car["Line"] for car in prospective} == {
        "X": "存2线",
        "A": "机南",
        "B": "机南",
        "Y": "存2线",
        "C": "机南",
    }


def test_depot_inbound_mixed_extraction_owner_can_stage_on_jinan() -> None:
    cars = [
        car("S", line="存4线", position=1, target_lines=["修3库内"], length=310.0),
        car("X", line="预修线", position=1, target_lines=["存2线"]),
        car("A", line="预修线", position=2, target_lines=["修1库内"]),
        car("B", line="预修线", position=3, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.temporary_line_by_no["A"] == "机南"
    assert strategic_plan.depot_inbound.temporary_line_by_no["B"] == "机南"
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("预修线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundMixedExtractionSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("预修线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 2
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "预修线", ("X", "A", "B")),
        ("Put", "机南", ("A", "B")),
        ("Put", "存2线", ("X",)),
    ]
    assert envelope.resource_request.same_plan_source_return_nos == ()
    assert any(item.resource_request.same_plan_source_return_nos == ("X",) for item in envelopes)
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("预修线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_depot_inbound_dirty_cleanout_counts_as_support_gain() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["油漆线"]),
        car("A", line="预修线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.purity_violation_nos == ("X",)
    contract = FlowContract(
        contract_id="REMOTE_SESSION:X,A",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("X", "A"),
        source_lines=("机走棚", "预修线"),
        target_lines=("存4线", "修1库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyCleanoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    envelope = envelopes[0]
    assert envelope.candidate.source_line == "机走棚"
    assert envelope.candidate.target_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机走棚"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.support_gain == 1


def test_depot_inbound_dirty_cleanout_is_global_support_action() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["油漆线"]),
        car("A", line="预修线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.depot_inbound.purity_violation_nos == ("X",)
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A",),
        source_lines=("预修线",),
        target_lines=("修1库内",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyCleanoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    assert set(envelopes[0].candidate.move_car_nos) == {"X"}


def test_depot_inbound_dirty_cleanout_can_hold_non_depot_origin_cun4_pollution() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["存4线"]),
        car("A", line="预修线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.depot_inbound.purity_violation_nos == ("X",)
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A",),
        source_lines=("预修线",),
        target_lines=("修1库内",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyCleanoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    envelope = envelopes[0]
    assert envelope.candidate.source_line == "机走棚"
    assert envelope.candidate.target_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES
    assert envelope.candidate.target_line != "存4线"
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机走棚"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.support_gain == 1


def test_depot_inbound_dirty_cleanout_does_not_hold_depot_origin_cun4_outbound() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["存4线"]),
        car("A", line="预修线", position=1, target_lines=["修1库内"]),
    ]
    cars[0]["_InitialLine"] = "修1库内"
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = SimpleNamespace(
        depot_inbound=SimpleNamespace(
            purity_violation_nos=("X",),
            purity_violation_lines=("机走棚",),
        )
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A",),
        source_lines=("预修线",),
        target_lines=("修1库内",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyCleanoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_inbound_dirty_exchange_moves_blocker_after_target_line_inbound() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["油漆线"]),
        car("Y", line="油漆线", position=1, target_lines=["机库线"]),
        car("Z", line="油漆线", position=2, target_lines=["抛丸线"]),
        car("A", line="油漆线", position=3, target_lines=["修1库内"]),
        car("B", line="油漆线", position=4, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("油漆线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyExchangeSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "机走棚", ("X",)),
        ("Get", "油漆线", ("Y", "Z", "A", "B")),
        ("Put", "机南", ("A", "B")),
        ("Put", "抛丸线", ("Z",)),
        ("Put", "机库线", ("Y",)),
        ("Put", "油漆线", ("X",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机走棚"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    assert {physical.car_no(car): car["Line"] for car in prospective} == {
        "X": "油漆线",
        "Y": "机库线",
        "Z": "抛丸线",
        "A": "机南",
        "B": "机南",
    }


def test_policy_prioritizes_dirty_exchange_that_also_places_inbound_cars() -> None:
    cars = [
        car("X", line="机走棚", position=1, target_lines=["油漆线"]),
        car("A", line="油漆线", position=1, target_lines=["修1库内"]),
        car("B", line="油漆线", position=2, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("油漆线",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelope = next(
        iter(
            DepotInboundDirtyExchangeSessionEpisode().generate(
                case_id="T",
                hook_index=1,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=physical.TrackGraph(),
                loco_location=physical.LocoLocation("机走棚"),
                serial_gate_leases={},
                contract=contract,
                strategic_plan=strategic_plan,
            )
        )
    )
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机走棚"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    candidate = SimpleNamespace(
        envelope=envelope,
        prospective_cars=prospective,
        resource_delta=SimpleNamespace(request=envelope.resource_request),
    )
    context = SimpleNamespace(strategic_plan=strategic_plan)

    assert BaselinePolicy().clears_depot_inbound_assembly_line(candidate, context)


def test_policy_detects_depot_inbound_put_that_closes_remaining_source_route() -> None:
    cars = [
        car("A", line="存3线", position=1, target_lines=["修1库内"]),
        car("B", line="洗罐线北", position=1, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    prospective = [
        {**cars[0], "Line": "机走棚", "Position": 1},
        cars[1],
    ]
    candidate = SimpleNamespace(
        envelope=SimpleNamespace(
            intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
            candidate=SimpleNamespace(move_car_nos=("A",)),
        ),
        prospective_cars=prospective,
        resource_delta=SimpleNamespace(
            request=SimpleNamespace(put_lines=("机走棚",)),
        ),
    )
    context = SimpleNamespace(strategic_plan=strategic_plan)

    assert BaselinePolicy().closes_depot_inbound_route_before_complete(candidate, context)


def test_depot_inbound_rebalance_can_move_grouped_blocker_for_downstream_contract() -> None:
    cars = [
        car("A", line="机走棚", position=1, target_lines=["修1库内"]),
        car("B", line="洗罐线北", position=1, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert "A" in strategic_plan.depot_inbound.grouped_nos
    assert "B" in strategic_plan.depot_inbound.ungrouped_nos
    contract = FlowContract(
        contract_id="REMOTE_SESSION:B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("B",),
        source_lines=("洗罐线北",),
        target_lines=("修2库内",),
        priority=1,
        obligations=("remote_session_debt",),
    )

    envelopes = list(
        DepotInboundAssemblyRebalanceEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert envelopes
    assert envelopes[0].candidate.source_line == "机走棚"
    assert envelopes[0].candidate.move_car_nos == ("A",)


def test_depot_inbound_gate_rejects_direct_depot_put_before_assembly() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="修1库内",
        batch=cars,
        cars=cars,
        depot_assignment=depot_assignment,
        reason="test",
        candidate_kind="target_move",
        planned_positions={"A": 1},
    )
    assert candidate is not None
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.REPAIR_INBOUND,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="存1线",
        target_line="修1库内",
        move_nos=("A",),
        touched_lines=("存1线", "修1库内"),
        put_lines=("修1库内",),
        intent=IntentKind.REMOTE_DEPOT,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.REPAIR_INBOUND,
            before_unsatisfied=1,
            after_unsatisfied=0,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=()),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert not decision.accepted
    assert "depot_inbound_release_without_assembly:A:存1线->修1库内" in decision.reason


def test_depot_inbound_gate_allows_temporary_non_depot_car_on_assembly_line() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
        car("X", line="存2线", position=1, target_lines=["油漆线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="存2线",
        target_line="机南",
        batch=[cars[1]],
        cars=cars,
        depot_assignment=depot_assignment,
        reason="temporary_cleanout",
        candidate_kind="temporary_cleanout",
        planned_positions={"X": 1},
    )
    assert candidate is not None
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="存2线",
        target_line="机南",
        move_nos=("X",),
        touched_lines=("存2线", "机南"),
        put_lines=("机南",),
        intent=IntentKind.FRONT_PREP,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.FUNCTION_LINE_SERVICE,
            before_unsatisfied=2,
            after_unsatisfied=1,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=("存2线",)),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert decision.accepted, decision.reason


def test_depot_inbound_gate_allows_same_plan_temporary_cun4_staging() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.depot_inbound.temporary_line_by_no == {"A": "机南"}
    candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="存1线",
        target_line="机南",
        batch=[cars[0]],
        steps=(
            physical.plan_step("Get", "存1线", ("A",)),
            physical.plan_step("Put", "存4线", ("A",), {"A": 1}),
            physical.plan_step("Get", "存4线", ("A",)),
            physical.plan_step("Put", "机南", ("A",), {"A": 1}),
        ),
        reason="same_plan_temporary_cun4_staging",
        candidate_kind="vnext_depot_inbound_assembly_session",
    )
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.REPAIR_INBOUND,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="存1线",
        target_line="机南",
        move_nos=("A",),
        touched_lines=("存1线", "存4线", "机南"),
        put_lines=("存4线", "机南"),
        intent=IntentKind.DEPOT_INBOUND_ASSEMBLY,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.REPAIR_INBOUND,
            before_unsatisfied=1,
            after_unsatisfied=1,
            before_contract_debt=1,
            after_contract_debt=1,
            support_gain=1,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=("存1线",)),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert decision.accepted, decision.reason


@pytest.mark.parametrize(
    "candidate_kind",
    ["vnext_depot_outbound_session", "vnext_remote_session_digest", "vnext_front_direct"],
)
def test_depot_inbound_gate_rejects_remote_window_pollution_before_release(candidate_kind: str) -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
        car("X", line="修1库内", position=1, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="修1库内",
        target_line="存4线",
        batch=[cars[1]],
        cars=cars,
        depot_assignment=depot_assignment,
        reason="test",
        candidate_kind=candidate_kind,
        planned_positions={"X": 1},
    )
    assert candidate is not None
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.DEPOT_OUTBOUND,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="修1库内",
        target_line="存4线",
        move_nos=("X",),
        touched_lines=("修1库内", "存4线"),
        put_lines=("存4线",),
        intent=IntentKind.CUN4_OUTBOUND_HOLD,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.DEPOT_OUTBOUND,
            before_unsatisfied=2,
            after_unsatisfied=1,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=("修1库内",)),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert not decision.accepted
    assert "depot_inbound_assembly_window_conflict:X:存4线" in decision.reason


def test_depot_inbound_gate_allows_post_acceptance_assembly_line_reuse() -> None:
    cars = [
        car("A", line="存4线", position=1, target_lines=["卸轮线"]),
        car("X", line="存2线", position=1, target_lines=["油漆线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="存2线",
        target_line="机南",
        batch=[cars[1]],
        cars=cars,
        depot_assignment=depot_assignment,
        reason="late_pollution",
        candidate_kind="vnext_front_direct",
        planned_positions={"X": 1},
    )
    assert candidate is not None
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.FUNCTION_LINE_SERVICE,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="存2线",
        target_line="机南",
        move_nos=("X",),
        touched_lines=("存2线", "机南"),
        put_lines=("机南",),
        intent=IntentKind.FRONT_PREP,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.FUNCTION_LINE_SERVICE,
            before_unsatisfied=1,
            after_unsatisfied=0,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=("存2线",)),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert decision.accepted


def test_depot_inbound_gate_allows_cun4_release_group_as_inbound_owner() -> None:
    cars = [
        car("A", line="调梁棚", position=1, target_lines=["卸轮线"]),
        car("B", line="调梁棚", position=2, target_lines=["卸轮线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.temporary_line_by_no == {"A": "存4线", "B": "存4线"}
    candidate = physical.build_direct_candidate(
        case_id="T",
        hook_index=1,
        source_line="调梁棚",
        target_line="存4线",
        batch=cars,
        cars=cars,
        depot_assignment=depot_assignment,
        reason="cun4_release_group",
        candidate_kind="vnext_cun4_release_group_assembly",
        planned_positions={"A": 1, "B": 2},
    )
    assert candidate is not None
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.REPAIR_INBOUND,
        candidate_id=candidate.candidate_id,
        resources=(),
        source_line="调梁棚",
        target_line="存4线",
        move_nos=("A", "B"),
        touched_lines=("调梁棚", "存4线"),
        put_lines=("存4线",),
        intent=IntentKind.CUN4_RELEASE_GROUP,
    )
    decision = AcceptRejectGate().decide(
        ContractDelta(
            contract_id="C",
            family=ContractFamily.REPAIR_INBOUND,
            before_unsatisfied=2,
            after_unsatisfied=2,
            before_contract_debt=2,
            after_contract_debt=2,
            support_gain=2,
        ),
        ResourceDelta(request=request, acquired=(), released_lines=("调梁棚",)),
        strategic_plan=strategic_plan,
        candidate=candidate,
    )
    assert decision.accepted, decision.reason


def test_depot_inbound_assembly_plan_treats_unwheel_as_inbound_destination() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["卸轮线"]),
        car("B", line="机南", position=1, target_lines=["卸轮线"]),
    ]
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos=set(),
    )
    assert set(plan.inbound_nos) == {"A", "B"}
    assert plan.grouped_nos == ()
    assert plan.temporary_line_by_no == {"A": "存4线", "B": "存4线"}
    assert plan.purity_violation_nos == ()


def test_depot_inbound_assembly_plan_keeps_cun4_for_unwheel_only() -> None:
    cars = [
        car("A", line="存1线", position=1, target_lines=["修1库内"]),
        car("B", line="存1线", position=2, target_lines=["卸轮线"]),
    ]
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos=set(),
    )
    assert plan.temporary_line_by_no["A"] != "存4线"
    assert plan.temporary_line_by_no["B"] == "存4线"


def test_depot_inbound_assembly_plan_uses_inner_to_outer_topology_when_outbound_exists() -> None:
    cars = [
        car("A", line="调梁棚", position=1, target_lines=["修4库内"], length=10.0),
        car("B", line="调梁棚", position=2, target_lines=["修3库内"], length=10.0),
        car("C", line="预修线", position=1, target_lines=["修1库内"], length=10.0),
    ]
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos={"O"},
    )
    assert plan.temporary_line_by_no == {
        "A": "机南",
        "B": "机南",
        "C": "机南",
    }


def test_depot_inbound_stepwise_put_splits_pending_weigh_tail() -> None:
    cars = [
        car("W1", line="油漆线", position=1, target_lines=["修4库外"], weigh=True),
        car("W2", line="油漆线", position=2, target_lines=["修4库外"], weigh=True),
    ]
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos=set(),
        depot_outbound_nos=set(),
    )
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:W1,W2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("W1", "W2"),
        source_lines=("油漆线",),
        target_lines=("修4库外",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblySessionEpisode().generate(
            case_id="CASE",
            hook_index=1,
            cars=cars,
            depot_assignment=physical.DepotAssignment(slots={}, failures={}),
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation(line="机库线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert plan.temporary_line_by_no == {"W1": "机南", "W2": "机南"}
    assert envelopes
    steps = physical.candidate_plan_steps(envelopes[0].candidate)
    assert [step.action for step in steps] == ["Get", "Weigh", "Put", "Weigh", "Put"]
    assert [step.move_car_nos for step in steps if step.action == "Put"] == [("W2",), ("W1",)]


def test_depot_inbound_rebalance_does_not_move_stable_machine_north_group_inward() -> None:
    cars = [
        car("A", line="机走北", position=1, target_lines=["修1库内"]),
        car("B", line="调梁棚", position=1, target_lines=["修3库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert "A" in strategic_plan.depot_inbound.grouped_nos
    assert "B" in strategic_plan.depot_inbound.ungrouped_nos
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("机走北", "调梁棚"),
        target_lines=("修1库内", "修3库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )

    envelopes = list(
        DepotInboundAssemblyRebalanceEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert envelopes == []


def test_strategic_plan_opens_cun4_after_depot_inbound_acceptance() -> None:
    cars = [
        car("A", line="存4线", position=1, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    before_acceptance = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
        depot_inbound_assembly_accepted=False,
    )
    assert not before_acceptance.depot_inbound.assembly_complete
    assert before_acceptance.depot_inbound.temporary_line_by_no["A"] != "存4线"
    assert not before_acceptance.depot_inbound_assembly_accepted

    after_acceptance = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
        depot_inbound_assembly_accepted=True,
    )
    assert after_acceptance.depot_inbound.assembly_complete
    assert after_acceptance.depot_inbound.temporary_line_by_no == {"A": "存4线"}
    assert after_acceptance.depot_inbound_assembly_accepted


def test_depot_inbound_assembly_plan_opens_lines_after_inbound_is_released() -> None:
    cars = [
        car("O", line="存4线", position=1, target_lines=["油漆线"]),
    ]
    cars[0]["_InitialLine"] = "修1库内"
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos={"O"},
        depot_outbound_nos=set(),
    )
    assert plan.assembly_complete
    assert plan.purity_violation_nos == ()
    assert plan.reason == "no_depot_inbound_assembly_debt"


def test_depot_inbound_assembly_plan_exempts_only_cun4_outbound_hold() -> None:
    cars = [
        car("I", line="机南", position=1, target_lines=["修1库内"]),
        car("O", line="存4线", position=1, target_lines=["存4线"]),
        car("X", line="存4线", position=2, target_lines=["油漆线"]),
    ]
    cars[1]["_InitialLine"] = "修1库内"
    cars[2]["_InitialLine"] = "修2库内"
    plan = build_depot_inbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        cun4_outbound_hold_nos={"O", "X"},
        depot_outbound_nos=set(),
    )
    assert plan.grouped_nos == ("I",)
    assert plan.purity_exempt_nos == ("O",)
    assert plan.purity_violation_nos == ("X",)
    assert plan.purity_violation_lines == ("存4线",)
    assert plan.status == "fail"


def test_strategic_plan_keeps_cun4_outbound_hold_owner_visible() -> None:
    cars = [
        car("I", line="机南", position=1, target_lines=["修1库内"]),
        car("O", line="存4线", position=1, target_lines=["存4线"]),
    ]
    cars[1]["_InitialLine"] = "修1库内"
    plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert plan.cun4_release.mode == "OUTBOUND_HOLD"
    assert plan.cun4_release.owner == "outbound_assembly"
    assert plan.cun4_release.outbound_hold_nos == ("O",)
    assert plan.depot_inbound.purity_exempt_nos == ("O",)
    assert plan.depot_inbound.purity_violation_nos == ()
    assert plan.depot_inbound.assembly_complete


def test_unlocked_depot_slot_uses_alternate_valid_position_when_preferred_is_occupied() -> None:
    cars = [
        car("I", line="存4线", position=1, target_lines=["修1库内"]),
        car("O", line="修1库内", position=1, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(
        slots={"I": physical.DepotSlot("修1库内", 1, locked=False)},
        failures={},
    )
    positions = physical.planned_positions_for_batch(
        batch=[cars[0]],
        target_line="修1库内",
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos={"I"},
    )
    assert positions == {"I": 2}


def test_depot_inbound_assembly_release_runs_after_grouping_complete() -> None:
    cars = [
        car("A", line="机南", position=1, target_lines=["修1库内"]),
        car("B", line="机南", position=2, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("机南",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机南"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.candidate.source_line == "机南"
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "机南", ("A", "B")),
        ("Put", "修2库内", ("B",)),
        ("Put", "修1库内", ("A",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机南"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.contract_reduction == 2


def test_depot_inbound_assembly_release_attaches_to_repair_inbound_contract() -> None:
    cars = [
        car("A", line="机南", position=1, target_lines=["修1库内"]),
        car("B", line="机南", position=2, target_lines=["修1库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    contract = FlowContract(
        contract_id="REPAIR_INBOUND:机南->修1库内:A,B",
        family=ContractFamily.REPAIR_INBOUND,
        subject_nos=("A", "B"),
        source_lines=("机南",),
        target_lines=("修1库内",),
        priority=80,
        obligations=("move_to_target", "remote_depot_debt"),
        protections=("preserve_remote_session",),
    )

    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机南"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.contract.family == ContractFamily.REPAIR_INBOUND
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "机南", ("A", "B")),
        ("Put", "修1库内", ("A", "B")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("机南"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    delta = build_contract_delta(
        envelope,
        cars=cars,
        prospective_cars=prospective,
        depot_assignment=depot_assignment,
        strategic_plan=strategic_plan,
    )
    assert delta.contract_reduction == 2


def test_depot_inbound_assembly_release_rejects_reverse_slot_reorder_without_preassembly() -> None:
    cars = [
        car("A", line="洗油北", position=1, target_lines=["修2库内"]),
        car("B", line="洗油北", position=2, target_lines=["修2库内"]),
        car("C", line="洗油北", position=3, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(
        slots={
            "A": physical.DepotSlot("修2库内", 3, locked=False),
            "B": physical.DepotSlot("修2库内", 2, locked=False),
            "C": physical.DepotSlot("修2库内", 1, locked=False),
        },
        failures={},
    )
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=3,
    )
    contract = FlowContract(
        contract_id="REPAIR_INBOUND:洗油北->修2库内:A,B,C",
        family=ContractFamily.REPAIR_INBOUND,
        subject_nos=("A", "B", "C"),
        source_lines=("洗油北",),
        target_lines=("修2库内",),
        priority=80,
        obligations=("move_to_target", "remote_depot_debt"),
        protections=("preserve_remote_session",),
    )

    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("洗油北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert envelopes == []


def test_depot_inbound_assembly_release_allows_locked_section_stayer_behind_factory_slot() -> None:
    cars = [
        car("F", line="机南", position=1, target_lines=["修4库内"]),
        car("L", line="修4库内", position=5, target_lines=["修4库内"]),
    ]
    cars[0]["RepairProcess"] = "厂修"
    depot_assignment = physical.DepotAssignment(
        slots={
            "F": physical.DepotSlot("修4库内", 4, locked=False),
            "L": physical.DepotSlot("修4库内", 5, locked=True),
        },
        failures={},
    )
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    contract = FlowContract(
        contract_id="REPAIR_INBOUND:机南->修4库内:F",
        family=ContractFamily.REPAIR_INBOUND,
        subject_nos=("F",),
        source_lines=("机南",),
        target_lines=("修4库内",),
        priority=80,
        obligations=("move_to_target", "remote_depot_debt"),
        protections=("preserve_remote_session",),
    )

    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机南"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos, step.planned_positions) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "机南", ("F",), {}),
        ("Put", "修4库内", ("F",), {"F": 4}),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("机南"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_depot_inbound_assembly_release_clears_target_line_blockers_in_same_hook() -> None:
    cars = [
        car("O1", line="修1库内", position=1, target_lines=["修2库外"]),
        car("O2", line="修1库内", position=2, target_lines=["修2库外"]),
        car("I1", line="机走棚", position=1, target_lines=["修1库内"]),
        car("I2", line="机走棚", position=2, target_lines=["修1库内"]),
        car("I3", line="机走棚", position=3, target_lines=["修1库内"]),
        car("I4", line="机走棚", position=4, target_lines=["修1库内"]),
        car("J", line="机南", position=1, target_lines=["修2库内"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={}, capacities={"修1库内": 5})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=5,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:O1,O2,I1,I2,J",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("O1", "O2", "I1", "I2", "I3", "I4", "J"),
        source_lines=("修1库内", "机走棚", "机南"),
        target_lines=("修2库外", "修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )

    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "机走棚", ("I1", "I2", "I3", "I4")),
        ("Get", "机南", ("J",)),
        ("Put", "修2库内", ("J",)),
        ("Get", "修1库内", ("O1", "O2")),
        ("Put", "修2库外", ("O1", "O2")),
        ("Put", "修1库内", ("I1", "I2", "I3", "I4")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存4线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_depot_inbound_assembly_release_clearance_uses_current_carry_not_total_moved_batch() -> None:
    repair2_nos = tuple(f"J{index:02d}" for index in range(1, 20))
    cars = [
        car("O", line="修4库内", position=1, target_lines=["修1库外"]),
        car("I", line="机走棚", position=1, target_lines=["修4库内"]),
        *[
            car(no, line="机南", position=index, target_lines=["修2库内"])
            for index, no in enumerate(repair2_nos, start=1)
        ],
    ]
    depot_assignment = physical.DepotAssignment(
        slots={},
        failures={},
        capacities={"修4库内": 1, "修2库内": 30},
    )
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=21,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    by_no = {physical.car_no(item): item for item in cars}
    plan = _depot_inbound_multisource_stepwise_put_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        source_batches=(
            ("机走棚", (by_no["I"],)),
            ("机南", tuple(by_no[no] for no in repair2_nos)),
        ),
        target_by_no={
            "I": "修4库内",
            **{no: "修2库内" for no in repair2_nos},
        },
    )

    assert plan is not None
    steps, _put_lines, _batch = plan
    assert [(step.action, step.line, step.move_car_nos) for step in steps] == [
        ("Get", "机走棚", ("I",)),
        ("Get", "机南", repair2_nos),
        ("Put", "修2库内", repair2_nos),
        ("Get", "修4库内", ("O",)),
        ("Put", "修1库外", ("O",)),
        ("Put", "修4库内", ("I",)),
    ]


def test_depot_inbound_target_clearance_splits_blockers_across_outside_lines_by_capacity() -> None:
    blocker_nos = tuple(f"O{index}" for index in range(1, 6))
    cars = [
        car(no, line="修4库内", position=index, target_lines=["修1库外"])
        for index, no in enumerate(blocker_nos, start=1)
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})

    clearance = _depot_inbound_target_clearance_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        target_line="修4库内",
        carried_cars=[],
    )

    assert clearance is not None
    steps, put_lines, clearance_cars, planning_cars = clearance
    assert put_lines == ("修1库外", "修2库外")
    assert [physical.car_no(item) for item in clearance_cars] == list(blocker_nos)
    assert [(step.action, step.line, step.move_car_nos) for step in steps] == [
        ("Get", "修4库内", blocker_nos),
        ("Put", "修1库外", ("O3", "O4", "O5")),
        ("Put", "修2库外", ("O1", "O2")),
    ]
    assert {
        physical.car_no(item): item["Line"]
        for item in planning_cars
        if physical.car_no(item) in blocker_nos
    } == {
        "O1": "修2库外",
        "O2": "修2库外",
        "O3": "修1库外",
        "O4": "修1库外",
        "O5": "修1库外",
    }


def test_depot_inbound_target_clearance_uses_cun4_when_outside_lines_block_pending_inner_targets() -> None:
    blocker_nos = tuple(f"O{index}" for index in range(1, 6))
    cars = [
        car(no, line="修4库内", position=index, target_lines=["修1库外"])
        for index, no in enumerate(blocker_nos, start=1)
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})

    clearance = _depot_inbound_target_clearance_plan(
        cars=cars,
        depot_assignment=depot_assignment,
        target_line="修4库内",
        carried_cars=[],
        blocked_staging_lines={"修2库外", "修3库外", "修4库外"},
    )

    assert clearance is not None
    steps, put_lines, _clearance_cars, planning_cars = clearance
    assert put_lines == ("修1库外", "存4线")
    assert [(step.action, step.line, step.move_car_nos) for step in steps] == [
        ("Get", "修4库内", blocker_nos),
        ("Put", "修1库外", ("O3", "O4", "O5")),
        ("Put", "存4线", ("O1", "O2")),
    ]
    assert {
        physical.car_no(item): item["Line"]
        for item in planning_cars
        if physical.car_no(item) in blocker_nos
    } == {
        "O1": "存4线",
        "O2": "存4线",
        "O3": "修1库外",
        "O4": "修1库外",
        "O5": "修1库外",
    }


def test_remote_direct_closeout_moves_only_capacity_legal_remote_tail() -> None:
    cars = [
        car("U1", line="卸轮线", position=1, target_lines=["存4线"]),
        car("U2", line="卸轮线", position=2, target_lines=["存4线"]),
        car("S1", line="存4线", position=1, target_lines=["修1库外"]),
        car("S2", line="存4线", position=2, target_lines=["修1库外"]),
        car("O1", line="修1库外", position=1, target_lines=["修1库外"]),
        car("O2", line="修1库外", position=2, target_lines=["修1库外"]),
        car("O3", line="修1库外", position=3, target_lines=["修1库外"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=4,
        depot_inbound_assembly_accepted=True,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:U1,U2,S1,S2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("U1", "U2", "S1", "S2"),
        source_lines=("卸轮线", "存4线"),
        target_lines=("存4线", "修1库外"),
        priority=1,
        obligations=("remote_session_debt",),
    )

    envelopes = list(
        RemoteDirectCloseoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("卸轮线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert len(envelopes) == 1
    assert envelopes[0].candidate.source_line == "卸轮线"
    assert envelopes[0].candidate.target_line == "存4线"
    assert envelopes[0].candidate.move_car_nos == ("U1", "U2")
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("卸轮线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_flow_facts_do_not_block_h4_on_capacity_impossible_remote_debt() -> None:
    cars = [
        car("S", line="存4线", position=1, target_lines=["修1库外"], length=14.3),
        car("O1", line="修1库外", position=1, target_lines=["修1库外"], length=17.6),
        car("O2", line="修1库外", position=2, target_lines=["修1库外"], length=17.6),
        car("O3", line="修1库外", position=3, target_lines=["修1库外"], length=14.3),
    ]
    facts = classify_flow_facts(cars, physical.DepotAssignment(slots={}, failures={}))

    assert facts.remote_debt == 0
    assert facts.cun4_port_debt == 1


def test_flow_facts_keep_capacity_legal_remote_debt_blocking() -> None:
    cars = [
        car("S", line="存4线", position=1, target_lines=["修1库外"], length=14.3),
        car("O1", line="修1库外", position=1, target_lines=["修1库外"], length=14.3),
    ]
    facts = classify_flow_facts(cars, physical.DepotAssignment(slots={}, failures={}))

    assert facts.remote_debt == 1


def test_policy_prioritizes_depot_inbound_release_over_cun4_support_after_assembly_complete() -> None:
    cars = [
        car("A", line="机南", position=1, target_lines=["修1库内"]),
        car("U", line="存4线", position=1, target_lines=["卸轮线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    phase_state = PhaseState(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        front_debt=0,
        cun4_port_debt=0,
        remote_debt=2,
        closeout_debt=0,
        reason="depot_inbound_release_after_assembly_complete",
        depot_inbound_assembly_complete=True,
        depot_outbound_assembly_complete=True,
    )
    context = PolicyContext(
        phase_state=phase_state,
        remote_session=RemoteSessionState(),
        remote_open=True,
        last_business_remote=None,
        strategic_plan=strategic_plan,
    )
    release_contract = FlowContract(
        contract_id="REPAIR_INBOUND:机南->修1库内:A",
        family=ContractFamily.REPAIR_INBOUND,
        subject_nos=("A",),
        source_lines=("机南",),
        target_lines=("修1库内",),
        priority=80,
        obligations=("move_to_target", "remote_depot_debt"),
    )
    support_contract = FlowContract(
        contract_id="REPAIR_INBOUND:存4线->卸轮线:U",
        family=ContractFamily.REPAIR_INBOUND,
        subject_nos=("U",),
        source_lines=("存4线",),
        target_lines=("卸轮线",),
        priority=80,
        obligations=("move_to_target", "remote_depot_debt"),
    )
    release_candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="机南",
        target_line="修1库内",
        batch=[cars[0]],
        steps=(
            physical.plan_step("Get", "机南", ("A",)),
            physical.plan_step("Put", "修1库内", ("A",), {"A": 1}),
        ),
        reason="release_mainline",
        candidate_kind="vnext_depot_inbound_assembly_release",
    )
    support_candidate = physical.build_planlet_candidate(
        case_id="T",
        hook_index=1,
        source_line="存4线",
        target_line="卸轮线",
        batch=[cars[1]],
        steps=(
            physical.plan_step("Get", "存4线", ("U",)),
            physical.plan_step("Put", "卸轮线", ("U",), {"U": 1}),
        ),
        reason="cun4_support",
        candidate_kind="vnext_source_prefix_release",
    )
    release_request = ResourceRequest(
        contract_id=release_contract.contract_id,
        family=release_contract.family,
        candidate_id=release_candidate.candidate_id,
        resources=(),
        source_line="机南",
        target_line="修1库内",
        move_nos=("A",),
        touched_lines=("机南", "修1库内"),
        put_lines=("修1库内",),
        intent=IntentKind.REMOTE_DEPOT,
    )
    support_request = ResourceRequest(
        contract_id=support_contract.contract_id,
        family=support_contract.family,
        candidate_id=support_candidate.candidate_id,
        resources=(),
        source_line="存4线",
        target_line="卸轮线",
        move_nos=("U",),
        touched_lines=("存4线", "卸轮线"),
        put_lines=("卸轮线",),
        intent=IntentKind.CUN4_RELEASE_ACCEPT,
    )
    release = SimpleNamespace(
        envelope=CandidateEnvelope(
            candidate=release_candidate,
            contract=release_contract,
            intent=IntentKind.REMOTE_DEPOT,
            resource_request=release_request,
            template_name="depot_inbound_assembly_release",
        ),
        prospective_cars=cars,
        contract_delta=ContractDelta(
            contract_id=release_contract.contract_id,
            family=release_contract.family,
            before_unsatisfied=2,
            after_unsatisfied=1,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        resource_delta=ResourceDelta(request=release_request, acquired=(), released_lines=()),
        next_loco_location=physical.LocoLocation("修1库内"),
    )
    support = SimpleNamespace(
        envelope=CandidateEnvelope(
            candidate=support_candidate,
            contract=support_contract,
            intent=IntentKind.CUN4_RELEASE_ACCEPT,
            resource_request=support_request,
            template_name="source_prefix_release",
        ),
        prospective_cars=cars,
        contract_delta=ContractDelta(
            contract_id=support_contract.contract_id,
            family=support_contract.family,
            before_unsatisfied=2,
            after_unsatisfied=0,
            before_contract_debt=2,
            after_contract_debt=0,
        ),
        resource_delta=ResourceDelta(request=support_request, acquired=(), released_lines=()),
        next_loco_location=physical.LocoLocation("卸轮线"),
    )

    policy = BaselinePolicy()
    assert policy.better(release, support, context)


def test_cun4_unwheel_release_clears_unwheel_outbound_before_put() -> None:
    cars = [
        car("O", line="卸轮线", position=1, target_lines=["存2线"], length=10.0),
        car("U1", line="存4线", position=1, target_lines=["卸轮线"], length=10.0),
        car("U2", line="存4线", position=2, target_lines=["卸轮线"], length=10.0),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=3,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    contract = FlowContract(
        contract_id="REMOTE_SESSION:O,U1,U2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("O", "U1", "U2"),
        source_lines=("卸轮线", "存4线"),
        target_lines=("存2线", "卸轮线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        Cun4UnwheelReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    steps = [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps]
    assert steps[:2] == [
        ("Get", "卸轮线", ("O",)),
        ("Put", "修4库外", ("O",)),
    ]
    assert steps[-2:] == [
        ("Get", "存4线", ("U1", "U2")),
        ("Put", "卸轮线", ("U1", "U2")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存4线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelopes[0].candidate, cars, validation)
    assert {physical.car_no(item): item["Line"] for item in prospective} == {
        "O": "修4库外",
        "U1": "卸轮线",
        "U2": "卸轮线",
    }


def test_cun4_unwheel_release_pulls_unwheel_outbound_behind_satisfied_prefix() -> None:
    cars = [
        car("B", line="卸轮线", position=1, target_lines=["卸轮线"], length=10.0),
        car("O", line="卸轮线", position=2, target_lines=["存4线"], length=10.0),
        car("S", line="存4线", position=1, target_lines=["存4线"], length=10.0),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=1,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:O",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("O",),
        source_lines=("卸轮线",),
        target_lines=("存4线",),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        Cun4UnwheelReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("卸轮线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    assert envelopes[0].intent == IntentKind.CUN4_OUTBOUND_HOLD
    steps = [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps]
    assert steps == [
        ("Get", "卸轮线", ("B", "O")),
        ("Put", "存4线", ("O",)),
        ("Put", "卸轮线", ("B",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("卸轮线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelopes[0].candidate, cars, validation)
    assert {physical.car_no(item): item["Line"] for item in prospective} == {
        "B": "卸轮线",
        "O": "存4线",
        "S": "存4线",
    }


def test_depot_inbound_assembly_release_rejects_rolling_depot_rebuild() -> None:
    depot_targets = ["修1库内", "修2库内", "修3库内", "修4库内"]
    cars = [
        car("A", line="机南", position=1, target_lines=depot_targets),
        car("B", line="机南", position=2, target_lines=depot_targets),
        car("C", line="机南", position=3, target_lines=depot_targets),
        car("E1", line="修1库内", position=1, target_lines=depot_targets),
        car("E2", line="修1库内", position=2, target_lines=depot_targets),
        car("F", line="修1库内", position=5, target_lines=depot_targets),
    ]
    cars[-1]["RepairProcess"] = "厂修"
    depot_assignment = physical.DepotAssignment(
        slots={
            "A": physical.DepotSlot("修1库内", 1, locked=False),
            "B": physical.DepotSlot("修2库内", 1, locked=False),
            "C": physical.DepotSlot("修1库内", 2, locked=False),
            "F": physical.DepotSlot("修1库内", 5, locked=True),
        },
        failures={},
    )
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=3,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B,C",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B", "C"),
        source_lines=("机南",),
        target_lines=("修1库内", "修2库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机南"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_outbound_assembly_plan_orders_non_cun4_before_cun4() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["机走棚"], length=10.0),
        car("B", line="修1库内", position=2, target_lines=["存4线"], length=10.0),
    ]
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.pull_order_nos == ("A", "B")
    assert plan.non_cun4_nos == ("A",)
    assert plan.cun4_target_nos == ("B",)
    assert plan.cun4_nos == ("A", "B")
    assert plan.status == "pass"


def test_depot_inbound_release_defers_outer_slot_until_inner_slot_is_clear() -> None:
    cars = [
        car("I", line="机走北", position=1, target_lines=["修3库内"]),
        car("O", line="机南", position=1, target_lines=["修3库外"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:I,O",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("I", "O"),
        source_lines=("机走北", "机南"),
        target_lines=("修3库内", "修3库外"),
        priority=1,
        obligations=("remote_session_debt",),
    )

    envelopes = list(
        DepotInboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )

    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "机走北", ("I",)),
        ("Put", "修3库内", ("I",)),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelopes[0].candidate,
        cars,
        physical.LocoLocation("存4线"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons


def test_depot_outbound_assembly_plan_reports_cun4_capacity_short_without_overflow() -> None:
    cars = [
        car(f"H{index}", line="存4线", position=index, target_lines=["存4线"], length=60.0)
        for index in range(1, 6)
    ]
    cars.extend(
        [
            car("A", line="修1库内", position=1, target_lines=["机走棚"], length=14.3),
            car("B", line="修1库内", position=2, target_lines=["存4线"], length=14.3),
        ]
    )
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.pull_order_nos == ("A", "B")
    assert plan.cun4_nos == ("A", "B")
    assert plan.overflow_nos == ()
    assert plan.status == "fail"
    assert plan.reason == "cun4_capacity_insufficient_for_depot_outbound"


def test_depot_outbound_assembly_plan_reports_source_order_when_cun4_target_is_north() -> None:
    cars = [
        car("B", line="修1库内", position=1, target_lines=["存4线"], length=10.0),
        car("A", line="修1库内", position=2, target_lines=["油漆线"], length=10.0),
    ]
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.cun4_prefix_unsafe_nos == ("A",)
    assert plan.cun4_nos == ("B", "A")
    assert plan.overflow_nos == ()
    assert plan.status == "warn"
    assert plan.reason == "cun4_target_suffix_requires_source_repack"


def test_depot_outbound_assembly_plan_uses_cun4_even_when_cun4_target_cars_are_already_south() -> None:
    cars = [
        car("H", line="存4线", position=1, target_lines=["存4线"], length=10.0),
        car("A", line="修1库内", position=1, target_lines=["油漆线"], length=10.0),
    ]
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.cun4_reserved_by_outbound_hold_nos == ()
    assert plan.cun4_nos == ("A",)
    assert plan.overflow_nos == ()
    assert plan.temporary_line_by_no["A"] == "存4线"


def test_depot_outbound_assembly_plan_ignores_cars_not_initially_in_depot() -> None:
    inbound = car("I", line="修1库内", position=1, target_lines=["油漆线"], length=10.0)
    inbound["_InitialLine"] = "存4线"
    depot = car("D", line="修2库内", position=1, target_lines=["机走棚"], length=10.0)
    plan = build_depot_outbound_assembly_plan(
        cars=[inbound, depot],
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.outbound_nos == ("D",)
    assert plan.temporary_line_by_no == {"D": "存4线"}


def test_depot_outbound_assembly_plan_includes_outer_route_blocker() -> None:
    cars = [
        car("B", line="修1库外", position=1, target_lines=["存4线"], length=10.0),
        car("A", line="修1库内", position=1, target_lines=["油漆线"], length=10.0),
    ]
    cars[0]["_InitialLine"] = "存1线"
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.outbound_nos == ("A",)
    assert plan.route_blocker_nos == ("B",)
    assert plan.pull_order_nos == ("B", "A")
    assert plan.cun4_nos == ("B", "A")
    assert plan.temporary_line_by_no == {"B": "存4线", "A": "存4线"}


def test_depot_outbound_assembly_plan_includes_unwheel_outer_route_blocker() -> None:
    cars = [
        car("B", line="修1库外", position=1, target_lines=["卸轮线"], length=10.0),
        car("A", line="修1库内", position=1, target_lines=["存4线"], length=10.0),
    ]
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.outbound_nos == ("A",)
    assert plan.route_blocker_nos == ("B",)
    assert plan.pull_order_nos == ("B", "A")
    assert plan.temporary_line_by_no == {"B": "存4线", "A": "存4线"}


def test_depot_outbound_assembly_plan_includes_interleaved_unwheel_source_blocker() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["存4线"], length=10.0),
        car("B", line="修1库内", position=2, target_lines=["卸轮线"], length=10.0),
        car("C", line="修1库内", position=3, target_lines=["存4线"], length=10.0),
    ]
    plan = build_depot_outbound_assembly_plan(
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
    )
    assert plan.outbound_nos == ("A", "B", "C")
    assert plan.route_blocker_nos == ()
    assert plan.pull_order_nos == ("A", "B", "C")
    assert plan.temporary_line_by_no == {"A": "存4线", "B": "存4线", "C": "存4线"}


def test_depot_outbound_session_waits_for_cun4_inbound_release() -> None:
    cars = [
        car("A", line="存4线", position=1, target_lines=["卸轮线"]),
        car("O", line="修1库内", position=1, target_lines=["油漆线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    assert strategic_plan.cun4_release.release_nos == ("A",)
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,O",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "O"),
        source_lines=("存4线", "修1库内"),
        target_lines=("修1库内", "油漆线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotOutboundSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_outbound_session_pulls_initial_depot_outbound_to_cun4() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["油漆线"]),
        car("B", line="修2库内", position=1, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=2,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    assert strategic_plan.cun4_release.release_nos == ()
    assert strategic_plan.depot_outbound.temporary_line_by_no == {"A": "存4线", "B": "存4线"}
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("修1库内", "修2库内"),
        target_lines=("油漆线", "存4线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotOutboundSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert [(step.action, step.line, step.move_car_nos) for step in envelope.candidate.plan_steps] == [
        ("Get", "修1库内", ("A",)),
        ("Get", "修2库内", ("B",)),
        ("Put", "存4线", ("A", "B")),
    ]
    validation = physical.validate_candidate(
        physical.TrackGraph(),
        envelope.candidate,
        cars,
        physical.LocoLocation("修1库内"),
        depot_assignment,
    )
    assert validation.accepted, validation.reasons
    prospective = simulate_candidate(envelope.candidate, cars, validation)
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in prospective
    } == {
        "A": ("存4线", 1),
        "B": ("存4线", 2),
    }


def test_depot_outbound_session_uses_full_plan_for_depot_outbound_contract() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["存4线"]),
        car("B", line="修2库内", position=1, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=2,
    )
    contract = FlowContract(
        contract_id="DEPOT_OUTBOUND:修1库内->存4线:A",
        family=ContractFamily.DEPOT_OUTBOUND,
        subject_nos=("A",),
        source_lines=("修1库内",),
        target_lines=("存4线",),
        priority=1,
        obligations=("remote_depot_debt",),
    )
    envelopes = list(
        DepotOutboundSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert len(envelopes) == 1
    assert [(step.action, step.line, step.move_car_nos) for step in envelopes[0].candidate.plan_steps] == [
        ("Get", "修1库内", ("A",)),
        ("Get", "修2库内", ("B",)),
        ("Put", "存4线", ("A", "B")),
    ]


def test_depot_outbound_session_requires_complete_cun4_plan() -> None:
    cars = [
        car("A", line="修1库内", position=1, target_lines=["油漆线"]),
        car("B", line="修2库内", position=1, target_lines=["机走棚"]),
        car("C", line="修3库内", position=1, target_lines=["存2线"]),
        car("X", line="修4库内", position=1, target_lines=["存3线"]),
        car("D", line="修4库内", position=2, target_lines=["油漆线"]),
    ]
    cars[3]["_InitialLine"] = "存1线"
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=4,
    )
    assert strategic_plan.cun4_release.release_nos == ()
    assert strategic_plan.depot_outbound.pull_order_nos == ("A", "B", "C", "X", "D")
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B,C,D",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B", "C", "D"),
        source_lines=("修1库内", "修2库内", "修3库内", "修4库内"),
        target_lines=("油漆线", "机走棚", "存2线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotOutboundSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_cun4_exchange_does_not_take_unwheel_release_port() -> None:
    cars = [
        car("I1", line="存4线", position=1, target_lines=["卸轮线"], length=10.0),
        car("I2", line="存4线", position=2, target_lines=["卸轮线"], length=10.0),
        car("O1", line="修1库内", position=1, target_lines=["油漆线"], length=10.0),
        car("O2", line="修2库内", position=1, target_lines=["存4线"], length=10.0),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=4,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    assert strategic_plan.cun4_release.release_nos == ("I1", "I2")
    assert strategic_plan.depot_outbound.pull_order_nos == ("O1", "O2")
    contract = FlowContract(
        contract_id="REMOTE_SESSION:I1,I2,O1,O2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("I1", "I2", "O1", "O2"),
        source_lines=("存4线", "修1库内", "修2库内"),
        target_lines=("卸轮线", "油漆线", "存4线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotCun4InboundOutboundExchangeEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_cun4_source_repack_exchange_does_not_take_unwheel_release_port() -> None:
    cars = [
        car("I1", line="存4线", position=1, target_lines=["卸轮线"], length=10.0),
        car("I2", line="存4线", position=2, target_lines=["卸轮线"], length=10.0),
        car("C", line="修1库内", position=1, target_lines=["存4线"], length=10.0),
        car("A", line="修2库内", position=1, target_lines=["存4线"], length=10.0),
        car("B", line="修2库内", position=2, target_lines=["油漆线"], length=10.0),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(active=True),
        remote_debt=5,
    )
    assert strategic_plan.depot_inbound.assembly_complete
    assert strategic_plan.cun4_release.release_nos == ("I1", "I2")
    assert strategic_plan.depot_outbound.pull_order_nos == ("C", "A", "B")
    assert strategic_plan.depot_outbound.cun4_prefix_unsafe_nos == ("B",)
    assert strategic_plan.depot_outbound.status == "warn"
    assert strategic_plan.depot_outbound.reason == "cun4_target_suffix_requires_source_repack"
    contract = FlowContract(
        contract_id="REMOTE_SESSION:I1,I2,A,B,C",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("I1", "I2", "A", "B", "C"),
        source_lines=("存4线", "修1库内", "修2库内"),
        target_lines=("卸轮线", "油漆线", "存4线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotCun4SourceRepackExchangeEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_front_topology_plan_vetoes_remote_candidate_inside_h1() -> None:
    cars = [
        car("F", line="油漆线", position=1, target_lines=["存1线"]),
        car("R", line="修1库内", position=1, target_lines=["存4线"]),
    ]
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.front_topology.must_finish_before_remote
    request = ResourceRequest(
        contract_id="C",
        family=ContractFamily.DEPOT_OUTBOUND,
        candidate_id="candidate",
        resources=(),
        source_line="修1库内",
        target_line="存4线",
        move_nos=("R",),
        touched_lines=("修1库内", "存4线"),
        put_lines=("存4线",),
        intent=IntentKind.CUN4_OUTBOUND_HOLD,
    )
    contract = FlowContract(
        contract_id="C",
        family=ContractFamily.DEPOT_OUTBOUND,
        subject_nos=("R",),
        source_lines=("修1库内",),
        target_lines=("存4线",),
        priority=1,
        obligations=("move_to_target",),
    )
    permission = HumanPhaseGate().permission(
        phase_state=PhaseState(
            phase=PhaseKind.H1_FRONT_SERVICE,
            front_debt=1,
            cun4_port_debt=0,
            remote_debt=1,
            closeout_debt=0,
            reason="test",
            front_topology_clear_for_remote=False,
        ),
        envelope=CandidateEnvelope(
            candidate=object(),
            contract=contract,
            intent=IntentKind.CUN4_OUTBOUND_HOLD,
            resource_request=request,
            template_name="depot_outbound_session",
        ),
        contract_delta=ContractDelta(
            contract_id="C",
            family=ContractFamily.DEPOT_OUTBOUND,
            before_unsatisfied=1,
            after_unsatisfied=0,
            before_contract_debt=1,
            after_contract_debt=0,
        ),
        resource_delta=ResourceDelta(request=request, acquired=(), released_lines=()),
        remote_session=RemoteSessionState(),
    )
    assert not permission.allowed
    assert permission.reason == "h1_front_topology_requires_service_before_remote"


def test_policy_enters_h4_when_depot_inbound_plan_is_complete_before_state_flag() -> None:
    cars = [
        car("A", line="机南", position=1, target_lines=["修1库内"]),
        car("U", line="卸轮线", position=1, target_lines=["存4线"]),
    ]
    state = SolverState(
        case_id="T",
        cars=cars,
        depot_assignment=physical.DepotAssignment(slots={}, failures={}),
        loco_location=physical.LocoLocation("机南"),
        depot_inbound_assembly_accepted=False,
    )
    context = BaselinePolicy().context(state)
    assert context.strategic_plan.depot_inbound.assembly_complete
    assert context.phase_state.phase == PhaseKind.H4_REMOTE_DEPOT
    assert context.phase_state.reason != "front_topology_priority_before_remote:卸轮线"


def test_depot_outbound_session_does_not_split_to_overflow_when_cun4_is_short() -> None:
    cars = [
        *[
            car(f"H{index}", line="存4线", position=index, target_lines=["存4线"], length=60.0)
            for index in range(1, 6)
        ],
        car("A", line="修1库内", position=1, target_lines=["机走棚"], length=14.3),
        car("B", line="修1库内", position=2, target_lines=["存4线"], length=14.3),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H4_REMOTE_DEPOT,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=2,
    )
    assert strategic_plan.depot_outbound.status == "fail"
    assert strategic_plan.depot_outbound.reason == "cun4_capacity_insufficient_for_depot_outbound"
    assert strategic_plan.depot_outbound.temporary_line_by_no["A"] == "存4线"
    assert strategic_plan.depot_outbound.temporary_line_by_no["B"] == "存4线"
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A,B",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A", "B"),
        source_lines=("修1库内",),
        target_lines=("机走棚", "存4线"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotOutboundSessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("修1库内"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes == []


def test_depot_inbound_dirty_cleanout_uses_holding_when_final_target_is_full() -> None:
    cars = [
        car("D1", line="机走棚", position=1, target_lines=["油漆线"], length=17.6),
        car("D2", line="机走棚", position=2, target_lines=["油漆线"], length=17.6),
        car("D3", line="机走棚", position=3, target_lines=["油漆线"], length=17.6),
        car("O1", line="油漆线", position=1, target_lines=["油漆线"], length=100.0),
        car("R1", line="预修线", position=1, target_lines=["修1库内"], length=14.3),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    assert strategic_plan.depot_inbound.purity_violation_lines == ("机走棚",)
    contract = FlowContract(
        contract_id="REMOTE_SESSION:D1,D2,D3,R1",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("D1", "D2", "D3", "R1"),
        source_lines=("机走棚", "预修线"),
        target_lines=("油漆线", "修1库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundDirtyCleanoutEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走棚"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    assert envelopes[0].candidate.source_line == "机走棚"
    assert envelopes[0].candidate.target_line != "油漆线"
    assert envelopes[0].candidate.target_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES


def test_depot_inbound_prefix_assembly_allows_material_blocker_window() -> None:
    cars = [
        car("B1", line="预修线", position=1, target_lines=["存1线"], length=14.3),
        car("B2", line="预修线", position=2, target_lines=["存1线"], length=14.3),
        car("B3", line="预修线", position=3, target_lines=["存1线"], length=14.3),
        car("B4", line="预修线", position=4, target_lines=["存1线"], length=14.3),
        car("R1", line="预修线", position=5, target_lines=["修1库内"], length=14.3),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:B1,B2,B3,B4,R1",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("B1", "B2", "B3", "B4", "R1"),
        source_lines=("预修线",),
        target_lines=("存1线", "修1库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundPrefixAssemblySessionEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("预修线"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    assert envelopes[0].candidate.move_car_nos == ("B1", "B2", "B3", "B4", "R1")
    assert envelopes[0].candidate.plan_steps[-1].line == "预修线"


def test_depot_inbound_rebalance_moves_wash_oil_blocker_outward_for_pending_source() -> None:
    cars = [
        car("A1", line="洗油北", position=1, target_lines=["修1库内"], length=14.3),
        car("A2", line="洗油北", position=2, target_lines=["修2库内"], length=14.3),
        car("B1", line="油漆线", position=1, target_lines=["修3库内"], length=14.3),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A1,A2,B1",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A1", "A2", "B1"),
        source_lines=("洗油北", "油漆线"),
        target_lines=("修1库内", "修2库内", "修3库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundAssemblyRebalanceEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("洗油北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    assert envelopes[0].candidate.source_line == "洗油北"
    assert envelopes[0].candidate.target_line == "机走北"


def test_depot_inbound_route_blocker_digest_combines_blocker_and_downstream_source() -> None:
    cars = [
        car("A1", line="洗油北", position=1, target_lines=["修1库内"], length=14.3),
        car("A2", line="洗油北", position=2, target_lines=["修2库内"], length=14.3),
        car("B1", line="油漆线", position=1, target_lines=["修3库内"], length=14.3),
        car("B2", line="油漆线", position=2, target_lines=["修4库内"], length=14.3),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    strategic_plan = build_strategic_plan(
        phase=PhaseKind.H1_FRONT_SERVICE,
        cars=cars,
        depot_assignment=depot_assignment,
        remote_session=RemoteSessionState(),
        remote_debt=1,
    )
    contract = FlowContract(
        contract_id="REMOTE_SESSION:A1,A2,B1,B2",
        family=ContractFamily.REMOTE_SESSION,
        subject_nos=("A1", "A2", "B1", "B2"),
        source_lines=("洗油北", "油漆线"),
        target_lines=("修1库内", "修2库内", "修3库内", "修4库内"),
        priority=1,
        obligations=("remote_session_debt",),
    )
    envelopes = list(
        DepotInboundRouteBlockerDigestEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("机走北"),
            serial_gate_leases={},
            contract=contract,
            strategic_plan=strategic_plan,
        )
    )
    assert envelopes
    steps = envelopes[0].candidate.plan_steps
    assert [(step.action, step.line) for step in steps[:2]] == [("Get", "洗油北"), ("Get", "油漆线")]
    assert set(envelopes[0].candidate.move_car_nos) == {"A1", "A2", "B1", "B2"}


def test_cun4_outbound_assembly_release_moves_only_non_cun4_prefix() -> None:
    cars = [
        car("A", line="存4线", position=1, target_lines=["机走棚"]),
        car("B", line="存4线", position=2, target_lines=["存4线"]),
    ]
    depot_assignment = physical.DepotAssignment(slots={}, failures={})
    contract = FlowContract(
        contract_id="LOCO_AREA_STAGING:存4线->机走棚:A",
        family=ContractFamily.LOCO_AREA_STAGING,
        subject_nos=("A",),
        source_lines=("存4线",),
        target_lines=("机走棚",),
        priority=1,
        obligations=("move_to_target",),
    )
    envelopes = list(
        Cun4OutboundAssemblyReleaseEpisode().generate(
            case_id="T",
            hook_index=1,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=physical.TrackGraph(),
            loco_location=physical.LocoLocation("存4线"),
            serial_gate_leases={},
            contract=contract,
        )
    )
    assert len(envelopes) == 1
    assert envelopes[0].candidate.source_line == "存4线"
    assert envelopes[0].candidate.target_line == "机走棚"
    assert envelopes[0].candidate.move_car_nos == ("A",)


def test_depot_outbound_overflow_release_is_not_in_default_episode_order() -> None:
    assert "depot_outbound_overflow_release" not in {episode.template_name for episode in EPISODES}


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
