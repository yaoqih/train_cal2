from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from solver_vnext import physical  # noqa: E402
from stage4_simple.construct import (  # noqa: E402
    SourceWindowGenerator,
    monotone_stack_prepend_allowed,
)
from stage4_simple.contracts import (  # noqa: E402
    DEPOT_REHOOK_ID,
    build_contract_graph,
    classify_depot_rehook,
)
from stage4_simple.domain import (  # noqa: E402
    CarrySegment,
    ContractStatus,
    DepotRehookMode,
    OwnedStack,
)
from stage4_simple.optimizer import (  # noqa: E402
    BlockFlowOptimizer,
    OptimizationConfig,
)
from stage4_simple.planner import PlanningCheckpoint  # noqa: E402
from stage4_simple.search import (  # noqa: E402
    OperationTransitions,
    SearchNode,
    Stage4Problem,
)
from stage4_simple.solve import Stage4Solver  # noqa: E402
from stage4_simple.topology import (  # noqa: E402
    RESOURCE_GATES,
    resource_gate_closure,
)


FULLFLOW = ROOT / "artifacts" / "fullflow_current"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_car(
    no: str,
    line: str,
    position: int,
    target: str,
    *,
    force: tuple[int, ...] = (),
) -> dict:
    return physical.normalized_car({
        "No": no,
        "Line": line,
        "Position": position,
        "Length": 14.3,
        "IsHeavy": False,
        "IsWeigh": False,
        "IsClosedDoor": False,
        "TargetLines": [target],
        "ForceTargetPosition": list(force),
        "_Weighed": True,
    })


def make_problem(
    rows: tuple[tuple[str, str, int, str], ...],
    *,
    loco: str,
    forces: dict[str, tuple[int, ...]] | None = None,
) -> Stage4Problem:
    physical.clear_state_caches()
    forces = forces or {}
    cars = [
        make_car(no, line, position, target, force=forces.get(no, ()))
        for no, line, position, target in rows
    ]
    assignment = physical.DepotAssignment({}, {}, {})
    unsatisfied = {
        physical.car_no(car)
        for car in physical.unsatisfied_cars(cars, assignment)
    }
    return Stage4Problem(
        case_id="TEST",
        cars=cars,
        loco_location=physical.LocoLocation(loco),
        depot_assignment=assignment,
        target_by_no={no: target for no, _line, _position, target in rows},
        active_nos=frozenset(unsatisfied),
        protected_nos=frozenset({car["_No"] for car in cars} - unsatisfied),
    )


def exact_stage4_solver(
    case_id: str,
    date_code: str,
    *,
    dataset: str = "truth2",
) -> Stage4Solver:
    truth_path = next((ROOT / "data" / dataset).glob(f"*{date_code}.json"))
    _case_id, request, _cars, assignment, _loco = physical.read_case(truth_path)
    stage3 = FULLFLOW / dataset / "stage3"
    return Stage4Solver(
        case_id,
        request,
        assignment,
        load_json(stage3 / f"{case_id}_stage3_request.json"),
        load_json(stage3 / f"{case_id}_response.json"),
        load_json(stage3 / f"{case_id}_combined_response.json"),
        time_budget_seconds=30.0,
        max_labels=16,
        max_expansions=30_000,
    )


def test_incremental_transition_matches_full_planlet_validation() -> None:
    problem = make_problem(
        (
            ("A", "存2线", 1, "存1线"),
            ("TAIL", "存2线", 2, "机库线"),
            ("B", "存3线", 1, "存1线"),
        ),
        loco="存2线",
    )
    transitions = OperationTransitions(problem)
    initial = SearchNode(physical.initial_planlet_state(
        problem.cars,
        problem.loco_location,
    ))
    first_get = physical.plan_step("Get", "存2线", ("A", "TAIL"))
    first = transitions.apply_step(initial, first_get)
    assert first is not None

    alternate = replace(
        initial,
        state=replace(initial.state, operation_paths=(("sentinel",),)),
    )
    alternate_first = transitions.apply_step(alternate, first_get)
    assert alternate_first is not None
    assert alternate_first.state.operation_paths[0] == ("sentinel",)
    assert alternate_first.state.operation_paths[1] == first.state.operation_paths[0]

    steps = [first_get]
    node = first
    for action, line, move in (
        ("Put", "机库线", ("TAIL",)),
        ("Get", "存3线", ("B",)),
        ("Put", "存1线", ("A", "B")),
    ):
        positions = transitions.planned_positions(node.state, line, move) if action == "Put" else {}
        assert positions is not None
        step = physical.plan_step(action, line, move, positions)
        successor = transitions.apply_step(node, step)
        assert successor is not None
        node = successor
        steps.append(step)

    candidate = physical.build_planlet_candidate(
        case_id="TEST",
        hook_index=1,
        source_line="存2线",
        target_line="存1线",
        batch=problem.cars,
        steps=tuple(steps),
        reason="incremental_equivalence",
        candidate_kind="blocker_relocation",
    )
    validation = physical.validate_planlet(
        problem.graph,
        candidate,
        problem.cars,
        problem.loco_location,
        problem.depot_assignment,
    )

    assert validation.accepted, validation.reasons
    assert validation.operation_paths == node.state.operation_paths
    assert problem.complete(node)


def test_contract_graph_has_one_explicit_predecessor_boundary() -> None:
    problem = make_problem(
        (("A", "存2线", 1, "存1线"),),
        loco="存2线",
    )
    assert classify_depot_rehook(problem).mode == DepotRehookMode.NOT_REQUIRED

    graph = build_contract_graph(problem)
    assert [contract.contract_id for contract in graph.ready()] == [DEPOT_REHOOK_ID]
    graph = graph.activate(DEPOT_REHOOK_ID).close(DEPOT_REHOOK_ID)
    ready = graph.ready()

    assert len(ready) == 1
    assert ready[0].target == "存1线"
    assert ready[0].status == ContractStatus.PENDING


def test_owned_stack_distinguishes_ranked_and_restore_segments() -> None:
    ranked = OwnedStack("存1线", (CarrySegment("油漆线", ("B",), (2,)),))
    ranked = ranked.prepend(CarrySegment("油漆线", ("A",), (1,)))
    assert ranked is not None
    assert ranked.nos == ("A", "B")

    restore = OwnedStack(
        "存2线",
        (CarrySegment("restore:存3线", ("Y",), (0,), protected=True),),
    )
    restore = restore.prepend(
        CarrySegment("restore:存3线", ("X",), (0,), protected=True)
    )
    assert restore is not None
    assert restore.nos == ("X", "Y")
    assert restore.prepend(CarrySegment("restore:存3线", ("R",), (1,), True)) is None
    assert restore.consume(("X",)).nos == ("Y",)


def test_resource_gate_closure_is_transitive_and_reserved() -> None:
    assert resource_gate_closure("油漆线") == frozenset({
        "洗油北",
        "机走棚",
        "机走北",
    })
    problem = make_problem(
        (("PENDING", "存2线", 1, "油漆线"),),
        loco="存2线",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))

    for resource in resource_gate_closure("油漆线"):
        assert generator.resource_reserved(resource)
    assert not generator.resource_reserved("存1线")


def test_active_resource_blockers_transfer_to_their_flow_owner() -> None:
    problem = make_problem(
        (
            ("ACTIVE", "调梁线北", 1, "洗罐站"),
            ("SATISFIED", "调梁线北", 2, "调梁线北"),
        ),
        loco="调梁线北",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))

    assert generator.transferable_resource_owner(("ACTIVE",)) == "洗罐站"
    assert generator.transferable_resource_owner(("ACTIVE", "SATISFIED")) == ""


def test_staging_does_not_occupy_open_operation_gates() -> None:
    problem = make_problem(
        (
            ("PENDING", "存2线", 1, "抛丸线"),
            ("MOVE", "存3线", 1, "存1线"),
        ),
        loco="存3线",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))
    assert generator.apply(physical.plan_step("Get", "存3线", ("MOVE",)))

    candidates = {
        line
        for _rank, line in generator.staging_candidates(("MOVE",), "存1线")
    }
    assert not candidates.intersection(RESOURCE_GATES["抛丸线"])


def test_source_window_combines_two_sources_before_one_put() -> None:
    problem = make_problem(
        (
            ("A", "存2线", 1, "调梁棚"),
            ("B", "存3线", 1, "调梁棚"),
        ),
        loco="存2线",
    )
    result = SourceWindowGenerator(problem, OperationTransitions(problem)).advance()

    assert result.complete, result.reason
    assert [(step.action, step.line, step.move_car_nos) for step in result.node.steps] == [
        ("Get", "存2线", ("A",)),
        ("Get", "存3线", ("B",)),
        ("Put", "调梁棚", ("A", "B")),
    ]


def test_source_window_splits_one_get_into_tail_first_puts() -> None:
    problem = make_problem(
        (
            ("A", "存2线", 1, "存1线"),
            ("B", "存2线", 2, "存3线"),
        ),
        loco="存2线",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))

    assert generator.digest_line("存2线", clear_all=False)
    assert problem.complete(generator.node)
    assert [(step.action, step.line, step.move_car_nos) for step in generator.node.steps] == [
        ("Get", "存2线", ("A", "B")),
        ("Put", "存3线", ("B",)),
        ("Put", "存1线", ("A",)),
    ]


def test_optimizer_returns_a_closed_physically_valid_plan() -> None:
    problem = make_problem(
        (
            ("A", "存2线", 1, "调梁棚"),
            ("B", "存3线", 1, "调梁棚"),
        ),
        loco="存2线",
    )
    optimized = BlockFlowOptimizer(
        problem,
        OptimizationConfig(
            time_budget_seconds=5.0,
            max_labels=8,
            max_expansions=1_000,
        ),
    ).solve()

    assert optimized.plan.complete, optimized.plan.reason
    assert optimized.feasible_labels >= 1
    assert not optimized.plan.node.state.carried_order
    assert not optimized.plan.leases
    assert problem.complete(optimized.plan.node)


def test_source_search_deepens_only_inside_terminal_recovery_domain() -> None:
    problem = make_problem(
        (("A", "存3线", 1, "存1线"),),
        loco="存3线",
    )
    optimizer = BlockFlowOptimizer(problem, OptimizationConfig())
    checkpoint = PlanningCheckpoint(
        node=SearchNode(physical.initial_planlet_state(
            problem.cars,
            problem.loco_location,
        )),
        contracts=build_contract_graph(problem),
        stacks=(),
        leases=(),
        trace=(),
        expansions=0,
        goal_owners=(),
    )

    assert optimizer._source_attempt_limit(()) == 64
    assert optimizer._source_attempt_limit((checkpoint,)) == 6

    staged = replace(
        checkpoint,
        stacks=((
            "存3线",
            OwnedStack(
                "存3线",
                (CarrySegment("存1线", ("A",), (1,)),),
            ),
        ),),
    )
    assert optimizer._source_attempt_limit((staged,)) == 128


def test_source_label_rank_refresh_is_isolated_from_the_problem() -> None:
    problem = make_problem(
        (("A", "存3线", 1, "调梁棚"),),
        loco="存3线",
    )
    baseline = dict(problem.final_rank_by_no)
    first = SourceWindowGenerator(problem, OperationTransitions(problem))
    first.rank_by_no["A"] = 99

    assert dict(problem.final_rank_by_no) == baseline
    assert SourceWindowGenerator(
        problem,
        OperationTransitions(problem),
    ).rank_by_no == baseline
    with pytest.raises(TypeError):
        problem.final_rank_by_no["A"] = 99


def test_spotting_stacks_only_prepend_consecutive_lower_ranks() -> None:
    assert monotone_stack_prepend_allowed([1, 2], [3, 4])
    assert not monotone_stack_prepend_allowed([1, 2], [4, 5])
    assert not monotone_stack_prepend_allowed([2, 1], [3, 4])
    assert not monotone_stack_prepend_allowed([1, 3], [4, 5])
    assert not monotone_stack_prepend_allowed([3, 4], [1, 2])
    assert not monotone_stack_prepend_allowed([], [1, 2])


def test_owner_stack_ranks_remove_unused_physical_position_gaps() -> None:
    problem = make_problem(
        (
            ("A", "存2线", 1, "调梁棚"),
            ("B", "存3线", 1, "调梁棚"),
            ("C", "存5线南", 1, "调梁棚"),
        ),
        loco="存2线",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))
    generator.rank_by_no = {"A": 4, "B": 6, "C": 9}

    assert generator.owner_stack_rank_by_no("调梁棚") == {
        "A": 1,
        "B": 2,
        "C": 3,
    }


def test_unranked_protected_owner_keeps_target_access_order() -> None:
    problem = make_problem(
        (
            ("A", "调梁棚", 1, "调梁棚"),
            ("B", "调梁棚", 2, "调梁棚"),
            ("C", "调梁棚", 3, "调梁棚"),
        ),
        loco="调梁棚",
    )
    generator = SourceWindowGenerator(problem, OperationTransitions(problem))

    assert problem.owner_order("调梁棚") == ("A", "B", "C")
    assert generator.owner_stack_rank_by_no("调梁棚") == {
        "A": 1,
        "B": 2,
        "C": 3,
    }


def test_target_ranks_pull_the_full_dirty_target_before_repacking() -> None:
    problem = make_problem(
        (
            ("A", "调梁棚", 1, "调梁棚"),
            ("OUT", "调梁棚", 2, "存1线"),
            ("B", "调梁棚", 3, "调梁棚"),
        ),
        loco="调梁棚",
    )

    assert problem.final_rank_by_no["A"] > 0
    assert problem.final_rank_by_no["B"] > 0


@pytest.mark.parametrize(
    ("case_id", "date_code", "line", "holdouts"),
    (
        ("0128W", "20260128W", "洗罐站", 1),
        ("0203W", "20260203W", "调梁棚", 1),
    ),
)
def test_capacity_holdout_is_selected_before_search(
    case_id: str,
    date_code: str,
    line: str,
    holdouts: int,
) -> None:
    solver = exact_stage4_solver(case_id, date_code)

    assert solver.scope.infeasible_lines == {line}
    assert solver.scope.capacity_holdout_count_by_line == {line: holdouts}
    assert len(solver.scope.infeasible_nos) == holdouts
    held_length = sum(
        physical.car_length(solver.problem.by_no[no])
        for no in solver.scope.infeasible_nos
    )
    assert held_length >= solver.scope.capacity_overflow_by_line[line]


@pytest.mark.parametrize(
    ("case_id", "date_code", "hook_ceiling"),
    (
        ("0127Z", "20260127Z", 18),
        ("0205Z", "20260205Z", 26),
        ("0209W", "20260209W", 35),
    ),
)
def test_representative_cases_complete_and_replay_cleanly(
    case_id: str,
    date_code: str,
    hook_ceiling: int,
) -> None:
    result = exact_stage4_solver(case_id, date_code).solve()
    summary = result["summary"]

    assert summary["status"] == "complete", summary["blocking_reasons"]
    assert summary["business_hooks"] <= hook_ceiling
    assert summary["replay_physical_ok"]
    assert summary["combined_replay_physical_ok"]
    assert summary["unrecovered_lease_count"] == 0
    assert rv.replay(result["stage4_request"], result["response"])[1] == []
