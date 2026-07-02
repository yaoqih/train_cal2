from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical
from solver_vnext import serial
from solver_vnext.placement import planned_positions_for_batch
from solver_vnext.contracts import classify_family
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
)
from solver_vnext.phase import HumanPhaseGate
from solver_vnext.resources import StationResourceGraph


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


def test_depot_put_uses_business_planned_positions_not_north_end_shift() -> None:
    cars = [
        car("M1", line="存1线", position=1, target_lines=["修1库内"]),
        car("M2", line="存1线", position=2, target_lines=["修1库内"]),
        car("E1", line="修1库内", position=1, target_lines=["修1库内"]),
        car("E2", line="修1库内", position=3, target_lines=["修1库内"]),
    ]
    physical.apply_physical_put_order(
        cars,
        "修1库内",
        ["M1", "M2"],
        {"M1": 4, "M2": 5},
    )
    assert {
        physical.car_no(item): (item["Line"], item["Position"])
        for item in cars
    } == {
        "M1": ("修1库内", 4),
        "M2": ("修1库内", 5),
        "E1": ("修1库内", 1),
        "E2": ("修1库内", 3),
    }


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
        intent=IntentKind.PREFIX_DIGEST,
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
        intent=IntentKind.PREFIX_DIGEST,
    )
    assert graph._serial_blocker_storage_violations(
        other_owner_request,
        candidate=candidate,
        cars=cars,
        depot_assignment=depot_assignment,
        serial_gate_leases={"机走棚": lease},
    )[0].startswith("serial_blocker_storage_before_downstream_clear")


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


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
