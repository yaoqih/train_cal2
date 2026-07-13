from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping

from solver_vnext import physical

from .model import FlowDiagnostics, FlowModel
from .topology import resource_gate_closure


HOT_THROATS = frozenset({"渡10", "联7", "渡9", "渡8", "渡7", "渡4"})


@dataclass(frozen=True, order=True)
class SearchCost:
    """Lexicographic Stage4 objective."""

    hooks: int = 0
    repeated_gets: int = 0
    target_reopens: int = 0
    route_cost: int = 0


class WindowStatus(str, Enum):
    OPEN = "open"
    SEALED = "sealed"


@dataclass(frozen=True)
class SearchNode:
    state: physical.PlanletState
    steps: tuple[physical.PlanStep, ...] = ()
    cost: SearchCost = SearchCost()
    handled_nos: frozenset[str] = frozenset()
    target_windows: tuple[tuple[str, WindowStatus], ...] = ()
    carry_origins: tuple[tuple[str, str], ...] = ()


@dataclass
class Stage4Problem:
    case_id: str
    cars: list[dict]
    loco_location: physical.LocoLocation
    depot_assignment: physical.DepotAssignment
    target_by_no: Mapping[str, str]
    active_nos: frozenset[str]
    protected_nos: frozenset[str]
    capacity_holdout_nos: frozenset[str] = frozenset()
    graph: physical.TrackGraph = field(default_factory=physical.TrackGraph)

    def __post_init__(self) -> None:
        self.target_by_no = dict(self.target_by_no)
        self.by_no = {physical.car_no(car): car for car in self.cars}
        self.target_options_by_no = {
            no: self._target_options(no)
            for no in self.by_no
        }
        active_targets = {
            target
            for no in self.active_nos
            for target in self.target_options_by_no.get(no, ())
        }
        protected_spotting_targets = {
            str(self.by_no[no].get("Line") or "")
            for no in self.protected_nos
            if physical.is_spotting_line(str(self.by_no[no].get("Line") or ""))
            and str(self.by_no[no].get("Line") or "")
            in self.target_options_by_no.get(no, ())
        }
        self.target_lines = frozenset(
            active_targets | protected_spotting_targets
        )
        self.active_target_lines = frozenset(active_targets)
        self.final_rank_by_no = MappingProxyType(self._compute_final_ranks())
        self.owner_order_by_target = MappingProxyType(
            self._compute_owner_orders()
        )
        self._unsatisfied_cache: dict[tuple, frozenset[str]] = {}
        self._protected_cache: dict[tuple, frozenset[str]] = {}
        self._hook_lb_cache: dict[tuple, int] = {}
        self._robf_cache: dict[tuple, int] = {}
        self._source_profile_cache: dict[tuple, tuple[int, int]] = {}

    def _compute_final_ranks(self) -> dict[str, int]:
        ranks: dict[str, int] = {}
        for target in sorted(self.target_lines):
            existing = [
                no
                for no in physical.line_access_order(self.cars, target)
                if target in self.target_options_by_no.get(no, ())
                and not (
                    no in self.active_nos
                    and self.target_by_no.get(no) != target
                )
            ]
            inbound = sorted(
                (
                    no
                    for no in self.active_nos
                    if self.target_by_no.get(no) == target
                    and self.by_no[no].get("Line") != target
                ),
                key=lambda no: (
                    self.by_no[no].get("Line") or "",
                    int(self.by_no[no].get("Position") or 0),
                    no,
                ),
            )
            participants = tuple(dict.fromkeys((*existing, *inbound)))
            if not participants:
                continue
            planning = [dict(car) for car in self.cars]
            participant_set = set(participants)
            target_order = tuple(physical.line_access_order(planning, target))
            if target_order:
                physical.apply_physical_get_order(planning, target, target_order)
            sources = {
                self.by_no[no].get("Line") or ""
                for no in participants
                if (self.by_no[no].get("Line") or "") != target
            }
            for source in sorted(sources):
                group = tuple(
                    no
                    for no in physical.line_access_order(planning, source)
                    if no in participant_set
                )
                if group:
                    physical.apply_physical_get_order(planning, source, group)
            positions = physical.planned_positions_for_batch(
                batch=[self.by_no[no] for no in participants],
                target_line=target,
                cars=planning,
                depot_assignment=self.depot_assignment,
                batch_nos=participant_set,
            )
            ranks.update(positions)
        return ranks

    def _target_options(self, no: str) -> tuple[str, ...]:
        car = self.by_no[no]
        if no in self.active_nos:
            chosen = self.target_by_no.get(no, "")
            return (chosen,) if chosen else ()
        return tuple(dict.fromkeys(
            line
            for line in physical.target_lines(car)
            if line in physical.TRACK_SPECS
        ))

    def _compute_owner_orders(self) -> dict[str, tuple[str, ...]]:
        orders: dict[str, tuple[str, ...]] = {}
        targets = sorted({
            target
            for options in self.target_options_by_no.values()
            for target in options
            if physical.is_spotting_line(target)
        })
        for target in targets:
            existing = [
                no
                for no in physical.line_access_order(self.cars, target)
                if target in self.target_options_by_no.get(no, ())
                and not (
                    no in self.active_nos
                    and self.target_by_no.get(no) != target
                )
            ]
            inbound = sorted(
                (
                    no
                    for no in self.active_nos
                    if self.target_by_no.get(no) == target
                    and self.by_no[no].get("Line") != target
                ),
                key=lambda no: (
                    self.by_no[no].get("Line") or "",
                    int(self.by_no[no].get("Position") or 0),
                    no,
                ),
            )
            participants = tuple(dict.fromkeys((*existing, *inbound)))
            if participants and all(
                self.final_rank_by_no.get(no, 0) > 0
                for no in participants
            ):
                participants = tuple(sorted(
                    participants,
                    key=lambda no: (self.final_rank_by_no[no], no),
                ))
            if participants:
                orders[target] = participants
        return orders

    def owner_order(self, target: str) -> tuple[str, ...]:
        return self.owner_order_by_target.get(target, ())

    def committed_target_suffix_positions(
        self,
        state: physical.PlanletState,
        target: str,
        move: tuple[str, ...],
    ) -> dict[str, int] | None:
        """Certify a final-rank suffix that never needs to be recovered."""

        if not move or not physical.is_spotting_line(target):
            return None
        if any(
            no not in self.active_nos or self.target_by_no.get(no) != target
            for no in move
        ):
            return None
        ranks = tuple(self.final_rank_by_no.get(no, 0) for no in move)
        if (
            any(rank <= 0 for rank in ranks)
            or ranks != tuple(sorted(ranks))
            or any(right != left + 1 for left, right in zip(ranks, ranks[1:]))
        ):
            return None

        pending = {
            no
            for no in self.unsatisfied_active(state)
            if self.target_by_no.get(no) == target
        }
        moved = set(move)
        if not moved <= pending:
            return None
        remaining_ranks = {
            self.final_rank_by_no.get(no, 0)
            for no in pending - moved
        }
        if any(rank <= 0 or rank >= ranks[0] for rank in remaining_ranks):
            return None

        cars = self.cars_list(state)
        by_no = {physical.car_no(car): car for car in cars}
        target_order = physical.line_access_order(cars, target)
        if any(
            no in self.active_nos and self.target_by_no.get(no) != target
            for no in target_order
        ):
            return None
        existing_ranks = [
            self.final_rank_by_no[no]
            for no in target_order
            if no in self.final_rank_by_no
        ]
        if existing_ranks != sorted(existing_ranks):
            return None
        if any(
            int(by_no[no].get("Position") or 0) != self.final_rank_by_no[no]
            for no in target_order
            if no in self.final_rank_by_no
        ):
            return None
        occupied = {
            int(car.get("Position") or 0)
            for car in cars
            if car.get("Line") == target
        }
        if occupied.intersection(ranks):
            return None
        return dict(zip(move, ranks))

    @staticmethod
    def cars_list(state: physical.PlanletState) -> list[dict]:
        return list(state.cars)

    def _yard_key(self, state: physical.PlanletState) -> tuple:
        return tuple(sorted(
            (
                physical.car_no(car),
                car.get("Line") or "",
                int(car.get("Position") or 0),
                bool(car.get("_Weighed")),
            )
            for car in state.cars
        ))

    def unsatisfied_active(self, state: physical.PlanletState) -> frozenset[str]:
        key = self._yard_key(state)
        cached = self._unsatisfied_cache.get(key)
        if cached is not None:
            return cached
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(
                self.cars_list(state),
                self.depot_assignment,
            )
        }
        result = frozenset(unsatisfied & self.active_nos)
        self._unsatisfied_cache[key] = result
        return result

    def protected_damage(self, state: physical.PlanletState) -> frozenset[str]:
        key = self._yard_key(state)
        cached = self._protected_cache.get(key)
        if cached is not None:
            return cached
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(
                self.cars_list(state),
                self.depot_assignment,
            )
        }
        result = frozenset(unsatisfied & self.protected_nos)
        self._protected_cache[key] = result
        return result

    def complete(self, node: SearchNode) -> bool:
        return (
            not node.state.carried_order
            and not self.unsatisfied_active(node.state)
            and not self.protected_damage(node.state)
        )

    def physical_signature(self, state: physical.PlanletState) -> tuple:
        return (
            self._yard_key(state),
            state.loco_location.line,
            state.carried_order,
        )

    def hook_lower_bound(self, state: physical.PlanletState) -> int:
        """Admissible source/target/weigh lower bound."""

        key = self.physical_signature(state)
        cached = self._hook_lb_cache.get(key)
        if cached is not None:
            return cached

        debt = self.unsatisfied_active(state)
        if not debt:
            result = int(bool(state.carried_order))
            self._hook_lb_cache[key] = result
            return result
        by_no = {physical.car_no(car): car for car in state.cars}
        carried = set(state.carried_order)
        source_lines = {
            str(by_no[no].get("Line") or "")
            for no in debt - carried
            if by_no[no].get("Line")
        }
        target_lines = {
            self.target_by_no.get(no, "")
            for no in debt
            if self.target_by_no.get(no)
            and (
                by_no[no].get("Line") != self.target_by_no.get(no)
                or physical.force_positions(by_no[no])
            )
        }
        weigh_count = sum(
            bool(by_no[no].get("IsWeigh") and not by_no[no].get("_Weighed"))
            for no in debt
        )
        result = len(source_lines) + len(target_lines) + weigh_count
        self._hook_lb_cache[key] = result
        return result

    def ordered_block_estimate(self, state: physical.PlanletState) -> int:
        """Owner-run estimate used only to order labels, never to prune."""

        key = self.physical_signature(state)
        cached = self._robf_cache.get(key)
        if cached is not None:
            return cached

        debt = self.unsatisfied_active(state)
        if not debt:
            result = int(bool(state.carried_order))
            self._robf_cache[key] = result
            return result
        by_no = {physical.car_no(car): car for car in state.cars}
        carried = set(state.carried_order)
        target_lines: set[str] = set()
        weigh_count = 0
        for no in debt:
            car = by_no[no]
            target = self._debt_owner(no)
            if target and (
                car.get("Line") != target
                or physical.force_positions(car)
            ):
                target_lines.add(target)
            if car.get("IsWeigh") and not car.get("_Weighed"):
                weigh_count += 1

        source_words: list[tuple[str, ...]] = []
        source_lines = sorted({
            str(by_no[no].get("Line") or "")
            for no in debt - carried
            if by_no[no].get("Line")
        })
        for line in source_lines:
            order = physical.line_access_order(self.cars_list(state), line)
            deepest = max(
                index for index, no in enumerate(order)
                if no in debt
            )
            word = self._compress_word(
                self._debt_owner(no)
                for no in order[: deepest + 1]
                if no in debt
            )
            if word:
                source_words.append(word)
        carry_word = self._compress_word(
            self._debt_owner(no)
            for no in state.carried_order
            if no in debt
        )
        owner_runs = self._minimum_owner_runs(carry_word, tuple(source_words))
        result = (
            len(source_words)
            + max(len(target_lines), owner_runs)
            + weigh_count
        )
        self._robf_cache[key] = result
        return result

    def source_fragmentation(self, state: physical.PlanletState) -> tuple[int, int]:
        """Return remaining source-line count and maximum owner-run count."""

        key = self.physical_signature(state)
        cached = self._source_profile_cache.get(key)
        if cached is not None:
            return cached
        debt = self.unsatisfied_active(state)
        carried = set(state.carried_order)
        by_no = {physical.car_no(car): car for car in state.cars}
        source_lines = sorted({
            str(by_no[no].get("Line") or "")
            for no in debt - carried
            if by_no[no].get("Line")
        })
        max_runs = 0
        cars = self.cars_list(state)
        for line in source_lines:
            order = physical.line_access_order(cars, line)
            deepest = max(index for index, no in enumerate(order) if no in debt)
            owners = self._compress_word(
                self._debt_owner(no) if no in debt else f"restore:{line}"
                for no in order[: deepest + 1]
            )
            max_runs = max(max_runs, len(owners))
        result = (len(source_lines), max_runs)
        self._source_profile_cache[key] = result
        return result

    def _debt_owner(self, no: str) -> str:
        if no in self.active_nos:
            return self.target_by_no.get(no, "")
        return str(self.by_no[no].get("Line") or "")

    @staticmethod
    def _compress_word(owners: Iterable[str]) -> tuple[str, ...]:
        result: list[str] = []
        for owner in owners:
            if owner and (not result or result[-1] != owner):
                result.append(owner)
        return tuple(result)

    @staticmethod
    def _minimum_owner_runs(
        carry: tuple[str, ...],
        sources: tuple[tuple[str, ...], ...],
    ) -> int:
        if not sources:
            return len(carry)
        count = len(sources)
        frontier: dict[tuple[int, int], int] = {}
        for index, word in enumerate(sources):
            joins_carry = bool(carry and carry[-1] == word[0])
            frontier[(1 << index, index)] = (
                len(carry) + len(word) - int(joins_carry)
            )
        for mask_size in range(1, count):
            next_frontier = dict(frontier)
            for (mask, last), runs in frontier.items():
                if mask.bit_count() != mask_size:
                    continue
                for index, word in enumerate(sources):
                    if mask & (1 << index):
                        continue
                    joined = sources[last][-1] == word[0]
                    key = (mask | (1 << index), index)
                    candidate = runs + len(word) - int(joined)
                    next_frontier[key] = min(
                        candidate,
                        next_frontier.get(key, candidate),
                    )
            frontier = next_frontier
        full = (1 << count) - 1
        return min(
            runs for (mask, _last), runs in frontier.items()
            if mask == full
        )

    def target_can_seal(
        self,
        state: physical.PlanletState,
        target: str,
    ) -> bool:
        debt = self.unsatisfied_active(state) | self.protected_damage(state)
        by_no = {physical.car_no(car): car for car in state.cars}
        for no in debt:
            owner = self._debt_owner(no)
            line = str(by_no[no].get("Line") or "")
            if owner == target or line == target:
                return False
            if target in resource_gate_closure(owner):
                return False
            if line and target in physical.operation_approach_lines(line):
                return False
        return True

    def diagnostics(self, state: physical.PlanletState) -> FlowDiagnostics:
        return FlowModel(
            self.cars_list(state),
            self.target_by_no,
            self.active_nos,
            self.protected_nos,
            self.depot_assignment,
        ).diagnostics()

    def target_options(self, no: str) -> frozenset[str]:
        return frozenset(self.target_options_by_no.get(no, ()))

    def common_targets(self, nos: Iterable[str]) -> set[str]:
        iterator = iter(nos)
        first = next(iterator, "")
        if not first:
            return set()
        common = set(self.target_options(first))
        for no in iterator:
            common.intersection_update(self.target_options(no))
        return common

    def target_exposed(
        self,
        target: str,
        state: physical.PlanletState,
        unsatisfied: frozenset[str] | None = None,
    ) -> bool:
        unsatisfied = (
            unsatisfied
            if unsatisfied is not None
            else self.unsatisfied_active(state)
        )
        pending = {
            no
            for no in unsatisfied
            if self.target_by_no.get(no) == target
        }
        if not pending:
            return False
        cars = self.cars_list(state)
        lines = {car.get("Line") or "" for car in cars if car.get("Line")}
        for line in sorted(lines):
            order = physical.line_access_order(cars, line)
            indexes = [index for index, no in enumerate(order) if no in pending]
            if indexes and any(no not in pending for no in order[: max(indexes) + 1]):
                return False
        return True


class OperationTransitions:
    """The sole operation transition system used by construction and replay."""

    def __init__(self, problem: Stage4Problem) -> None:
        self.problem = problem
        self._cache: dict[tuple, physical.PlanStepTransition] = {}

    def planned_positions(
        self,
        state: physical.PlanletState,
        line: str,
        move: tuple[str, ...],
    ) -> dict[str, int] | None:
        cars = self.problem.cars_list(state)
        if not physical.line_uses_business_positions(cars, line, set(move)):
            return {}
        by_no = {physical.car_no(car): car for car in cars}
        positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in move],
            target_line=line,
            cars=cars,
            depot_assignment=self.problem.depot_assignment,
            batch_nos=set(move),
        )
        return positions if len(positions) == len(move) else None

    def transition_step(
        self,
        node: SearchNode,
        step: physical.PlanStep,
    ) -> physical.PlanStepTransition:
        step_index = len(node.steps) + 1
        cache_key = (
            self.problem.physical_signature(node.state),
            step.action,
            step.line,
            step.move_car_nos,
            tuple(sorted(step.planned_positions.items())),
        )
        cached = self._cache.get(cache_key)
        if cached is None:
            candidate = physical.HookCandidate(
                case_id=self.problem.case_id,
                hook_index=1,
                candidate_id="stage4_transition",
                source_line=node.state.loco_location.line,
                target_line=step.line,
                move_car_nos=step.move_car_nos,
                action_family="",
                train_length_m=0.0,
                pull_equivalent_count=0,
                has_weigh=False,
                planned_positions=step.planned_positions,
                generation_reason="joint_flow_transition",
                candidate_kind="blocker_relocation",
            )
            transition = physical.transition_plan_step(
                self.problem.graph,
                candidate,
                node.state,
                step,
                self.problem.depot_assignment,
                step_index=step_index,
            )
            if transition.accepted:
                cached_state = replace(transition.state, operation_paths=())
                cached = replace(transition, state=cached_state)
                self._cache[cache_key] = cached
            else:
                return replace(transition, state=node.state)
        if not cached.accepted:
            return replace(cached, state=node.state)
        return replace(
            cached,
            state=replace(
                cached.state,
                operation_paths=(*node.state.operation_paths, cached.path),
            ),
        )

    def apply_step(
        self,
        node: SearchNode,
        step: physical.PlanStep,
    ) -> SearchNode | None:
        transition = self.transition_step(node, step)
        if not transition.accepted:
            return None

        handled = node.handled_nos
        windows = dict(node.target_windows)
        origins = dict(node.carry_origins)
        repeated_gets = node.cost.repeated_gets
        target_reopens = node.cost.target_reopens
        if step.action == "Get":
            repeated_gets += sum(no in handled for no in step.move_car_nos)
            handled = handled | frozenset(step.move_car_nos)
            origins.update((no, step.line) for no in step.move_car_nos)
            if windows.get(step.line) == WindowStatus.SEALED:
                target_reopens += 1
                windows[step.line] = WindowStatus.OPEN
        elif step.action == "Put":
            moved = set(step.move_car_nos)
            origins = {
                no: line
                for no, line in origins.items()
                if no not in moved
            }
            if step.line in self.problem.active_target_lines:
                windows[step.line] = WindowStatus.OPEN
        for target, status in tuple(windows.items()):
            if (
                status == WindowStatus.OPEN
                and self.problem.target_can_seal(transition.state, target)
            ):
                windows[target] = WindowStatus.SEALED
        path_cost = max(0, len(transition.path) - 1) + 2 * sum(
            line in HOT_THROATS
            for line in transition.path
        )
        cost = SearchCost(
            hooks=node.cost.hooks + 1,
            repeated_gets=repeated_gets,
            target_reopens=target_reopens,
            route_cost=node.cost.route_cost + path_cost,
        )
        return SearchNode(
            state=transition.state,
            steps=(*node.steps, step),
            cost=cost,
            handled_nos=handled,
            target_windows=tuple(sorted(windows.items())),
            carry_origins=tuple(sorted(origins.items())),
        )
