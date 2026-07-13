from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stage1_simple.solve import Stage1Solver, solve_one  # noqa: E402
from compare_stage1_manual_replay import positioned_cars, response_state  # noqa: E402
from solver_vnext import physical  # noqa: E402


CASE_0309Z = ROOT / "data/truth2/validation_取送车计划_20260309Z.json"
CASE_0305Z = ROOT / "data/truth2/validation_取送车计划_20260305Z.json"
CASE_0109W = ROOT / "data/truth2/validation_取送车计划_20260109W.json"
CASE_0201W = ROOT / "data/truth2/validation_取送车计划_20260201W.json"
CASE_0122W = ROOT / "data/truth2/validation_取送车计划_20260122W.json"
CASE_0320Z = ROOT / "data/truth2/validation_取送车计划_20260320Z.json"
CASE_0330W = ROOT / "data/truth2/validation_取送车计划_20260330W.json"
CASE_0112W = ROOT / "data/truth2/validation_取送车计划_20260112W.json"
CASE_0304Z = ROOT / "data/truth2/validation_取送车计划_20260304Z.json"
CASE_0302W = ROOT / "data/truth2/validation_取送车计划_20260302W.json"
CASE_0318W = ROOT / "data/truth2/validation_取送车计划_20260318W.json"
CASE_0126W = ROOT / "data/truth2/validation_取送车计划_20260126W.json"
CASE_0311W = ROOT / "data/truth2/validation_取送车计划_20260311W.json"
CASE_0205W = ROOT / "data/truth2/validation_取送车计划_20260205W.json"
CASE_0210Z = ROOT / "data/truth2/validation_取送车计划_20260210Z.json"
CASE_0105W = ROOT / "data/truth2/validation_取送车计划_20260105W.json"


def test_stage1_exposes_one_policy() -> None:
    solver = Stage1Solver(CASE_0309Z)

    assert not hasattr(solver, "profile")
    assert not hasattr(solver, "try_service_finish_step")


def test_initial_candidate_pool_contains_a_valid_move() -> None:
    solver = Stage1Solver(CASE_0309Z)
    debt = solver.stage1_debt()

    candidates = solver.generate_candidates(debt)

    assert any(solver.validate_candidate(view.candidate).accepted for view in candidates)


def test_primary_candidate_pool_has_no_put_route_cleanup_candidates() -> None:
    solver = Stage1Solver(CASE_0309Z)

    candidates = solver.generate_candidates(solver.stage1_debt())

    assert all(
        not view.reason.startswith("clear_put_route_blocker_for:")
        for view in candidates
    )


def test_contextual_selection_validates_each_candidate_at_most_once() -> None:
    solver = Stage1Solver(CASE_0309Z, time_budget_seconds=30)
    debt = solver.stage1_debt()
    views = sorted(solver.generate_candidates(debt), key=lambda item: item.score)
    calls: Counter[str] = Counter()
    validate_candidate = solver.validate_candidate

    def counted_validate(candidate):
        calls[candidate.candidate_id] += 1
        return validate_candidate(candidate)

    with patch.object(solver, "validate_candidate", side_effect=counted_validate):
        accepted = solver.choose_and_accept_candidate(views, debt, [])

    assert accepted
    assert calls
    assert max(calls.values()) == 1


def test_solve_one_runs_exactly_one_solver(tmp_path: Path) -> None:
    fake_result = {
        "response": {"Data": {"Operations": [], "GeneratedEndStatus": []}},
        "summary": {
            "case_id": "0309Z",
            "status": "complete",
            "hooks": 0,
            "business_hooks": 0,
            "stage1_debt": {"debt_count": 0},
        },
        "trace": [],
    }
    with patch("stage1_simple.solve.Stage1Solver") as solver_type:
        solver_type.return_value.solve.return_value = fake_result

        solve_one(
            CASE_0309Z,
            tmp_path,
            max_hooks=80,
            time_budget_seconds=5,
        )

    solver_type.assert_called_once_with(
        CASE_0309Z,
        max_hooks=80,
        time_budget_seconds=5,
    )
    solver_type.return_value.solve.assert_called_once_with()


def test_rolling_sessions_are_physically_valid_closed_stack_words() -> None:
    solver = Stage1Solver(CASE_0330W)
    candidates = list(solver.rolling_session_candidates(
        solver.stage1_debt(),
        service_only=False,
    ))

    assert candidates
    for view in candidates:
        steps = physical.candidate_plan_steps(view.candidate)
        metrics = solver.session_metrics(steps)
        get_nos = [
            no
            for step in steps
            if step.action == "Get"
            for no in step.move_car_nos
        ]
        assert steps[0].action == "Get"
        assert steps[-1].action == "Put"
        assert metrics.stack_valid
        assert len(get_nos) == len(set(get_nos))
        assert solver.validate_candidate(view.candidate).accepted


def test_session_metrics_do_not_reward_repeated_puts_to_same_line() -> None:
    solver = Stage1Solver(CASE_0309Z)
    steps = (
        physical.plan_step("Get", "预修线", ("A", "B", "C")),
        physical.plan_step("Put", "机走棚", ("C",)),
        physical.plan_step("Put", "机走棚", ("B",)),
        physical.plan_step("Put", "机走棚", ("A",)),
    )

    metrics = solver.session_metrics(steps)

    assert metrics.stack_valid
    assert metrics.business_hooks == 4
    assert metrics.flow_count == 1
    assert metrics.redundant_put_count == 2

    normalized = solver.coalesce_adjacent_put_steps(steps)
    assert normalized == (
        steps[0],
        physical.plan_step("Put", "机走棚", ("A", "B", "C")),
    )

    identity = solver.session_metrics((
        physical.plan_step("Get", "洗罐站", ("A",)),
        physical.plan_step("Put", "洗罐站", ("A",)),
    ))
    assert identity.stack_valid
    assert identity.flow_count == 0


def test_contiguous_mixed_target_block_does_not_force_an_expensive_rebuild() -> None:
    result = Stage1Solver(CASE_0305Z, time_budget_seconds=120).solve()
    quality = result["summary"]["service_quality"]

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 27
    assert result["summary"]["rehandled_car_count"] <= 1
    assert quality["service_forced_position_satisfied_count"] >= 4
    assert quality["service_satisfied_count"] >= 37
    assert quality["service_south_contiguous_count"] >= 37
    assert all(item.get("phase") != "service_finish" for item in result["trace"])


def test_rolling_session_uses_passive_insertion_to_improve_forced_positions() -> None:
    solver = Stage1Solver(CASE_0311W)
    before = solver.service_quality()["service_forced_position_satisfied_count"]
    candidate = next(
        view.candidate
        for view in solver.rolling_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if [
            (step.action, step.line)
            for step in physical.candidate_plan_steps(view.candidate)
        ] == [("Get", "油漆线"), ("Put", "洗罐站")]
    )
    validation = solver.validate_candidate(candidate)
    probe = solver.probe_after(candidate, validation)

    assert validation.accepted
    assert probe.service_quality()["service_forced_position_satisfied_count"] == before + 2


def test_source_dependency_boundary_avoids_rehandling_real_0205w() -> None:
    result = Stage1Solver(CASE_0205W, time_budget_seconds=60).solve()
    quality = result["summary"]["downstream_quality"]

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 27
    assert quality["service_satisfied_count"] >= 47
    assert quality["service_south_contiguous_count"] >= 47
    assert quality["service_forced_position_satisfied_count"] >= 4


def test_contextual_rank_finishes_nearer_source_phase_before_cross_phase_gather() -> None:
    solver = Stage1Solver(CASE_0105W, time_budget_seconds=60)
    assert solver.step(solver.stage1_debt())
    debt = solver.stage1_debt()
    context = solver.selection_context(debt)
    assert context is not None

    views = solver.generate_candidates(debt)
    direct = next(
        view
        for view in views
        if [
            (step.action, step.line, step.move_car_nos)
            for step in physical.candidate_plan_steps(view.candidate)
        ] == [
            ("Get", "抛丸线", ("5241331", "5313548")),
            ("Put", "机南", ("5241331", "5313548")),
        ]
    )
    direct_evaluation = solver.evaluate_candidate(
        direct,
        debt,
        [],
        reject_dead_end=False,
        compute_downstream_quality=True,
    )
    assert direct_evaluation is not None
    direct_rank, violations = solver.rank_contextual_candidate(
        context,
        direct_evaluation,
        debt,
    )
    assert not violations
    assert direct_rank is not None

    cross_phase = None
    cross_phase_rank = None
    for view in views:
        if solver.candidate_source_phase_gap(view.candidate, debt) <= 0:
            continue
        evaluation = solver.evaluate_candidate(
            view,
            debt,
            [],
            reject_dead_end=False,
            compute_downstream_quality=True,
        )
        if evaluation is None:
            continue
        ranked, _violations = solver.rank_contextual_candidate(
            context,
            evaluation,
            debt,
        )
        if ranked is not None:
            cross_phase = view
            cross_phase_rank = ranked
            break

    assert cross_phase is not None
    assert cross_phase_rank is not None
    assert solver.candidate_source_phase_gap(direct.candidate, debt) == 0
    assert solver.candidate_source_phase_gap(cross_phase.candidate, debt) > 0
    assert direct_rank.score < cross_phase_rank.score


def test_get_blocker_pressure_rejects_loading_an_already_blocking_line() -> None:
    solver = Stage1Solver(CASE_0309Z, time_budget_seconds=60)
    debt = solver.stage1_debt()
    context = solver.selection_context(debt)
    assert context is not None
    assert context.mode == "get_unlock"
    assert "机走棚" in context.get_blockers.lines

    view = next(
        candidate
        for candidate in solver.generate_candidates(debt)
        if [
            (step.action, step.line, step.move_car_nos)
            for step in physical.candidate_plan_steps(candidate.candidate)
        ] == [
            ("Get", "预修线", ("4872648", "4873395", "7701952")),
            ("Put", "机走棚", ("4872648", "4873395", "7701952")),
        ]
    )
    evaluation = solver.evaluate_candidate(
        view,
        debt,
        [],
        reject_dead_end=False,
        compute_downstream_quality=True,
    )
    assert evaluation is not None

    after = evaluation.probe.pending_get_blocker_info(evaluation.after_debt)
    ranked, violations = solver.rank_contextual_candidate(context, evaluation, debt)

    assert after.lines == context.get_blockers.lines
    assert after.pressure > context.get_blockers.pressure
    assert ranked is None
    assert violations == ("worsens_get_route_blockers",)


def test_route_support_lane_gathers_from_an_assembly_blocker_real_0309z() -> None:
    solver = Stage1Solver(CASE_0309Z, time_budget_seconds=60)

    assert solver.step(solver.stage1_debt())

    accepted = solver.trace[-1]
    assert accepted["reason"] == "stage1_rolling_session"
    assert "Get:预修线:4872648,4873395,7701952" in accepted["accepted"]
    assert "Get:机走棚:4872019,5492118,5496322" in accepted["accepted"]
    assert accepted["debt_after"]["debt_count"] == 9
    assert accepted["route_blocker_pressure_after"] < accepted["route_blocker_pressure_before"]


def test_route_support_lane_preserves_ordinary_completion_real_0105w() -> None:
    result = Stage1Solver(CASE_0105W, time_budget_seconds=120).solve()
    quality = result["summary"]["service_quality"]

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 32
    assert result["summary"]["rehandled_car_count"] <= 6
    assert quality["service_satisfied_count"] >= 50
    assert quality["service_south_contiguous_count"] >= 40
    assert quality["service_forced_position_satisfied_count"] >= 3


def test_rolling_session_retains_short_prefix_for_reachable_target_real_0210z() -> None:
    solver = Stage1Solver(CASE_0210Z, time_budget_seconds=60)
    for _ in range(2):
        assert solver.step(solver.stage1_debt())

    expected = [
        ("Get", "预修线", ("5347131", "5349797", "5487841")),
        ("Get", "存2线", ("1660229",)),
        ("Put", "洗油北", ("5347131", "5349797", "5487841", "1660229")),
    ]
    candidate = next(
        view.candidate
        for view in solver.rolling_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if [
            (step.action, step.line, step.move_car_nos)
            for step in physical.candidate_plan_steps(view.candidate)
        ] == expected
    )

    assert solver.validate_candidate(candidate).accepted


def test_service_rolling_can_peel_a_reachable_tail_and_restore_its_prefix() -> None:
    solver = Stage1Solver(CASE_0320Z, time_budget_seconds=120)
    result = solver.solve()

    quality = result["summary"]["service_quality"]
    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 36
    assert quality["service_satisfied_count"] >= 46
    assert quality["service_south_contiguous_count"] >= 43
    assert quality["service_forced_position_satisfied_count"] >= 5


def test_rolling_session_completes_real_0210z_with_clean_service_tails() -> None:
    result = Stage1Solver(CASE_0210Z, time_budget_seconds=60).solve()
    quality = result["summary"]["downstream_quality"]

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 20
    assert quality["service_satisfied_count"] >= 37
    assert quality["service_south_contiguous_count"] >= 37
    assert quality["service_forced_position_satisfied_count"] >= 2


def test_service_quality_matches_stage1_reporting_scope() -> None:
    solver = Stage1Solver(CASE_0320Z)
    quality = solver.service_quality()

    expected_eligible = sum(1 for car in solver.cars if solver.service_eligible(car))
    expected_satisfied = sum(
        1
        for car in solver.cars
        if solver.service_eligible(car) and solver.target_satisfied(car)
    )
    forced = [
        car
        for car in solver.cars
        if solver.service_eligible(car) and physical.force_positions(car)
    ]
    assert quality["service_eligible_count"] == expected_eligible
    assert quality["service_satisfied_count"] == expected_satisfied
    assert quality["service_forced_position_eligible_count"] == len(forced)
    assert quality["service_forced_position_satisfied_count"] == sum(
        solver.target_satisfied(car) for car in forced
    )
    assert all(
        not solver.service_eligible(car)
        for car in solver.cars
        if set(car.get("TargetLines") or ()) & {"机走棚", "机走北"}
    )


def test_service_target_satisfaction_requires_forced_position() -> None:
    solver = Stage1Solver(CASE_0309Z)
    car = next(
        solver.clone_car(item)
        for item in solver.cars
        if solver.service_eligible(item) and physical.force_positions(item)
    )
    forced = physical.force_positions(car)
    car["Line"] = solver.service_target_options(car)[0]
    car["Position"] = max(forced) + 1

    assert not solver.target_satisfied(car)

    car["Position"] = forced[0]
    assert solver.target_satisfied(car)


def test_manual_comparison_preserves_solver_business_positions(tmp_path: Path) -> None:
    response_path = tmp_path / "response.json"
    response_path.write_text(
        json.dumps({
            "Data": {
                "GeneratedEndStatus": [
                    {"No": "F1", "Line": "调梁棚", "Position": 6},
                    {"No": "F2", "Line": "调梁棚", "Position": 8},
                ],
            },
        }),
        encoding="utf-8",
    )
    cars = [
        {"No": "F1", "Line": "存5线北", "Position": 1},
        {"No": "F2", "Line": "存5线北", "Position": 2},
    ]

    final = positioned_cars(response_state(response_path), cars)

    assert {car["No"]: car["Position"] for car in final} == {"F1": 6, "F2": 8}


def test_stage1_improves_forced_positions_without_reopening_stage1() -> None:
    solver = Stage1Solver(CASE_0109W, time_budget_seconds=60)
    before = solver.service_quality()

    result = solver.solve()
    after = result["summary"]["downstream_quality"]

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] == (
        result["summary"]["primary_business_hooks"]
        + result["summary"]["service_closure_business_hooks"]
    )
    assert (
        after["service_forced_position_satisfied_count"]
        > before["service_forced_position_satisfied_count"]
    )
    assert after["service_south_contiguous_count"] >= before["service_south_contiguous_count"]


def test_target_rebuild_retains_target_prefix_until_final_put() -> None:
    solver = Stage1Solver(CASE_0201W)
    target_cars = solver.line_ordered_cars("预修线")
    source_cars = solver.line_ordered_cars("存5线北")

    steps = solver.build_target_rebuild_steps(
        target="预修线",
        clear_batch=target_cars[:2],
        retained_prefix=target_cars[:1],
        blocker_suffix=target_cars[1:2],
        staging="存3线",
        source="存5线北",
        incoming=source_cars[:1],
    )

    assert [step.action for step in steps] == ["Get", "Put", "Get", "Put", "Put"]
    assert steps[1].move_car_nos == (physical.car_no(target_cars[1]),)
    assert steps[3].move_car_nos == (physical.car_no(source_cars[0]),)
    assert steps[4].move_car_nos == (physical.car_no(target_cars[0]),)


def test_rolling_session_search_is_not_limited_to_three_sources() -> None:
    solver = Stage1Solver(CASE_0112W)
    candidate = next(
        view.candidate
        for view in solver.rolling_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if len({
            step.line
            for step in physical.candidate_plan_steps(view.candidate)
            if step.action == "Get"
        }) >= 4
    )

    assert solver.session_metrics(physical.candidate_plan_steps(candidate)).stack_valid


def test_rolling_session_merges_real_0112w_sources_and_closes_stack() -> None:
    solver = Stage1Solver(CASE_0112W)

    candidates = list(solver.rolling_session_candidates(
        solver.stage1_debt(),
        service_only=False,
    ))
    candidate = next(
        view.candidate
        for view in candidates
        if {step.line for step in physical.candidate_plan_steps(view.candidate) if step.action == "Get"}
        >= {"油漆线", "洗罐站"}
        and solver.validate_candidate(view.candidate).accepted
    )
    metrics = solver.session_metrics(physical.candidate_plan_steps(candidate))

    assert metrics.stack_valid
    assert metrics.flow_count >= 2
    assert solver.candidate_closure_savings(candidate) >= 1


def test_rolling_session_covers_real_0318w_multi_get_and_multi_put() -> None:
    solver = Stage1Solver(CASE_0318W)

    candidate = next(
        view.candidate
        for view in solver.rolling_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if sum(
            step.action == "Get"
            for step in physical.candidate_plan_steps(view.candidate)
        ) >= 2
        and sum(
            step.action == "Put"
            for step in physical.candidate_plan_steps(view.candidate)
        ) >= 2
    )
    steps = physical.candidate_plan_steps(candidate)
    metrics = solver.session_metrics(steps)

    assert metrics.stack_valid
    assert metrics.flow_count >= 2


def test_rolling_session_covers_real_0304z_put_then_get() -> None:
    solver = Stage1Solver(CASE_0304Z)

    candidate = next(
        view.candidate
        for view in solver.rolling_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if solver.session_metrics(
            physical.candidate_plan_steps(view.candidate)
        ).retains_across_put_then_get
    )
    metrics = solver.session_metrics(physical.candidate_plan_steps(candidate))

    assert metrics.stack_valid
    assert metrics.retains_across_put_then_get
    assert solver.candidate_closure_savings(candidate) > 0


def test_blocker_relocation_does_not_close_an_unresolved_stage1_source() -> None:
    solver = Stage1Solver(CASE_0302W)
    debt = solver.stage1_debt()
    car = solver.by_no()["4873284"]

    targets = solver.blocker_targets([car], source="调梁棚", debt=debt)

    assert "预修线" in debt["lines_with_pending_stage1"]
    assert "预修线" not in targets


def test_unresolved_source_dependencies_order_real_0302w_and_0318w() -> None:
    solver_0302 = Stage1Solver(CASE_0302W)
    solver_0318 = Stage1Solver(CASE_0318W)

    dependencies_0302 = solver_0302.deferred_stage1_source_dependencies(
        solver_0302.stage1_debt()
    )
    dependencies_0318 = solver_0318.deferred_stage1_source_dependencies(
        solver_0318.stage1_debt()
    )

    assert dependencies_0302["调梁棚"] == "预修线"
    assert dependencies_0318["存5线北"] == "预修线"


def test_forced_spotting_cars_reserve_real_0318w_target() -> None:
    solver = Stage1Solver(CASE_0318W)
    mixed_target_block = Stage1Solver(CASE_0122W)
    forced_nos = {"1676903", "3470077", "1503793", "4950626"}

    assert solver.target_reserved_for_forced_cars("调梁棚")
    assert not solver.target_reserved_for_forced_cars("调梁棚", forced_nos)
    assert mixed_target_block.target_reserved_for_forced_cars("调梁棚")


def test_service_rebuild_places_complete_real_0126w_position_group() -> None:
    # This is a capability assertion; the complete four-planlet rebuild runs
    # close to 60 seconds on a cold worker, so keep latency out of the verdict.
    result = Stage1Solver(CASE_0126W, time_budget_seconds=75).solve()

    rebuild = max(
        (
            item
            for item in result["trace"]
            if item.get("phase") == "service_closure"
            and item.get("service_quality_before")
            and item.get("service_quality_after")
        ),
        key=lambda item: (
            item["service_quality_after"]["service_forced_position_satisfied_count"]
            - item["service_quality_before"]["service_forced_position_satisfied_count"]
        ),
    )

    assert result["summary"]["stage1_debt"]["complete"]
    assert result["summary"]["business_hooks"] <= 29
    assert result["summary"]["rehandled_car_count"] <= 4
    assert result["summary"]["service_quality"]["service_satisfied_count"] >= 37
    assert result["summary"]["service_quality"]["service_south_contiguous_count"] >= 37
    assert (
        result["summary"]["service_quality"]["service_forced_position_satisfied_count"]
        >= 5
    )
    assert (
        rebuild["service_quality_after"]["service_forced_position_satisfied_count"]
        > rebuild["service_quality_before"]["service_forced_position_satisfied_count"]
    )
