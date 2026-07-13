from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from stage3_simple.placement import Atom, CarDomain, Problem, solve  # noqa: E402


INNER = (("I1", 5),)


def outer_car(
    no: str,
    length: int,
    lines: tuple[str, ...],
    *,
    restoration_line: str | None = None,
    inner_alternative: bool = False,
) -> CarDomain:
    atoms = tuple(Atom("outer", line) for line in lines)
    if inner_alternative:
        atoms = (*atoms, Atom("inner", "I1", 1))
    return CarDomain(
        no=no,
        length=length,
        process="section",
        atoms=atoms,
        restoration_line=restoration_line,
    )


def test_outer_subset_capacity_witness_uses_the_restricted_car_set() -> None:
    problem = Problem(
        cars=tuple(
            outer_car(no, 70, ("O1", "O2"))
            for no in ("A", "B", "C")
        ),
        inner_capacities=INNER,
        outer_capacities=(("O1", 100), ("O2", 100), ("O3", 100)),
    )

    result = solve(problem, node_budget=100)

    witness = result.outer_capacity_witness
    assert result.complete
    assert result.explored_nodes == 0
    assert result.reason == "outer_subset_capacity_infeasible"
    assert witness is not None
    assert witness.cars == ("A", "B", "C")
    assert witness.lines == ("O1", "O2")
    assert witness.demand == 210
    assert witness.capacity == 200
    assert witness.deficit == 10


def test_inner_alternative_excludes_car_from_outer_capacity_demand() -> None:
    problem = Problem(
        cars=(
            outer_car("A", 70, ("O1", "O2")),
            outer_car("B", 70, ("O1", "O2")),
            outer_car("C", 70, ("O1", "O2"), inner_alternative=True),
        ),
        inner_capacities=INNER,
        outer_capacities=(("O1", 100), ("O2", 100)),
    )

    result = solve(problem, node_budget=1_000)

    assert result.complete
    assert result.outer_capacity_witness is None
    assert result.plans
    assert result.plans[0].atom_by_no()["C"].kind == "inner"


def test_restoration_and_base_load_are_both_reserved_in_witness() -> None:
    problem = Problem(
        cars=(
            outer_car("A", 35, ("O1",)),
            outer_car("R", 40, ("O1",), restoration_line="O1"),
        ),
        inner_capacities=INNER,
        outer_capacities=(("O1", 100),),
        outer_base_loads=(("O1", 30),),
    )

    result = solve(problem, node_budget=100)

    witness = result.outer_capacity_witness
    assert witness is not None
    assert witness.cars == ("A", "R")
    assert witness.lines == ("O1",)
    assert witness.demand == 75
    assert witness.capacity == 70
    assert witness.deficit == 5
