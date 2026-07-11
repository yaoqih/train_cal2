from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical  # noqa: E402
from stage4_simple.solve import MacroView, SourceRun, Stage4Solver, clone_car  # noqa: E402


BASELINE = ROOT / "artifacts" / "stage4_capability_portfolio_full"
FULLFLOW = ROOT / "artifacts" / "fullflow_truth23_spotting_parallel_v1"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def truth_path(date_code: str) -> Path:
    return next((ROOT / "data" / "truth2").glob(f"*{date_code}.json"))


def residual_solver(case_id: str, date_code: str) -> Stage4Solver:
    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(truth_path(date_code))
    stage4_request = load_json(BASELINE / f"{case_id}_stage4_request.json")
    stage4_response = load_json(BASELINE / f"{case_id}_response.json")
    return Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage4_request,
        stage4_response,
        stage4_response,
        time_budget_seconds=60.0,
    )


def initial_solver(case_id: str, date_code: str) -> Stage4Solver:
    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(truth_path(date_code))
    stage4_request = load_json(BASELINE / f"{case_id}_stage4_request.json")
    response = {
        "Data": {
            "Operations": [],
            "GeneratedEndStatus": stage4_request["StartStatus"],
        }
    }
    return Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage4_request,
        response,
        response,
        time_budget_seconds=60.0,
    )


def fullflow_residual_solver(case_id: str, date_code: str, dataset: str) -> Stage4Solver:
    truth_dir = ROOT / "data" / dataset
    truth_file = next(truth_dir.glob(f"*{date_code}.json"))
    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(truth_file)
    stage4_dir = FULLFLOW / dataset / "stage4"
    stage4_request = load_json(stage4_dir / f"{case_id}_stage4_request.json")
    stage4_response = load_json(stage4_dir / f"{case_id}_response.json")
    return Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage4_request,
        stage4_response,
        stage4_response,
        time_budget_seconds=60.0,
    )


def synthetic_solver(
    rows: tuple[tuple[str, str, int, str, bool, bool], ...],
    *,
    loco: str,
) -> Stage4Solver:
    solver = initial_solver("0120W", "20260120W")
    assert len(solver.cars) >= len(rows)
    cars = [clone_car(car) for car in solver.cars[: len(rows)]]
    active_nos: set[str] = set()
    target_by_no: dict[str, str] = {}
    for car, (no, line, position, target, heavy, active) in zip(cars, rows):
        car.update({
            "No": no,
            "_No": no,
            "Line": line,
            "Position": position,
            "TargetLines": [target],
            "_TargetLineSet": {target},
            "ForceTargetPosition": [],
            "_Force": (),
            "_ForcePositions": (),
            "IsHeavy": heavy,
            "IsWeigh": False,
            "_Weighed": True,
            "IsClosedDoor": False,
        })
        target_by_no[no] = target
        if active:
            active_nos.add(no)
    solver.cars = cars
    solver.initial_cars = [clone_car(car) for car in cars]
    solver.target_by_no = target_by_no
    solver.target_reason_by_no = {no: "synthetic" for no in target_by_no}
    solver.active_nos = active_nos
    solver.protected_satisfied_nos = set(target_by_no) - active_nos
    solver.initial_unsatisfied_nos = set(active_nos)
    solver.initial_unresolved_weigh_nos = set()
    solver.infeasible_nos = set()
    solver.infeasible_lines = set()
    solver.capacity_overflow_by_line = {}
    solver.capacity_holdout_count_by_line = {}
    solver.out_of_scope_nos = set()
    solver.excluded_line_nos = set()
    solver.loco = physical.LocoLocation(loco)
    solver.initial_loco = solver.loco
    solver.invalidate_caches()
    solver.seen_signatures = {solver.state_signature()}
    solver.best_progress = solver.main_progress()
    return solver


def target_variant_view(
    candidate_id: str,
    *,
    base_rank: int,
    window_rank: tuple[int, int, int],
) -> MacroView:
    candidate = physical.HookCandidate(
        case_id="RANK",
        hook_index=1,
        candidate_id=candidate_id,
        source_line="存2线",
        target_line="调梁棚",
        move_car_nos=(),
        action_family="",
        train_length_m=0.0,
        pull_equivalent_count=0,
        has_weigh=False,
        planned_positions={},
        generation_reason="test_target_window_rank",
    )
    validation = physical.PhysicalValidation(True, (), (), (), (), ())
    return MacroView(
        candidate=candidate,
        validation=validation,
        score=(0, 0, (base_rank,)),
        reason="test_target_window_rank",
        progress_after=(),
        target_key=("调梁棚",),
        target_window_rank=window_rank,
    )


def test_target_window_rank_can_select_a_strictly_cheaper_closing_variant() -> None:
    solver = initial_solver("0120W", "20260120W")
    anchor = target_variant_view(
        "anchor",
        base_rank=0,
        window_rank=(5_000, 5, 2),
    )
    closing = target_variant_view(
        "closing",
        base_rank=1,
        window_rank=(4_000, 4, 1),
    )

    ranked = sorted(solver.rank_target_variants([anchor, closing]), key=lambda view: view.score)

    assert ranked[0].candidate.candidate_id == "closing"


def test_target_window_rank_preserves_anchor_when_future_rounds_do_not_drop() -> None:
    solver = initial_solver("0120W", "20260120W")
    anchor = target_variant_view(
        "anchor",
        base_rank=0,
        window_rank=(5_000, 5, 2),
    )
    local_gain_only = target_variant_view(
        "local_gain_only",
        base_rank=1,
        window_rank=(3_000, 3, 2),
    )

    ranked = sorted(
        solver.rank_target_variants([anchor, local_gain_only]),
        key=lambda view: view.score,
    )

    assert ranked[0].candidate.candidate_id == "anchor"


def test_layout_rebuild_closes_cross_line_spotting_window() -> None:
    solver = residual_solver("0116W", "20260116W")
    move = tuple(physical.line_access_order(solver.cars, "存5线南"))

    candidate = solver.build_layout_rebuild_session("存5线南", move, "调梁棚")

    assert candidate is not None
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons
    assert len(physical.candidate_plan_steps(candidate)) <= 11


def test_ordered_prefix_restore_rebuilds_occupied_target_in_five_steps() -> None:
    cases = (
        ("0116W", "20260116W", "truth2", "卸轮线"),
        ("0416W", "20260416W", "truth3", "修1库外"),
    )
    for case_id, date_code, dataset, source_line in cases:
        solver = fullflow_residual_solver(case_id, date_code, dataset)
        pending = solver.current_unsatisfied_nos() & solver.active_nos
        source_order = tuple(physical.line_access_order(solver.cars, source_line))
        active_indexes = [index for index, no in enumerate(source_order) if no in pending]
        assert active_indexes
        move = source_order[: max(active_indexes) + 1]

        candidate = solver.build_prefix_ordered_target_restore_session(source_line, move)

        assert candidate is not None
        assert len(physical.candidate_plan_steps(candidate)) == 5
        validation = solver.validate(candidate)
        assert validation.accepted, validation.reasons
        probe = solver.probe_after(candidate, validation)
        assert probe.debt()["actionable_complete"]
        assert not probe.protected_damage_nos()


def test_ordered_prefix_restore_accepts_flexible_satisfied_outer_line_cars() -> None:
    cases = (
        ("0202Z", "20260202Z", "修1库外"),
        ("0206Z", "20260206Z", "修4库外"),
        ("0324W", "20260324W", "修2库外"),
    )
    for case_id, date_code, source_line in cases:
        solver = fullflow_residual_solver(case_id, date_code, "truth2")
        pending = solver.current_unsatisfied_nos() & solver.active_nos
        source_order = tuple(physical.line_access_order(solver.cars, source_line))
        source_pending = {no for no in source_order if no in pending}
        active_indexes = [index for index, no in enumerate(source_order) if no in source_pending]
        assert active_indexes
        move = source_order[: max(active_indexes) + 1]

        candidate = solver.build_prefix_ordered_target_restore_session(source_line, move)

        assert candidate is not None
        assert len(physical.candidate_plan_steps(candidate)) == 5
        validation = solver.validate(candidate)
        assert validation.accepted, validation.reasons
        probe = solver.probe_after(candidate, validation)
        assert not (source_pending & probe.current_unsatisfied_nos())
        assert not probe.protected_damage_nos()


def test_three_source_same_target_rebuilds_occupied_target_once() -> None:
    solver = synthetic_solver(
        (
            ("A", "存2线", 1, "存1线", False, True),
            ("B", "存3线", 1, "存1线", False, True),
            ("C", "存5线南", 1, "存1线", False, True),
            ("E", "存1线", 1, "存1线", False, False),
        ),
        loco="存2线",
    )

    candidate = solver.build_multi_source_same_target_session(
        {"存2线": ("A",), "存3线": ("B",), "存5线南": ("C",)},
        "存1线",
    )

    assert candidate is not None
    steps = physical.candidate_plan_steps(candidate)
    assert [step.action for step in steps] == ["Get", "Get", "Get", "Get", "Put"]
    assert sum(step.action == "Put" and step.line == "存1线" for step in steps) == 1
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons
    probe = solver.probe_after(candidate, validation)
    assert probe.debt()["actionable_complete"]
    assert not probe.protected_damage_nos()


def test_two_source_same_target_uses_two_gets_and_one_put() -> None:
    solver = synthetic_solver(
        (
            ("A", "存2线", 1, "存1线", False, True),
            ("B", "存3线", 1, "存1线", False, True),
        ),
        loco="存2线",
    )

    candidate = solver.build_multi_source_same_target_session(
        {"存2线": ("A",), "存3线": ("B",)},
        "存1线",
    )

    assert candidate is not None
    assert [step.action for step in physical.candidate_plan_steps(candidate)] == [
        "Get",
        "Get",
        "Put",
    ]


def test_partial_put_then_fresh_get_merges_the_retained_target_run() -> None:
    solver = synthetic_solver(
        (
            ("A", "存2线", 1, "存1线", False, True),
            ("TAIL", "存2线", 2, "机库线", False, True),
            ("B", "存3线", 1, "存1线", False, True),
        ),
        loco="存2线",
    )
    runs = (
        SourceRun("存2线", "存1线", ("A",)),
        SourceRun("存2线", "机库线", ("TAIL",)),
    )

    candidate = solver.build_partial_drop_continue_get_session(
        first_runs=runs,
        join_index=0,
        second_source="存3线",
        second_group=("B",),
    )

    assert candidate is not None
    steps = physical.candidate_plan_steps(candidate)
    assert [(step.action, step.line, step.move_car_nos) for step in steps] == [
        ("Get", "存2线", ("A", "TAIL")),
        ("Put", "机库线", ("TAIL",)),
        ("Get", "存3线", ("B",)),
        ("Put", "存1线", ("A", "B")),
    ]
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons


def test_partial_put_session_uses_stepwise_pull_limit() -> None:
    solver = synthetic_solver(
        (
            ("A", "存2线", 1, "存1线", True, True),
            ("T1", "存2线", 2, "机库线", True, True),
            ("T2", "存2线", 3, "机库线", True, True),
            ("T3", "存2线", 4, "机库线", True, True),
            ("T4", "存2线", 5, "机库线", True, True),
            ("B1", "存3线", 1, "存1线", True, True),
            ("B2", "存3线", 2, "存1线", True, True),
            ("B3", "存3线", 3, "存1线", True, True),
            ("B4", "存3线", 4, "存1线", True, True),
        ),
        loco="存2线",
    )
    runs = (
        SourceRun("存2线", "存1线", ("A",)),
        SourceRun("存2线", "机库线", ("T1", "T2", "T3", "T4")),
    )

    candidate = solver.build_partial_drop_continue_get_session(
        first_runs=runs,
        join_index=0,
        second_source="存3线",
        second_group=("B1", "B2", "B3", "B4"),
    )

    assert candidate is not None
    assert candidate.pull_equivalent_count > physical.PULL_LIMIT_EQUIVALENT
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons


def test_partial_put_session_can_append_to_clean_occupied_storage_target() -> None:
    solver = synthetic_solver(
        (
            ("A", "存2线", 1, "存1线", False, True),
            ("TAIL", "存2线", 2, "机库线", False, True),
            ("B", "存3线", 1, "存1线", False, True),
            ("EXISTING", "存1线", 1, "存1线", False, False),
        ),
        loco="存2线",
    )
    runs = (
        SourceRun("存2线", "存1线", ("A",)),
        SourceRun("存2线", "机库线", ("TAIL",)),
    )

    candidate = solver.build_partial_drop_continue_get_session(
        first_runs=runs,
        join_index=0,
        second_source="存3线",
        second_group=("B",),
    )

    assert candidate is not None
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons
    probe = solver.probe_after(candidate, validation)
    assert probe.debt()["actionable_complete"]


def test_multi_source_session_enforces_pull_equivalent_boundary() -> None:
    rows = (
        ("A1", "存1线", 1, "存3线", True, True),
        ("A2", "存1线", 2, "存3线", True, True),
        ("A3", "存1线", 3, "存3线", True, True),
        ("B1", "存2线", 1, "存3线", True, True),
        ("B2", "存2线", 2, "存3线", True, True),
        ("B3", "存2线", 3, "存3线", False, True),
    )
    solver = synthetic_solver(rows, loco="存1线")

    at_limit = solver.build_multi_source_same_target_session(
        {"存1线": ("A1", "A2", "A3"), "存2线": ("B1", "B2")},
        "存3线",
    )
    over_limit = solver.build_multi_source_same_target_session(
        {"存1线": ("A1", "A2", "A3"), "存2线": ("B1", "B2", "B3")},
        "存3线",
    )

    assert at_limit is not None
    assert at_limit.pull_equivalent_count == physical.PULL_LIMIT_EQUIVALENT
    assert over_limit is None


def test_persistent_corridor_puts_preserve_pending_service_routes() -> None:
    scenarios = (
        ("洗罐线北", "抛丸线", "存2线", "机南"),
        ("存2线", "油漆线", "存3线", "洗油北"),
        ("存2线", "调梁棚", "存3线", "调梁线北"),
    )
    for pending_source, pending_target, move_source, blocked_line in scenarios:
        solver = synthetic_solver(
            (
                ("PENDING", pending_source, 1, pending_target, False, True),
                ("MOVE", move_source, 1, blocked_line, False, True),
            ),
            loco=move_source,
        )
        planning_cars = [clone_car(car) for car in solver.cars]
        physical.apply_physical_get_order(planning_cars, move_source, ("MOVE",))

        damage = solver.route_lock_damage_after_put(
            target_line=blocked_line,
            move=("MOVE",),
            planning_cars=planning_cars,
        )

        assert damage == {"PENDING"}


def test_gate_lease_allows_restoring_the_original_blocker_set() -> None:
    solver = synthetic_solver(
        (
            ("PENDING", "存2线", 1, "调梁棚", False, True),
            ("BLOCKER", "调梁线北", 1, "调梁线北", False, False),
        ),
        loco="调梁线北",
    )
    candidate = physical.build_planlet_candidate(
        case_id="GATE",
        hook_index=1,
        source_line="调梁线北",
        target_line="调梁线北",
        batch=[solver.by_no()["BLOCKER"]],
        steps=(
            physical.plan_step("Get", "调梁线北", ("BLOCKER",)),
            physical.plan_step("Put", "调梁线北", ("BLOCKER",), {"BLOCKER": 1}),
        ),
        reason="test_gate_restore",
        candidate_kind="stage4_closed_macro",
    )
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons
    probe = solver.probe_after(candidate, validation)

    assert not solver.persistent_gate_lease_damage(candidate, probe)


def test_same_line_spotting_repack_can_use_chunk_staging() -> None:
    solver = residual_solver("0204Z", "20260204Z")

    candidates = list(solver.same_line_spotting_repack_candidates(solver.debt()))

    assert candidates
    candidate, reason = candidates[0]
    assert reason == "closed_spotting_same_line_repack"
    validation = solver.validate(candidate)
    assert validation.accepted, validation.reasons
    assert len(physical.candidate_plan_steps(candidate)) <= 17


def test_capacity_proof_excludes_only_the_unavoidable_overflow_subset() -> None:
    solver = initial_solver("0109W", "20260109W")

    assert solver.infeasible_lines == {"存5线南"}
    assert round(solver.capacity_overflow_by_line["存5线南"], 1) == 22.8
    assert solver.capacity_holdout_count_by_line == {"存5线南": 2}
    assert len(solver.infeasible_nos) == 2
    assert len({"3462450", "1844907", "1850929"} - solver.infeasible_nos) == 1


def test_capacity_overflow_prefers_the_short_accessible_holdout() -> None:
    solver = initial_solver("0306Z", "20260306Z")

    assert solver.infeasible_lines == {"存5线南"}
    assert round(solver.capacity_overflow_by_line["存5线南"], 1) == 12.9
    assert solver.capacity_holdout_count_by_line == {"存5线南": 1}
    assert solver.infeasible_nos == {"5249102"}


def test_spotting_capacity_lower_bound_is_not_reported_as_search_failure() -> None:
    for case_id, date_code, target in (
        ("0128W", "20260128W", "洗罐站"),
        ("0203W", "20260203W", "调梁棚"),
    ):
        solver = residual_solver(case_id, date_code)
        held_no = next(iter(solver.current_unsatisfied_nos()))
        solver.infeasible_nos.clear()
        solver.active_nos.add(held_no)
        debt = solver.debt()

        assert solver.capacity_holdout_count_by_line == {target: 1}
        assert debt["actionable_complete"]
        assert debt["active_unsatisfied_count"] == 0
        assert debt["infeasible_unsatisfied_count"] == 1

        solver.capacity_overflow_by_line[target] = (
            physical.car_length(solver.by_no()[held_no]) + 0.1
        )
        insufficient_debt = solver.debt()
        assert not insufficient_debt["actionable_complete"]
        assert insufficient_debt["active_unsatisfied_count"] == 1
        assert insufficient_debt["infeasible_unsatisfied_count"] == 0


def test_capacity_classification_counts_preselected_and_active_holdouts_together() -> None:
    solver = residual_solver("0129Z", "20260129Z")
    debt = solver.debt()

    assert solver.capacity_holdout_count_by_line == {"存5线南": 1}
    assert not debt["actionable_complete"]
    assert debt["active_unsatisfied_count"] == 1
    assert debt["infeasible_unsatisfied_count"] == 1


def test_cache_reserves_lines_with_pending_inbound_service() -> None:
    solver = initial_solver("0120W", "20260120W")
    move = ("5740270",)
    planning_cars = [clone_car(car) for car in solver.cars]
    physical.apply_physical_get_order(planning_cars, "存5线北", move)

    cache_line = solver.choose_cache_line(move, "存5线北", planning_cars)

    assert cache_line
    assert cache_line not in solver.pending_target_lines(planning_cars)
    assert cache_line != "洗罐线北"


def test_dirty_non_corridor_terminal_can_be_stacked_for_later_cleanup() -> None:
    solver = initial_solver("0310W", "20260310W")
    source_move = (
        "1656904",
        "1450094",
        "1780389",
        "3829174",
        "3500114",
        "5314312",
    )
    planning_cars = [clone_car(car) for car in solver.cars]
    physical.apply_physical_get_order(planning_cars, "存5线北", source_move)

    assert solver.dirty_terminal_stack_allowed(
        target_line="预修线",
        move=source_move[:3],
        planning_cars=planning_cars,
    )
    assert solver.line_is_pending_transit_corridor("洗罐线北", solver.cars)


def test_spotting_repack_cannot_bury_unfinished_outbound_cars() -> None:
    solver = initial_solver("0225W", "20260225W")
    probe = initial_solver("0225W", "20260225W")
    for car in probe.cars:
        no = physical.car_no(car)
        if no == "5243652":
            car["Line"] = "油漆线"
            car["Position"] = 1
        elif no == "5242641":
            car["Line"] = "油漆线"
            car["Position"] = 2
        elif car["Line"] == "油漆线":
            car["Position"] = int(car["Position"]) + 2
    probe.invalidate_caches()
    move = ("5243652", "5242641")
    candidate = physical.build_planlet_candidate(
        case_id="0225W",
        hook_index=1,
        source_line="卸轮线",
        target_line="油漆线",
        batch=[solver.by_no()[no] for no in move],
        steps=(physical.plan_step("Put", "油漆线", move),),
        reason="test_target_put_burial",
        candidate_kind="stage4_closed_macro",
    )

    buried = solver.newly_buried_by_target_put(candidate, probe)

    assert buried == {"3803921"}
    assert not solver.target_put_burial_recoverable(candidate, probe, buried)
