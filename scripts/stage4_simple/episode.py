from __future__ import annotations

import time
from dataclasses import dataclass, replace

from solver_vnext import physical

from .search import OperationTransitions, SearchNode, Stage4Problem


@dataclass(frozen=True)
class EpisodeContraction:
    kind: str
    removed_hooks: int
    retained_cars: tuple[str, ...]
    start_hook: int
    end_hook: int


@dataclass(frozen=True)
class EpisodeOptimization:
    node: SearchNode
    contractions: tuple[EpisodeContraction, ...]
    evaluated_paths: int
    label_budget_exhausted: bool = False
    time_budget_exhausted: bool = False


@dataclass(frozen=True)
class _PairedProjection:
    steps: tuple[physical.PlanStep, ...]
    fixed_positions: tuple[
        tuple[int, tuple[tuple[str, int], ...]], ...
    ] = ()


class OpenCarryEpisodeOptimizer:
    """Shortest projection of a plan on its physical line-visit skeleton.

    Each incumbent hook contributes one visit slot.  A slot may be skipped or
    may execute any physically legal prefix Get / suffix Put of the same kind
    and line.  This single DAG contains source-depth deferral, open-carry
    retention and adjacent operation fusion without recognizing those shapes
    separately.
    """

    WITNESSES_PER_FUTURE_GET = 256

    def __init__(
        self,
        problem: Stage4Problem,
        transitions: OperationTransitions,
        *,
        mandatory_get_prefix_hooks: int = 0,
        max_labels: int = 8_192,
        deadline: float | None = None,
        include_aligned_macros: bool = True,
    ) -> None:
        if max_labels < 1:
            raise ValueError("episode max_labels must be positive")
        self.problem = problem
        self.transitions = transitions
        self.mandatory_get_prefix_hooks = mandatory_get_prefix_hooks
        self.max_labels = max_labels
        self.deadline = deadline
        self.include_aligned_macros = include_aligned_macros
        self.evaluated_paths = 0
        self.label_budget_exhausted = False
        self.time_budget_exhausted = False

    def optimize(self, incumbent: SearchNode) -> EpisodeOptimization:
        if not self.problem.complete(incumbent):
            return EpisodeOptimization(incumbent, (), 0)
        if self._expired():
            return EpisodeOptimization(
                incumbent,
                (),
                0,
                time_budget_exhausted=True,
            )

        prefix = incumbent.steps[: self.mandatory_get_prefix_hooks]
        seed = self.replay(prefix)
        if seed is None:
            raise ValueError("mandatory episode prefix is not physically valid")

        skeleton = incumbent.steps[self.mandatory_get_prefix_hooks :]
        aligned_nodes = self._aligned_nodes(seed, skeleton)
        (
            future_nos,
            future_get_lines,
            future_put,
            future_get_nos,
            future_put_nos,
        ) = self._future_obligations(skeleton)
        frontier: dict[tuple, list[SearchNode]] = {
            self.problem.physical_signature(seed.state): [seed]
        }
        best = self._best_paired_projection(incumbent)

        for index, original in enumerate(skeleton):
            if self._expired():
                break
            successors: dict[tuple, list[SearchNode]] = {}
            for node in self._frontier_nodes(frontier):
                if self._expired():
                    break
                self._admit(
                    successors,
                    node,
                    best,
                    len(skeleton) - index - 1,
                    future_nos[index + 1],
                    future_get_lines[index + 1],
                    future_put[index + 1],
                )
                for step in self._slot_steps(
                    node,
                    original,
                    future_nos[index],
                    future_get_nos[index + 1],
                    future_put_nos[index + 1],
                ):
                    self.evaluated_paths += 1
                    successor = self.transitions.apply_step(node, step)
                    if successor is None:
                        continue
                    if successor.cost.target_reopens > incumbent.cost.target_reopens:
                        continue
                    if self.problem.complete(successor):
                        if successor.cost < best.cost:
                            best = successor
                        continue
                    self._admit(
                        successors,
                        successor,
                        best,
                        len(skeleton) - index - 1,
                        future_nos[index + 1],
                        future_get_lines[index + 1],
                        future_put[index + 1],
                    )
            if self._expired():
                break
            frontier = successors
            frontier = self._trim_frontier(
                frontier,
                future_nos[index + 1],
                skeleton[index + 1 :],
                aligned_nodes[index],
            )
            if not frontier:
                break

        for node in self._frontier_nodes(frontier):
            if self.problem.complete(node) and node.cost < best.cost:
                best = node

        if best.cost >= incumbent.cost:
            return EpisodeOptimization(
                incumbent,
                (),
                self.evaluated_paths,
                self.label_budget_exhausted,
                self.time_budget_exhausted,
            )
        removed = incumbent.cost.hooks - best.cost.hooks
        retained = tuple(sorted(
            {
                no
                for step in incumbent.steps
                for no in step.move_car_nos
            }
            - {
                no
                for step in best.steps
                for no in step.move_car_nos
            }
        ))
        contraction = EpisodeContraction(
            kind="skeleton_shortest_path",
            removed_hooks=removed,
            retained_cars=retained,
            start_hook=self.mandatory_get_prefix_hooks + 1,
            end_hook=len(incumbent.steps),
        )
        fixed_point = (
            self.optimize(best)
            if not self._expired()
            else EpisodeOptimization(
                best,
                (),
                self.evaluated_paths,
                self.label_budget_exhausted,
                self.time_budget_exhausted,
            )
        )
        return EpisodeOptimization(
            node=fixed_point.node,
            contractions=(contraction, *fixed_point.contractions),
            evaluated_paths=self.evaluated_paths,
            label_budget_exhausted=(
                self.label_budget_exhausted
                or fixed_point.label_budget_exhausted
            ),
            time_budget_exhausted=(
                self.time_budget_exhausted
                or fixed_point.time_budget_exhausted
            ),
        )

    def _best_paired_projection(self, incumbent: SearchNode) -> SearchNode:
        best = incumbent
        for projection in self._paired_projection_steps(incumbent.steps):
            if self._expired():
                break
            self.evaluated_paths += 1
            candidate = self.replay(
                projection.steps,
                fixed_positions={
                    index: dict(positions)
                    for index, positions in projection.fixed_positions
                },
            )
            if candidate is None or not self.problem.complete(candidate):
                continue
            if candidate.cost.target_reopens > incumbent.cost.target_reopens:
                continue
            if candidate.cost < best.cost:
                best = candidate
        return best

    def _expired(self) -> bool:
        expired = self.deadline is not None and time.monotonic() >= self.deadline
        if expired:
            self.time_budget_exhausted = True
        return expired

    def _paired_projection_steps(
        self,
        steps: tuple[physical.PlanStep, ...],
    ) -> tuple[_PairedProjection, ...]:
        before: tuple[SearchNode, ...] | None = None
        if self.include_aligned_macros:
            initial = SearchNode(physical.initial_planlet_state(
                self.problem.cars,
                self.problem.loco_location,
            ))
            aligned = self._aligned_nodes(initial, steps)
            before = (initial, *aligned[:-1])
        candidates: list[_PairedProjection] = []
        for start in range(self.mandatory_get_prefix_hooks, len(steps)):
            first = steps[start]
            if first.action == "Put":
                candidates.extend(
                    _PairedProjection(candidate)
                    for candidate in self._carry_lease_projections(steps, start)
                )
                if before is not None:
                    committed = self._target_suffix_projection(
                        steps,
                        start,
                        before[start],
                    )
                    if committed is not None:
                        candidates.append(committed)
            elif first.action == "Get":
                candidates.extend(
                    _PairedProjection(candidate)
                    for candidate in self._source_suffix_projections(steps, start)
                )
        return tuple(candidates)

    def _target_suffix_projection(
        self,
        steps: tuple[physical.PlanStep, ...],
        start: int,
        node: SearchNode,
    ) -> _PairedProjection | None:
        first = steps[start]
        retained = first.move_car_nos
        targets = {
            self.problem.target_by_no.get(no, "")
            for no in retained
        }
        if len(targets) != 1:
            return None
        target = next(iter(targets))
        if not target or target == first.line:
            return None
        positions = self.problem.committed_target_suffix_positions(
            node.state,
            target,
            retained,
        )
        if positions is None:
            return None

        recovery = next((
            index
            for index in range(start + 1, len(steps))
            if steps[index].action == "Get"
            and steps[index].line == first.line
            and steps[index].move_car_nos[-len(retained):] == retained
        ), None)
        if recovery is None or any(
            set(retained).intersection(steps[index].move_car_nos)
            for index in range(start + 1, recovery)
        ):
            return None
        final = next((
            index
            for index in range(recovery + 1, len(steps))
            if steps[index].action == "Put"
            and steps[index].line == target
            and steps[index].move_car_nos[-len(retained):] == retained
        ), None)
        if final is None or any(
            set(retained).intersection(steps[index].move_car_nos)
            for index in range(recovery + 1, final)
        ):
            return None

        transformed = list(steps)
        transformed[start] = physical.plan_step(
            "Put",
            target,
            retained,
            positions,
        )
        transformed[recovery] = replace(
            transformed[recovery],
            move_car_nos=transformed[recovery].move_car_nos[:-len(retained)],
        )
        transformed[final] = replace(
            transformed[final],
            move_car_nos=transformed[final].move_car_nos[:-len(retained)],
        )
        filtered: list[physical.PlanStep] = []
        fixed_index = -1
        for index, step in enumerate(transformed):
            if not step.move_car_nos:
                continue
            if index == start:
                fixed_index = len(filtered)
            filtered.append(step)
        if fixed_index < 0:
            return None
        return _PairedProjection(
            tuple(filtered),
            ((fixed_index, tuple(sorted(positions.items()))),),
        )

    @staticmethod
    def _carry_lease_projections(
        steps: tuple[physical.PlanStep, ...],
        start: int,
    ) -> tuple[tuple[physical.PlanStep, ...], ...]:
        first = steps[start]
        candidates: list[tuple[physical.PlanStep, ...]] = []
        for count in range(1, len(first.move_car_nos) + 1):
            retained = frozenset(first.move_car_nos[:count])
            recovery = next((
                index
                for index in range(start + 1, len(steps))
                if steps[index].action == "Get"
                and steps[index].line == first.line
                and retained <= set(steps[index].move_car_nos)
            ), None)
            if recovery is None or any(
                retained.intersection(steps[index].move_car_nos)
                for index in range(start + 1, recovery)
            ):
                continue
            transformed = list(steps)
            transformed[start] = replace(
                first,
                move_car_nos=first.move_car_nos[count:],
            )
            recovered = transformed[recovery]
            transformed[recovery] = replace(
                recovered,
                move_car_nos=tuple(
                    no for no in recovered.move_car_nos
                    if no not in retained
                ),
            )
            candidates.append(OpenCarryEpisodeOptimizer._nonempty(transformed))
        return tuple(candidates)

    @staticmethod
    def _source_suffix_projections(
        steps: tuple[physical.PlanStep, ...],
        start: int,
    ) -> tuple[tuple[physical.PlanStep, ...], ...]:
        first = steps[start]
        candidates: list[tuple[physical.PlanStep, ...]] = []
        for depth in range(1, len(first.move_car_nos)):
            retained = frozenset(first.move_car_nos[depth:])
            recovery = next((
                index
                for index in range(start + 1, len(steps))
                if steps[index].action == "Get"
                and steps[index].line == first.line
                and retained <= set(steps[index].move_car_nos)
            ), None)
            if recovery is None:
                continue
            transformed = list(steps)
            transformed[start] = replace(
                first,
                move_car_nos=first.move_car_nos[:depth],
            )
            valid = True
            for index in range(start + 1, recovery):
                current = transformed[index]
                overlap = retained.intersection(current.move_car_nos)
                if not overlap:
                    continue
                if current.action != "Put" or current.line != first.line:
                    valid = False
                    break
                transformed[index] = replace(
                    current,
                    move_car_nos=tuple(
                        no for no in current.move_car_nos
                        if no not in retained
                    ),
                )
            if valid:
                candidates.append(OpenCarryEpisodeOptimizer._nonempty(transformed))
        return tuple(candidates)

    @staticmethod
    def _nonempty(
        steps: list[physical.PlanStep],
    ) -> tuple[physical.PlanStep, ...]:
        return tuple(step for step in steps if step.move_car_nos)

    def _aligned_nodes(
        self,
        seed: SearchNode,
        skeleton: tuple[physical.PlanStep, ...],
    ) -> tuple[SearchNode, ...]:
        aligned: list[SearchNode] = []
        node = seed
        for step in skeleton:
            successor = self.transitions.apply_step(node, step)
            if successor is None:
                raise ValueError("incumbent skeleton is not physically valid")
            node = successor
            aligned.append(node)
        return tuple(aligned)

    def replay(
        self,
        steps: tuple[physical.PlanStep, ...],
        *,
        fixed_positions: dict[int, dict[str, int]] | None = None,
    ) -> SearchNode | None:
        node = SearchNode(physical.initial_planlet_state(
            self.problem.cars,
            self.problem.loco_location,
        ))
        fixed_positions = fixed_positions or {}
        for index, original in enumerate(steps):
            positions = (
                fixed_positions[index]
                if index in fixed_positions
                else self.transitions.planned_positions(
                    node.state,
                    original.line,
                    original.move_car_nos,
                )
                if original.action == "Put"
                else {}
            )
            if positions is None:
                return None
            step = replace(original, planned_positions=positions)
            successor = self.transitions.apply_step(node, step)
            if successor is None:
                return None
            node = successor
        return node

    def _slot_steps(
        self,
        node: SearchNode,
        original: physical.PlanStep,
        future_nos: frozenset[str],
        future_get_nos: dict[str, frozenset[str]],
        future_put_nos: dict[str, frozenset[str]],
    ) -> tuple[physical.PlanStep, ...]:
        if original.action == "Get":
            return self._get_steps(
                node,
                original,
                future_nos,
                future_get_nos.get(original.line, frozenset()),
            )
        if original.action == "Put":
            return self._put_steps(
                node,
                original,
                future_get_nos.get(original.line, frozenset()),
                future_put_nos.get(original.line, frozenset()),
            )
        if original.action == "Weigh":
            return self._weigh_steps(node)
        return ()

    def _get_steps(
        self,
        node: SearchNode,
        original: physical.PlanStep,
        future_nos: frozenset[str],
        future_get_nos: frozenset[str],
    ) -> tuple[physical.PlanStep, ...]:
        line = original.line
        cars = self.problem.cars_list(node.state)
        order = physical.line_access_order(cars, line)
        if not order:
            return ()
        by_no = {physical.car_no(car): car for car in cars}
        carried = [by_no[no] for no in node.state.carried_order]
        equivalent = physical.pull_equivalent(carried)
        moves: list[tuple[str, ...]] = []
        original_available = set(original.move_car_nos) & set(order)
        for depth, no in enumerate(order, start=1):
            if no not in future_nos:
                break
            equivalent += physical.pull_equivalent([by_no[no]])
            if equivalent > physical.PULL_LIMIT_EQUIVALENT:
                break
            move = tuple(order[:depth])
            move_set = set(move)
            if (
                move_set - original_available <= future_get_nos
                and original_available - move_set <= future_get_nos
            ):
                moves.append(move)
        return tuple(
            physical.plan_step("Get", line, move)
            for move in reversed(moves)
        )

    def _put_steps(
        self,
        node: SearchNode,
        original: physical.PlanStep,
        future_get_nos: frozenset[str],
        future_put_nos: frozenset[str],
    ) -> tuple[physical.PlanStep, ...]:
        line = original.line
        carry = node.state.carried_order
        original_available = set(original.move_car_nos) & set(carry)
        candidates: list[physical.PlanStep] = []
        for start in range(len(carry)):
            move = tuple(carry[start:])
            move_set = set(move)
            if (
                not (original_available - move_set <= future_get_nos)
                or not (move_set - original_available <= future_put_nos)
            ):
                continue
            positions = self.transitions.planned_positions(node.state, line, move)
            if positions is None:
                continue
            candidates.append(physical.plan_step("Put", line, move, positions))
        return tuple(candidates)

    def _weigh_steps(
        self,
        node: SearchNode,
    ) -> tuple[physical.PlanStep, ...]:
        if not node.state.carried_order:
            return ()
        tail = node.state.carried_order[-1]
        by_no = {
            physical.car_no(car): car
            for car in node.state.cars
        }
        car = by_no[tail]
        if not car.get("IsWeigh") or car.get("_Weighed"):
            return ()
        return (physical.plan_step("Weigh", physical.WEIGH_LINE, (tail,)),)

    def _admit(
        self,
        frontier: dict[tuple, list[SearchNode]],
        node: SearchNode,
        upper_bound: SearchNode,
        remaining_slots: int,
        future_nos: frozenset[str],
        future_get_lines: frozenset[str],
        future_put: bool,
    ) -> None:
        if node.cost.hooks > upper_bound.cost.hooks:
            return
        remaining_hooks = self.problem.hook_lower_bound(node.state)
        if remaining_hooks > remaining_slots:
            return
        if node.cost.hooks + remaining_hooks > upper_bound.cost.hooks:
            return
        if not self._future_reachable(node, future_get_lines, future_put):
            return
        signature = self.problem.physical_signature(node.state)
        bucket = frontier.setdefault(signature, [])
        if any(
            self._dominates(previous, node, future_nos)
            for previous in bucket
        ):
            return
        bucket[:] = [
            previous
            for previous in bucket
            if not self._dominates(node, previous, future_nos)
        ]
        bucket.append(node)

    def _future_reachable(
        self,
        node: SearchNode,
        future_get_lines: frozenset[str],
        future_put: bool,
    ) -> bool:
        if node.state.carried_order and not future_put:
            return False
        debt = (
            self.problem.unsatisfied_active(node.state)
            | self.problem.protected_damage(node.state)
        )
        if not debt:
            return True
        carried = set(node.state.carried_order)
        by_no = {
            physical.car_no(car): car
            for car in node.state.cars
        }
        return all(
            no in carried
            or str(by_no[no].get("Line") or "") in future_get_lines
            for no in debt
        )

    @staticmethod
    def _dominates(
        left: SearchNode,
        right: SearchNode,
        future_nos: frozenset[str],
    ) -> bool:
        if not OpenCarryEpisodeOptimizer._windows_no_worse(
            left.target_windows,
            right.target_windows,
        ):
            return False
        if left.cost.hooks < right.cost.hooks:
            return True
        if left.cost.hooks > right.cost.hooks:
            return False
        return bool(
            left.handled_nos & future_nos
            <= right.handled_nos & future_nos
            and left.cost <= right.cost
        )

    @staticmethod
    def _windows_no_worse(
        left: tuple,
        right: tuple,
    ) -> bool:
        risk = {"open": 1, "sealed": 2}
        left_status = {target: risk[status.value] for target, status in left}
        right_status = {target: risk[status.value] for target, status in right}
        return all(
            left_status.get(target, 0) <= right_status.get(target, 0)
            for target in left_status.keys() | right_status.keys()
        )

    @staticmethod
    def _frontier_nodes(
        frontier: dict[tuple, list[SearchNode]],
    ) -> tuple[SearchNode, ...]:
        return tuple(
            node
            for bucket in frontier.values()
            for node in bucket
        )

    def _trim_frontier(
        self,
        frontier: dict[tuple, list[SearchNode]],
        future_nos: frozenset[str],
        remaining_steps: tuple[physical.PlanStep, ...],
        aligned: SearchNode,
    ) -> dict[tuple, list[SearchNode]]:
        nodes = self._frontier_nodes(frontier)
        if len(nodes) <= self.max_labels:
            return frontier
        self.label_budget_exhausted = True
        ranked = sorted(
            nodes,
            key=lambda node: self._label_priority(
                node,
                future_nos,
                remaining_steps,
            ),
        )
        reserved: list[SearchNode] = []
        reserved_ids: set[int] = set()
        future_gets = sorted(
            (
                step for step in remaining_steps
                if step.action == "Get" and step.move_car_nos
            ),
            key=lambda step: (-len(step.move_car_nos), step.line, step.move_car_nos),
        )
        for step in future_gets:
            moved = set(step.move_car_nos)
            witnesses = sorted(
                nodes,
                key=lambda node: (
                    -len(moved.intersection(node.state.carried_order)),
                    self._label_priority(node, future_nos, remaining_steps),
                ),
            )
            for witness in witnesses[: self.WITNESSES_PER_FUTURE_GET]:
                if id(witness) not in reserved_ids:
                    reserved.append(witness)
                    reserved_ids.add(id(witness))
        selected = list(reserved[: self.max_labels])
        selected_ids = {id(node) for node in selected}
        for node in ranked:
            if len(selected) >= self.max_labels:
                break
            if id(node) in selected_ids:
                continue
            selected.append(node)
            selected_ids.add(id(node))
        aligned_match = next(
            (node for node in nodes if node.steps == aligned.steps),
            None,
        )
        if aligned_match is not None and id(aligned_match) not in selected_ids:
            selected[-1] = aligned_match
        trimmed: dict[tuple, list[SearchNode]] = {}
        for node in selected:
            trimmed.setdefault(
                self.problem.physical_signature(node.state),
                [],
            ).append(node)
        return trimmed

    def _label_priority(
        self,
        node: SearchNode,
        future_nos: frozenset[str],
        remaining_steps: tuple[physical.PlanStep, ...],
    ) -> tuple:
        state = node.state
        credit = self._projection_credit(node, remaining_steps)
        return (
            node.cost.hooks - credit,
            node.cost.hooks + self.problem.hook_lower_bound(state),
            node.cost.hooks + self.problem.ordered_block_estimate(state),
            node.cost,
            len(node.handled_nos & future_nos),
            tuple(
                (step.action, step.line, step.move_car_nos)
                for step in node.steps
            ),
        )

    @staticmethod
    def _projection_credit(
        node: SearchNode,
        remaining_steps: tuple[physical.PlanStep, ...],
    ) -> int:
        carried = set(node.state.carried_order)
        credit = 0
        for step in remaining_steps:
            moved = set(step.move_car_nos)
            if step.action == "Get" and moved and moved <= carried:
                credit += 1
        return credit

    @staticmethod
    def _future_obligations(
        steps: tuple[physical.PlanStep, ...],
    ) -> tuple[
        tuple[frozenset[str], ...],
        tuple[frozenset[str], ...],
        tuple[bool, ...],
        tuple[dict[str, frozenset[str]], ...],
        tuple[dict[str, frozenset[str]], ...],
    ]:
        nos: list[frozenset[str]] = [frozenset() for _ in range(len(steps) + 1)]
        get_lines: list[frozenset[str]] = [
            frozenset() for _ in range(len(steps) + 1)
        ]
        put: list[bool] = [False for _ in range(len(steps) + 1)]
        get_nos: list[dict[str, frozenset[str]]] = [
            {} for _ in range(len(steps) + 1)
        ]
        put_nos: list[dict[str, frozenset[str]]] = [
            {} for _ in range(len(steps) + 1)
        ]
        for index in range(len(steps) - 1, -1, -1):
            step = steps[index]
            nos[index] = nos[index + 1] | frozenset(step.move_car_nos)
            get_lines[index] = get_lines[index + 1] | (
                frozenset({step.line}) if step.action == "Get" else frozenset()
            )
            put[index] = put[index + 1] or step.action == "Put"
            get_nos[index] = dict(get_nos[index + 1])
            put_nos[index] = dict(put_nos[index + 1])
            if step.action == "Get":
                get_nos[index][step.line] = (
                    get_nos[index].get(step.line, frozenset())
                    | frozenset(step.move_car_nos)
                )
            elif step.action == "Put":
                put_nos[index][step.line] = (
                    put_nos[index].get(step.line, frozenset())
                    | frozenset(step.move_car_nos)
                )
        return (
            tuple(nos),
            tuple(get_lines),
            tuple(put),
            tuple(get_nos),
            tuple(put_nos),
        )
