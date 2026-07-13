#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from stage3_simple import placement  # noqa: E402
from stage3_simple import transactions  # noqa: E402
from solver_vnext import physical  # noqa: E402


INF_COST = 10**9
DEPOT_IN = tuple(f"修{i}库内" for i in range(1, 5))
DEPOT_OUT = tuple(f"修{i}库外" for i in range(1, 5))
POSITIONED_LINES = (*DEPOT_IN, *DEPOT_OUT)
DEPOT_TARGETS = (*DEPOT_IN, *DEPOT_OUT)
DEPOT_OUT_BY_IN = {f"修{i}库内": f"修{i}库外" for i in range(1, 5)}
DEPOT_IN_BY_OUT = {value: key for key, value in DEPOT_OUT_BY_IN.items()}
UNWHEEL = "卸轮线"
STAGE4_DEFER_TARGETS = {"油漆线", "存4线"}
STAGE4_DEFER_LINES = (*DEPOT_OUT, UNWHEEL, *DEPOT_IN)
STAGE4_STAGING_LINES = (UNWHEEL, *DEPOT_OUT)
ASSEMBLY_LINES = ("机走北", "机走棚", "洗油北", "机南")
PREPICKUP_OUTER_SOURCE = "存4线"
STAGE3_SOURCE_LINES = (*ASSEMBLY_LINES, PREPICKUP_OUTER_SOURCE)
TEMPLATE_B_ORDER = ("机走北", "机走棚", "洗油北", "机南")
TEMPLATE_A_FIRST_ORDER = ("机走北", "机走棚", "机南")
TEMPLATE_A_SECOND_LINE = "洗油北"
DEFAULT_TIME_BUDGET_SECONDS = 180.0
PLACEMENT_MAX_PLANS = 8
PLACEMENT_NODE_BUDGET = 10_000
TRANSACTION_EXPANSION_LEVELS = (25, 100, 5_000, 10_000)
SHALLOW_COMPLETE_QUORUM = 2
SCREEN_COMPLETE_QUORUM = 3
SCREEN_PLAN_BUDGET = 6
SCREEN_BASE_PLAN_BUDGET = 4
STATIC_FINALIST_PLAN_BUDGET = 2
FINALIST_PLAN_BUDGET = 3
ALIGNMENT_STATE_BUDGET = 256
POST_BUDGET_FRONTIER_WIDTH = 2
STAGE3_REHOOK_LOCO = PREPICKUP_OUTER_SOURCE
BLOCKING_REPLAY_KINDS = {"schema", "physical", "business", "state"}


@dataclass(frozen=True)
class State:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    held: tuple[str, ...]
    loco: tuple[str, ...]
    phase: int
    positioned_positions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Op:
    action: str
    line: str
    move: tuple[str, ...]
    path: tuple[str, ...]
    train_after: tuple[str, ...]
    note: str = ""
    positions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class SearchResult:
    status: str
    template: str
    state: State | None
    ops: tuple[Op, ...]
    cost: tuple[int, int, int, int]
    reasons: tuple[str, ...]
    expansions: int
    elapsed_seconds: float
    layout: str = "unified"
    deferred_clear: bool = False
    terminal_merge: bool = True
    inner_clear_policy: str = "transaction"
    lower_bound: int | None = None
    lower_bound_components: tuple[tuple[str, int], ...] = ()
    lower_bound_scope: str = "not_applicable"
    search_spec_evaluated: bool = True
    committed_count: int = 0
    budgeted_progress_key: tuple[int, int, int, int, int] | None = None


@dataclass(frozen=True)
class PlacementSpec:
    operation_lower_bound: int
    placement_score: tuple[int, ...]
    signature: tuple[tuple[str, str, str, int], ...]
    template: str
    layout: str

    @property
    def shallow_group(self) -> tuple[int, tuple[int, ...]]:
        return self.operation_lower_bound, self.placement_score


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"cannot infer case id from {path}")
    return match.group(1).upper()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def final_loco_after_response(request: dict[str, Any], response: dict[str, Any]) -> str:
    loco = {rv.norm((request.get("locoNode") or {}).get("Line"))}
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = rv.norm(row.get("Line"))
        if action == "Get":
            loco = {line}
        elif action == "Put":
            loco = rv.put_loco_positions(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            loco = {rv.WEIGH}
    positions = sorted(line for line in loco if line)
    if not positions:
        raise ValueError("stage2_final_loco_undefined")
    return positions[0]


def normalize_car(car: dict[str, Any]) -> dict[str, Any]:
    out = rv.ncar(car)
    out["_TargetSet"] = set(out.get("TargetLines") or [])
    out["_ForcePositions"] = tuple(int(value) for value in out.get("ForceTargetPosition") or out.get("_Force") or () if int(value) > 0)
    return out


class Stage3Solver:
    def __init__(
        self,
        case_id: str,
        request: dict[str, Any],
        stage2_combined_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> None:
        self.case_id = case_id
        self.original_request = request
        self.stage2_combined_response = stage2_combined_response
        self.validate_input_contract(request)
        replayed, replay_bad = rv.replay(request, stage2_combined_response)
        blocking_stage2_bad = [
            violation for violation in replay_bad if violation.kind in BLOCKING_REPLAY_KINDS
        ]
        if blocking_stage2_bad:
            detail = "|".join(
                f"{violation.kind}:{violation.code}:{violation.detail}"
                for violation in blocking_stage2_bad[:12]
            )
            raise ValueError(f"stage2_combined_replay_invalid:{detail}")
        self.initial_cars = [normalize_car(car) for car in rv.final_cars(stage2_combined_response, replayed)]
        self.stage2_final_loco = final_loco_after_response(request, stage2_combined_response)
        # Stage 2 finishes by putting the outbound consist on 存4线.  The stage
        # contract says the locomotive re-couples at 存4北 before depot inbound
        # work starts; using the post-Put standing point 存4南 would make the
        # north-side pickup templates unreachable behind occupied 存4/存3.
        self.initial_loco = STAGE3_REHOOK_LOCO
        self.meta = {rv.car_no(car): dict(car) for car in self.initial_cars}
        self.caps = {
            rv.norm(item["Line"]): 7 if item["IsInspectionMode"] else 5
            for item in request["TerminalLines"]
        }
        self.graph = physical.TrackGraph()
        self.time_budget_seconds = time_budget_seconds
        self.started_at = time.monotonic()
        self.global_deadline = self.started_at + time_budget_seconds
        self.deadline = self.global_deadline
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}
        self.cars_cache: dict[State, tuple[dict[str, Any], ...]] = {}
        self.line_map_cache: dict[State, dict[str, tuple[str, ...]]] = {}
        self.duplicate_nos = self.find_duplicate_nos()
        self.depot_target_nos = self.find_depot_target_nos()
        self.restoration_nos: set[str] = set()
        self.task_nos = self.find_stage3_task_nos()
        self.stage3_business_nos = self.task_nos & self.depot_target_nos
        self.active_nos = self.find_active_nos(self.task_nos)
        self.restoration_nos = self.active_nos - self.task_nos
        deepest_task_by_source: dict[str, int] = {}
        for no in self.task_nos:
            car = self.meta[no]
            if car["Line"] in set(STAGE3_SOURCE_LINES):
                deepest_task_by_source[car["Line"]] = max(
                    deepest_task_by_source.get(car["Line"], 0),
                    int(car.get("Position") or 0),
                )
        self.restoration_position_nos = {
            no
            for no in self.restoration_nos
            if self.meta[no]["Line"] in deepest_task_by_source
            and int(self.meta[no].get("Position") or 0)
            <= deepest_task_by_source[self.meta[no]["Line"]]
        }
        self.fixed_cars = [dict(car) for car in self.initial_cars if rv.car_no(car) not in self.active_nos]
        self.fixed_by_no = {rv.car_no(car): dict(car) for car in self.fixed_cars}
        self.fixed_positioned_positions = self.build_fixed_positioned_positions()
        self.fixed_outer_lines = {
            car["Line"] for car in self.fixed_cars if car["Line"] in set(DEPOT_OUT)
        }
        self.assigned_slot_by_no: dict[str, tuple[str, int]] = {}
        self.assignment_reasons: tuple[str, ...] = ()
        self.assignment_cache: dict[
            tuple[str, str],
            tuple[dict[str, str], dict[str, tuple[str, int]], tuple[str, ...]],
        ] = {}
        self.pickup_cache: dict[
            tuple[str, str],
            tuple[State, tuple[Op, ...]] | SearchResult,
        ] = {}
        self.candidate_evaluation_cache: dict[
            tuple[State, tuple[Op, ...]],
            dict[str, Any],
        ] = {}
        self.assigned_line_by_no: dict[str, str] = {}
        self.placement_results: dict[str, placement.SolveResult] = {}
        self.optimization_attempted = 0
        self.optimization_expansions = 0
        self.optimization_budget_exhausted = False
        self.search_space_evaluation_incomplete = False

    def validate_input_contract(self, request: dict[str, Any]) -> None:
        errors: list[str] = []
        for index, car in enumerate(request.get("StartStatus") or []):
            no = str(car.get("No") or f"row_{index}")
            try:
                length = float(car.get("Length"))
            except (TypeError, ValueError):
                length = 0.0
            if length <= 0.0:
                errors.append(f"car_length_missing_or_invalid:{no}")
            if not str(car.get("RepairProcess") or "").strip():
                errors.append(f"repair_process_missing:{no}")
        terminal_lines = request.get("TerminalLines")
        terminal_rows = terminal_lines if isinstance(terminal_lines, list) else []
        configured_depot_lines = [
            rv.norm(item.get("Line"))
            for item in terminal_rows
            if isinstance(item, dict) and rv.norm(item.get("Line")) in set(DEPOT_IN)
        ]
        missing_depot_lines = sorted(set(DEPOT_IN) - set(configured_depot_lines))
        if missing_depot_lines:
            errors.append("terminal_capacity_missing:" + ",".join(missing_depot_lines))
        duplicate_depot_lines = sorted(
            line for line, count in Counter(configured_depot_lines).items() if count > 1
        )
        if duplicate_depot_lines:
            errors.append("terminal_capacity_duplicate:" + ",".join(duplicate_depot_lines))
        for item in terminal_rows:
            if not isinstance(item, dict):
                errors.append("terminal_capacity_row_invalid")
                continue
            line = rv.norm(item.get("Line"))
            if line in set(DEPOT_IN) and not isinstance(item.get("IsInspectionMode"), bool):
                errors.append(f"terminal_inspection_mode_missing_or_invalid:{line}")
        loco = request.get("locoNode") or {}
        if not rv.norm(loco.get("Line")):
            errors.append("loco_line_missing")
        if str(loco.get("End") or "") not in {"North", "South"}:
            errors.append("loco_end_missing_or_invalid")
        if errors:
            raise ValueError("stage3_input_contract_invalid:" + "|".join(errors[:20]))

    def find_duplicate_nos(self) -> tuple[str, ...]:
        counts = Counter(rv.car_no(car) for car in self.initial_cars)
        return tuple(sorted(no for no, count in counts.items() if not no or count > 1))

    def find_depot_target_nos(self) -> set[str]:
        return {
            rv.car_no(car)
            for car in self.initial_cars
            if set(car.get("TargetLines") or []) & set(DEPOT_TARGETS)
        }

    def find_stage3_task_nos(self) -> set[str]:
        active: set[str] = set()
        for car in self.initial_cars:
            targets = set(car.get("TargetLines") or [])
            if car["Line"] in set(ASSEMBLY_LINES) and targets & set(DEPOT_TARGETS):
                active.add(rv.car_no(car))
            elif (
                car["Line"] == PREPICKUP_OUTER_SOURCE
                and targets & set(DEPOT_OUT)
                and not (targets & set(DEPOT_IN))
            ):
                active.add(rv.car_no(car))
            elif (
                car["Line"] in set(DEPOT_IN)
                and targets & set(DEPOT_OUT)
                and not (targets & set(DEPOT_IN))
            ):
                active.add(rv.car_no(car))
            elif (
                car["Line"] in set(STAGE4_STAGING_LINES)
                and targets & set(DEPOT_OUT)
                and car["Line"] not in targets
            ):
                active.add(rv.car_no(car))
            elif car["Line"] in set(STAGE4_DEFER_LINES) and targets & STAGE4_DEFER_TARGETS:
                active.add(rv.car_no(car))
            elif (
                car["Line"] in set(DEPOT_IN)
                and car["Line"] in targets
                and not self.slot_allowed_for_car(
                    car,
                    car["Line"],
                    int(car.get("Position") or 0),
                    self.caps[car["Line"]],
                )
            ):
                active.add(rv.car_no(car))
            elif (
                car["Line"] in set(DEPOT_OUT)
                and car["Line"] in targets
                and car.get("_ForcePositions")
                and int(car.get("Position") or 0) not in set(car["_ForcePositions"])
            ):
                active.add(rv.car_no(car))
        return active

    def find_active_nos(self, task_nos: set[str]) -> set[str]:
        """Close business tasks over every physical prefix and depot door they can touch."""
        active = set(task_nos)
        controlled_lines = set(STAGE3_SOURCE_LINES) | set(STAGE4_DEFER_LINES)
        while True:
            before = len(active)
            if active:
                active.update(
                    rv.car_no(car)
                    for car in self.initial_cars
                    if car["Line"] == UNWHEEL
                )
            inner_targets = {
                line
                for no in active
                for line in self.inner_target_lines(no)
            }
            outer_targets = {
                line
                for no in active
                for line in self.outer_target_lines(no)
            }
            touched_outer = outer_targets | {
                DEPOT_OUT_BY_IN[line] for line in inner_targets
            }
            active.update(
                rv.car_no(car)
                for car in self.initial_cars
                if car["Line"] in touched_outer
            )

            deepest_by_line: dict[str, int] = {}
            for no in active:
                car = self.meta[no]
                if car["Line"] not in controlled_lines:
                    continue
                must_move = (
                    not self.terminal_line_satisfied(no, car["Line"])
                    or car["Line"] in {DEPOT_OUT_BY_IN[line] for line in inner_targets}
                )
                if not must_move:
                    continue
                deepest_by_line[car["Line"]] = max(
                    deepest_by_line.get(car["Line"], 0),
                    int(car.get("Position") or 0),
                )
            touched_source_lines = set(deepest_by_line) & set(STAGE3_SOURCE_LINES)
            active.update(
                rv.car_no(car)
                for car in self.initial_cars
                if car["Line"] in touched_source_lines
            )
            active.update(
                rv.car_no(car)
                for car in self.initial_cars
                if car["Line"] in deepest_by_line
                and int(car.get("Position") or 0) <= deepest_by_line[car["Line"]]
            )
            if len(active) == before:
                return active

    def build_fixed_positioned_positions(self) -> dict[str, dict[int, str]]:
        out: dict[str, dict[int, str]] = {line: {} for line in POSITIONED_LINES}
        for car in self.fixed_cars:
            line = car["Line"]
            if line not in out:
                continue
            position = int(car.get("Position") or 0)
            if position > 0:
                out[line][position] = rv.car_no(car)
        return out

    @staticmethod
    def placement_length_units(value: float) -> int:
        return int(round(float(value) * 10))

    def build_placement_problem(
        self,
        template: str,
    ) -> tuple[placement.Problem, dict[str, str]]:
        """Build the single finite terminal domain used by every Stage3 plan."""

        domains: list[placement.CarDomain] = []
        fixed_assignments: dict[str, str] = {}
        positioned_fixed = tuple(
            (line, position)
            for line, by_position in self.fixed_positioned_positions.items()
            for position in by_position
        )
        fixed_factory = tuple(
            (line, position)
            for line, by_position in self.fixed_positioned_positions.items()
            for position, no in by_position.items()
            if line in set(DEPOT_IN)
            and self.repair_process(self.fixed_by_no[no]).startswith("厂")
        )
        outer_base_loads = tuple(
            (
                line,
                self.placement_length_units(sum(
                    float(car["Length"])
                    for car in self.fixed_cars
                    if car["Line"] == line
                )),
            )
            for line in STAGE4_STAGING_LINES
        )
        active_inner_gate_demand = {
            line
            for no in self.active_nos
            for line in (
                {self.meta[no]["Line"]}
                if no in self.restoration_nos
                and self.meta[no]["Line"] in set(DEPOT_IN)
                else self.inner_target_lines(no)
            )
        }

        def staging_gate_conflict(line: str) -> int:
            paired_inner = DEPOT_IN_BY_OUT.get(line)
            return int(
                paired_inner is not None
                and paired_inner in active_inner_gate_demand
            )

        for no in sorted(self.active_nos):
            car = self.meta[no]
            atoms: list[placement.Atom] = []
            restoration_line: str | None = None
            restoration_position: int | None = None
            if no in self.restoration_nos:
                line = car["Line"]
                if line not in set(POSITIONED_LINES) | {UNWHEEL}:
                    fixed_assignments[no] = line
                    continue
                restoration_line = line
                position = int(car.get("Position") or 0)
                restoration_position = position if line in set(POSITIONED_LINES) and position > 0 else None
                atoms.append(placement.Atom(
                    "inner" if line in set(DEPOT_IN) else "outer",
                    line,
                    restoration_position,
                    (0, 0),
                ))
            elif self.is_stage4_deferred(no):
                initial_line = car["Line"]
                initial_position = int(car.get("Position") or 0)

                def staging_cost(line: str) -> int:
                    if line == initial_line and line in set(STAGE4_STAGING_LINES):
                        return 0
                    if initial_line == UNWHEEL:
                        return 100 + initial_position + self.line_rank_for_deferred(line)
                    if line == UNWHEEL:
                        return 10
                    return 1_000 + self.line_rank_for_deferred(line)

                atoms.extend(
                    placement.Atom(
                        "outer",
                        line,
                        None,
                        (staging_gate_conflict(line), staging_cost(line)),
                    )
                    for line in STAGE4_STAGING_LINES
                )
            else:
                forced = tuple(int(value) for value in car.get("_ForcePositions") or () if int(value) > 0)
                for line in sorted(self.inner_target_lines(no)):
                    if DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
                        continue
                    fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
                    usable_limit = min(fixed_positions) - 1 if fixed_positions else self.caps[line]
                    for position in range(1, usable_limit + 1):
                        if self.slot_allowed_for_car(car, line, position, self.caps[line]):
                            atoms.append(placement.Atom(
                                "inner",
                                line,
                                position,
                                (
                                    0,
                                    self.slot_preference_cost(
                                        no,
                                        (line, position),
                                        template,
                                    ),
                                ),
                            ))
                for line in sorted(self.outer_target_lines(no)):
                    positions = forced or (None,)
                    atoms.extend(
                        placement.Atom(
                            "outer",
                            line,
                            position,
                            (0, self.line_number(line)),
                        )
                        for position in positions
                    )
            if not atoms:
                raise ValueError(f"stage3_terminal_domain_empty:{no}")
            domains.append(placement.CarDomain(
                no=no,
                length=self.placement_length_units(float(car["Length"])),
                process=self.repair_process(car),
                atoms=tuple(dict.fromkeys(atoms)),
                restoration_line=restoration_line,
                restoration_position=restoration_position,
            ))

        domain_nos = {domain.no for domain in domains}
        exposure = tuple(
            no for no in self.template_exposure_order(template) if no in domain_nos
        )
        if template == "A":
            first = tuple(no for no in exposure if self.meta[no]["Line"] != TEMPLATE_A_SECOND_LINE)
            second = tuple(no for no in exposure if self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE)
            exposure_segments = tuple(segment for segment in (first, second) if segment)
        else:
            exposure_segments = (exposure,) if exposure else ()
        problem = placement.Problem(
            cars=tuple(domains),
            inner_capacities=tuple((line, self.caps[line]) for line in DEPOT_IN),
            outer_capacities=tuple(
                (line, self.placement_length_units(float(rv.TRACK_LEN[line])))
                for line in STAGE4_STAGING_LINES
            ),
            inner_fixed_positions=tuple(
                item for item in positioned_fixed if item[0] in set(DEPOT_IN)
            ),
            inner_fixed_factory_positions=fixed_factory,
            outer_base_loads=outer_base_loads,
            outer_fixed_positions=tuple(
                item for item in positioned_fixed if item[0] in set(DEPOT_OUT)
            ),
            exposure_segments=exposure_segments,
        )
        return problem, fixed_assignments

    def unified_placements(self, template: str) -> placement.SolveResult:
        problem, fixed_assignments = self.build_placement_problem(template)
        solved = placement.solve(
            problem,
            max_plans=PLACEMENT_MAX_PLANS,
            node_budget=PLACEMENT_NODE_BUDGET,
        )
        self.placement_results[template] = solved
        for index, plan in enumerate(solved.plans):
            layout = f"unified:{template}:{index:02d}"
            line_by_no = dict(fixed_assignments)
            slot_by_no: dict[str, tuple[str, int]] = {}
            for no, atom in plan.assignments:
                line_by_no[no] = atom.line
                if atom.position is not None:
                    slot_by_no[no] = (atom.line, atom.position)
            self.assignment_cache[(template, layout)] = (line_by_no, slot_by_no, ())
        return solved

    def build_assigned_line_by_no(self, template: str, layout: str) -> dict[str, str]:
        cache_key = (template, layout)
        cached = self.assignment_cache.get(cache_key)
        if cached is None:
            raise ValueError(f"unified_stage3_layout_not_built:{template}:{layout}")
        assigned, slots, reasons = cached
        self.assigned_slot_by_no = dict(slots)
        self.assignment_reasons = reasons
        return dict(assigned)

    def slot_preference_cost(self, no: str, slot: tuple[str, int], template: str) -> int:
        car = self.meta[no]
        line, position = slot
        line_no = self.line_number(line)
        cost = 0
        if self.repair_process(car).startswith("厂"):
            cost += 0 if position == 5 else 1
            cost += {4: 0, 2: 1, 1: 2, 3: 3}.get(line_no, 4)
        elif float(car["Length"]) >= 17.6:
            cost += {3: 0, 4: 1}[line_no]
            cost += position
        else:
            cost += 0 if line_no in {1, 2} else 2
            cost += position // 4
            if template == "A" and car.get("Line") == TEMPLATE_A_SECOND_LINE and line_no in {3, 4}:
                cost += 20
        return cost

    def is_stage4_deferred(self, no: str) -> bool:
        car = self.meta.get(no, {})
        targets = set(car.get("TargetLines") or [])
        return bool(targets & STAGE4_DEFER_TARGETS) and not (targets & set(DEPOT_TARGETS))

    def deferred_stage4_target(self, no: str) -> str:
        targets = set(self.meta[no].get("TargetLines") or []) & STAGE4_DEFER_TARGETS
        if "存4线" in targets:
            return "存4线"
        if "油漆线" in targets:
            return "油漆线"
        return sorted(targets)[0] if targets else ""

    def line_rank_for_deferred(self, line: str) -> int:
        order = {
            "卸轮线": 0,
            "修1库内": 11,
            "修1库外": 12,
            "修2库内": 21,
            "修2库外": 22,
            "修3库内": 31,
            "修3库外": 32,
            "修4库内": 41,
            "修4库外": 42,
        }
        return order.get(line, 100)

    def template_exposure_order(self, template: str) -> tuple[str, ...]:
        line_map = self.initial_source_pickup_map()
        prepickup = list(line_map.get(PREPICKUP_OUTER_SOURCE, ()))
        if template == "A":
            first = [*prepickup]
            for line in TEMPLATE_A_FIRST_ORDER:
                first.extend(line_map.get(line, ()))
            second = list(line_map.get(TEMPLATE_A_SECOND_LINE, ()))
            return (*reversed(first), *reversed(second))
        all_nos = [*prepickup]
        for line in TEMPLATE_B_ORDER:
            all_nos.extend(line_map.get(line, ()))
        return tuple(reversed(all_nos))

    def solve(self) -> dict[str, Any]:
        early = self.early_rejections()
        if early:
            failed = SearchResult("partial", "none", None, (), (INF_COST, 0, 0, 0), tuple(early), 0, 0.0)
            return self.result(failed, [failed])

        if not self.active_nos:
            empty = SearchResult(
                status="complete",
                template="none",
                state=self.initial_state_without_pickup(),
                ops=(),
                cost=(0, 0, 0, 0),
                reasons=(),
                expansions=0,
                elapsed_seconds=0.0,
                lower_bound=0,
                lower_bound_scope="no_stage3_work",
            )
            return self.result(empty, [empty])

        placement_specs: list[PlacementSpec] = []
        placement_reasons: list[str] = []
        for template in ("B", "A"):
            solved = self.unified_placements(template)
            if not solved.complete or solved.frontier_truncated:
                self.search_space_evaluation_incomplete = True
            if not solved.plans:
                if solved.hall_witness is not None:
                    witness = solved.hall_witness
                    placement_reasons.append(
                        "inner_hall_infeasible:"
                        f"cars={','.join(witness.cars)}:"
                        f"slots={','.join(f'{line}#{position}' for line, position in witness.slots)}:"
                        f"deficit={witness.deficit}"
                    )
                elif solved.outer_capacity_witness is not None:
                    witness = solved.outer_capacity_witness
                    placement_reasons.append(
                        "outer_subset_capacity_infeasible:"
                        f"cars={','.join(witness.cars)}:"
                        f"lines={','.join(witness.lines)}:"
                        f"demand={witness.demand}:capacity={witness.capacity}:"
                        f"deficit={witness.deficit}"
                    )
                else:
                    placement_reasons.append(f"{solved.reason}:{template}")
                continue
            for index, plan in enumerate(solved.plans):
                layout = f"unified:{template}:{index:02d}"
                self.assigned_line_by_no = self.build_assigned_line_by_no(
                    template,
                    layout,
                )
                lower_bound = sum(
                    self.template_operation_lower_bound_components(template).values()
                )
                placement_specs.append(PlacementSpec(
                    operation_lower_bound=lower_bound,
                    placement_score=plan.score,
                    signature=plan.signature,
                    template=template,
                    layout=layout,
                ))

        placement_specs.sort(
            key=lambda item: (
                item.operation_lower_bound,
                item.placement_score,
                0 if item.template == "B" else 1,
                item.layout,
            )
        )

        if not placement_specs:
            failed = SearchResult(
                status="partial",
                template="unified",
                state=None,
                ops=(),
                cost=(INF_COST, 0, 0, 0),
                reasons=tuple(dict.fromkeys(placement_reasons)) or ("placement_infeasible",),
                expansions=0,
                elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                layout="unified",
                deferred_clear=False,
                terminal_merge=True,
                inner_clear_policy="transaction",
            )
            return self.result(failed, [failed])

        results_by_spec: dict[tuple[str, str], SearchResult] = {}
        self.deadline = self.global_deadline
        for level_index, expansion_limit in enumerate(TRANSACTION_EXPANSION_LEVELS):
            completed_group: tuple[int, tuple[int, ...]] | None = None
            incumbent_cost = min(
                (
                    item.cost
                    for item in results_by_spec.values()
                    if item.status == "complete"
                ),
                default=None,
            )
            strict_improvement = False
            level_specs = placement_specs
            if level_index > 0:
                seen_signatures: set[tuple[tuple[str, str, str, int], ...]] = set()
                diverse: list[PlacementSpec] = []
                variants: list[PlacementSpec] = []
                for spec in placement_specs:
                    target = diverse if spec.signature not in seen_signatures else variants
                    target.append(spec)
                    seen_signatures.add(spec.signature)
                level_specs = [*diverse, *variants]
            if level_index == 1:
                level_specs = self.select_screen_specs(
                    level_specs,
                    results_by_spec,
                )
            if level_index >= 2:
                level_specs = self.select_finalist_specs(
                    placement_specs,
                    results_by_spec,
                )
            for spec_index, spec in enumerate(level_specs):
                if (
                    level_index == 0
                    and completed_group is not None
                    and spec.shallow_group != completed_group
                ):
                    break
                if self.deadline_reached():
                    self.search_space_evaluation_incomplete = True
                    break
                prior = results_by_spec.get((spec.template, spec.layout))
                if prior is not None and prior.status == "complete":
                    continue
                candidate = self.solve_template(
                    spec.template,
                    layout=spec.layout,
                    transaction_expansion_limit=expansion_limit,
                )
                validated = self.validate_candidate(candidate)
                results_by_spec[(spec.template, spec.layout)] = (
                    validated
                    if prior is None
                    else self.choose_result([prior, validated])
                )
                if validated.status == "complete":
                    if incumbent_cost is not None and validated.cost < incumbent_cost:
                        strict_improvement = True
                    if incumbent_cost is None or validated.cost < incumbent_cost:
                        incumbent_cost = validated.cost
                if (
                    level_index == 0
                    and validated.status == "complete"
                    and completed_group is None
                ):
                    # Complete the whole lower-bound/proxy tie group before deciding
                    # whether the shallow search has enough independent plans.
                    completed_group = spec.shallow_group
                complete_count = sum(
                    item.status == "complete"
                    for item in results_by_spec.values()
                )
                static_pair_evaluated = (
                    level_index >= 2
                    and spec_index + 1 >= min(
                        STATIC_FINALIST_PLAN_BUDGET,
                        len(level_specs),
                    )
                )
                deep_complete_after_static_pair = (
                    static_pair_evaluated and complete_count > 0
                )
                if level_index > 0 and (
                    deep_complete_after_static_pair
                    or strict_improvement
                    or complete_count >= SCREEN_COMPLETE_QUORUM
                ):
                    break
            if self.deadline_reached():
                break
            complete_count = sum(
                item.status == "complete" for item in results_by_spec.values()
            )
            if level_index == 0 and complete_count >= SHALLOW_COMPLETE_QUORUM:
                break
            if level_index > 0 and complete_count:
                break
        results = [
            results_by_spec[(spec.template, spec.layout)]
            for spec in placement_specs
            if (spec.template, spec.layout) in results_by_spec
        ]
        if len(results) != len(placement_specs):
            self.search_space_evaluation_incomplete = True
        if not results:
            failed = SearchResult(
                status="partial",
                template="unified",
                state=None,
                ops=(),
                cost=(INF_COST, 0, 0, 0),
                reasons=("stage3_global_time_budget_exhausted",),
                expansions=0,
                elapsed_seconds=round(time.monotonic() - self.started_at, 3),
                layout="unified",
                inner_clear_policy="transaction",
                search_spec_evaluated=False,
            )
            return self.result(failed, [failed])
        chosen = self.choose_result(results)
        return self.result(chosen, results)

    def initial_state_without_pickup(self) -> State:
        line_map: dict[str, list[str]] = {}
        for car in self.initial_cars:
            no = rv.car_no(car)
            if no in self.active_nos:
                line_map.setdefault(car["Line"], []).append(no)
        packed = {
            line: tuple(
                no for _pos, no in sorted(
                    (int(self.meta[no].get("Position") or 0), no) for no in nos
                )
            )
            for line, nos in line_map.items()
        }
        return State(
            lines=self.pack_lines(packed),
            held=(),
            loco=(self.initial_loco,),
            phase=1,
            positioned_positions=self.initial_active_positioned_positions(),
        )

    def early_rejections(self) -> list[str]:
        reasons: list[str] = []
        if self.duplicate_nos:
            reasons.append(f"duplicate_or_empty_car_no:{','.join(self.duplicate_nos)}")
        for no in sorted(self.depot_target_nos - self.active_nos):
            car = self.meta[no]
            targets = set(car.get("TargetLines") or []) & set(DEPOT_TARGETS)
            line = car["Line"]
            if line in targets:
                continue
            if line in set(DEPOT_IN) and line in set(car.get("TargetLines") or []):
                continue
            reasons.append(f"unsupported_stage3_depot_target_source:{no}:{line}->{','.join(sorted(targets))}")
        unsupported_sources = sorted(
            {
                self.meta[no]["Line"]
                for no in self.active_nos
                if self.meta[no]["Line"] not in set(STAGE3_SOURCE_LINES) | set(STAGE4_DEFER_LINES)
            }
        )
        if unsupported_sources:
            reasons.append(f"unsupported_stage3_sources:{','.join(unsupported_sources)}")
        initial_line_map = self.initial_active_line_map()
        for line in DEPOT_IN:
            move = tuple(initial_line_map.get(line, ()))
            if not move:
                continue
            reachable = self.active_prefix(initial_line_map, line)
            if reachable[: len(move)] != move:
                reasons.append(f"pickup_active_not_prefix:{line}:{','.join(move)}")
        for line in DEPOT_OUT:
            bad_fixed = [
                rv.car_no(car)
                for car in self.fixed_cars
                if car["Line"] == line and line not in set(car.get("TargetLines") or [])
            ]
            if bad_fixed:
                reasons.append(f"fixed_car_blocks_depot_outer:{line}:{','.join(sorted(bad_fixed))}")
        for no in sorted(self.active_nos):
            car = self.meta[no]
            depot_targets = set(car.get("TargetLines") or []) & set(DEPOT_TARGETS)
            if (
                not depot_targets
                and not self.is_stage4_deferred(no)
                and no not in self.restoration_nos
            ):
                reasons.append(f"active_without_depot_target:{no}")
            if self.stage3_weigh_pending(no):
                reasons.append(f"active_unweighed:{no}")
        return reasons

    def stage3_weigh_pending(self, no: str) -> bool:
        car = self.meta[no]
        return (
            no in self.stage3_business_nos
            and bool(car["IsWeigh"])
            and not bool(car["_Weighed"])
        )

    def clear_state_materialization_caches(self) -> None:
        """Release state-derived rows between independent layout searches."""

        self.cars_cache.clear()
        self.line_map_cache.clear()

    def solve_template(
        self,
        template: str,
        *,
        layout: str,
        transaction_expansion_limit: int = TRANSACTION_EXPANSION_LEVELS[-1],
    ) -> SearchResult:
        started = time.monotonic()
        if transaction_expansion_limit <= 0:
            raise ValueError("transaction_expansion_limit_must_be_positive")
        self.clear_state_materialization_caches()
        if self.deadline_reached():
            return SearchResult(
                status="partial",
                template=template,
                state=None,
                ops=(),
                cost=(INF_COST, 0, 0, 0),
                reasons=("stage3_global_time_budget_exhausted",),
                expansions=0,
                elapsed_seconds=round(time.monotonic() - started, 3),
                layout=layout,
                deferred_clear=False,
                terminal_merge=True,
                inner_clear_policy="transaction",
                search_spec_evaluated=False,
            )
        self.assigned_line_by_no = self.build_assigned_line_by_no(template, layout)
        if self.assignment_reasons:
            return SearchResult(
                status="partial",
                template=template,
                state=None,
                ops=(),
                cost=(INF_COST, 0, 0, 0),
                reasons=self.assignment_reasons,
                expansions=0,
                elapsed_seconds=round(time.monotonic() - started, 3),
                layout=layout,
                deferred_clear=False,
                terminal_merge=True,
                inner_clear_policy="transaction",
            )
        lower_bound_components = self.template_operation_lower_bound_components(template)
        lower_bound = sum(lower_bound_components.values())

        def annotate(result: SearchResult) -> SearchResult:
            return replace(
                result,
                layout=layout,
                deferred_clear=False,
                terminal_merge=True,
                inner_clear_policy="transaction",
                lower_bound=lower_bound,
                lower_bound_components=tuple(sorted(lower_bound_components.items())),
                lower_bound_scope="fixed_template_layout_relaxation",
            )
        if template == "B":
            pickup_order = TEMPLATE_B_ORDER
            phase = 1
        elif template == "A":
            pickup_order = TEMPLATE_A_FIRST_ORDER
            phase = 0
        else:
            raise ValueError(f"unknown_template:{template}")

        pickup_key = (template, layout)
        built = self.pickup_cache.get(pickup_key)
        if built is None:
            built_raw = self.apply_pickup_template(
                pickup_order,
                phase=phase,
                template=template,
            )
            built = (
                (built_raw[0], tuple(built_raw[1]))
                if isinstance(built_raw, tuple)
                else built_raw
            )
            self.pickup_cache[pickup_key] = built
        if isinstance(built, SearchResult):
            return annotate(built)
        state, pickup_ops = built
        if self.complete(state):
            return annotate(SearchResult(
                status="complete",
                template=template,
                state=state,
                ops=tuple(pickup_ops),
                cost=self.ops_cost(pickup_ops),
                reasons=(),
                expansions=0,
                elapsed_seconds=round(time.monotonic() - started, 3),
                committed_count=len(self.active_nos),
            ))
        return annotate(self.plan_transactions(
            template,
            state,
            tuple(pickup_ops),
            started,
            expansion_limit=transaction_expansion_limit,
        ))

    def stable_closure(
        self,
        state: State,
        seed: frozenset[str],
    ) -> frozenset[str]:
        line_map = self.line_map(state)
        line_by_no = {
            no: line
            for line, nos in line_map.items()
            for no in nos
        }
        position_by_no = dict(state.positioned_positions)
        closure = set(seed)
        changed = True
        while changed:
            changed = False
            for no in self.active_nos - closure:
                if no in set(state.held):
                    continue
                assigned = self.assigned_line_by_no.get(no, "")
                line = line_by_no.get(no, "")
                if not assigned or line != assigned:
                    continue
                slot = self.assigned_slot_by_no.get(no)
                if slot is not None and position_by_no.get(no) != slot[1]:
                    continue
                if line in set(DEPOT_IN):
                    deeper = {
                        other
                        for other, other_slot in self.assigned_slot_by_no.items()
                        if other_slot[0] == line and other_slot[1] > (slot[1] if slot else 0)
                    }
                    if not deeper <= closure:
                        continue
                elif line in set(DEPOT_OUT):
                    paired_inner = DEPOT_IN_BY_OUT[line]
                    pending_inner = {
                        other
                        for other, target in self.assigned_line_by_no.items()
                        if target == paired_inner
                    }
                    if not pending_inner <= closure:
                        continue
                elif no in self.restoration_position_nos:
                    initial_position = int(self.meta[no].get("Position") or 0)
                    if line_map.get(line, ()).index(no) + 1 != initial_position:
                        continue
                closure.add(no)
                changed = True
        return frozenset(closure)

    def committed_projection(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[tuple[str, str, int], ...]:
        line_map = self.line_map(state)
        position_by_no = dict(state.positioned_positions)
        rows: list[tuple[str, str, int]] = []
        for no in sorted(committed):
            found = [line for line, nos in line_map.items() if no in nos]
            if not found:
                rows.append((no, "", 0))
                continue
            line = found[0]
            if line in set(DEPOT_OUT) and self.assigned_slot_by_no.get(no) is None:
                committed_order = tuple(
                    item for item in line_map[line] if item in committed
                )
                position = committed_order.index(no) + 1
            else:
                position = position_by_no.get(no, line_map[line].index(no) + 1)
            rows.append((no, line, position))
        return tuple(rows)

    def transaction_neighbors(
        self,
        state: State,
        template: str,
    ) -> Iterable[transactions.LegalTransition[State, Op]]:
        emitted: set[State] = set()
        for op, next_state, reject in self.neighbors(state, template):
            if reject:
                continue
            if next_state in emitted:
                continue
            emitted.add(next_state)
            yield transactions.LegalTransition(op, next_state)

    def capacity_exchange_transactions(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[transactions.Transaction[State, Op, str], ...]:
        """Atomically exchange an accessible staging prefix for one pending car."""

        line_map = self.line_map(state)
        before = self.stable_closure(state, committed)
        before_projection = self.committed_projection(state, before)
        out: list[transactions.Transaction[State, Op, str]] = []
        for no in sorted(self.active_nos - before):
            if not self.is_stage4_deferred(no) or no in set(state.held):
                continue
            target = self.assigned_line_by_no.get(no, "")
            if target not in set(STAGE4_STAGING_LINES):
                continue
            source = next(
                (line for line, nos in line_map.items() if no in nos),
                "",
            )
            if not source or source == target:
                continue
            ordered_target = tuple(line_map.get(target, ()))
            required = self.length((no,))
            free = float(rv.TRACK_LEN[target]) - self.length(ordered_target) - sum(
                float(car["Length"]) for car in self.fixed_cars if car["Line"] == target
            )
            release: list[str] = []
            released = 0.0
            for blocker in ordered_target:
                if blocker in before:
                    break
                blocker_target = self.assigned_line_by_no.get(blocker, "")
                if blocker_target == target:
                    break
                release.append(blocker)
                released += self.length((blocker,))
                if free + released + rv.TOL >= required:
                    break
            if free + released + rv.TOL < required or not release:
                continue
            release_move = tuple(release)
            release_target = self.common_assigned_target(release_move)
            if release_target not in set(STAGE4_STAGING_LINES) or release_target == target:
                continue
            working = state
            macro_ops: list[Op] = []
            get_release, working, reject = self.apply_get(working, target, release_move)
            if reject:
                continue
            macro_ops.append(replace(get_release, note=f"transaction_release_capacity:{target}"))
            put_release, working, reject = self.apply_put(working, release_target, release_move)
            if reject:
                continue
            macro_ops.append(replace(put_release, note=f"transaction_rehome_prefix:{release_target}"))
            current_source = tuple(self.line_map(working).get(source, ()))
            if not current_source or current_source[0] != no:
                continue
            get_pending, working, reject = self.apply_get(working, source, (no,))
            if reject:
                continue
            macro_ops.append(replace(get_pending, note=f"transaction_collect_deferred:{target}"))
            put_pending, working, reject = self.apply_put(working, target, (no,))
            if reject:
                continue
            macro_ops.append(replace(put_pending, note=f"transaction_commit_deferred:{target}"))
            after = self.stable_closure(working, before)
            if not before < after:
                continue
            if self.committed_projection(working, before) != before_projection:
                continue
            out.append(transactions.Transaction(
                start_state=state,
                end_state=working,
                actions=tuple(macro_ops),
                committed_before=before,
                committed_after=after,
                cost=self.ops_cost(macro_ops),
            ))
        return tuple(out)

    def direct_put_transactions(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[transactions.Transaction[State, Op, str], ...]:
        """Return one-Put transactions that immediately enlarge stable closure."""

        if not state.held:
            return ()
        before = self.stable_closure(state, committed)
        before_projection = self.committed_projection(state, before)
        out: list[transactions.Transaction[State, Op, str]] = []
        emitted: set[tuple[State, frozenset[str]]] = set()
        for line, move in self.put_suffixes(state):
            op, end_state, reject = self.apply_put(state, line, move)
            if reject:
                continue
            after = self.stable_closure(end_state, before)
            key = (end_state, after)
            if (
                not before < after
                or key in emitted
                or self.committed_projection(end_state, before) != before_projection
            ):
                continue
            emitted.add(key)
            action = replace(op, note=f"transaction_direct_put:{line}")
            out.append(transactions.Transaction(
                start_state=state,
                end_state=end_state,
                actions=(action,),
                committed_before=before,
                committed_after=after,
                cost=self.delta(action),
            ))
        return tuple(out)

    def gate_chain_transactions(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[transactions.Transaction[State, Op, str], ...]:
        """Build arbitrary-depth acyclic door chains and commit them atomically."""

        before = self.stable_closure(state, committed)
        before_projection = self.committed_projection(state, before)
        results: list[transactions.Transaction[State, Op, str]] = []

        def close_inner(
            working: State,
            move: tuple[str, ...],
            target: str,
            seen_targets: frozenset[str],
        ) -> tuple[tuple[Op, ...], State] | None:
            if target in seen_targets or not self.depot_move_is_ready(working, target, move):
                return None
            outer = DEPOT_OUT_BY_IN[target]
            gate = tuple(self.line_map(working).get(outer, ()))
            ops: list[Op] = []
            if gate:
                get_gate, working, reject = self.apply_get(working, outer, gate)
                if reject:
                    return None
                ops.append(replace(get_gate, note=f"transaction_gate_chain_get:{target}"))
                gate_target = self.common_assigned_target(gate)
                if gate_target in set(DEPOT_IN):
                    nested = close_inner(
                        working,
                        gate,
                        gate_target,
                        seen_targets | {target},
                    )
                    if nested is None:
                        return None
                    nested_ops, working = nested
                    ops.extend(nested_ops)
                elif gate_target in set(STAGE4_STAGING_LINES) and gate_target != outer:
                    put_gate, working, reject = self.apply_put(working, gate_target, gate)
                    if reject:
                        return None
                    ops.append(replace(
                        put_gate,
                        note=f"transaction_gate_chain_rehome:{gate_target}",
                    ))
                else:
                    return None
            put_inner, working, reject = self.apply_put(working, target, move)
            if reject:
                return None
            ops.append(replace(put_inner, note=f"transaction_gate_chain_commit:{target}"))
            return tuple(ops), working

        for start in range(len(state.held)):
            move = state.held[start:]
            target = self.common_assigned_target(move)
            if target not in set(DEPOT_IN):
                continue
            closed = close_inner(state, move, target, frozenset())
            if closed is None:
                continue
            macro_ops, end_state = closed
            after = self.stable_closure(end_state, before)
            if not before < after:
                continue
            if self.committed_projection(end_state, before) != before_projection:
                continue
            results.append(transactions.Transaction(
                start_state=state,
                end_state=end_state,
                actions=macro_ops,
                committed_before=before,
                committed_after=after,
                cost=self.ops_cost(macro_ops),
            ))
        return tuple(results)

    def alignment_transactions(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[transactions.Transaction[State, Op, str], ...]:
        """Enumerate staging sequences that expose and commit a deepest slot."""

        if not state.held:
            return ()
        before = self.stable_closure(state, committed)
        before_projection = self.committed_projection(state, before)
        position_by_no = {
            no: position
            for no, (_line, position) in self.assigned_slot_by_no.items()
        }
        results: list[transactions.Transaction[State, Op, str]] = []
        for inner in DEPOT_IN:
            pending = [
                no
                for no, slot in self.assigned_slot_by_no.items()
                if slot[0] == inner
                and no not in before
                and no in set(state.held)
            ]
            if not pending:
                continue
            deepest = max(pending, key=lambda no: (position_by_no[no], no))
            index = state.held.index(deepest)
            if index == len(state.held) - 1:
                continue
            serial = itertools.count()
            staging_frontier: list[
                tuple[
                    int,
                    tuple[int, int, int, int],
                    int,
                    State,
                    tuple[Op, ...],
                ]
            ] = [(len(state.held), (0, 0, 0, 0), next(serial), state, ())]
            best_staging_cost = {state: (0, 0, 0, 0)}
            expanded = 0
            while staging_frontier and expanded < ALIGNMENT_STATE_BUDGET:
                if self.deadline_reached():
                    self.search_space_evaluation_incomplete = True
                    break
                _held_count, staged_cost, _serial, working, staged_ops = heapq.heappop(
                    staging_frontier
                )
                if best_staging_cost.get(working) != staged_cost:
                    continue
                expanded += 1
                if not working.held or deepest not in set(working.held):
                    continue
                deepest_index = working.held.index(deepest)
                if deepest_index == len(working.held) - 1:
                    for chain in self.gate_chain_transactions(working, before):
                        if deepest not in chain.newly_committed:
                            continue
                        macro_ops = (*staged_ops, *chain.actions)
                        after = self.stable_closure(chain.end_state, before)
                        if not before < after:
                            continue
                        if (
                            self.committed_projection(chain.end_state, before)
                            != before_projection
                        ):
                            continue
                        results.append(transactions.Transaction(
                            start_state=state,
                            end_state=chain.end_state,
                            actions=macro_ops,
                            committed_before=before,
                            committed_after=after,
                            cost=self.ops_cost(macro_ops),
                        ))
                    continue

                run_target = self.assigned_line_by_no.get(working.held[-1], "")
                run_start = len(working.held) - 1
                while (
                    run_start > deepest_index + 1
                    and self.assigned_line_by_no.get(working.held[run_start - 1], "")
                    == run_target
                ):
                    run_start -= 1
                for chunk_start in range(run_start, len(working.held)):
                    move = working.held[chunk_start:]
                    pending_inners = self.pending_inner_targets(
                        working,
                        exclude=set(move),
                    )
                    candidate_lines = sorted(
                        STAGE4_STAGING_LINES,
                        key=lambda line: (
                            line != UNWHEEL and DEPOT_IN_BY_OUT[line] in pending_inners,
                            line != run_target,
                            line != UNWHEEL,
                            self.line_number(line),
                        ),
                    )
                    for line in candidate_lines:
                        if not self.put_candidate_allowed(working, line, move):
                            continue
                        op, next_state, reject = self.apply_put(working, line, move)
                        if reject:
                            continue
                        if (
                            self.committed_projection(next_state, before)
                            != before_projection
                        ):
                            continue
                        next_ops = (*staged_ops, replace(
                            op,
                            note=f"transaction_align_stage:{inner}",
                        ))
                        next_cost = self.ops_cost(next_ops)
                        incumbent = best_staging_cost.get(next_state)
                        if incumbent is not None and incumbent <= next_cost:
                            continue
                        best_staging_cost[next_state] = next_cost
                        heapq.heappush(
                            staging_frontier,
                            (
                                len(next_state.held),
                                next_cost,
                                next(serial),
                                next_state,
                                next_ops,
                            ),
                        )
            if staging_frontier:
                self.search_space_evaluation_incomplete = True

        best_results: dict[
            tuple[State, frozenset[str]],
            transactions.Transaction[State, Op, str],
        ] = {}
        for candidate in results:
            key = (candidate.end_state, candidate.committed_after)
            incumbent = best_results.get(key)
            if incumbent is None or candidate.cost < incumbent.cost:
                best_results[key] = candidate
        return tuple(best_results.values())

    def transaction_progress_key(
        self,
        state: State,
        committed: frozenset[str],
    ) -> tuple[int, int, int, int, int]:
        """Order planner states by monotone closure progress, then obstruction."""

        line_by_no = {
            no: line
            for line, nos in self.line_map(state).items()
            for no in nos
        }
        pending = self.active_nos - committed
        trapped_inner_sources = sum(
            line_by_no.get(no) in set(DEPOT_IN)
            and line_by_no.get(no) != self.assigned_line_by_no.get(no)
            for no in pending
        )
        pending_inner_slots = sum(
            self.assigned_line_by_no.get(no) in set(DEPOT_IN)
            for no in pending
        )
        misplaced_staged_cars = sum(
            line_by_no.get(no) in set(STAGE4_STAGING_LINES)
            and line_by_no.get(no) != self.assigned_line_by_no.get(no)
            for no in pending
        )
        return (
            len(pending),
            trapped_inner_sources,
            pending_inner_slots,
            misplaced_staged_cars,
            len(state.held),
        )

    def plan_transactions(
        self,
        template: str,
        start: State,
        prefix_ops: tuple[Op, ...],
        started: float,
        *,
        expansion_limit: int,
    ) -> SearchResult:
        initial_committed = self.stable_closure(start, frozenset())
        initial_cost = self.ops_cost(prefix_ops)
        initial_progress = self.transaction_progress_key(start, initial_committed)

        serial = itertools.count()
        frontier: list[
            tuple[
                int,
                int,
                int,
                int,
                int,
                tuple[int, int, int, int],
                int,
                State,
                frozenset[str],
                tuple[Op, ...],
            ]
        ] = [
            (
                *initial_progress,
                initial_cost,
                next(serial),
                start,
                initial_committed,
                prefix_ops,
            )
        ]
        best_cost: dict[
            tuple[State, frozenset[str]], tuple[int, int, int, int]
        ] = {(start, initial_committed): initial_cost}
        best_partial = (start, initial_committed, prefix_ops)
        best_budgeted_progress = initial_progress
        expansions = 0
        post_budget_states = 0
        search_complete = True
        exhaustion_reason = ""

        while frontier:
            if self.deadline_reached():
                return SearchResult(
                    status="partial",
                    template=template,
                    state=best_partial[0],
                    ops=best_partial[2],
                    cost=(INF_COST, 0, 0, 0),
                    reasons=("stage3_global_time_budget_exhausted",),
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    search_spec_evaluated=False,
                    committed_count=len(best_partial[1]),
                    budgeted_progress_key=best_budgeted_progress,
                )
            (
                _remaining,
                _trapped_inner_sources,
                _pending_inner_slots,
                _misplaced_staged_cars,
                _held_count,
                path_cost,
                _serial,
                state,
                committed,
                ops,
            ) = heapq.heappop(frontier)
            if best_cost.get((state, committed)) != path_cost:
                continue
            if self.complete(state):
                return SearchResult(
                    status="complete",
                    template=template,
                    state=state,
                    ops=ops,
                    cost=self.ops_cost(ops),
                    reasons=(),
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    # This is the first feasible planner goal under a progress
                    # potential, not an exhausted cost-ordered frontier.
                    search_spec_evaluated=False,
                    committed_count=len(committed),
                    budgeted_progress_key=(0, 0, 0, 0, 0),
                )
            search_budget_open = expansions < expansion_limit
            if not search_budget_open:
                if post_budget_states >= POST_BUDGET_FRONTIER_WIDTH:
                    search_complete = False
                    exhaustion_reason = "transaction_post_budget_frontier_exhausted"
                    break
                post_budget_states += 1
            closed_form = (
                *self.direct_put_transactions(state, committed),
                *self.capacity_exchange_transactions(state, committed),
                *self.gate_chain_transactions(state, committed),
            )
            structural = (
                *closed_form,
                *(
                    self.alignment_transactions(state, committed)
                    if search_budget_open
                    else ()
                ),
            )
            searched_transactions: tuple[
                transactions.Transaction[State, Op, str], ...
            ] = ()
            if search_budget_open:
                remaining_expansions = expansion_limit - expansions
                searched = transactions.enumerate_minimal_transactions(
                    start_state=state,
                    committed=committed,
                    legal_neighbors=lambda current: self.transaction_neighbors(current, template),
                    stable_closure=self.stable_closure,
                    committed_projection=self.committed_projection,
                    action_cost=self.delta,
                    is_closed=lambda _state: True,
                    state_key=lambda current: current,
                    zero_cost=(0, 0, 0, 0),
                    budget=transactions.SearchBudget(
                        deadline=self.deadline,
                        max_expansions=min(
                            remaining_expansions,
                            500 if structural else remaining_expansions,
                        ),
                    ),
                )
                expansions += searched.expansions
                searched_transactions = searched.transactions
                if not searched.search_spec_evaluated:
                    search_complete = False
                    exhaustion_reason = searched.termination.value
            else:
                search_complete = False
                exhaustion_reason = "transaction_expansion_budget_exhausted"
            candidates_by_state: dict[
                tuple[State, frozenset[str]],
                transactions.Transaction[State, Op, str],
            ] = {}
            for candidate in (*structural, *searched_transactions):
                key = (candidate.end_state, candidate.committed_after)
                incumbent = candidates_by_state.get(key)
                if incumbent is None or candidate.cost < incumbent.cost:
                    candidates_by_state[key] = candidate
            candidates = tuple(candidates_by_state.values())
            if not candidates:
                if expansions >= expansion_limit:
                    search_complete = False
                    exhaustion_reason = "transaction_expansion_budget_exhausted"
                continue
            ordered = sorted(
                candidates,
                key=lambda item: (
                    -len(item.newly_committed),
                    self.unsatisfied_active_count(item.end_state),
                    item.cost,
                    tuple((op.action, op.line, op.move) for op in item.actions),
                ),
            )
            for candidate in ordered:
                if not committed < candidate.committed_after:
                    raise RuntimeError("transaction_did_not_expand_stable_closure")
                candidate_ops = (*ops, *candidate.actions)
                candidate_cost = self.ops_cost(candidate_ops)
                key = (candidate.end_state, candidate.committed_after)
                incumbent = best_cost.get(key)
                if incumbent is not None and incumbent <= candidate_cost:
                    continue
                best_cost[key] = candidate_cost
                candidate_remaining = len(
                    self.active_nos - candidate.committed_after
                )
                if (
                    search_budget_open
                    and candidate_remaining < len(self.active_nos - best_partial[1])
                ):
                    best_partial = (
                        candidate.end_state,
                        candidate.committed_after,
                        candidate_ops,
                    )
                candidate_progress = self.transaction_progress_key(
                    candidate.end_state,
                    candidate.committed_after,
                )
                if search_budget_open:
                    best_budgeted_progress = min(
                        best_budgeted_progress,
                        candidate_progress,
                    )
                heapq.heappush(
                    frontier,
                    (
                        *candidate_progress,
                        candidate_cost,
                        next(serial),
                        candidate.end_state,
                        candidate.committed_after,
                        candidate_ops,
                    ),
                )

        return SearchResult(
            status="partial",
            template=template,
            state=best_partial[0],
            ops=best_partial[2],
            cost=(INF_COST, 0, 0, 0),
            reasons=(
                exhaustion_reason
                if not search_complete and exhaustion_reason
                else "no_strict_progress_transaction",
            ),
            expansions=expansions,
            elapsed_seconds=round(time.monotonic() - started, 3),
            search_spec_evaluated=search_complete,
            committed_count=len(best_partial[1]),
            budgeted_progress_key=best_budgeted_progress,
        )

    def template_operation_lower_bound_components(self, template: str) -> dict[str, int]:
        """Cheap admissible hook bound for one fixed template/layout search spec."""
        used_inner = {
            line
            for no, line in self.assigned_line_by_no.items()
            if line in set(DEPOT_IN) and self.inner_target_lines(no)
        }
        must_move = {
            no
            for no in self.active_nos
            if not self.terminal_line_satisfied(no, self.meta[no]["Line"])
            or (
                self.meta[no]["Line"] in set(DEPOT_OUT)
                and DEPOT_IN_BY_OUT[self.meta[no]["Line"]] in used_inner
            )
        }
        source_gets = len({self.meta[no]["Line"] for no in must_move})

        exposure = self.template_exposure_order(template)
        exposure_index = {no: index for index, no in enumerate(exposure)}
        inner_groups = {
            (
                self.assigned_line_by_no[no],
                1 if template == "A" and self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE else 0,
            )
            for no in must_move
            if self.assigned_line_by_no.get(no) in set(DEPOT_IN)
            and self.inner_target_lines(no)
        }
        inner_puts = len(inner_groups)
        for line in used_inner:
            assigned = [
                no
                for no in must_move
                if self.assigned_line_by_no.get(no) == line and no in exposure_index
            ]
            section_indexes = [
                exposure_index[no]
                for no in assigned
                if self.repair_process(self.meta[no]).startswith("段")
            ]
            factory_indexes = [
                exposure_index[no]
                for no in assigned
                if self.repair_process(self.meta[no]).startswith("厂")
            ]
            if section_indexes and factory_indexes and min(section_indexes) < max(factory_indexes):
                inner_puts += 1

        non_inner = [
            no
            for no in must_move
            if self.assigned_line_by_no.get(no) not in set(DEPOT_IN)
        ]
        non_inner_puts = self.minimum_non_inner_terminal_lines(non_inner)

        frontier_debt = 0
        epochs = (0, 1) if template == "A" else (0,)
        for epoch in epochs:
            pending_inner = {
                self.assigned_line_by_no[no]
                for no in must_move
                if self.assigned_line_by_no.get(no) in set(DEPOT_IN)
                and self.inner_target_lines(no)
                and (
                    1
                    if template == "A" and self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE
                    else 0
                )
                >= epoch
            }
            epoch_exposure = [
                no
                for no in exposure
                if (1 if template == "A" and self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE else 0)
                == epoch
            ]
            if not epoch_exposure:
                continue
            first = epoch_exposure[0]
            if self.inner_target_lines(first) or not self.outer_target_lines(first):
                continue
            if pending_inner and all(
                DEPOT_IN_BY_OUT[line] in pending_inner
                for line in self.outer_target_lines(first)
            ):
                frontier_debt = 1
                break
        return {
            "source_gets": source_gets,
            "inner_puts": inner_puts,
            "non_inner_puts": non_inner_puts,
            "frontier_rehandle": frontier_debt,
        }

    def minimum_non_inner_terminal_lines(self, nos: list[str]) -> int:
        if not nos:
            return 0
        fixed_return_lines = {
            self.assigned_line_by_no[no]
            for no in nos
            if no in self.restoration_nos
            and self.assigned_line_by_no[no] not in set(STAGE4_STAGING_LINES)
        }
        packing_nos = [
            no
            for no in nos
            if no not in self.restoration_nos
            or self.assigned_line_by_no[no] in set(STAGE4_STAGING_LINES)
        ]
        fixed_return_puts = len(fixed_return_lines)
        if not packing_nos:
            return fixed_return_puts
        fixed_load = {
            line: sum(
                float(car["Length"])
                for car in self.fixed_cars
                if car["Line"] == line
            )
            for line in STAGE4_STAGING_LINES
        }
        remaining = {
            line: float(rv.TRACK_LEN.get(line) or 0.0) - fixed_load[line]
            for line in STAGE4_STAGING_LINES
        }
        allowed = {
            no: (
                {self.assigned_line_by_no[no]}
                if no in self.restoration_nos
                else set(STAGE4_STAGING_LINES)
                if self.is_stage4_deferred(no)
                else self.outer_target_lines(no)
            )
            for no in packing_nos
        }
        ordered = sorted(
            packing_nos,
            key=lambda no: (len(allowed[no]), -self.length((no,)), no),
        )

        def pack(index: int, chosen: frozenset[str]) -> bool:
            if index == len(ordered):
                return True
            no = ordered[index]
            car_length = self.length((no,))
            for line in sorted(allowed[no] & set(chosen)):
                if remaining[line] + rv.TOL < car_length:
                    continue
                remaining[line] -= car_length
                if pack(index + 1, chosen):
                    return True
                remaining[line] += car_length
            return False

        lines = tuple(STAGE4_STAGING_LINES)
        for count in range(1, len(lines) + 1):
            for mask in range(1, 1 << len(lines)):
                chosen = frozenset(lines[index] for index in range(len(lines)) if mask & (1 << index))
                if len(chosen) != count:
                    continue
                snapshot = dict(remaining)
                if pack(0, chosen):
                    return fixed_return_puts + count
                remaining.update(snapshot)
        return fixed_return_puts + len(lines)

    def deadline_reached(self) -> bool:
        return time.monotonic() >= self.deadline

    def common_assigned_target(self, nos: tuple[str, ...]) -> str:
        if not nos:
            return ""
        targets = {self.assigned_line_by_no.get(no, "") for no in nos}
        if len(targets) != 1:
            return ""
        target = next(iter(targets))
        allowed = set(DEPOT_TARGETS) | set(STAGE4_DEFER_LINES) | set(STAGE3_SOURCE_LINES)
        return target if target in allowed else ""

    def apply_pickup_template(
        self,
        pickup_order: tuple[str, ...],
        *,
        phase: int,
        template: str,
    ) -> tuple[State, list[Op]] | SearchResult:
        line_map = self.initial_active_line_map()
        pickup_map = self.initial_source_pickup_map()
        state = State(
            lines=self.pack_lines(line_map),
            held=(),
            loco=(self.initial_loco,),
            phase=phase,
            positioned_positions=self.initial_active_positioned_positions(),
        )
        ops: list[Op] = []
        pre_move = tuple(pickup_map.get(PREPICKUP_OUTER_SOURCE, ()))
        if pre_move:
            reachable = self.active_prefix(line_map, PREPICKUP_OUTER_SOURCE)
            if reachable[: len(pre_move)] != pre_move:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=tuple(ops),
                    cost=(INF_COST, 0, 0, 0),
                    reasons=(f"pickup_active_not_prefix:{PREPICKUP_OUTER_SOURCE}",),
                    expansions=0,
                    elapsed_seconds=0.0,
                )
            op, next_state, reject = self.apply_get(
                state,
                PREPICKUP_OUTER_SOURCE,
                pre_move,
                allow_source=True,
            )
            if reject:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=tuple(ops),
                    cost=(INF_COST, 0, 0, 0),
                    reasons=(f"pickup_{reject}",),
                    expansions=0,
                    elapsed_seconds=0.0,
                )
            ops.append(replace(op, note=f"pickup:{template}:{PREPICKUP_OUTER_SOURCE}"))
            state = next_state
            line_map = self.line_map(state)

        for line in pickup_order:
            move = tuple(pickup_map.get(line, ()))
            if not move:
                continue
            reachable = self.active_prefix(line_map, line)
            if reachable[: len(move)] != move:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=tuple(ops),
                    cost=(INF_COST, 0, 0, 0),
                    reasons=(f"pickup_active_not_prefix:{line}",),
                    expansions=0,
                    elapsed_seconds=0.0,
                )
            op, next_state, reject = self.apply_get(state, line, move, allow_source=True)
            if reject:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=tuple(ops),
                    cost=(INF_COST, 0, 0, 0),
                    reasons=(f"pickup_{reject}",),
                    expansions=0,
                    elapsed_seconds=0.0,
                )
            ops.append(replace(op, note=f"pickup:{template}:{line}"))
            state = next_state
            line_map = self.line_map(state)

        if template == "B":
            remaining = sorted(
                no
                for line, nos in self.line_map(state).items()
                if line in set(STAGE3_SOURCE_LINES)
                for no in nos
                if no in self.task_nos
            )
            if remaining:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=tuple(ops),
                    cost=(INF_COST, 0, 0, 0),
                    reasons=(f"template_b_unpicked_active:{','.join(remaining)}",),
                    expansions=0,
                    elapsed_seconds=0.0,
                )
        return state, ops

    def initial_active_line_map(self) -> dict[str, tuple[str, ...]]:
        by_line: dict[str, list[tuple[int, str]]] = {}
        for no in self.active_nos:
            car = self.meta[no]
            by_line.setdefault(car["Line"], []).append((int(car.get("Position") or 0), no))
        return {line: tuple(no for _pos, no in sorted(rows)) for line, rows in by_line.items()}

    def initial_source_pickup_map(self) -> dict[str, tuple[str, ...]]:
        line_map = self.initial_active_line_map()
        out: dict[str, tuple[str, ...]] = {}
        for line in STAGE3_SOURCE_LINES:
            task_positions = [
                int(self.meta[no].get("Position") or 0)
                for no in self.task_nos
                if self.meta[no]["Line"] == line
            ]
            if not task_positions:
                continue
            deepest = max(task_positions)
            move = tuple(
                no
                for no in line_map.get(line, ())
                if int(self.meta[no].get("Position") or 0) <= deepest
            )
            if move:
                out[line] = move
        return out

    def initial_active_positioned_positions(self) -> tuple[tuple[str, int], ...]:
        return self.pack_positioned_positions({
            no: int(self.meta[no].get("Position") or 0)
            for no in self.active_nos
            if self.meta[no]["Line"] in set(POSITIONED_LINES)
        })

    def pack_positioned_positions(self, positions: dict[str, int]) -> tuple[tuple[str, int], ...]:
        invalid = sorted(no for no, position in positions.items() if int(position) <= 0)
        if invalid:
            raise ValueError("stage3_positioned_line_position_invalid:" + ",".join(invalid))
        return tuple(sorted((no, int(position)) for no, position in positions.items()))

    def plan_depot_put_positions(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
    ) -> tuple[dict[str, int], str]:
        """Assign explicit accessible slots to one depot Put."""
        if line not in set(DEPOT_IN):
            return {}, f"depot_position_line_invalid:{line}"
        position_by_no = dict(state.positioned_positions)
        existing = tuple(self.line_map(state).get(line, ()))
        missing_existing = sorted(no for no in existing if no not in position_by_no)
        if missing_existing:
            return {}, "depot_existing_position_missing:" + ",".join(missing_existing)
        occupied = {
            *self.fixed_positioned_positions.get(line, {}).keys(),
            *(position_by_no[no] for no in existing),
        }
        capacity = self.caps[line]
        first_existing = min(occupied, default=capacity + 1)
        available = set(range(1, first_existing)) - occupied
        planned: dict[str, int] = {}
        next_position = first_existing
        for no in reversed(move):
            assigned_slot = self.assigned_slot_by_no.get(no)
            is_final_inner = (
                self.assigned_line_by_no.get(no) == line
                and bool(self.inner_target_lines(no))
            )
            if is_final_inner:
                if assigned_slot is None or assigned_slot[0] != line:
                    return {}, f"depot_assigned_slot_missing:{no}:{line}"
                candidates = [assigned_slot[1]]
            else:
                candidates = [
                    candidate
                    for candidate in sorted(available, reverse=True)
                    if not is_final_inner
                    or self.slot_allowed_for_car(self.meta[no], line, candidate, capacity)
                ]
            position = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate in available and candidate < next_position
                ),
                0,
            )
            if position <= 0:
                return {}, f"depot_put_position_unavailable:{no}:{line}"
            if is_final_inner and not self.slot_allowed_for_car(
                self.meta[no], line, position, capacity
            ):
                return {}, f"depot_slot_rule_violation:{no}:{line}:{position}"
            planned[no] = position
            available.remove(position)
            next_position = position
        return {no: planned[no] for no in move}, ""

    def plan_outer_put_positions(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
    ) -> tuple[dict[str, int], str]:
        """Assign one explicit north-to-south position vector on a depot lead."""
        if line not in set(DEPOT_OUT):
            return {}, f"outer_position_line_invalid:{line}"
        position_by_no = dict(state.positioned_positions)
        existing = tuple(self.line_map(state).get(line, ()))
        missing_existing = sorted(no for no in existing if no not in position_by_no)
        if missing_existing:
            return {}, "outer_existing_position_missing:" + ",".join(missing_existing)
        occupied = {
            *self.fixed_positioned_positions.get(line, {}).keys(),
            *(position_by_no[no] for no in existing),
        }
        reserved = self.pending_outer_reserved_positions(
            state,
            line,
            exclude=set(move),
        )
        forced_positions = [
            int(position)
            for no in move
            if self.terminal_line_satisfied(no, line)
            for position in tuple(self.meta[no].get("_ForcePositions") or ())
        ]
        upper_exclusive = (
            min(occupied)
            if occupied
            else max(
                [len(move) + len(reserved), *forced_positions, *reserved],
                default=len(move),
            ) + 1
        )
        available = tuple(
            position
            for position in range(1, upper_exclusive)
            if position not in occupied and position not in reserved
        )
        domains: dict[str, tuple[int, ...]] = {}
        for no in move:
            forced = tuple(self.meta[no].get("_ForcePositions") or ())
            if forced and self.terminal_line_satisfied(no, line):
                domains[no] = tuple(sorted(int(position) for position in forced))
                continue
            domains[no] = available

        planned: dict[str, int] = {}

        def assign(index: int, previous: int) -> bool:
            if index == len(move):
                return True
            no = move[index]
            for position in domains[no]:
                if position <= previous or position not in available or position in planned.values():
                    continue
                planned[no] = position
                if assign(index + 1, position):
                    return True
                planned.pop(no)
            return False

        if not assign(0, 0):
            return {}, f"outer_put_position_unavailable:{line}:{','.join(move)}"
        return {no: planned[no] for no in move}, ""

    def project_outer_put(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
    ) -> tuple[dict[str, int], dict[str, int], str, str]:
        """Project one of the two protocol-defined depot-lead Put modes."""
        existing = tuple(self.line_map(state).get(line, ()))
        managed = (*move, *existing)
        reserved = self.pending_outer_reserved_positions(
            state,
            line,
            exclude=set(move),
        )
        has_forced_position = any(
            self.meta[no].get("_ForcePositions")
            and self.terminal_line_satisfied(no, line)
            for no in managed
        ) or bool(reserved)
        if not has_forced_position:
            if self.fixed_positioned_positions.get(line):
                return {}, {}, "", f"outer_compact_has_unmanaged_occupants:{line}"
            projected = {no: index for index, no in enumerate(managed, start=1)}
            return {}, projected, "compact_shift", ""

        operation_positions, reject = self.plan_outer_put_positions(state, line, move)
        if reject:
            return {}, {}, "", reject
        return operation_positions, operation_positions, "preserve_sparse", ""

    def pending_outer_reserved_positions(
        self,
        state: State,
        line: str,
        *,
        exclude: set[str],
    ) -> set[int]:
        line_by_no = {
            no: current_line
            for current_line, nos in self.line_map(state).items()
            for no in nos
        }
        reserved: set[int] = set()
        for no in self.active_nos - exclude:
            if self.assigned_line_by_no.get(no) != line or line_by_no.get(no) == line:
                continue
            assigned = self.assigned_slot_by_no.get(no)
            if assigned is not None and assigned[0] == line:
                reserved.add(assigned[1])
                continue
            forced = tuple(self.meta[no].get("_ForcePositions") or ())
            if len(forced) == 1:
                reserved.add(int(forced[0]))
        return reserved

    def active_prefix(self, line_map: dict[str, tuple[str, ...]], line: str) -> tuple[str, ...]:
        active_on_line = set(line_map.get(line, ()))
        out: list[str] = []
        for car in sorted(
            (item for item in self.initial_cars if item["Line"] == line),
            key=lambda item: (int(item.get("Position") or 0), rv.car_no(item)),
        ):
            no = rv.car_no(car)
            if no not in active_on_line:
                break
            out.append(no)
        return tuple(out)

    def unsatisfied_active_count(self, state: State) -> int:
        line_map = self.line_map(state)
        count = len(state.held)
        for line, nos in line_map.items():
            for no in nos:
                if not self.terminal_line_satisfied(no, line):
                    count += 1
        return count

    def neighbors(self, state: State, template: str) -> Iterable[tuple[Op, State, str]]:
        if template == "A" and state.phase == 0 and not state.held:
            line_map = self.line_map(state)
            wash = tuple(line_map.get(TEMPLATE_A_SECOND_LINE, ()))
            if wash:
                for cut in range(len(wash), 0, -1):
                    move = wash[:cut]
                    next_phase = 1 if cut == len(wash) else 0
                    op, next_state, reject = self.apply_get(
                        state,
                        TEMPLATE_A_SECOND_LINE,
                        move,
                        allow_source=True,
                        next_phase=next_phase,
                    )
                    yield op, next_state, reject
            else:
                yield Op("Phase", TEMPLATE_A_SECOND_LINE, (), (), state.held, "A_second_empty"), State(
                    lines=state.lines,
                    held=state.held,
                    loco=state.loco,
                    phase=1,
                    positioned_positions=state.positioned_positions,
                ), ""

        if state.held:
            blocking_gets = list(self.blocking_inner_get_prefixes(state))
            if blocking_gets:
                for line, move in blocking_gets:
                    op, next_state, reject = self.apply_get(state, line, move)
                    yield op, next_state, reject
            put_results: list[tuple[Op, State, str]] = []
            for line, move in self.put_suffixes(state):
                op, next_state, reject = self.apply_put(state, line, move)
                put_results.append((op, next_state, reject))
            for item in put_results:
                yield item
            # A legal immediate put is not proof that every useful get is
            # dominated.  Clearing a buffer first can be required to avoid a
            # later dead end, so keep those physical alternatives in search.

        for line, move in self.get_prefixes(state):
            op, next_state, reject = self.apply_get(state, line, move)
            yield op, next_state, reject

    def blocking_inner_get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        if not state.held:
            return
        line_map = self.line_map(state)
        held_inner_targets = {
            target
            for no in state.held
            for target in (self.assigned_line_by_no.get(no, ""),)
            if target in set(DEPOT_IN)
        }
        for line in DEPOT_IN:
            if line not in held_inner_targets:
                continue
            ordered = tuple(line_map.get(line, ()))
            if not ordered:
                continue
            leading_deferred: list[str] = []
            for no in ordered:
                if self.is_stage4_deferred(no):
                    leading_deferred.append(no)
                    continue
                break
            if leading_deferred:
                yield line, tuple(leading_deferred)
                continue
            if all(self.terminal_line_satisfied(no, line) for no in ordered):
                continue
            for cut in range(len(ordered), 0, -1):
                yield line, ordered[:cut]

    def put_suffixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        held = state.held
        if not held:
            return
        candidate_lines = (*DEPOT_IN, *DEPOT_OUT, UNWHEEL, *STAGE3_SOURCE_LINES)
        for line in candidate_lines:
            for cut in range(0, len(held)):
                move = held[cut:]
                if not move:
                    continue
                if not self.put_candidate_allowed(state, line, move):
                    continue
                # Longer suffixes are yielded first because they dominate on
                # hook count when capacity and terminal feasibility permit it.
                yield line, move

    def put_candidate_allowed(self, state: State, line: str, move: tuple[str, ...]) -> bool:
        assigned = {self.assigned_line_by_no.get(no, "") for no in move}
        move_is_deferred = all(self.is_stage4_deferred(no) for no in move)
        if len(assigned) == 1:
            target = next(iter(assigned))
            if move_is_deferred:
                if line in set(DEPOT_IN):
                    if line != target:
                        return False
                    pending_before = state.held[: len(state.held) - len(move)]
                    if any(
                        self.assigned_line_by_no.get(no) == line and not self.is_stage4_deferred(no)
                        for no in pending_before
                    ):
                        return False
                    _positions, position_reject = self.plan_depot_put_positions(state, line, move)
                    return not position_reject
                return line in set(STAGE4_STAGING_LINES)
            if target in set(STAGE3_SOURCE_LINES):
                return line == target and all(no in self.restoration_nos for no in move)
            if line == target:
                if line in set(DEPOT_IN):
                    if not self.depot_move_is_ready(state, line, move):
                        return False
                    _positions, position_reject = self.plan_depot_put_positions(state, line, move)
                    if position_reject:
                        return False
                return True
            if target in set(DEPOT_IN) and line == DEPOT_OUT_BY_IN[target]:
                return True
            if target in set(DEPOT_IN) and line in set(STAGE4_STAGING_LINES):
                return self.line_has_capacity(state, line, move)
            if target in set(DEPOT_OUT):
                if line in set(DEPOT_OUT) and all(line in self.outer_target_lines(no) for no in move):
                    return True
                if line == DEPOT_IN_BY_OUT[target]:
                    return True
                if line in set(STAGE4_STAGING_LINES):
                    return self.line_has_capacity(state, line, move)
            return False
        if line in set(STAGE3_SOURCE_LINES):
            return all(
                no in self.restoration_nos
                and self.assigned_line_by_no.get(no) == line
                for no in move
            )
        if line not in set(STAGE4_STAGING_LINES):
            return False
        return self.line_has_capacity(state, line, move)

    def depot_move_is_ready(self, state: State, line: str, move: tuple[str, ...]) -> bool:
        if not move or line not in set(DEPOT_IN):
            return False
        line_map = self.line_map(state)
        if any(
            not self.terminal_line_satisfied(no, line)
            for no in line_map.get(line, ())
        ):
            return False
        remaining = sorted(
            (
                slot[1],
                no,
            )
            for no, slot in self.assigned_slot_by_no.items()
            if slot[0] == line and no not in set(line_map.get(line, ()))
        )
        if len(move) > len(remaining):
            return False
        ready = remaining[-len(move):]
        return tuple(no for _position, no in ready) == move

    def pending_inner_targets(self, state: State, *, exclude: set[str]) -> set[str]:
        line_by_no = {
            no: line
            for line, nos in self.line_map(state).items()
            for no in nos
        }
        return {
            target
            for no in self.active_nos - exclude
            for target in (self.assigned_line_by_no.get(no, ""),)
            if target in set(DEPOT_IN)
            and line_by_no.get(no) != target
        }

    def get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        line_map = self.line_map(state)
        # Outside buffers are preferred, but inner depot get-back is part of
        # the stage-3 reordering contract.  It is kept prefix-only; process
        # legality is length/route based, while final slot rules are terminal.
        for line in (*DEPOT_OUT, UNWHEEL, *DEPOT_IN):
            ordered = line_map.get(line, ())
            if not ordered:
                continue
            if (
                self.line_can_stay_terminal(state, line, ordered)
                and not self.outer_blocks_held_inner(state, line)
                and not self.outer_blocks_held_position(state, line)
            ):
                continue
            for cut in range(len(ordered), 0, -1):
                yield line, ordered[:cut]

    def line_can_stay_terminal(
        self,
        state: State,
        line: str,
        ordered: tuple[str, ...],
    ) -> bool:
        if line in set(DEPOT_OUT):
            return self.outer_line_terminal_possible(state, line, ordered)
        if line == UNWHEEL:
            return all(self.terminal_line_satisfied(no, line) for no in ordered)
        if line in set(DEPOT_IN):
            return self.depot_line_terminal_possible(state, line, ordered)
        return False

    def outer_line_terminal_possible(
        self,
        state: State,
        line: str,
        nos: tuple[str, ...],
    ) -> bool:
        if line not in set(DEPOT_OUT):
            return False
        position_by_no = dict(state.positioned_positions)
        if any(no not in position_by_no for no in nos):
            return False
        positions = [position_by_no[no] for no in nos]
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            return False
        fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
        if fixed_positions and any(position >= min(fixed_positions) for position in positions):
            return False
        for no, position in zip(nos, positions):
            if not self.terminal_line_satisfied(no, line):
                return False
            if (
                no not in self.restoration_nos
                and not self.is_stage4_deferred(no)
                and self.assigned_line_by_no.get(no) != line
            ):
                return False
            forced = tuple(self.meta[no].get("_ForcePositions") or ())
            if forced and position not in forced:
                return False
        return True

    def outer_blocks_held_inner(self, state: State, line: str) -> bool:
        if line not in set(DEPOT_OUT):
            return False
        blocked_inner = DEPOT_IN_BY_OUT[line]
        return any(self.assigned_line_by_no.get(no) == blocked_inner for no in state.held)

    def outer_blocks_held_position(self, state: State, line: str) -> bool:
        if line not in set(DEPOT_OUT) or not self.line_map(state).get(line):
            return False
        for start in range(len(state.held)):
            move = state.held[start:]
            if not move or any(self.assigned_line_by_no.get(no) != line for no in move):
                continue
            _positions, _updates, _mode, reject = self.project_outer_put(state, line, move)
            if reject:
                return True
        return False

    def apply_get(
        self,
        state: State,
        line: str,
        move: tuple[str, ...],
        *,
        allow_source: bool = False,
        next_phase: int | None = None,
    ) -> tuple[Op, State, str]:
        line_map = self.line_map(state)
        if not allow_source and line not in set(DEPOT_IN) | set(DEPOT_OUT) | {UNWHEEL}:
            return Op("Get", line, move, (), state.held), state, "get_line_not_allowed"
        if tuple(line_map.get(line, ())[: len(move)]) != move:
            return Op("Get", line, move, (), state.held), state, "get_order_violation"
        if self.pull_equivalent((*state.held, *move)) > rv.PULL_LIMIT:
            return Op("Get", line, move, (), state.held), state, "pull_limit_violation"
        held_after = (*state.held, *move)
        if self.closed_door_train_reject(held_after):
            return Op("Get", line, move, (), state.held), state, "closed_door_process_violation"
        route, reject = self.route(state, "Get", line, move)
        if reject:
            return Op("Get", line, move, (), state.held), state, reject
        next_lines = dict(line_map)
        next_lines[line] = tuple(next_lines.get(line, ())[len(move):])
        positioned_positions = {
            no: position
            for no, position in state.positioned_positions
            if no not in set(move)
        }
        phase = state.phase if next_phase is None else next_phase
        next_state = State(
            lines=self.pack_lines(next_lines),
            held=held_after,
            loco=(line,),
            phase=phase,
            positioned_positions=self.pack_positioned_positions(positioned_positions),
        )
        note = (
            f"retrieve_nonterminal:{line}"
            if any(not self.terminal_line_satisfied(no, line) for no in move)
            else f"clear_terminal_block:{line}"
        )
        return Op("Get", line, move, route, held_after, note=note), next_state, ""

    def apply_put(self, state: State, line: str, move: tuple[str, ...]) -> tuple[Op, State, str]:
        if not move or state.held[-len(move):] != move:
            return Op("Put", line, move, (), state.held), state, "put_tail_order_violation"
        if self.closed_door_train_reject(state.held):
            return Op("Put", line, move, (), state.held), state, "closed_door_process_violation"
        if line in set(DEPOT_IN):
            outer = DEPOT_OUT_BY_IN[line]
            if self.line_map(state).get(outer) or outer in self.fixed_outer_lines:
                return Op("Put", line, move, (), state.held), state, f"inner_put_outer_not_clear:{outer}"
        if not self.line_has_capacity(state, line, move):
            return Op("Put", line, move, (), state.held), state, f"target_capacity_violation:{line}"
        planned_positions: dict[str, int] = {}
        position_updates: dict[str, int] = {}
        outer_put_mode = ""
        if line in set(DEPOT_IN):
            planned_positions, position_reject = self.plan_depot_put_positions(state, line, move)
            position_updates = planned_positions
            if position_reject:
                return Op("Put", line, move, (), state.held), state, position_reject
        elif line in set(DEPOT_OUT):
            planned_positions, position_updates, outer_put_mode, position_reject = (
                self.project_outer_put(state, line, move)
            )
            if position_reject:
                return Op("Put", line, move, (), state.held), state, position_reject
        route, reject = self.route(state, "Put", line, move)
        if reject:
            return Op("Put", line, move, (), state.held), state, reject
        line_map = dict(self.line_map(state))
        line_map[line] = (*move, *line_map.get(line, ()))
        move_set = set(move)
        held_after = tuple(no for no in state.held if no not in move_set)
        post_put_loco = tuple(sorted(rv.put_loco_positions(route, line)))
        if not post_put_loco:
            return Op("Put", line, move, route, state.held), state, "post_put_loco_undefined"
        positioned_positions = dict(state.positioned_positions)
        positioned_positions.update(position_updates)
        next_state = State(
            lines=self.pack_lines(line_map),
            held=held_after,
            loco=post_put_loco,
            phase=state.phase,
            positioned_positions=self.pack_positioned_positions(positioned_positions),
        )
        positions = tuple((no, planned_positions[no]) for no in move if no in planned_positions)
        note = (
            f"place_terminal_block:{line}"
            if all(self.terminal_line_satisfied(no, line) for no in move)
            else f"temporary_buffer:{line}"
        )
        if outer_put_mode:
            note = f"{note}:{outer_put_mode}"
        return Op("Put", line, move, route, held_after, note=note, positions=positions), next_state, ""

    def closed_door_train_reject(self, held: tuple[str, ...]) -> bool:
        if not held:
            return False
        first = self.meta[held[0]]
        if not first.get("IsClosedDoor"):
            return False
        return len(held) > 10 or any(self.meta[no].get("IsHeavy") for no in held)

    def route(self, state: State, action: str, line: str, move: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
        cars = self.cars_from_state(state)
        if action == "Get":
            moving = set(state.held) | set(move)
            train_len = self.length(state.held)
            target_approach = physical.route_approach_lines_for_get(line)
        else:
            moving = set(state.held)
            train_len = self.length(state.held)
            target_approach = physical.route_approach_lines_for_put(line, cars, moving)
        occupied = physical.occupied_lines_for_route(cars, moving)
        stationary_loads = self.route_stationary_loads(cars, moving)
        departures = tuple(
            (start, tuple(sorted(physical.route_departure_lines_for_source(start, cars, moving))))
            for start in state.loco
        )
        cache_key = (
            tuple(state.loco),
            action,
            line,
            round(train_len, 3),
            tuple(sorted(occupied)),
            stationary_loads,
            tuple(sorted(target_approach)),
            departures,
        )
        cached = self.route_cache.get(cache_key)
        if cached is not None:
            return cached
        choices: list[tuple[int, tuple[str, ...]]] = []
        blockers: list[str] = []
        for start, source_departure in departures:
            path = self.graph.route_avoiding_occupied(
                start,
                line,
                occupied,
                source_departure_lines=set(source_departure),
                target_approach_lines=target_approach,
                cars=cars,
                moving_nos=moving,
                train_length_m=train_len,
            )
            if path:
                choices.append((len(path), tuple(path)))
            else:
                blockers.append(f"{start}->{line}")
        if not choices:
            result = ((), f"{action.lower()}_route_blocked:{blockers[0] if blockers else line}")
            self.route_cache[cache_key] = result
            return result
        result = (min(choices, key=lambda item: (item[0], item[1]))[1], "")
        self.route_cache[cache_key] = result
        return result

    def route_stationary_loads(
        self,
        cars: list[dict[str, Any]],
        moving: set[str],
    ) -> tuple[tuple[str, float], ...]:
        loads: Counter[str] = Counter()
        for car in cars:
            line = car.get("Line")
            if not line or rv.car_no(car) in moving:
                continue
            loads[line] += float(car["Length"])
        return tuple(sorted((line, round(load, 3)) for line, load in loads.items()))

    def complete(self, state: State) -> bool:
        if state.held:
            return False
        if state.phase == 0:
            return False
        line_map = self.line_map(state)
        for line in ASSEMBLY_LINES:
            if any(no in self.task_nos for no in line_map.get(line, ())):
                return False
        ok, _reasons, _positions = self.terminal_depot_ok(state)
        return ok

    def terminal_depot_ok(self, state: State) -> tuple[bool, list[str], dict[str, tuple[str, int]]]:
        line_map = self.line_map(state)
        positioned_by_no = dict(state.positioned_positions)
        positions: dict[str, tuple[str, int]] = {}
        reasons: list[str] = []
        for no in self.active_nos:
            found = [(line, nos.index(no) + 1) for line, nos in line_map.items() if no in nos]
            if not found:
                reasons.append(f"active_missing:{no}")
                continue
            line, _index = found[0]
            if self.stage3_weigh_pending(no):
                reasons.append(f"depot_weigh_pending:{no}")
            if not self.terminal_line_satisfied(no, line):
                reasons.append(f"active_terminal_line_violation:{no}:{line}")
            elif (
                no not in self.restoration_nos
                and not self.is_stage4_deferred(no)
                and self.assigned_line_by_no.get(no)
                and line != self.assigned_line_by_no[no]
            ):
                reasons.append(
                    "assignment_line_contract_violation:"
                    f"{no}:{line}!={self.assigned_line_by_no[no]}"
                )
            elif no in self.restoration_position_nos:
                initial_position = int(self.meta[no].get("Position") or 0)
                if _index != initial_position:
                    reasons.append(
                        f"restoration_position_violation:{no}:{line}:{_index}!={initial_position}"
                    )
            elif line in set(DEPOT_OUT) or line == UNWHEEL:
                position = positioned_by_no.get(no, 0) if line in set(DEPOT_OUT) else _index
                if position <= 0:
                    reasons.append(f"outer_position_missing:{no}:{line}")
                    continue
                positions[no] = (line, position)
                car = self.meta[no]
                assigned_slot = self.assigned_slot_by_no.get(no)
                if assigned_slot is not None and assigned_slot != (line, position):
                    reasons.append(
                        "assignment_position_contract_violation:"
                        f"{no}:{line}:{position}!={assigned_slot[0]}:{assigned_slot[1]}"
                    )
                forced = tuple(int(value) for value in car.get("_ForcePositions") or () if int(value) > 0)
                if forced and position not in forced:
                    reasons.append(f"outer_force_position_violation:{no}:{line}:{position}")

        for line in DEPOT_OUT:
            active = tuple(line_map.get(line, ()))
            ordered_positions = [positioned_by_no.get(no, 0) for no in active]
            fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
            if any(position <= 0 for position in ordered_positions):
                continue
            if ordered_positions != sorted(ordered_positions):
                reasons.append(f"outer_position_order_violation:{line}")
            if len(ordered_positions) != len(set(ordered_positions)):
                reasons.append(f"outer_position_collision:{line}")
            if fixed_positions and any(position >= min(fixed_positions) for position in ordered_positions):
                reasons.append(f"outer_fixed_position_access_violation:{line}")
            if set(ordered_positions) & set(fixed_positions):
                reasons.append(f"outer_fixed_position_collision:{line}")

        for car in self.fixed_cars:
            no = rv.car_no(car)
            line = car["Line"]
            if no not in self.depot_target_nos or line not in set(DEPOT_OUT):
                continue
            position = int(car.get("Position") or 0)
            positions[no] = (line, position)
            if not self.terminal_line_satisfied(no, line):
                reasons.append(f"fixed_outer_target_line_violation:{no}:{line}")
            forced = tuple(int(value) for value in car.get("_ForcePositions") or () if int(value) > 0)
            if forced and position not in forced:
                reasons.append(f"outer_force_position_violation:{no}:{line}:{position}")

        for line in DEPOT_IN:
            active = tuple(line_map.get(line, ()))
            depot_active: list[str] = []
            seen_depot = False
            ordered_positions: list[int] = []
            for no in active:
                position = positioned_by_no.get(no, 0)
                if position <= 0:
                    reasons.append(f"depot_position_missing:{no}:{line}")
                    continue
                ordered_positions.append(position)
                positions[no] = (line, position)
                if self.is_stage4_deferred(no):
                    if seen_depot:
                        reasons.append(f"deferred_after_depot_car:{no}:{line}")
                else:
                    seen_depot = True
                    depot_active.append(no)
            fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
            capacity = self.caps[line]
            if ordered_positions != sorted(ordered_positions):
                reasons.append(f"depot_position_order_violation:{line}")
            if len(ordered_positions) != len(set(ordered_positions)):
                reasons.append(f"depot_position_collision:{line}")
            if fixed_positions and any(position >= min(fixed_positions) for position in ordered_positions):
                reasons.append(f"depot_locked_tail_position_violation:{line}")
            if set(ordered_positions) & set(fixed_positions):
                reasons.append(f"depot_fixed_position_collision:{line}")
            for no in active:
                position = positioned_by_no.get(no, 0)
                if position <= 0:
                    continue
                car = self.meta[no]
                if self.is_stage4_deferred(no):
                    if not self.terminal_line_satisfied(no, line):
                        reasons.append(f"deferred_stage4_line_violation:{no}:{line}")
                    continue
                if not self.inner_target_lines(no) or line not in self.inner_target_lines(no):
                    reasons.append(f"depot_target_line_violation:{no}:{line}")
                if not self.slot_allowed_for_car(car, line, position, capacity):
                    reasons.append(f"depot_slot_rule_violation:{no}:{line}:{position}")
                assigned_slot = self.assigned_slot_by_no.get(no)
                if assigned_slot is not None and assigned_slot != (line, position):
                    reasons.append(
                        "assignment_position_contract_violation:"
                        f"{no}:{line}:{position}!={assigned_slot[0]}:{assigned_slot[1]}"
                    )

            depot_positions = [(no, positions[no][1]) for no in depot_active]
            for position, no in self.fixed_positioned_positions.get(line, {}).items():
                fixed = self.fixed_by_no.get(no)
                if fixed is None:
                    reasons.append(f"fixed_depot_car_missing:{no}:{line}:{position}")
                    continue
                positions[no] = (line, position)
                if not self.terminal_line_satisfied(no, line):
                    reasons.append(f"fixed_depot_target_line_violation:{no}:{line}")
                if not self.slot_allowed_for_car(fixed, line, position, capacity):
                    reasons.append(f"depot_slot_rule_violation:{no}:{line}:{position}")

            factory_positions = [
                position
                for no, position in depot_positions
                if self.repair_process(self.meta[no]).startswith("厂")
            ]
            factory_positions.extend(
                position
                for position, no in self.fixed_positioned_positions.get(line, {}).items()
                if self.repair_process(self.fixed_by_no[no]).startswith("厂")
            )
            if factory_positions:
                factory_min = min(factory_positions)
                for no in depot_active:
                    position = positions[no][1]
                    if self.repair_process(self.meta[no]).startswith("段") and position > factory_min:
                        reasons.append(f"depot_section_after_factory:{no}:{line}:{position}>{factory_min}")
        expected_positioned_positions = {
            no
            for line in POSITIONED_LINES
            for no in line_map.get(line, ())
        }
        orphaned_positions = sorted(set(positioned_by_no) - expected_positioned_positions)
        if orphaned_positions:
            reasons.append("positioned_line_position_orphaned:" + ",".join(orphaned_positions))
        return not reasons, reasons, positions

    def stage3_business_violations(
        self,
        request: dict[str, Any],
        cars: list[dict[str, Any]],
    ) -> list[rv.V]:
        """Return final-business violations owned by the Stage 3 boundary."""
        always_relevant = {
            "locked_depot_stayer_moved",
            "depot_slot_rule_violation",
        }
        owned_depot_relevant = {
            "depot_assignment_failure",
            "target_line_unsatisfied",
            "weigh_not_completed",
            "force_position_unsatisfied",
        }
        violations: list[rv.V] = []
        for violation in rv.business_errors(request, cars):
            if violation.code == "final_line_length_violation":
                line = violation.detail.split(":", 1)[0]
                if line in set(STAGE4_DEFER_LINES):
                    violations.append(violation)
                continue
            if violation.code in always_relevant:
                violations.append(violation)
                continue
            no = violation.detail.split(":", 1)[0]
            if (
                violation.code in owned_depot_relevant
                and no in self.stage3_business_nos
            ):
                violations.append(violation)
        return violations

    def depot_line_terminal_possible(
        self,
        state: State,
        line: str,
        nos: tuple[str, ...],
    ) -> bool:
        position_by_no = dict(state.positioned_positions)
        if any(no not in position_by_no for no in nos):
            return False
        positions = [position_by_no[no] for no in nos]
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            return False
        fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
        capacity = self.caps[line]
        if any(position < 1 or position > capacity for position in positions):
            return False
        if fixed_positions and any(position >= min(fixed_positions) for position in positions):
            return False
        seen_depot = False
        for no, position in zip(nos, positions):
            if self.is_stage4_deferred(no):
                if seen_depot:
                    return False
                if not self.terminal_line_satisfied(no, line):
                    return False
                continue
            seen_depot = True
            if self.assigned_line_by_no.get(no) != line:
                return False
            if line not in set(self.meta[no].get("TargetLines") or []):
                return False
            if not self.slot_allowed_for_car(self.meta[no], line, position, capacity):
                return False
        factory_positions = [
            position
            for no, position in zip(nos, positions)
            if self.repair_process(self.meta[no]).startswith("厂")
        ]
        for position, fixed_no in self.fixed_positioned_positions.get(line, {}).items():
            fixed = self.fixed_by_no.get(fixed_no)
            if fixed and self.repair_process(fixed).startswith("厂"):
                factory_positions.append(position)
        if factory_positions:
            factory_min = min(factory_positions)
            for no, position in zip(nos, positions):
                if self.repair_process(self.meta[no]).startswith("段") and position > factory_min:
                    return False
        return True

    def terminal_line_satisfied(self, no: str, line: str) -> bool:
        if no in self.restoration_nos:
            return line == self.meta[no]["Line"]
        if self.is_stage4_deferred(no):
            return line in set(STAGE4_STAGING_LINES)
        terminal_domain = self.inner_target_lines(no) | self.outer_target_lines(no)
        return line in terminal_domain

    def inner_target_lines(self, no: str) -> set[str]:
        return set(self.meta[no].get("TargetLines") or []) & set(DEPOT_IN)

    def outer_target_lines(self, no: str) -> set[str]:
        return set(self.meta[no].get("TargetLines") or []) & set(DEPOT_OUT)

    def slot_allowed_for_car(self, car: dict[str, Any], line: str, position: int, capacity: int) -> bool:
        if position < 1 or position > capacity:
            return False
        forced = tuple(int(value) for value in car.get("_ForcePositions") or () if int(value) > 0)
        if forced and position not in forced:
            return False
        if float(car["Length"]) >= 17.6 and self.line_number(line) not in {3, 4}:
            return False
        if self.repair_process(car).startswith("厂") and position not in {4, 5}:
            return False
        return True

    def line_number(self, line: str) -> int:
        for index in range(1, 5):
            if line in {f"修{index}库内", f"修{index}库外"}:
                return index
        return 0

    def repair_process(self, car: dict[str, Any]) -> str:
        return str(car.get("RepairProcess") or "")

    def evaluate_complete_candidate(self, candidate: SearchResult) -> dict[str, Any]:
        if candidate.state is None or candidate.state.held:
            raise ValueError("complete_candidate_requires_closed_state")
        if candidate.template in {"A", "B"}:
            self.assigned_line_by_no = self.build_assigned_line_by_no(
                candidate.template,
                candidate.layout,
            )
        cache_key = (candidate.state, candidate.ops)
        cached = self.candidate_evaluation_cache.get(cache_key)
        if cached is not None:
            return cached
        stage3_request = self.stage3_request()
        response = {
            "Data": {
                "Operations": self.response_operations(candidate.ops),
                "GeneratedEndStatus": [],
            }
        }
        replayed_without_generated, _ = rv.replay(stage3_request, response)
        generated = self.replayed_end_status(replayed_without_generated)
        response["Data"]["GeneratedEndStatus"] = generated
        replayed, replay_bad = rv.replay(stage3_request, response)
        stage3_final = rv.final_cars(response, replayed)
        replay_bad = [
            *replay_bad,
            *self.stage3_business_violations(stage3_request, stage3_final),
        ]

        internal_projection = {
            rv.car_no(car): (car["Line"], int(car.get("Position") or 0))
            for car in self.cars_from_state(candidate.state)
        }
        replay_projection = {
            rv.car_no(car): (car["Line"], int(car.get("Position") or 0))
            for car in replayed
        }
        mismatches = [
            f"{no}:state={internal_projection.get(no)}:replay={replay_projection.get(no)}"
            for no in sorted(set(internal_projection) | set(replay_projection))
            if internal_projection.get(no) != replay_projection.get(no)
        ]
        if mismatches:
            replay_bad.append(
                rv.V(
                    0,
                    "state",
                    "candidate_state_replay_mismatch",
                    "|".join(mismatches[:8]),
                )
            )

        combined = {
            "Data": {
                "Operations": self.combined_operations(response),
                "GeneratedEndStatus": generated,
            }
        }
        combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        combined_final = rv.final_cars(combined, combined_replayed)
        combined_bad = [
            *combined_bad,
            *self.stage3_business_violations(self.original_request, combined_final),
        ]
        terminal_ok, terminal_bad, _positions = self.terminal_depot_ok(candidate.state)
        evaluated = {
            "stage3_request": stage3_request,
            "response": response,
            "combined": combined,
            "replayed": replayed,
            "combined_replayed": combined_replayed,
            "replay_bad": replay_bad,
            "combined_bad": combined_bad,
            "terminal_ok": terminal_ok,
            "terminal_bad": terminal_bad,
        }
        self.candidate_evaluation_cache[cache_key] = evaluated
        return evaluated

    def validate_candidate(self, candidate: SearchResult) -> SearchResult:
        if candidate.status != "complete":
            return candidate
        evaluated = self.evaluate_complete_candidate(candidate)
        replay_bad = [
            violation
            for violation in evaluated["replay_bad"]
            if violation.kind in BLOCKING_REPLAY_KINDS
        ]
        combined_bad = [
            violation
            for violation in evaluated["combined_bad"]
            if violation.kind in BLOCKING_REPLAY_KINDS
        ]
        if evaluated["terminal_ok"] and not replay_bad and not combined_bad:
            return candidate
        reasons = tuple(dict.fromkeys((
            *candidate.reasons,
            *(
                f"candidate_replay_{violation.kind}:{violation.code}:{violation.detail}"
                for violation in replay_bad[:12]
            ),
            *(
                f"candidate_combined_{violation.kind}:{violation.code}:{violation.detail}"
                for violation in combined_bad[:12]
            ),
            *(
                f"candidate_terminal:{reason}"
                for reason in evaluated["terminal_bad"][:12]
            ),
        )))
        return replace(
            candidate,
            status="partial",
            cost=(INF_COST, 0, 0, 0),
            reasons=reasons or ("candidate_validation_failed",),
        )

    @staticmethod
    def template_covered_prefix(
        specs: list[PlacementSpec],
        limit: int,
    ) -> list[PlacementSpec]:
        """Return a ranked prefix covering each template when the limit permits."""

        if limit <= 0:
            raise ValueError("placement_spec_limit_must_be_positive")
        selected = list(specs[:limit])
        if len(selected) < limit:
            return selected
        rank = {spec: index for index, spec in enumerate(specs)}
        all_templates = {spec.template for spec in specs}
        if limit < len(all_templates):
            return selected
        selected_counts = Counter(spec.template for spec in selected)
        for template in sorted(all_templates - set(selected_counts)):
            candidate = next(spec for spec in specs if spec.template == template)
            replace_index = next(
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected_counts[selected[index].template] > 1
            )
            removed = selected[replace_index]
            selected_counts[removed.template] -= 1
            selected[replace_index] = candidate
            selected_counts[template] += 1
        return sorted(selected, key=rank.__getitem__)

    def select_screen_specs(
        self,
        specs: list[PlacementSpec],
        results_by_spec: dict[tuple[str, str], SearchResult],
    ) -> list[PlacementSpec]:
        """Select a fair screening cohort from rank and shallow progress."""

        if not specs:
            return []

        def progress_key(spec: PlacementSpec) -> tuple[Any, ...]:
            prior = results_by_spec.get((spec.template, spec.layout))
            progress = self.partial_progress_key(prior) if prior is not None else (
                INF_COST,
            ) * 7
            return (
                *progress,
                spec.operation_lower_bound,
                spec.placement_score,
                spec.template,
                spec.layout,
            )

        base_limit = min(
            SCREEN_BASE_PLAN_BUDGET,
            SCREEN_PLAN_BUDGET,
            len(specs),
        )
        selected = self.template_covered_prefix(specs, base_limit)
        selected_set = set(selected)
        for spec in sorted(
            (item for item in specs if item not in selected_set),
            key=progress_key,
        ):
            if len(selected) >= SCREEN_PLAN_BUDGET:
                break
            selected.append(spec)
            selected_set.add(spec)
        return sorted(selected, key=progress_key)

    def partial_progress_key(self, item: SearchResult) -> tuple[int, ...]:
        progress = item.budgeted_progress_key or (
            len(self.active_nos) - item.committed_count,
            INF_COST,
            INF_COST,
            INF_COST,
            INF_COST,
        )
        return (
            *progress,
            self.business_hook_count(item.ops),
            len(item.ops),
        )

    def select_finalist_specs(
        self,
        specs: list[PlacementSpec],
        results_by_spec: dict[tuple[str, str], SearchResult],
    ) -> list[PlacementSpec]:
        """Keep two distinct static leaders and one budgeted-search leader."""

        eligible = [
            spec
            for spec in specs
            if (
                prior := results_by_spec.get((spec.template, spec.layout))
            ) is not None
            and prior.status != "complete"
        ]
        if not eligible:
            return []
        selected: list[PlacementSpec] = []
        signatures: set[tuple[tuple[str, str, str, int], ...]] = set()
        for spec in eligible:
            if spec.signature in signatures:
                continue
            selected.append(spec)
            signatures.add(spec.signature)
            if len(selected) >= STATIC_FINALIST_PLAN_BUDGET:
                break
        if len(selected) >= FINALIST_PLAN_BUDGET:
            return selected[:FINALIST_PLAN_BUDGET]
        selected_set = set(selected)
        progress_ranked = sorted(
            (spec for spec in eligible if spec not in selected_set),
            key=lambda spec: (
                *self.partial_progress_key(
                    results_by_spec[(spec.template, spec.layout)]
                ),
                spec.operation_lower_bound,
                spec.placement_score,
                spec.template,
                spec.layout,
            ),
        )
        selected.extend(progress_ranked[: FINALIST_PLAN_BUDGET - len(selected)])
        return selected

    def choose_result(self, results: list[SearchResult]) -> SearchResult:
        complete = [item for item in results if item.status == "complete"]
        if complete:
            return min(
                complete,
                key=lambda item: (
                    item.cost,
                    0 if item.template == "B" else 1,
                    item.layout,
                ),
            )
        return min(
            results,
            key=lambda item: (
                *self.partial_progress_key(item),
                len(item.reasons),
                -item.expansions,
                item.template,
                item.layout,
            ),
        )

    def result(self, chosen: SearchResult, results: list[SearchResult]) -> dict[str, Any]:
        if chosen.template in {"A", "B"}:
            self.assigned_line_by_no = self.build_assigned_line_by_no(chosen.template, chosen.layout)
        stage3_request = self.stage3_request()
        executable_ops = (
            chosen.ops
            if chosen.status == "complete"
            and chosen.state is not None
            and not chosen.state.held
            else ()
        )
        response = {"Data": {"Operations": self.response_operations(executable_ops), "GeneratedEndStatus": []}}
        replayed_without_generated, _operational_bad = rv.replay(stage3_request, response)
        generated = (
            self.replayed_end_status(replayed_without_generated)
            if chosen.status == "complete" and chosen.state is not None
            else []
        )
        response["Data"]["GeneratedEndStatus"] = generated
        combined = {"Data": {"Operations": self.combined_operations(response), "GeneratedEndStatus": generated}}
        replayed, replay_bad = rv.replay(stage3_request, response)
        residual_business_bad = self.stage3_business_violations(
            stage3_request,
            rv.final_cars(response, replayed),
        )
        if chosen.status == "complete":
            replay_bad = [*replay_bad, *residual_business_bad]
        hard_bad = [v for v in replay_bad if v.kind in BLOCKING_REPLAY_KINDS]
        combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        combined_residual_business_bad = self.stage3_business_violations(
            self.original_request,
            rv.final_cars(combined, combined_replayed),
        )
        if chosen.status == "complete":
            combined_bad = [*combined_bad, *combined_residual_business_bad]
        combined_hard_bad = [v for v in combined_bad if v.kind in BLOCKING_REPLAY_KINDS]
        terminal_ok, terminal_bad, _terminal_positions = (
            self.terminal_depot_ok(chosen.state)
            if chosen.state is not None
            else (False, ["terminal_state_missing"], {})
        )
        validated_complete = (
            chosen.status == "complete"
            and terminal_ok
            and not hard_bad
            and not combined_hard_bad
        )
        lower_bound_components = dict(chosen.lower_bound_components)
        lower_bound = chosen.lower_bound
        executable_hooks = self.business_hook_count(executable_ops)
        optimality_gap = (
            executable_hooks - lower_bound
            if validated_complete and lower_bound is not None
            else None
        )
        evaluated_bounds = [
            item.lower_bound for item in results if item.lower_bound is not None
        ]
        search_space_lower_bound = min(
            evaluated_bounds,
            default=None,
        )
        search_space_gap = (
            executable_hooks - search_space_lower_bound
            if validated_complete and search_space_lower_bound is not None
            else None
        )
        search_space_evaluation_complete = (
            not self.search_space_evaluation_incomplete
            and all(item.search_spec_evaluated for item in results)
        )
        replay_reasons = tuple(f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12])
        combined_reasons = tuple(
            f"combined_replay_{v.kind}:{v.code}:{v.detail}"
            for v in combined_hard_bad[:12]
        )
        terminal_reasons = (
            tuple(f"terminal:{reason}" for reason in terminal_bad[:12])
            if chosen.state is not None
            else ()
        )
        infeasibility_certificates = tuple(dict.fromkeys(
            reason
            for item in results
            for reason in item.reasons
            if "_infeasible:" in reason
        ))
        reasons = tuple(dict.fromkeys((
            *chosen.reasons,
            *infeasibility_certificates,
            *replay_reasons,
            *combined_reasons,
            *terminal_reasons,
        )))
        template_summaries = [
            {
                "template": item.template,
                "layout": item.layout,
                "deferred_clear": item.deferred_clear,
                "terminal_merge": item.terminal_merge,
                "inner_clear_policy": item.inner_clear_policy,
                "status": item.status,
                "operations": self.business_hook_count(item.ops),
                "cost": list(item.cost),
                "operation_lower_bound": item.lower_bound,
                "operation_lower_bound_components": dict(item.lower_bound_components),
                "operation_lower_bound_scope": item.lower_bound_scope,
                "operation_lower_bound_gap": (
                    self.business_hook_count(item.ops) - item.lower_bound
                    if item.status == "complete" and item.lower_bound is not None
                    else None
                ),
                "search_spec_evaluated": item.search_spec_evaluated,
                "stable_committed_count": item.committed_count,
                "budgeted_progress_key": (
                    list(item.budgeted_progress_key)
                    if item.budgeted_progress_key is not None
                    else None
                ),
                "blocking_reasons": list(item.reasons),
                "expansions": item.expansions,
                "elapsed_seconds": item.elapsed_seconds,
            }
            for item in results
        ]
        lower_bound_validation_violations = [
            {
                "template": item.template,
                "layout": item.layout,
                "deferred_clear": item.deferred_clear,
                "terminal_merge": item.terminal_merge,
                "inner_clear_policy": item.inner_clear_policy,
                "operations": self.business_hook_count(item.ops),
                "operation_lower_bound": item.lower_bound,
            }
            for item in results
            if item.status == "complete"
            and item.lower_bound is not None
            and item.lower_bound > self.business_hook_count(item.ops)
        ]
        summary = {
            "case_id": self.case_id,
            "status": (
                "complete" if validated_complete else "partial"
            ),
            "template": chosen.template,
            "layout": chosen.layout,
            "deferred_clear": chosen.deferred_clear,
            "terminal_merge": chosen.terminal_merge,
            "inner_clear_policy": chosen.inner_clear_policy,
            "stage2_final_loco": self.stage2_final_loco,
            "stage3_start_loco": self.initial_loco,
            "operations": self.business_hook_count(executable_ops),
            "business_hooks": self.business_hook_count(executable_ops),
            "attempted_operations": self.business_hook_count(chosen.ops),
            "stable_committed_count": chosen.committed_count,
            "budgeted_progress_key": (
                list(chosen.budgeted_progress_key)
                if chosen.budgeted_progress_key is not None
                else None
            ),
            "partial_response_safe": not any(
                violation.kind in {"schema", "physical", "state"}
                for violation in replay_bad
            ),
            "operation_lower_bound": lower_bound,
            "operation_lower_bound_components": lower_bound_components,
            "operation_lower_bound_scope": chosen.lower_bound_scope,
            "evaluated_search_space_bound_scope": "unified_placement_transaction_specs",
            "operation_lower_bound_gap": optimality_gap,
            "evaluated_search_space_lower_bound": search_space_lower_bound,
            "evaluated_search_space_gap": search_space_gap,
            "search_space_evaluation_complete": search_space_evaluation_complete,
            "lower_bound_validation_violations": lower_bound_validation_violations,
            "optimization_attempted": self.optimization_attempted,
            "optimization_expansions": self.optimization_expansions,
            "optimization_budget_exhausted": self.optimization_budget_exhausted,
            "optimality_status": (
                "invalid_lower_bound_certificate"
                if validated_complete
                and (optimality_gap is not None and optimality_gap < 0
                     or search_space_gap is not None and search_space_gap < 0
                     or lower_bound_validation_violations)
                else "search_space_evaluation_incomplete"
                if validated_complete and not search_space_evaluation_complete
                else "search_space_lower_bound_reached"
                if validated_complete and search_space_gap == 0
                else "bounded_improvement_exhausted"
                if validated_complete and self.optimization_budget_exhausted
                else "best_known_with_gap"
                if validated_complete
                else "not_applicable"
            ),
            "active_count": len(self.active_nos),
            "active_nos": sorted(self.active_nos),
            "task_count": len(self.task_nos),
            "task_nos": sorted(self.task_nos),
            "restoration_count": len(self.restoration_nos),
            "restoration_nos": sorted(self.restoration_nos),
            "stage3_business_count": len(self.stage3_business_nos),
            "stage3_business_nos": sorted(self.stage3_business_nos),
            "template_summaries": template_summaries,
            "terminal_depot_ok": bool(terminal_ok),
            "blocking_reasons": list(reasons),
            "infeasibility_certificates": list(infeasibility_certificates),
            "replay_physical_ok": not any(v.kind == "physical" for v in hard_bad),
            "replay_business_ok": not residual_business_bad,
            "replay_ok": not hard_bad and not residual_business_bad,
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
            "residual_business_violations": [
                v.__dict__ for v in residual_business_bad[:20]
            ],
            "combined_replay_physical_ok": not any(
                v.kind == "physical" for v in combined_hard_bad
            ),
            "combined_replay_business_ok": not combined_residual_business_bad,
            "combined_replay_ok": (
                not combined_hard_bad and not combined_residual_business_bad
            ),
            "combined_replay_violations": [v.__dict__ for v in combined_hard_bad[:20]],
            "combined_residual_business_violations": [
                v.__dict__ for v in combined_residual_business_bad[:20]
            ],
            "expansions": chosen.expansions,
            "elapsed_seconds": chosen.elapsed_seconds,
        }
        trace = [
            {
                "index": index,
                "action": op.action,
                "line": op.line,
                "move": list(op.move),
                "train_after": list(op.train_after),
                "path": list(op.path),
                "note": op.note,
                "positions": dict(op.positions),
            }
            for index, op in enumerate(chosen.ops, start=1)
        ]
        assignment_plan = self.assignment_plan(chosen.template)
        return {
            "response": response,
            "combined_response": combined,
            "stage3_request": stage3_request,
            "summary": summary,
            "trace": trace,
            "assignment_plan": assignment_plan,
        }

    def assignment_plan(self, template: str) -> list[dict[str, Any]]:
        exposure = self.template_exposure_order(template) if template in {"A", "B"} else ()
        exposure_rank = {no: index for index, no in enumerate(exposure)}
        rows: list[dict[str, Any]] = []
        for no in sorted(self.active_nos, key=lambda item: (exposure_rank.get(item, INF_COST), item)):
            car = self.meta[no]
            assigned_line = self.assigned_line_by_no.get(no, "")
            assigned_slot = self.assigned_slot_by_no.get(no)
            constraints: list[str] = []
            if self.is_stage4_deferred(no):
                constraints.append(f"defer_to_stage4:{self.deferred_stage4_target(no)}")
            if no in self.restoration_nos:
                constraints.append(
                    f"restore_context:{self.meta[no]['Line']}#{int(self.meta[no].get('Position') or 0)}"
                )
            if self.repair_process(car).startswith("厂"):
                constraints.append("factory_deep_slot")
            elif self.repair_process(car).startswith("段"):
                constraints.append("section_before_factory")
            if float(car["Length"]) >= 17.6:
                constraints.append("long_car_repair_3_or_4")
            forced = tuple(car.get("_ForcePositions") or ())
            if forced:
                constraints.append("forced_positions:" + ",".join(str(value) for value in forced))
            rows.append({
                "no": no,
                "source_line": car["Line"],
                "source_position": int(car.get("Position") or 0),
                "assigned_line": assigned_line,
                "assigned_position": assigned_slot[1] if assigned_slot else None,
                "allowed_targets": sorted(car.get("TargetLines") or []),
                "exposure_rank": exposure_rank.get(no),
                "constraints": constraints,
            })
        return rows

    def replayed_end_status(self, cars: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "No": rv.car_no(car),
                    "Line": car["Line"],
                    "Position": int(car.get("Position") or 0),
                }
                for car in cars
            ),
            key=lambda item: item["No"],
        )

    def response_operations(self, ops: Iterable[Op], *, start_index: int = 1) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        index = start_index
        for op in ops:
            if op.action == "Phase":
                continue
            rows.append({
                "Index": index,
                "Line": op.line,
                "Action": op.action,
                "MoveCars": list(op.move),
                "TrainCars": list(op.train_after),
                "PassbyPath": list(op.path),
                **({"Positions": dict(op.positions)} if op.positions else {}),
            })
            index += 1
        return rows

    def combined_operations(self, stage3_response: dict[str, Any]) -> list[dict[str, Any]]:
        base = [dict(row) for row in rv.operations(self.stage2_combined_response)]
        start = len(base) + 1
        extra = self.response_operations(
            [
                Op(
                    action=row["Action"],
                    line=row["Line"],
                    move=tuple(row.get("MoveCars") or ()),
                    path=tuple(row.get("PassbyPath") or ()),
                    train_after=tuple(row.get("TrainCars") or ()),
                    positions=tuple(sorted(rv.operation_positions(row).items())),
                )
                for row in rv.operations(stage3_response)
            ],
            start_index=start,
        )
        return [*base, *extra]

    def stage3_request(self) -> dict[str, Any]:
        request = dict(self.original_request)
        request["StartStatus"] = [
            self.output_car(car)
            for car in sorted(self.initial_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), rv.car_no(item)))
        ]
        request["locoNode"] = {"Line": self.initial_loco, "End": "North"}
        return request

    def output_car(self, car: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in car.items() if not key.startswith("_") or key in {"_Weighed"}}

    def cars_from_state(self, state: State) -> list[dict[str, Any]]:
        cached = self.cars_cache.get(state)
        if cached is not None:
            return list(cached)
        rows = [dict(car) for car in self.fixed_cars]
        positioned_positions = dict(state.positioned_positions)
        for line, nos in state.lines:
            for order, no in enumerate(nos, start=1):
                car = dict(self.meta[no])
                car["Line"] = line
                if line in set(POSITIONED_LINES):
                    if no not in positioned_positions:
                        raise ValueError(f"stage3_state_position_missing:{no}:{line}")
                    car["Position"] = positioned_positions[no]
                else:
                    car["Position"] = order
                rows.append(car)
        for no in state.held:
            car = dict(self.meta[no])
            car["Line"] = ""
            car["Position"] = 0
            rows.append(car)
        self.cars_cache[state] = tuple(rows)
        return rows

    def line_map(self, state: State) -> dict[str, tuple[str, ...]]:
        cached = self.line_map_cache.get(state)
        if cached is None:
            cached = {line: tuple(nos) for line, nos in state.lines}
            self.line_map_cache[state] = cached
        return cached

    def pack_lines(self, line_map: dict[str, tuple[str, ...] | list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(sorted((line, tuple(nos)) for line, nos in line_map.items() if nos))

    def line_has_capacity(self, state: State, line: str, move: tuple[str, ...]) -> bool:
        limit = rv.TRACK_LEN.get(line)
        if limit is None:
            return False
        line_map = self.line_map(state)
        existing = sum(float(self.meta[no]["Length"]) for no in line_map.get(line, ()))
        fixed = sum(float(car["Length"]) for car in self.fixed_cars if car["Line"] == line)
        incoming = self.length(move)
        return existing + fixed + incoming <= limit + rv.TOL

    def pull_equivalent(self, nos: Iterable[str]) -> int:
        return sum(4 if self.meta[no].get("IsHeavy") else 1 for no in nos)

    def length(self, nos: Iterable[str]) -> float:
        return sum(float(self.meta[no]["Length"]) for no in nos)

    def delta(self, op: Op) -> tuple[int, int, int, int]:
        if op.action == "Phase":
            return (0, 0, 0, 0)
        lian7 = 1 if "联7" in op.path else 0
        outer = 1 if op.line in set(DEPOT_OUT) else 0
        return (1, lian7, len(op.path), outer)

    def ops_cost(self, ops: Iterable[Op]) -> tuple[int, int, int, int]:
        cost: tuple[int, int, int, int] = (0, 0, 0, 0)
        for op in ops:
            delta = self.delta(op)
            cost = (
                cost[0] + delta[0],
                cost[1] + delta[1],
                cost[2] + delta[2],
                cost[3] + delta[3],
            )
        return cost

    def business_hook_count(self, ops: Iterable[Op]) -> int:
        return sum(1 for op in ops if op.action in {"Get", "Put"})


def case_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"input path does not exist: {input_path}")
    files = sorted(input_path.glob("validation_*.json"))
    if not files:
        raise ValueError(
            f"input directory has no validation_*.json files: {input_path}"
        )
    return files


def diagnostic_summary(
    case_id: str,
    reason: str,
    *,
    status: str = "unavailable",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "template": "none",
        "layout": "none",
        "deferred_clear": False,
        "terminal_merge": False,
        "inner_clear_policy": "none",
        "operations": 0,
        "business_hooks": 0,
        "attempted_operations": 0,
        "stable_committed_count": 0,
        "budgeted_progress_key": None,
        "partial_response_safe": True,
        "operation_lower_bound": None,
        "operation_lower_bound_components": {},
        "operation_lower_bound_scope": "not_applicable",
        "operation_lower_bound_gap": None,
        "evaluated_search_space_bound_scope": "not_applicable",
        "evaluated_search_space_lower_bound": None,
        "evaluated_search_space_gap": None,
        "search_space_evaluation_complete": False,
        "lower_bound_validation_violations": [],
        "optimization_attempted": 0,
        "optimization_expansions": 0,
        "optimization_budget_exhausted": False,
        "optimality_status": "not_applicable",
        "active_count": 0,
        "active_nos": [],
        "task_count": 0,
        "task_nos": [],
        "restoration_count": 0,
        "restoration_nos": [],
        "stage3_business_count": 0,
        "stage3_business_nos": [],
        "template_summaries": [],
        "terminal_depot_ok": False,
        "blocking_reasons": [reason],
        "infeasibility_certificates": [],
        "replay_physical_ok": False,
        "replay_business_ok": False,
        "replay_ok": False,
        "replay_violations": [],
        "residual_business_violations": [],
        "combined_replay_physical_ok": False,
        "combined_replay_business_ok": False,
        "combined_replay_ok": False,
        "combined_replay_violations": [],
        "combined_residual_business_violations": [],
        "expansions": 0,
        "elapsed_seconds": 0.0,
    }


def solve_one(
    path: Path,
    stage2_out: Path,
    out_dir: Path,
    *,
    time_budget_seconds: float,
) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    stage2_summary_path = stage2_out / f"{case_id}_summary.json"
    combined_path = stage2_out / f"{case_id}_combined_response.json"
    missing = [
        str(artifact)
        for artifact in (stage2_summary_path, combined_path)
        if not artifact.exists()
    ]
    if missing:
        summary = diagnostic_summary(
            case_id,
            "stage2_artifact_missing:" + ",".join(missing),
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    stage2_summary = read_json(stage2_summary_path)
    if stage2_summary.get("status") != "complete":
        summary = diagnostic_summary(
            case_id,
            f"stage2_not_complete:{stage2_summary.get('status')}",
        )
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary

    solver = Stage3Solver(
        case_id,
        read_json(path),
        read_json(combined_path),
        time_budget_seconds=time_budget_seconds,
    )
    result = solver.solve()
    write_json(out_dir / f"{case_id}_stage3_request.json", result["stage3_request"])
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    write_json(out_dir / f"{case_id}_assignment_plan.json", result["assignment_plan"])
    return result["summary"]


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(summaries)
    complete = sum(1 for item in summaries if item.get("status") == "complete")
    partial = sum(1 for item in summaries if item.get("status") == "partial")
    unavailable = sum(1 for item in summaries if item.get("status") == "unavailable")
    errors = sum(1 for item in summaries if item.get("status") == "error")
    ops = [int(item.get("operations") or 0) for item in summaries if item.get("status") == "complete"]
    reasons = Counter(
        reason.split(":", 1)[0]
        for item in summaries
        if item.get("status") != "complete"
        for reason in item.get("blocking_reasons") or []
    )
    templates = Counter(item.get("template") for item in summaries if item.get("status") == "complete")
    return {
        "cases": total,
        "complete": complete,
        "partial": partial,
        "unavailable": unavailable,
        "error": errors,
        "avg_operations_complete": round(sum(ops) / len(ops), 3) if ops else 0,
        "max_operations_complete": max(ops) if ops else 0,
        "templates_complete": dict(sorted(templates.items())),
        "partial_reasons": dict(reasons.most_common()),
        "summaries": summaries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 3 unified placement and transaction solver."
    )
    parser.add_argument("input", type=Path, help="request JSON file or directory")
    parser.add_argument(
        "--stage2-out",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
    )
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in case_files(args.input):
        case_id = case_id_from_path(path)
        print(case_id, flush=True)
        summaries.append(
            solve_one(
                path,
                args.stage2_out,
                args.out,
                time_budget_seconds=args.time_budget_seconds,
            )
        )
    agg = aggregate(summaries)
    write_json(args.out / "aggregate_summary.json", agg)
    print(
        "done "
        f"cases={agg['cases']} complete={agg['complete']} partial={agg['partial']} "
        f"unavailable={agg['unavailable']} error={agg['error']} "
        f"avg_ops={agg['avg_operations_complete']} max_ops={agg['max_operations_complete']}",
        flush=True,
    )
    return 0 if all(item.get("status") == "complete" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
