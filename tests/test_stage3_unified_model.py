from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from stage3_simple.placement import (  # noqa: E402
    Atom,
    CarDomain,
    Problem,
    solve as solve_placements,
)
from stage3_simple.solve import Stage3Solver  # noqa: E402


EMPTY_STAGE2 = {"Data": {"Operations": []}}


def car(
    no: str,
    line: str,
    position: int,
    targets: Iterable[str],
    *,
    length: float = 14.3,
    process: str = "段修",
    forced: Iterable[int] = (),
) -> dict[str, Any]:
    return {
        "No": no,
        "Line": line,
        "Position": position,
        "Length": length,
        "RepairProcess": process,
        "TargetLines": list(targets),
        "ForceTargetPosition": list(forced),
        "IsWeigh": False,
        "_Weighed": False,
    }


def request(cars: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "StartStatus": list(cars),
        "TerminalLines": [
            {"Line": f"修{index}库内", "IsInspectionMode": False}
            for index in range(1, 5)
        ],
        "locoNode": {"Line": "存4线", "End": "North"},
    }


def solve(cars: Iterable[dict[str, Any]], *, case_id: str) -> dict[str, Any]:
    return Stage3Solver(
        case_id,
        request(cars),
        EMPTY_STAGE2,
        time_budget_seconds=30,
    ).solve()


def generated_projection(result: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {
        row["No"]: (row["Line"], int(row["Position"]))
        for row in result["response"]["Data"]["GeneratedEndStatus"]
    }


def planned_projection(result: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {
        row["no"]: (row["assigned_line"], int(row["assigned_position"]))
        for row in result["assignment_plan"]
        if row["assigned_line"] and row["assigned_position"] is not None
    }


def put_projection(result: dict[str, Any]) -> dict[str, tuple[str, int]]:
    projection: dict[str, tuple[str, int]] = {}
    for operation in result["response"]["Data"]["Operations"]:
        if operation["Action"] != "Put":
            continue
        for no, position in (operation.get("Positions") or {}).items():
            projection[no] = (operation["Line"], int(position))
    return projection


def assert_complete_and_replay_clean(result: dict[str, Any]) -> None:
    assert result["summary"]["status"] == "complete"
    assert rv.replay(result["stage3_request"], result["response"])[1] == []
    assert rv.replay(
        result["stage3_request"],
        result["combined_response"],
    )[1] == []


def assert_no_immediate_inverse_cycle(result: dict[str, Any]) -> None:
    operations = result["response"]["Data"]["Operations"]
    for first, second, third in zip(operations, operations[1:], operations[2:]):
        same_block_and_line = (
            first["Line"] == second["Line"] == third["Line"]
            and first["MoveCars"] == second["MoveCars"] == third["MoveCars"]
        )
        assert not (
            same_block_and_line
            and (first["Action"], second["Action"], third["Action"])
            == ("Get", "Put", "Get")
        ), (first, second, third)


def test_mixed_inner_outer_targets_share_one_feasible_domain() -> None:
    result = solve(
        [
            car("A", "机走北", 1, ["修1库内"], forced=[1]),
            car(
                "B",
                "机走北",
                2,
                ["修1库内", "修2库外"],
                forced=[1],
            ),
        ],
        case_id="MIXED_TARGETS",
    )

    assert_complete_and_replay_clean(result)
    assert result["summary"]["business_hooks"] == 3
    assert generated_projection(result) == {
        "A": ("修1库内", 1),
        "B": ("修2库外", 1),
    }
    assert_no_immediate_inverse_cycle(result)


def test_hall_subset_conflict_returns_a_checkable_witness() -> None:
    domains = (
        CarDomain(
            no="A",
            length=143,
            process="段修",
            atoms=(Atom("inner", "修1库内", 1),),
        ),
        CarDomain(
            no="B",
            length=143,
            process="段修",
            atoms=(Atom("inner", "修1库内", 1),),
        ),
        CarDomain(
            no="C",
            length=143,
            process="段修",
            atoms=tuple(Atom("inner", "修1库内", position) for position in (1, 2, 3)),
        ),
    )
    problem = Problem(
        cars=domains,
        inner_capacities=(("修1库内", 5),),
        outer_capacities=(("修1库外", 493),),
    )

    result = solve_placements(problem, max_plans=8, node_budget=1_000)

    assert result.complete
    assert not result.budget_exhausted
    assert result.plans == ()
    assert result.reason == "inner_hall_infeasible"
    witness = result.hall_witness
    assert witness is not None
    assert witness.cars == ("A", "B")
    assert witness.slots == (("修1库内", 1),)
    assert witness.deficit == len(witness.cars) - len(witness.slots) == 1

    atoms_by_no = {domain.no: set(domain.atoms) for domain in domains}
    neighbor_slots = {
        (atom.line, atom.position)
        for no in witness.cars
        for atom in atoms_by_no[no]
        if atom.kind == "inner" and atom.position is not None
    }
    assert neighbor_slots == set(witness.slots)


def test_bounded_frontier_reports_omitted_equal_score_placements() -> None:
    domains = tuple(
        CarDomain(
            no=no,
            length=143,
            process="段修",
            atoms=tuple(
                Atom("inner", "修1库内", position)
                for position in range(1, 5)
            ),
        )
        for no in ("A", "B", "C", "D")
    )
    problem = Problem(
        cars=domains,
        inner_capacities=(("修1库内", 5),),
        outer_capacities=(("修1库外", 493),),
    )

    bounded = solve_placements(problem, max_plans=8, node_budget=1_000)
    exhaustive = solve_placements(problem, max_plans=32, node_budget=1_000)

    assert bounded.complete
    assert bounded.frontier_truncated
    assert len(bounded.plans) == 8
    assert exhaustive.complete
    assert not exhaustive.frontier_truncated
    assert len(exhaustive.plans) == 24
    assert set(bounded.plans) <= set(exhaustive.plans)
    assert {plan.score for plan in bounded.plans} == {exhaustive.plans[0].score}
    assert bounded.plans == solve_placements(
        problem,
        max_plans=8,
        node_budget=1_000,
    ).plans


def test_sparse_inner_slots_form_one_direct_put_block() -> None:
    result = solve(
        [
            car("A", "机走北", 1, ["修1库内"], forced=[1]),
            car("B", "机走北", 2, ["修1库内"], forced=[4]),
        ],
        case_id="SPARSE_BLOCK",
    )

    assert_complete_and_replay_clean(result)
    operations = result["response"]["Data"]["Operations"]
    puts = [operation for operation in operations if operation["Action"] == "Put"]
    assert result["summary"]["business_hooks"] == 2
    assert len(puts) == 1
    assert puts[0]["Line"] == "修1库内"
    assert puts[0]["MoveCars"] == ["A", "B"]
    assert puts[0]["Positions"] == {"A": 1, "B": 4}
    assert result["summary"]["infeasibility_certificates"] == []
    assert_no_immediate_inverse_cycle(result)


def test_assignment_positions_are_the_execution_contract() -> None:
    result = solve(
        [car("A", "机走北", 1, ["修1库内"], forced=[1, 2])],
        case_id="ASSIGNMENT_CONTRACT",
    )

    assert_complete_and_replay_clean(result)
    generated = generated_projection(result)
    planned = planned_projection(result)
    emitted = put_projection(result)
    assert planned["A"] == generated["A"] == emitted["A"]
    assert_no_immediate_inverse_cycle(result)


def test_deferred_staging_cost_reserves_an_active_inner_gate() -> None:
    solver = Stage3Solver(
        "GATE_RESERVATION",
        request([
            car("A", "机走北", 1, ["修1库内"], forced=[1]),
            car("D", "修1库内", 4, ["油漆线"], process="厂修"),
        ]),
        EMPTY_STAGE2,
        time_budget_seconds=5,
    )

    problem, _fixed = solver.build_placement_problem("B")
    deferred = next(domain for domain in problem.cars if domain.no == "D")
    cost_by_line = {atom.line: atom.cost for atom in deferred.atoms}

    assert {len(atom.cost) for domain in problem.cars for atom in domain.atoms} == {2}
    assert cost_by_line["修1库外"][0] == 1
    assert cost_by_line["修4库外"][0] == 0


def test_solver_does_not_emit_get_put_get_inverse_cycle() -> None:
    # This is a self-contained extraction of the former 0416W cycle.  The old
    # cost candidate retrieves the same three-car block from 修2库外, puts it
    # back unchanged, and immediately retrieves it again.
    cars = [
        car("1680130", "机走北", 1, ["修2库内"], forced=[5]),
        car("5450448", "机走棚", 1, ["修3库内"], forced=[2]),
        car("4872341", "机走棚", 2, ["修3库内"], length=13.2, forced=[3]),
        car("1787562", "机走棚", 3, ["修2库内"], forced=[1]),
        car("5278702", "机走棚", 4, ["修2库内"], forced=[2]),
        car("5337940", "机走棚", 5, ["修2库内"], length=13.2, forced=[3]),
        car("1775198", "机走棚", 6, ["修2库内"], forced=[4]),
        car("5314200", "机南", 1, ["修1库内"], length=13.2, process="厂修", forced=[5]),
        car("5777571", "机南", 2, ["修1库内"], length=13.2, forced=[1]),
        car("5492555", "机南", 3, ["修1库内"], forced=[2]),
        car("5327824", "机南", 4, ["修1库内"], length=13.2, forced=[3]),
        car("5246606", "机南", 5, ["修1库外"], length=13.2, process="厂修"),
        car("5245004", "机南", 6, ["修1库外"], length=13.2, process="厂修"),
        car("4870027", "修2库内", 5, ["油漆线"], length=13.2),
        car("5240607", "卸轮线", 1, ["油漆线"], length=13.2, process="厂修"),
        car("5249598", "卸轮线", 2, ["油漆线"], length=13.2, process="厂修"),
        car("4922868", "卸轮线", 3, ["卸轮线"], length=13.2, process="其他"),
    ]

    result = solve(cars, case_id="NO_INVERSE_CYCLE")

    assert_complete_and_replay_clean(result)
    assert_no_immediate_inverse_cycle(result)
