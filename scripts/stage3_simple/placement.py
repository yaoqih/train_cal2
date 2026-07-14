"""Finite-domain terminal placement solver for Stage 3.

The module deliberately has no dependency on ``stage3_simple.solve``.  Callers
describe every legal terminal alternative as an :class:`Atom`; this keeps
mixed inner/outer targets in one domain instead of selecting a target class in
advance.  The solver enforces the shared placement constraints and returns a
bounded set of lowest-score deterministic representatives.

Lengths are integer units chosen by the caller (for example decimetres).  Base
outer loads must contain fixed cars only.  Restoration cars belong in
``Problem.cars`` with ``restoration_line`` set, so their length is reserved by
the same constraint as every other assigned car.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
from dataclasses import dataclass
from typing import Literal, TypeAlias


TerminalKind: TypeAlias = Literal["inner", "outer"]
Score: TypeAlias = tuple[int, ...]
InnerSlot: TypeAlias = tuple[str, int]
Choices: TypeAlias = tuple[tuple[int, str, int, tuple["Atom", ...]], ...]
ZeroStagingAtom: TypeAlias = tuple[TerminalKind, str, int | None]
ZeroStagingKey: TypeAlias = tuple[tuple[tuple[ZeroStagingAtom, ...], ...], ...]


@dataclass(frozen=True, slots=True)
class Atom:
    """One legal terminal alternative for one car.

    ``position`` is mandatory for inner atoms.  It is optional for outer atoms:
    a value represents a Force position, while ``None`` leaves final compact
    numbering to the execution layer.  ``cost`` is an additive non-negative
    multi-objective vector.
    """

    kind: TerminalKind
    line: str
    position: int | None = None
    cost: Score = (0,)


@dataclass(frozen=True, slots=True)
class CarDomain:
    """Finite placement domain and immutable business attributes for one car."""

    no: str
    length: int
    process: str
    atoms: tuple[Atom, ...]
    restoration_line: str | None = None
    restoration_position: int | None = None


@dataclass(frozen=True, slots=True)
class Problem:
    """Complete placement problem expressed without operational search state.

    ``inner_fixed_positions`` establishes both occupied slots and the locked
    access frontier.  A movable car may only use a position shallower than the
    first fixed position on that line.  ``inner_fixed_factory_positions`` is a
    subset used to initialize the section-before-factory ordering frontier.
    """

    cars: tuple[CarDomain, ...]
    inner_capacities: tuple[tuple[str, int], ...]
    outer_capacities: tuple[tuple[str, int], ...]
    inner_fixed_positions: tuple[InnerSlot, ...] = ()
    inner_fixed_factory_positions: tuple[InnerSlot, ...] = ()
    outer_base_loads: tuple[tuple[str, int], ...] = ()
    outer_fixed_positions: tuple[InnerSlot, ...] = ()
    exposure_segments: tuple[tuple[str, ...], ...] = ()
    factory_positions: tuple[int, ...] = (4, 5)

    def __post_init__(self) -> None:
        _validate_problem(self)


@dataclass(frozen=True, slots=True)
class Plan:
    """One feasible placement and its exact additive objective vector."""

    assignments: tuple[tuple[str, Atom], ...]
    score: Score

    @property
    def signature(self) -> tuple[tuple[str, str, str, int], ...]:
        return tuple(
            (
                no,
                atom.kind,
                atom.line,
                atom.position if atom.position is not None else -1,
            )
            for no, atom in self.assignments
        )

    @property
    def execution_signature(self) -> tuple[tuple[str, str, str, int], ...]:
        """Return a coarse ordering key used only to diversify scheduling.

        Absolute inner positions are deliberately omitted from this key, but
        that does *not* make two plans interchangeable: free headroom can
        change which temporary depot placements are executable.  Feasibility
        dominance therefore always uses :attr:`signature` instead.
        """

        inner_rank: dict[str, int] = {}
        by_line: dict[str, list[tuple[int, str]]] = {}
        for no, atom in self.assignments:
            if atom.kind == "inner" and atom.position is not None:
                by_line.setdefault(atom.line, []).append((atom.position, no))
        for rows in by_line.values():
            for rank, (_position, no) in enumerate(sorted(rows), start=1):
                inner_rank[no] = rank
        return tuple(
            (
                no,
                atom.kind,
                atom.line,
                inner_rank[no]
                if atom.kind == "inner"
                else atom.position
                if atom.position is not None
                else -1,
            )
            for no, atom in self.assignments
        )

    def atom_by_no(self) -> dict[str, Atom]:
        return dict(self.assignments)


@dataclass(frozen=True, slots=True)
class HallWitness:
    """Checkable deficient-set witness for forced-inner slot infeasibility."""

    cars: tuple[str, ...]
    slots: tuple[InnerSlot, ...]
    deficit: int


@dataclass(frozen=True, slots=True)
class OuterCapacityWitness:
    """Weighted deficient-set witness for a subset of outer lines.

    ``capacity`` is residual capacity after ``Problem.outer_base_loads``.
    Every listed car has no legal inner alternative and its complete outer-line
    domain is contained in ``lines``.  The witness is therefore independently
    checkable as ``demand > capacity``.
    """

    cars: tuple[str, ...]
    lines: tuple[str, ...]
    demand: int
    capacity: int
    deficit: int


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Bounded placement result.

    ``complete`` means the search space was exhausted or the retained score
    prefix was proved by score-bound pruning.  It is false when ``node_budget``
    stopped the search.  ``frontier_truncated`` means the full feasible set was
    not enumerated because ``max_plans`` dropped candidates or enabled a
    score-bound prune.  Once the portfolio is full, equal-score layouts are
    deterministic representatives under the complete readiness score.
    """

    plans: tuple[Plan, ...]
    explored_nodes: int
    complete: bool
    budget_exhausted: bool
    frontier_truncated: bool
    hall_witness: HallWitness | None = None
    reason: str = ""
    outer_capacity_witness: OuterCapacityWitness | None = None
    lower_bound: Score | None = None


@dataclass(slots=True)
class _State:
    assignments: dict[int, Atom]
    inner_used: dict[str, set[int]]
    factory_min: dict[str, int]
    section_max: dict[str, int]
    outer_load: dict[str, int]
    outer_used_positions: dict[str, set[int]]
    score: Score


def solve(
    problem: Problem,
    *,
    max_plans: int = 8,
    node_budget: int,
) -> SolveResult:
    """Return up to ``max_plans`` deterministically ranked feasible placements.

    A bounded descent on the same admissible ordering seeds at most
    ``max_plans`` distinct feasible leaves and stops as soon as that portfolio
    is full.  OPEN then improves it, while a second heap tracks the minimum
    full bound as the sole top-k termination certificate.  Expensive
    zero-staging refinement is lazy.  There is one constraint tree, one node
    budget and no failure-driven alternate solver.
    """

    if max_plans <= 0:
        raise ValueError("max_plans_must_be_positive")
    if node_budget <= 0:
        raise ValueError("node_budget_must_be_positive")

    score_dimensions = _score_dimensions(problem)
    inner_capacities = dict(problem.inner_capacities)
    outer_capacities = dict(problem.outer_capacities)
    inner_fixed = _positions_by_line(problem.inner_fixed_positions)
    outer_fixed = _positions_by_line(problem.outer_fixed_positions)
    inner_frontier = {
        line: min(inner_fixed.get(line, ()), default=capacity + 1)
        for line, capacity in inner_capacities.items()
    }
    fixed_factory = _positions_by_line(problem.inner_fixed_factory_positions)

    root_outer_load = {line: 0 for line in outer_capacities}
    root_outer_load.update(problem.outer_base_loads)
    overfull = sorted(
        line
        for line, load in root_outer_load.items()
        if load > outer_capacities[line]
    )
    if overfull:
        return SolveResult(
            plans=(),
            explored_nodes=0,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
            reason="outer_base_capacity_infeasible:" + ",".join(overfull),
        )

    root_hall = _root_inner_hall_witness(
        problem,
        inner_frontier,
        inner_fixed,
        fixed_factory,
        outer_fixed,
    )
    root_outer_capacity = _root_outer_capacity_witness(
        problem,
        inner_frontier,
        inner_fixed,
        fixed_factory,
        outer_fixed,
        outer_capacities,
        root_outer_load,
    )
    if root_hall is not None or root_outer_capacity is not None:
        reason = (
            "root_placement_infeasible"
            if root_hall is not None and root_outer_capacity is not None
            else "inner_hall_infeasible"
            if root_hall is not None
            else "outer_subset_capacity_infeasible"
        )
        return SolveResult(
            plans=(),
            explored_nodes=0,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
            hall_witness=root_hall,
            reason=reason,
            outer_capacity_witness=root_outer_capacity,
        )

    root_state = _State(
        assignments={},
        inner_used={line: set(inner_fixed.get(line, ())) for line in inner_capacities},
        factory_min={
            line: min(fixed_factory.get(line, ()), default=inner_capacities[line] + 1)
            for line in inner_capacities
        },
        section_max={line: 0 for line in inner_capacities},
        outer_load=root_outer_load,
        outer_used_positions={line: set(outer_fixed.get(line, ())) for line in outer_capacities},
        score=(0,) * score_dimensions,
    )

    plans: list[Plan] = []
    explored_nodes = 0
    budget_exhausted = False
    frontier_truncated = False
    zero_staging_cache: dict[ZeroStagingKey, int | None] = {}
    active_bounds: dict[int, Score] = {}
    bound_frontier: list[tuple[Score, int]] = []
    reverse_exposure_rank = {
        no: rank
        for segment in problem.exposure_segments
        for rank, no in enumerate(reversed(segment))
    }

    def selected_choice(choices: Choices) -> tuple[int, str, int, tuple[Atom, ...]]:
        return min(
            choices,
            key=lambda item: (
                0 if item[0] == 1 else 1,
                reverse_exposure_rank.get(item[1], len(problem.cars)),
                item[0],
                item[1],
            ),
        )
    def analyze(
        current: _State,
    ) -> tuple[Score, Choices] | None:
        remaining = [
            index
            for index in range(len(problem.cars))
            if index not in current.assignments
        ]
        choices: list[tuple[int, str, int, tuple[Atom, ...]]] = []
        for index in remaining:
            car = problem.cars[index]
            atoms = _available_atoms(
                problem,
                car,
                current,
                inner_frontier,
                inner_capacities,
                outer_capacities,
            )
            if not atoms:
                return None
            choices.append((len(atoms), car.no, index, atoms))
        packed_choices = tuple(choices)
        if not _remaining_domains_feasible(
            problem,
            current,
            packed_choices,
            outer_capacities,
        ):
            return None
        return (
            _state_score_lower_bound(problem, current, packed_choices),
            packed_choices,
        )

    root = analyze(root_state)

    primal_node_limit = min(node_budget, 256)

    def seed_candidates(current: _State, choices: Choices) -> bool:
        nonlocal explored_nodes, plans
        if explored_nodes >= primal_node_limit:
            return False
        explored_nodes += 1
        if not choices:
            plans, _truncated = _insert_frontier(
                plans,
                _plan_from_state(problem, current),
                max_plans=max_plans,
            )
            return len(plans) >= max_plans

        _domain_size, _no, selected, atoms = selected_choice(choices)
        car = problem.cars[selected]
        children: list[
            tuple[Score, tuple[Score, str, str, int], _State, Choices]
        ] = []
        for atom in atoms:
            next_state = _apply_atom(current, selected, car, atom)
            analyzed = analyze(next_state)
            if analyzed is None:
                continue
            child_bound, child_choices = analyzed
            children.append((
                child_bound,
                _atom_sort_key(atom),
                next_state,
                child_choices,
            ))
        for _bound, _atom_key, next_state, child_choices in sorted(
            children,
            key=lambda item: (item[0], item[1]),
        ):
            if seed_candidates(next_state, child_choices):
                return True
        return False

    if root is not None:
        _root_bound, root_choices = root
        seed_candidates(root_state, root_choices)

    search: list[
        tuple[int, int, int, Score, int, Score, bool, _State, Choices]
    ] = []
    serial = itertools.count()

    def enqueue(
        lower_bound: Score,
        *,
        depth: int,
        serial_id: int,
        refined: bool,
        state: _State,
        choices: Choices,
    ) -> None:
        heapq.heappush(
            search,
            (
                lower_bound[0],
                lower_bound[1],
                -depth,
                lower_bound[2:],
                serial_id,
                lower_bound,
                refined,
                state,
                choices,
            ),
        )
        active_bounds[serial_id] = lower_bound
        heapq.heappush(bound_frontier, (lower_bound, serial_id))

    if root is not None:
        root_bound, root_choices = root
        enqueue(
            root_bound,
            depth=0,
            serial_id=next(serial),
            refined=False,
            state=root_state,
            choices=root_choices,
        )

    while search:
        while (
            bound_frontier
            and active_bounds.get(bound_frontier[0][1]) != bound_frontier[0][0]
        ):
            heapq.heappop(bound_frontier)
        if (
            len(plans) >= max_plans
            and bound_frontier
            and bound_frontier[0][0] >= plans[-1].score
        ):
            frontier_truncated = True
            break
        if explored_nodes >= node_budget:
            budget_exhausted = True
            break

        (
            _hook_bound,
            _staged_bound,
            negative_depth,
            _additive_bound,
            serial_id,
            lower_bound,
            refined,
            current,
            choices,
        ) = heapq.heappop(search)
        if len(plans) >= max_plans and lower_bound >= plans[-1].score:
            active_bounds.pop(serial_id)
            frontier_truncated = True
            continue
        if (
            choices
            and lower_bound[1] == 0
            and not refined
        ):
            refined_bound = _state_score_lower_bound(
                problem,
                current,
                choices,
                refine_zero_staging=True,
                zero_staging_cache=zero_staging_cache,
            )
            if refined_bound < lower_bound:
                raise AssertionError("zero_staging_refinement_lowered_bound")
            if refined_bound > lower_bound:
                enqueue(
                    refined_bound,
                    depth=-negative_depth,
                    serial_id=serial_id,
                    refined=True,
                    state=current,
                    choices=choices,
                )
                continue
        active_bounds.pop(serial_id)

        explored_nodes += 1
        if not choices:
            plan = _plan_from_state(problem, current)
            plans, truncated = _insert_frontier(plans, plan, max_plans=max_plans)
            frontier_truncated = frontier_truncated or truncated
            continue

        _domain_size, _no, selected, atoms = selected_choice(choices)
        car = problem.cars[selected]
        for atom in atoms:
            next_state = _apply_atom(current, selected, car, atom)
            analyzed = analyze(next_state)
            if analyzed is None:
                continue
            child_bound, child_choices = analyzed
            if len(plans) >= max_plans and child_bound >= plans[-1].score:
                frontier_truncated = True
                continue
            enqueue(
                child_bound,
                depth=len(next_state.assignments),
                serial_id=next(serial),
                refined=False,
                state=next_state,
                choices=child_choices,
            )

    while (
        bound_frontier
        and active_bounds.get(bound_frontier[0][1]) != bound_frontier[0][0]
    ):
        heapq.heappop(bound_frontier)
    certified_bounds = [plan.score for plan in plans]
    if bound_frontier:
        certified_bounds.append(bound_frontier[0][0])
    placement_lower_bound = min(certified_bounds, default=None)

    ordered_plans = tuple(sorted(plans, key=lambda plan: (plan.score, plan.signature)))
    reason = ""
    if budget_exhausted:
        reason = "placement_node_budget_exhausted"
    elif not ordered_plans:
        reason = "placement_infeasible"
    return SolveResult(
        plans=ordered_plans,
        explored_nodes=explored_nodes,
        complete=not budget_exhausted,
        budget_exhausted=budget_exhausted,
        frontier_truncated=frontier_truncated,
        reason=reason,
        lower_bound=placement_lower_bound,
    )


def _validate_problem(problem: Problem) -> None:
    inner = _unique_non_negative_map("inner_capacity", problem.inner_capacities, positive=True)
    outer = _unique_non_negative_map("outer_capacity", problem.outer_capacities, positive=True)
    _unique_non_negative_map("outer_base_load", problem.outer_base_loads)
    if not inner:
        raise ValueError("inner_capacities_empty")
    if not outer:
        raise ValueError("outer_capacities_empty")

    fixed_inner = _positions_by_line(problem.inner_fixed_positions)
    fixed_factory = _positions_by_line(problem.inner_fixed_factory_positions)
    for line, positions in fixed_inner.items():
        if line not in inner:
            raise ValueError(f"unknown_fixed_inner_line:{line}")
        if any(position < 1 or position > inner[line] for position in positions):
            raise ValueError(f"fixed_inner_position_out_of_range:{line}")
        if len(positions) != len(set(positions)):
            raise ValueError(f"fixed_inner_position_collision:{line}")
    for line, positions in fixed_factory.items():
        if any(position not in set(fixed_inner.get(line, ())) for position in positions):
            raise ValueError(f"fixed_factory_position_not_fixed:{line}")

    fixed_outer = _positions_by_line(problem.outer_fixed_positions)
    for line, positions in fixed_outer.items():
        if line not in outer:
            raise ValueError(f"unknown_fixed_outer_line:{line}")
        if any(position <= 0 for position in positions):
            raise ValueError(f"fixed_outer_position_invalid:{line}")
        if len(positions) != len(set(positions)):
            raise ValueError(f"fixed_outer_position_collision:{line}")

    base_loads = dict(problem.outer_base_loads)
    if any(line not in outer for line in base_loads):
        raise ValueError("unknown_outer_base_load_line")
    if any(load < 0 for load in base_loads.values()):
        raise ValueError("outer_base_load_negative")

    car_nos = [car.no for car in problem.cars]
    if any(not no for no in car_nos) or len(car_nos) != len(set(car_nos)):
        raise ValueError("duplicate_or_empty_car_no")
    exposed = [no for segment in problem.exposure_segments for no in segment]
    if len(exposed) != len(set(exposed)):
        raise ValueError("duplicate_exposure_car")
    if set(exposed) - set(car_nos):
        raise ValueError("unknown_exposure_car")
    dimensions: int | None = None
    for car in problem.cars:
        if car.length <= 0:
            raise ValueError(f"car_length_invalid:{car.no}")
        if not car.atoms:
            raise ValueError(f"car_domain_empty:{car.no}")
        if car.restoration_position is not None and car.restoration_line is None:
            raise ValueError(f"restoration_position_without_line:{car.no}")
        if len(car.atoms) != len(set(car.atoms)):
            raise ValueError(f"duplicate_car_atom:{car.no}")
        for atom in car.atoms:
            if atom.kind == "inner":
                if atom.line not in inner:
                    raise ValueError(f"unknown_inner_atom_line:{car.no}:{atom.line}")
                if atom.position is None:
                    raise ValueError(f"inner_atom_position_missing:{car.no}:{atom.line}")
            elif atom.kind == "outer":
                if atom.line not in outer:
                    raise ValueError(f"unknown_outer_atom_line:{car.no}:{atom.line}")
                if atom.position is not None and atom.position <= 0:
                    raise ValueError(f"outer_atom_position_invalid:{car.no}:{atom.line}")
            else:
                raise ValueError(f"unknown_atom_kind:{car.no}:{atom.kind}")
            if not atom.cost or any(value < 0 for value in atom.cost):
                raise ValueError(f"atom_cost_invalid:{car.no}")
            if dimensions is None:
                dimensions = len(atom.cost)
            elif len(atom.cost) != dimensions:
                raise ValueError("atom_cost_dimension_mismatch")

    if not problem.factory_positions or any(position <= 0 for position in problem.factory_positions):
        raise ValueError("factory_positions_invalid")


def _unique_non_negative_map(
    label: str,
    rows: tuple[tuple[str, int], ...],
    *,
    positive: bool = False,
) -> dict[str, int]:
    keys = [key for key, _value in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label}_duplicate")
    if any(not key for key in keys):
        raise ValueError(f"{label}_line_empty")
    if any(value <= 0 if positive else value < 0 for _key, value in rows):
        raise ValueError(f"{label}_invalid")
    return dict(rows)


def _score_dimensions(problem: Problem) -> int:
    return len(problem.cars[0].atoms[0].cost) if problem.cars else 1


def _positions_by_line(rows: tuple[InnerSlot, ...]) -> dict[str, tuple[int, ...]]:
    by_line: dict[str, list[int]] = {}
    for line, position in rows:
        by_line.setdefault(line, []).append(position)
    return {line: tuple(sorted(positions)) for line, positions in by_line.items()}


def _process_kind(process: str) -> str:
    normalized = process.strip().lower()
    if normalized.startswith("厂") or normalized.startswith("factory"):
        return "factory"
    if normalized.startswith("段") or normalized.startswith("section"):
        return "section"
    return "other"


def _available_atoms(
    problem: Problem,
    car: CarDomain,
    state: _State,
    inner_frontier: dict[str, int],
    inner_capacities: dict[str, int],
    outer_capacities: dict[str, int],
) -> tuple[Atom, ...]:
    available: list[Atom] = []
    process = _process_kind(car.process)
    factory_positions = set(problem.factory_positions)
    for atom in car.atoms:
        if car.restoration_line is not None and atom.line != car.restoration_line:
            continue
        if car.restoration_position is not None and atom.position != car.restoration_position:
            continue
        if atom.kind == "inner":
            position = atom.position
            if position is None or position > inner_capacities[atom.line]:
                continue
            if position >= inner_frontier[atom.line] or position in state.inner_used[atom.line]:
                continue
            if process == "factory":
                if position not in factory_positions:
                    continue
                if state.section_max[atom.line] >= min(state.factory_min[atom.line], position):
                    continue
            elif process == "section" and position >= state.factory_min[atom.line]:
                continue
        else:
            if state.outer_load[atom.line] + car.length > outer_capacities[atom.line]:
                continue
            if atom.position is not None and atom.position in state.outer_used_positions[atom.line]:
                continue
        available.append(atom)
    return tuple(sorted(available, key=_atom_sort_key))


def _atom_sort_key(atom: Atom) -> tuple[Score, str, str, int]:
    return (
        atom.cost,
        atom.kind,
        atom.line,
        atom.position if atom.position is not None else -1,
    )


def _apply_atom(state: _State, index: int, car: CarDomain, atom: Atom) -> _State:
    assignments = dict(state.assignments)
    assignments[index] = atom
    inner_used = {line: set(positions) for line, positions in state.inner_used.items()}
    factory_min = dict(state.factory_min)
    section_max = dict(state.section_max)
    outer_load = dict(state.outer_load)
    outer_used = {
        line: set(positions) for line, positions in state.outer_used_positions.items()
    }
    if atom.kind == "inner":
        position = atom.position
        if position is None:
            raise ValueError("inner_atom_position_missing_during_apply")
        inner_used[atom.line].add(position)
        process = _process_kind(car.process)
        if process == "factory":
            factory_min[atom.line] = min(factory_min[atom.line], position)
        elif process == "section":
            section_max[atom.line] = max(section_max[atom.line], position)
    else:
        outer_load[atom.line] += car.length
        if atom.position is not None:
            outer_used[atom.line].add(atom.position)
    return _State(
        assignments=assignments,
        inner_used=inner_used,
        factory_min=factory_min,
        section_max=section_max,
        outer_load=outer_load,
        outer_used_positions=outer_used,
        score=_add_scores(state.score, atom.cost),
    )


def _component_min_cost(atoms: tuple[Atom, ...]) -> Score:
    dimensions = len(atoms[0].cost)
    return tuple(min(atom.cost[index] for atom in atoms) for index in range(dimensions))


def _add_scores(left: Score, right: Score) -> Score:
    return tuple(a + b for a, b in zip(left, right))


def _state_score_lower_bound(
    problem: Problem,
    state: _State,
    choices: Choices,
    *,
    refine_zero_staging: bool = False,
    zero_staging_cache: dict[ZeroStagingKey, int | None] | None = None,
) -> Score:
    """Return a lexicographically admissible bound for every completion."""

    assigned_by_no = {
        problem.cars[index].no: atom
        for index, atom in state.assignments.items()
    }
    available_lines_by_no = {
        no: frozenset(atom.line for atom in atoms)
        for _size, no, _index, atoms in choices
    }
    available_atoms_by_no = (
        {
            no: atoms
            for _size, no, _index, atoms in choices
        }
        if refine_zero_staging
        else None
    )
    hook_bound, staged_bound = _exposure_score_lower_bound(
        problem,
        assigned_by_no,
        available_lines_by_no,
        inner_remaining=_remaining_inner_slot_counts(problem, state),
        available_atoms_by_no=available_atoms_by_no,
        zero_staging_cache=zero_staging_cache,
    )
    additive_bound = state.score
    for _size, _no, _index, atoms in choices:
        additive_bound = _add_scores(
            additive_bound,
            _component_min_cost(atoms),
        )
    return (hook_bound, staged_bound, *additive_bound)


def _remaining_domains_feasible(
    problem: Problem,
    state: _State,
    choices: Choices,
    outer_capacities: dict[str, int],
) -> bool:
    """Reject only Hall- or capacity-deficient remaining domains."""

    forced_inner: list[tuple[InnerSlot, ...]] = []
    forced_outer: list[tuple[int, frozenset[str]]] = []
    for _size, _no, index, atoms in choices:
        if all(atom.kind == "inner" for atom in atoms):
            forced_inner.append(tuple(sorted({
                (atom.line, int(atom.position))
                for atom in atoms
                if atom.position is not None
            })))
        elif all(atom.kind == "outer" for atom in atoms):
            forced_outer.append((
                problem.cars[index].length,
                frozenset(atom.line for atom in atoms),
            ))

    slot_owner: dict[InnerSlot, int] = {}

    def augment(car_index: int, seen: set[InnerSlot]) -> bool:
        for slot in forced_inner[car_index]:
            if slot in seen:
                continue
            seen.add(slot)
            owner = slot_owner.get(slot)
            if owner is None or augment(owner, seen):
                slot_owner[slot] = car_index
                return True
        return False

    if any(
        not augment(car_index, set())
        for car_index in range(len(forced_inner))
    ):
        return False

    lines = tuple(sorted(outer_capacities))
    residual = {
        line: outer_capacities[line] - state.outer_load[line]
        for line in lines
    }
    for mask in range(1, 1 << len(lines)):
        subset = frozenset(
            lines[index]
            for index in range(len(lines))
            if mask & (1 << index)
        )
        demand = sum(
            length
            for length, domain in forced_outer
            if domain <= subset
        )
        if demand > sum(residual[line] for line in subset):
            return False
    return True


def _exposure_score_lower_bound(
    problem: Problem,
    assigned_by_no: dict[str, Atom],
    available_lines_by_no: dict[str, frozenset[str]],
    *,
    inner_remaining: dict[str, int] | None = None,
    available_atoms_by_no: dict[str, tuple[Atom, ...]] | None = None,
    zero_staging_cache: dict[ZeroStagingKey, int | None] | None = None,
) -> tuple[int, int]:
    """Relax unfinished exposure chains without undercounting fixed disorder.

    Unassigned cars may choose any currently available line.  Remaining inner
    slot counts constrain the run relaxation, while exact slot identities and
    cross-segment sharing stay relaxed.  Assigned inner positions already form
    a subsequence of every completion.  Its minimum deletions to become
    strictly decreasing cannot be repaired by inserting more values.  The
    primary hook bound charges one shared rehandle pair whenever staging is
    proved necessary; staged-car count remains a separate lexicographic bound.
    A second relaxation asks whether zero additional staging is possible and
    tightens the hook bound when forced positions rule it out.
    """

    terminal_runs = 0
    staged_cars = 0
    for segment in problem.exposure_segments:
        inner_lines = tuple(sorted(inner_remaining or {}))
        line_index = {line: index for index, line in enumerate(inner_lines)}
        run_states: dict[tuple[tuple[int, ...], str], int] = {
            ((0,) * len(inner_lines), ""): 0,
        }
        positions_by_line: dict[str, list[int]] = {}
        for no in segment:
            assigned = assigned_by_no.get(no)
            lines = (
                frozenset({assigned.line})
                if assigned is not None
                else available_lines_by_no[no]
            )
            next_states: dict[tuple[tuple[int, ...], str], int] = {}
            for (counts, prior_line), runs in run_states.items():
                for line in lines:
                    next_counts = counts
                    index = line_index.get(line)
                    if assigned is None and index is not None:
                        if counts[index] >= (inner_remaining or {})[line]:
                            continue
                        mutable_counts = list(counts)
                        mutable_counts[index] += 1
                        next_counts = tuple(mutable_counts)
                    key = (next_counts, line)
                    candidate = runs + (line != prior_line)
                    if candidate < next_states.get(key, 10**9):
                        next_states[key] = candidate
            run_states = next_states
            if (
                assigned is not None
                and assigned.kind == "inner"
                and assigned.position is not None
            ):
                positions_by_line.setdefault(assigned.line, []).append(
                    assigned.position
                )
        terminal_runs += min(run_states.values(), default=0)
        staged_cars += sum(
            len(positions) - _longest_strictly_decreasing_length(positions)
            for positions in positions_by_line.values()
        )
    # A staging batch costs one shared Put/Get pair regardless of how many
    # cars it carries.  Keep staged-car count as a secondary quality signal,
    # but make the primary score match the business-hook relaxation.
    hook_bound = terminal_runs + (2 if staged_cars else 0)
    if staged_cars == 0 and available_atoms_by_no is not None:
        key = _zero_staging_key(
            problem,
            assigned_by_no,
            available_atoms_by_no,
        )
        if zero_staging_cache is None or key not in zero_staging_cache:
            zero_staging_runs = _minimum_zero_staging_runs(
                problem,
                assigned_by_no,
                available_atoms_by_no,
            )
            if zero_staging_cache is not None:
                zero_staging_cache[key] = zero_staging_runs
        else:
            zero_staging_runs = zero_staging_cache[key]
        if zero_staging_runs is None:
            staged_cars = 1
            hook_bound = terminal_runs + 2
        else:
            hook_bound = min(zero_staging_runs, terminal_runs + 2)
    return hook_bound, staged_cars


def _zero_staging_key(
    problem: Problem,
    assigned_by_no: dict[str, Atom],
    available_atoms_by_no: dict[str, tuple[Atom, ...]],
) -> ZeroStagingKey:
    """Canonicalize exactly the structural domains observed by the DP."""

    return tuple(
        tuple(
            tuple(sorted({
                (
                    atom.kind,
                    atom.line,
                    atom.position if atom.kind == "inner" else None,
                )
                for atom in (
                    (assigned_by_no[no],)
                    if no in assigned_by_no
                    else available_atoms_by_no[no]
                )
            }))
            for no in segment
        )
        for segment in problem.exposure_segments
    )


def _minimum_zero_staging_runs(
    problem: Problem,
    assigned_by_no: dict[str, Atom],
    available_atoms_by_no: dict[str, tuple[Atom, ...]],
) -> int | None:
    """Minimize runs subject to every inner subsequence being decreasing.

    For one predecessor state and one inner line, every feasible position has
    the same run increment.  The largest feasible position dominates every
    smaller one because it leaves a superset of positions available to the
    remaining decreasing suffix.  Keeping that single transition is exact.
    """

    capacities = dict(problem.inner_capacities)
    inner_lines = tuple(sorted(capacities))
    line_index = {line: index for index, line in enumerate(inner_lines)}
    total_runs = 0
    for segment in problem.exposure_segments:
        structural_options: list[
            tuple[tuple[tuple[str, tuple[int, ...]], ...], tuple[str, ...]]
        ] = []
        for no in segment:
            assigned = assigned_by_no.get(no)
            atoms = (
                (assigned,)
                if assigned is not None
                else available_atoms_by_no[no]
            )
            inner_positions: dict[str, set[int]] = {}
            outer_lines: set[str] = set()
            for atom in atoms:
                if atom.kind == "inner":
                    if atom.position is not None:
                        inner_positions.setdefault(atom.line, set()).add(
                            atom.position
                        )
                else:
                    outer_lines.add(atom.line)
            structural_options.append((
                tuple(
                    (line, tuple(sorted(positions)))
                    for line, positions in sorted(inner_positions.items())
                ),
                tuple(sorted(outer_lines)),
            ))

        states: dict[tuple[tuple[int, ...], str], int] = {
            (tuple(capacities[line] + 1 for line in inner_lines), ""): 0,
        }
        for inner_options, outer_lines in structural_options:
            next_states: dict[tuple[tuple[int, ...], str], int] = {}
            for (last_positions, prior_line), runs in states.items():
                for line in outer_lines:
                    key = (last_positions, line)
                    candidate = runs + (line != prior_line)
                    if candidate < next_states.get(key, 10**9):
                        next_states[key] = candidate
                for line, positions in inner_options:
                    index = line_index[line]
                    cut = bisect.bisect_left(
                        positions,
                        last_positions[index],
                    )
                    if cut == 0:
                        continue
                    mutable_positions = list(last_positions)
                    mutable_positions[index] = positions[cut - 1]
                    next_positions = tuple(mutable_positions)
                    key = (next_positions, line)
                    candidate = runs + (line != prior_line)
                    if candidate < next_states.get(key, 10**9):
                        next_states[key] = candidate
            if not next_states:
                return None
            states = next_states
        total_runs += min(states.values(), default=0)
    return total_runs


def _remaining_inner_slot_counts(
    problem: Problem,
    state: _State,
) -> dict[str, int]:
    capacities = dict(problem.inner_capacities)
    fixed = _positions_by_line(problem.inner_fixed_positions)
    return {
        line: sum(
            position not in state.inner_used[line]
            for position in range(
                1,
                min(fixed.get(line, ()), default=capacity + 1),
            )
        )
        for line, capacity in capacities.items()
    }


def _plan_from_state(problem: Problem, state: _State) -> Plan:
    assignments = tuple(
        sorted(
            (
                (problem.cars[index].no, atom)
                for index, atom in state.assignments.items()
            ),
            key=lambda item: item[0],
        )
    )
    by_no = dict(assignments)
    terminal_runs = 0
    staged_cars = 0
    for segment in problem.exposure_segments:
        prior_line = ""
        positions_by_line: dict[str, list[int]] = {}
        for no in segment:
            atom = by_no[no]
            if atom.line != prior_line:
                terminal_runs += 1
                prior_line = atom.line
            if atom.kind == "inner" and atom.position is not None:
                positions_by_line.setdefault(atom.line, []).append(atom.position)
        staged_cars += sum(
            len(positions) - _longest_strictly_decreasing_length(positions)
            for positions in positions_by_line.values()
        )
    placement_hook_bound = terminal_runs + (2 if staged_cars else 0)
    return Plan(
        assignments=assignments,
        score=(placement_hook_bound, staged_cars, *state.score),
    )


def _insert_frontier(
    frontier: list[Plan],
    candidate: Plan,
    *,
    max_plans: int,
) -> tuple[list[Plan], bool]:
    same_assignment = next(
        (
            plan
            for plan in frontier
            if plan.signature == candidate.signature
        ),
        None,
    )
    if same_assignment is not None:
        if (same_assignment.score, same_assignment.signature) <= (
            candidate.score,
            candidate.signature,
        ):
            return frontier, False
        frontier = [plan for plan in frontier if plan is not same_assignment]
    ranked = sorted((*frontier, candidate), key=lambda plan: (plan.score, plan.signature))
    return ranked[:max_plans], len(ranked) > max_plans


def _longest_strictly_decreasing_length(values: list[int]) -> int:
    if not values:
        return 0
    best = [1] * len(values)
    for right, value in enumerate(values):
        best[right] = 1 + max(
            (best[left] for left in range(right) if values[left] > value),
            default=0,
        )
    return max(best)


def _root_inner_hall_witness(
    problem: Problem,
    inner_frontier: dict[str, int],
    inner_fixed: dict[str, tuple[int, ...]],
    fixed_factory: dict[str, tuple[int, ...]],
    outer_fixed: dict[str, tuple[int, ...]],
) -> HallWitness | None:
    """Return a Hall witness for cars whose root domains are forced inner."""

    forced: list[tuple[str, tuple[InnerSlot, ...]]] = []
    for car in problem.cars:
        atoms = _root_available_atoms(
            problem,
            car,
            inner_frontier,
            inner_fixed,
            fixed_factory,
            outer_fixed,
        )
        if not atoms or any(atom.kind == "outer" for atom in atoms):
            continue
        slots = tuple(sorted({
            (atom.line, int(atom.position))
            for atom in atoms
            if atom.position is not None
        }))
        forced.append((car.no, slots))
    if not forced:
        return None

    adjacency = [slots for _no, slots in forced]
    slot_owner: dict[InnerSlot, int] = {}
    car_slot: dict[int, InnerSlot] = {}

    def augment(car_index: int, seen: set[InnerSlot]) -> bool:
        for slot in adjacency[car_index]:
            if slot in seen:
                continue
            seen.add(slot)
            owner = slot_owner.get(slot)
            if owner is None or augment(owner, seen):
                slot_owner[slot] = car_index
                car_slot[car_index] = slot
                return True
        return False

    for index in range(len(forced)):
        augment(index, set())
    if len(car_slot) == len(forced):
        return None

    reachable_cars = {index for index in range(len(forced)) if index not in car_slot}
    reachable_slots: set[InnerSlot] = set()
    pending = list(reachable_cars)
    while pending:
        car_index = pending.pop()
        matched = car_slot.get(car_index)
        for slot in adjacency[car_index]:
            if slot == matched or slot in reachable_slots:
                continue
            reachable_slots.add(slot)
            owner = slot_owner.get(slot)
            if owner is not None and owner not in reachable_cars:
                reachable_cars.add(owner)
                pending.append(owner)

    cars = tuple(sorted(forced[index][0] for index in reachable_cars))
    slots = tuple(sorted(reachable_slots))
    return HallWitness(cars=cars, slots=slots, deficit=len(cars) - len(slots))


def _root_outer_capacity_witness(
    problem: Problem,
    inner_frontier: dict[str, int],
    inner_fixed: dict[str, tuple[int, ...]],
    fixed_factory: dict[str, tuple[int, ...]],
    outer_fixed: dict[str, tuple[int, ...]],
    outer_capacities: dict[str, int],
    outer_base_load: dict[str, int],
) -> OuterCapacityWitness | None:
    """Return the strongest deterministic outer-line subset certificate."""

    forced_outer: list[tuple[str, int, frozenset[str]]] = []
    for car in problem.cars:
        atoms = _root_available_atoms(
            problem,
            car,
            inner_frontier,
            inner_fixed,
            fixed_factory,
            outer_fixed,
        )
        if not atoms or any(atom.kind == "inner" for atom in atoms):
            continue
        lines = frozenset(atom.line for atom in atoms if atom.kind == "outer")
        if lines:
            forced_outer.append((car.no, car.length, lines))
    if not forced_outer:
        return None

    outer_lines = tuple(sorted(outer_capacities))
    witnesses: list[OuterCapacityWitness] = []
    for mask in range(1, 1 << len(outer_lines)):
        lines = tuple(
            outer_lines[index]
            for index in range(len(outer_lines))
            if mask & (1 << index)
        )
        line_set = frozenset(lines)
        constrained = tuple(
            sorted(
                (no, length)
                for no, length, domain in forced_outer
                if domain <= line_set
            )
        )
        if not constrained:
            continue
        demand = sum(length for _no, length in constrained)
        capacity = sum(
            outer_capacities[line] - outer_base_load.get(line, 0)
            for line in lines
        )
        if demand <= capacity:
            continue
        witnesses.append(OuterCapacityWitness(
            cars=tuple(no for no, _length in constrained),
            lines=lines,
            demand=demand,
            capacity=capacity,
            deficit=demand - capacity,
        ))
    if not witnesses:
        return None
    return min(
        witnesses,
        key=lambda witness: (
            len(witness.lines),
            -witness.deficit,
            witness.lines,
            witness.cars,
        ),
    )


def _root_available_atoms(
    problem: Problem,
    car: CarDomain,
    inner_frontier: dict[str, int],
    inner_fixed: dict[str, tuple[int, ...]],
    fixed_factory: dict[str, tuple[int, ...]],
    outer_fixed: dict[str, tuple[int, ...]],
) -> tuple[Atom, ...]:
    """Filter only root-local constraints shared by both root witnesses."""

    fixed_inner_sets = {
        line: set(positions) for line, positions in inner_fixed.items()
    }
    fixed_outer_sets = {
        line: set(positions) for line, positions in outer_fixed.items()
    }
    fixed_factory_min = {
        line: min(positions)
        for line, positions in fixed_factory.items()
        if positions
    }
    factory_positions = set(problem.factory_positions)
    process = _process_kind(car.process)
    atoms: list[Atom] = []
    for atom in car.atoms:
        if car.restoration_line is not None and atom.line != car.restoration_line:
            continue
        if car.restoration_position is not None and atom.position != car.restoration_position:
            continue
        if atom.kind == "inner":
            position = atom.position
            if position is None or position >= inner_frontier[atom.line]:
                continue
            if position in fixed_inner_sets.get(atom.line, set()):
                continue
            if process == "factory" and position not in factory_positions:
                continue
            if process == "section" and position >= fixed_factory_min.get(atom.line, 10**9):
                continue
        elif atom.position is not None and atom.position in fixed_outer_sets.get(atom.line, set()):
            continue
        atoms.append(atom)
    return tuple(atoms)


__all__ = [
    "Atom",
    "CarDomain",
    "HallWitness",
    "OuterCapacityWitness",
    "Plan",
    "Problem",
    "SolveResult",
    "solve",
]
