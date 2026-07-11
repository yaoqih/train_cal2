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


def test_default_profile_is_balanced() -> None:
    solver = Stage1Solver(CASE_0309Z)

    assert solver.profile == "balanced"


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
            profile="balanced",
        )

    solver_type.assert_called_once_with(
        CASE_0309Z,
        max_hooks=80,
        time_budget_seconds=5,
        profile="balanced",
    )
    solver_type.return_value.solve.assert_called_once_with()


def test_source_sessions_obey_train_tail_order_and_close_stage1_source() -> None:
    solver = Stage1Solver(CASE_0330W)
    debt = solver.stage1_debt()

    candidates = list(solver.source_session_candidates(
        debt,
        service_only=False,
        include_monotone=True,
        include_retained=False,
    ))

    assert candidates
    for view in candidates:
        steps = physical.candidate_plan_steps(view.candidate)
        assert steps[0].action == "Get"
        assert sum(step.action == "Put" for step in steps) >= 2
        carried = list(steps[0].move_car_nos)
        for step in steps[1:]:
            if step.action == "Weigh":
                continue
            assert step.action == "Put"
            assert carried[-len(step.move_car_nos):] == list(step.move_car_nos)
            del carried[-len(step.move_car_nos):]
        assert not carried

        source_pending = {
            physical.car_no(car)
            for car in solver.line_ordered_cars(view.candidate.source_line)
            if solver.stage1_goal(car) and not solver.stage1_car_complete(car)
        }
        assert source_pending <= set(view.candidate.move_car_nos)


def test_source_sessions_plan_forced_service_positions() -> None:
    solver = Stage1Solver(CASE_0309Z)

    candidates = [
        view.candidate
        for view in solver.source_session_candidates(
            solver.stage1_debt(),
            service_only=False,
            include_monotone=True,
            include_retained=False,
        )
        if view.candidate.source_line == "油漆线"
    ]

    assert candidates
    wash_puts = [
        step
        for candidate in candidates
        for step in physical.candidate_plan_steps(candidate)
        if step.action == "Put" and step.line == "洗罐站"
    ]
    assert wash_puts
    assert any(step.planned_positions for step in wash_puts)


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
        + result["summary"]["service_finish_business_hooks"]
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


def test_main_gather_collects_multiple_sources_before_putting() -> None:
    solver = Stage1Solver(CASE_0122W, time_budget_seconds=60)

    result = solver.solve()

    assert result["summary"]["status"] == "complete"
    multi_get = next(
        item
        for item in result["trace"]
        if item.get("reason") == "stage1_session_gather"
    )
    encoded_steps = str(multi_get["accepted"]).split(":", 4)[-1].split(";")
    actions = [step.split(":", 1)[0] for step in encoded_steps]
    first_put = actions.index("Put")
    assert first_put >= 2
    assert set(actions[:first_put]) == {"Get"}
    assert set(actions[first_put:]) == {"Put"}


def test_main_gather_session_merges_real_0112w_sources_and_closes_stack() -> None:
    solver = Stage1Solver(CASE_0112W)

    candidates = list(solver.gather_session_candidates(
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


def test_gather_session_covers_real_0318w_ggpp_word() -> None:
    solver = Stage1Solver(CASE_0318W)

    candidate = next(
        view.candidate
        for view in solver.gather_session_candidates(
            solver.stage1_debt(),
            service_only=False,
        )
        if "".join(
            step.action[0]
            for step in physical.candidate_plan_steps(view.candidate)
            if step.action in {"Get", "Put"}
        ) == "GGPP"
        if solver.validate_candidate(view.candidate).accepted
    )
    steps = physical.candidate_plan_steps(candidate)
    metrics = solver.session_metrics(steps)

    assert "".join(step.action[0] for step in steps if step.action in {"Get", "Put"}) == "GGPP"
    assert metrics.stack_valid
    assert metrics.flow_count == 2


def test_retained_source_session_covers_real_0304z_put_then_get() -> None:
    solver = Stage1Solver(CASE_0304Z)
    debt = {
        **solver.stage1_debt(),
        "lines_with_pending_stage1": ["预修线"],
    }

    candidate = next(
        view.candidate
        for view in solver.source_session_candidates(
            debt,
            service_only=False,
            include_monotone=False,
            include_retained=True,
        )
        if view.reason == "stage1_source_session"
        and solver.validate_candidate(view.candidate).accepted
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
    fragmented = Stage1Solver(CASE_0122W)
    forced_nos = {"1676903", "3470077", "1503793", "4950626"}

    assert solver.target_reserved_for_forced_cars("调梁棚")
    assert not solver.target_reserved_for_forced_cars("调梁棚", forced_nos)
    assert not fragmented.target_reserved_for_forced_cars("调梁棚")


def test_forced_rebuild_places_complete_real_0126w_position_group() -> None:
    result = Stage1Solver(CASE_0126W, time_budget_seconds=60).solve()

    rebuild = next(
        item
        for item in result["trace"]
        if item.get("reason") == "stage1_service_forced_rebuild"
    )

    assert result["summary"]["stage1_debt"]["complete"]
    assert (
        rebuild["service_quality_after"]["service_forced_position_satisfied_count"]
        > rebuild["service_quality_before"]["service_forced_position_satisfied_count"]
    )
