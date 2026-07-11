#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
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

import replay_validator as rv
from solver_vnext import physical


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
MAX_EXPANSIONS = 300_000
MAX_IMPROVEMENT_EXPANSIONS = 600
IMPROVEMENT_TIME_BUDGET_SECONDS = 0.05
EXACT_SEARCH_ACTIVE_LIMIT = 6
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
    layout: str = "cost"
    deferred_clear: bool = True
    terminal_merge: bool = True
    inner_clear_policy: str = "eager"
    lower_bound: int | None = None
    lower_bound_components: tuple[tuple[str, int], ...] = ()
    lower_bound_scope: str = "not_applicable"
    strategy_evaluated: bool = True


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
            tuple[str, str, bool],
            tuple[State, tuple[Op, ...]] | SearchResult,
        ] = {}
        self.candidate_evaluation_cache: dict[
            tuple[State, tuple[Op, ...]],
            dict[str, Any],
        ] = {}
        self.assigned_line_by_no = self.build_assigned_line_by_no("B", "cost")
        self.optimization_attempted = 0
        self.optimization_expansions = 0
        self.optimization_budget_exhausted = False
        self.portfolio_evaluation_incomplete = False

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

    def build_assigned_line_by_no(self, template: str, layout: str = "cost") -> dict[str, str]:
        cache_key = (template, layout)
        cached = self.assignment_cache.get(cache_key)
        if cached is not None:
            assigned, slots, reasons = cached
            self.assigned_slot_by_no = dict(slots)
            self.assignment_reasons = reasons
            return dict(assigned)
        if layout == "cohesive":
            assigned = self.build_cohesive_assigned_line_by_no(template)
        elif layout == "cost":
            assigned = self.build_cost_assigned_line_by_no(template)
        else:
            raise ValueError(f"unknown_stage3_layout:{layout}")
        self.assignment_cache[cache_key] = (
            dict(assigned),
            dict(self.assigned_slot_by_no),
            self.assignment_reasons,
        )
        return assigned

    def build_cost_assigned_line_by_no(self, template: str) -> dict[str, str]:
        self.assignment_reasons = ()
        self.assigned_slot_by_no = {}
        exposure = self.template_exposure_order(template)
        exposure_time = {no: index for index, no in enumerate(exposure)}
        inner_nos = sorted(
            no for no in self.active_nos - self.restoration_nos if self.inner_target_lines(no)
        )
        outer_nos = sorted(
            no
            for no in self.active_nos - self.restoration_nos
            if not self.inner_target_lines(no) and self.outer_target_lines(no)
        )
        deferred_nos = sorted(
            no for no in self.active_nos - self.restoration_nos if self.is_stage4_deferred(no)
        )
        slots: list[tuple[str, int]] = []
        for line in DEPOT_IN:
            if DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
                continue
            fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
            capacity = self.caps[line]
            usable_limit = (min(fixed_positions) - 1) if fixed_positions else capacity
            for position in range(1, usable_limit + 1):
                slots.append((line, position))

        candidates: dict[str, list[tuple[str, int]]] = {}
        for no in inner_nos:
            car = self.meta[no]
            allowed = []
            for line, position in slots:
                if line not in set(car.get("TargetLines") or []):
                    continue
                if self.slot_allowed_for_car(car, line, position, self.caps[line]):
                    allowed.append((line, position))
            candidates[no] = allowed

        ordered_nos = sorted(
            inner_nos,
            key=lambda no: (
                len(candidates.get(no, ())),
                not self.repair_process(self.meta[no]).startswith("厂"),
                float(self.meta[no]["Length"]) < 17.6,
                exposure_time.get(no, 10**6),
                no,
            ),
        )
        assigned_inner = self.minimum_cost_slot_assignment(ordered_nos, candidates, template)
        missing_inner = tuple(sorted(no for no in inner_nos if no not in assigned_inner))
        assigned_outer, assigned_outer_slots, missing_outer = self.assign_outer_targets(
            outer_nos,
            set(line for line, _pos in assigned_inner.values()),
        )
        assigned_lines = {no: line for no, (line, _position) in assigned_inner.items()}
        assigned_lines.update(assigned_outer)
        restoration_lines = {no: self.meta[no]["Line"] for no in self.restoration_nos}
        assigned_deferred, missing_deferred = self.assign_deferred_stage4_targets(
            deferred_nos,
            reserved_load=self.assigned_loads({**assigned_outer, **restoration_lines}),
            used_inner_lines=set(line for line, _position in assigned_inner.values()),
        )
        assigned_lines.update(assigned_deferred)
        assigned_lines.update(restoration_lines)
        if missing_inner or missing_outer:
            reasons: list[str] = []
            if missing_inner:
                reasons.append(f"inner_assignment_incomplete:{template}:{','.join(missing_inner)}")
                certificate = self.inner_slot_capacity_certificate(inner_nos, candidates)
                if certificate:
                    reasons.append(certificate)
            if missing_outer:
                reasons.append(f"outer_assignment_incomplete:{template}:{','.join(missing_outer)}")
                certificate = self.outer_capacity_certificate(outer_nos)
                if certificate:
                    reasons.append(certificate)
            if missing_deferred:
                reasons.append(f"stage4_defer_staging_incomplete:{template}:{','.join(missing_deferred)}")
            self.assignment_reasons = tuple(reasons)
            return assigned_lines
        if missing_deferred:
            self.assignment_reasons = (f"stage4_defer_staging_incomplete:{template}:{','.join(missing_deferred)}",)
            return assigned_lines

        self.assigned_slot_by_no = {**assigned_inner, **assigned_outer_slots}
        return assigned_lines

    def inner_slot_capacity_certificate(
        self,
        nos: list[str],
        candidates: dict[str, list[tuple[str, int]]],
    ) -> str:
        reachable = {slot for no in nos for slot in candidates.get(no, ())}
        if len(reachable) >= len(nos):
            return ""
        return (
            f"inner_slot_capacity_infeasible:cars={len(nos)}>"
            f"reachable_slots={len(reachable)}"
        )

    def outer_capacity_certificate(self, nos: list[str]) -> str:
        if not nos:
            return ""
        remaining = {
            line: float(rv.TRACK_LEN[line])
            - sum(float(car["Length"]) for car in self.fixed_cars if car["Line"] == line)
            for line in DEPOT_OUT
        }
        allowed = {no: self.outer_target_lines(no) for no in nos}
        for count in range(1, len(DEPOT_OUT) + 1):
            for mask in range(1, 1 << len(DEPOT_OUT)):
                lines = {
                    DEPOT_OUT[index]
                    for index in range(len(DEPOT_OUT))
                    if mask & (1 << index)
                }
                if len(lines) != count:
                    continue
                constrained = [
                    no for no in nos
                    if allowed[no] and allowed[no] <= lines
                ]
                demand = self.length(constrained)
                capacity = sum(remaining[line] for line in lines)
                if demand > capacity + rv.TOL:
                    return (
                        f"outer_capacity_infeasible:{','.join(sorted(lines))}:"
                        f"demand={demand:.1f}>capacity={capacity:.1f}:"
                        f"cars={','.join(sorted(constrained))}"
                    )
        return ""

    def minimum_cost_slot_assignment(
        self,
        ordered_nos: list[str],
        candidates: dict[str, list[tuple[str, int]]],
        template: str,
    ) -> dict[str, tuple[str, int]]:
        """Return an exact minimum-cost feasible car-to-slot matching."""
        if not ordered_nos or any(not candidates.get(no) for no in ordered_nos):
            return {}
        slots = sorted({slot for no in ordered_nos for slot in candidates[no]})
        car_count = len(ordered_nos)
        slot_count = len(slots)
        source = 0
        car_start = 1
        slot_start = car_start + car_count
        sink = slot_start + slot_count
        graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

        def add_edge(left: int, right: int, capacity: int, cost: int) -> list[int]:
            forward = [right, len(graph[right]), capacity, cost]
            reverse = [left, len(graph[left]), 0, -cost]
            graph[left].append(forward)
            graph[right].append(reverse)
            return forward

        for car_index in range(car_count):
            add_edge(source, car_start + car_index, 1, 0)
        for slot_index in range(slot_count):
            add_edge(slot_start + slot_index, sink, 1, 0)

        slot_index_by_value = {slot: index for index, slot in enumerate(slots)}
        assignment_edges: list[tuple[str, tuple[str, int], list[int]]] = []
        # Slot rank is a deterministic tie-break only; 1000 keeps the summed
        # tie cost below one unit of the business preference cost.
        for car_index, no in enumerate(ordered_nos):
            for slot in sorted(candidates[no]):
                slot_index = slot_index_by_value[slot]
                cost = self.slot_preference_cost(no, slot, template) * 1000 + slot_index
                edge = add_edge(car_start + car_index, slot_start + slot_index, 1, cost)
                assignment_edges.append((no, slot, edge))

        flow = 0
        node_count = sink + 1
        while flow < car_count:
            distance = [INF_COST * 1000] * node_count
            previous: list[tuple[int, int] | None] = [None] * node_count
            distance[source] = 0
            for _iteration in range(node_count - 1):
                changed = False
                for left in range(node_count):
                    if distance[left] >= INF_COST * 1000:
                        continue
                    for edge_index, edge in enumerate(graph[left]):
                        right, _reverse_index, capacity, cost = edge
                        candidate = distance[left] + cost
                        if capacity > 0 and candidate < distance[right]:
                            distance[right] = candidate
                            previous[right] = (left, edge_index)
                            changed = True
                if not changed:
                    break
            if previous[sink] is None:
                break
            node = sink
            while node != source:
                left, edge_index = previous[node] or (-1, -1)
                if left < 0:
                    break
                edge = graph[left][edge_index]
                reverse_index = edge[1]
                edge[2] -= 1
                graph[node][reverse_index][2] += 1
                node = left
            if node != source:
                break
            flow += 1
        if flow != car_count:
            return {}
        return {
            no: slot
            for no, slot, edge in assignment_edges
            if edge[2] == 0
        }

    def build_cohesive_assigned_line_by_no(self, template: str) -> dict[str, str]:
        """Assign direct-unloadable contiguous blocks in pickup exposure order.

        The cost-oriented matcher only answers whether every car has a legal
        slot.  This dynamic program additionally preserves the order in which
        cars become exposed from the locomotive consist.  Its primary cost is
        the number of target-line runs, which is also the direct lower bound on
        depot puts.  Slot legality is checked at the final position while the
        sequence is built from deep to shallow.
        """
        self.assignment_reasons = ()
        self.assigned_slot_by_no = {}
        exposure = self.template_exposure_order(template)
        inner_nos = [no for no in exposure if self.inner_target_lines(no)]
        missing_exposure = sorted(
            no
            for no in self.active_nos - self.restoration_nos
            if self.inner_target_lines(no) and no not in set(inner_nos)
        )
        if missing_exposure:
            self.assignment_reasons = (
                f"cohesive_exposure_incomplete:{template}:{','.join(missing_exposure)}",
            )
            return {}

        usable_limits: list[int] = []
        for line in DEPOT_IN:
            if DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
                usable_limits.append(0)
                continue
            fixed_positions = sorted(self.fixed_positioned_positions.get(line, {}))
            capacity = self.caps[line]
            usable_limits.append((min(fixed_positions) - 1) if fixed_positions else capacity)

        outer_nos = sorted(
            no
            for no in self.active_nos - self.restoration_nos
            if not self.inner_target_lines(no) and self.outer_target_lines(no)
        )
        reserved_gate_inners = {
            DEPOT_IN_BY_OUT[min(self.outer_target_lines(no), key=lambda line: (self.line_number(line), line))]
            for no in outer_nos
            if self.outer_target_lines(no)
        }

        # A non-inner car or the second trip breaks a direct target-line run,
        # even when the same inner line is selected on both sides.
        segment_by_no: dict[str, int] = {}
        segment = 0
        prior_was_inner = False
        prior_phase = -1
        for no in exposure:
            phase = 1 if template == "A" and self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE else 0
            is_inner = bool(self.inner_target_lines(no))
            if is_inner and (not prior_was_inner or phase != prior_phase):
                segment += 1
            elif not is_inner:
                segment += 1
            if is_inner:
                segment_by_no[no] = segment
            prior_was_inner = is_inner
            prior_phase = phase

        # key = counts per line, last line, last exposure segment, lines on
        # which a deeper section-repair car has already been assigned.
        start_key = ((0, 0, 0, 0), -1, -1, 0)
        plans: dict[
            tuple[tuple[int, int, int, int], int, int, int],
            tuple[tuple[int, int, int], tuple[str, ...]],
        ] = {
            start_key: ((0, 0, 0), ())
        }
        for exposure_index, no in enumerate(inner_nos):
            car = self.meta[no]
            next_plans: dict[
                tuple[tuple[int, int, int, int], int, int, int],
                tuple[tuple[int, int, int], tuple[str, ...]],
            ] = {}
            current_segment = segment_by_no[no]
            for (counts, last_line, last_segment, section_mask), (score, path) in plans.items():
                for line_index, line in enumerate(DEPOT_IN):
                    if line not in self.inner_target_lines(no):
                        continue
                    if counts[line_index] >= usable_limits[line_index]:
                        continue
                    # Exposure order is deep-to-shallow for a direct unload.
                    position = usable_limits[line_index] - counts[line_index]
                    if not self.slot_allowed_for_car(car, line, position, self.caps[line]):
                        continue
                    is_factory = self.repair_process(car).startswith("厂")
                    is_section = self.repair_process(car).startswith("段")
                    if is_factory and section_mask & (1 << line_index):
                        continue
                    next_counts = list(counts)
                    next_counts[line_index] += 1
                    next_mask = section_mask | ((1 << line_index) if is_section else 0)
                    run_delta = int(last_line != line_index or last_segment != current_segment)
                    next_score = (
                        score[0] + run_delta,
                        score[1] + (
                            len(inner_nos) - exposure_index
                            if line in reserved_gate_inners
                            else 0
                        ),
                        score[2] + self.slot_preference_cost(no, (line, position), template),
                    )
                    next_key = (tuple(next_counts), line_index, current_segment, next_mask)
                    incumbent = next_plans.get(next_key)
                    candidate = (next_score, (*path, line))
                    if incumbent is None or candidate < incumbent:
                        next_plans[next_key] = candidate
            plans = next_plans
            if not plans:
                self.assignment_reasons = (
                    f"cohesive_direct_unload_order_infeasible:{template}:{no}",
                )
                return {}

        _best_key, (best_score, best_path) = min(
            plans.items(),
            key=lambda item: (item[1][0], item[1][1]),
        )
        del best_score
        assigned_lines = dict(zip(inner_nos, best_path))
        seen_per_line = Counter()
        for no, line in zip(inner_nos, best_path):
            line_index = DEPOT_IN.index(line)
            position = usable_limits[line_index] - seen_per_line[line]
            seen_per_line[line] += 1
            self.assigned_slot_by_no[no] = (line, position)

        assigned_outer, assigned_outer_slots, missing_outer = self.assign_outer_targets(
            outer_nos,
            set(best_path),
        )
        assigned_lines.update(assigned_outer)
        self.assigned_slot_by_no.update(assigned_outer_slots)
        deferred_nos = sorted(
            no for no in self.active_nos - self.restoration_nos if self.is_stage4_deferred(no)
        )
        restoration_lines = {no: self.meta[no]["Line"] for no in self.restoration_nos}
        assigned_deferred, missing_deferred = self.assign_deferred_stage4_targets(
            deferred_nos,
            reserved_load=self.assigned_loads({**assigned_outer, **restoration_lines}),
            used_inner_lines=set(best_path),
        )
        assigned_lines.update(assigned_deferred)
        assigned_lines.update(restoration_lines)
        reasons: list[str] = []
        if missing_outer:
            reasons.append(f"outer_assignment_incomplete:{template}:{','.join(missing_outer)}")
        if missing_deferred:
            reasons.append(f"stage4_defer_staging_incomplete:{template}:{','.join(missing_deferred)}")
        self.assignment_reasons = tuple(reasons)
        return assigned_lines

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

    def assign_deferred_stage4_targets(
        self,
        deferred_nos: list[str],
        *,
        reserved_load: dict[str, float],
        used_inner_lines: set[str],
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        if not deferred_nos:
            return {}, ()
        remaining = {
            line: float(rv.TRACK_LEN.get(line) or 0.0)
            - sum(float(car["Length"]) for car in self.fixed_cars if car["Line"] == line)
            - reserved_load.get(line, 0.0)
            for line in STAGE4_STAGING_LINES
        }
        assigned: dict[str, str] = {}
        for no in deferred_nos:
            line = self.meta[no]["Line"]
            can_stay = (
                line == UNWHEEL
                or line in set(DEPOT_OUT)
                and DEPOT_IN_BY_OUT[line] not in used_inner_lines
            )
            if line not in set(STAGE4_STAGING_LINES) or not can_stay:
                continue
            car_length = self.length((no,))
            if remaining[line] + rv.TOL < car_length:
                return assigned, tuple(sorted(item for item in deferred_nos if item not in assigned))
            assigned[no] = line
            remaining[line] -= car_length
        for target in ("存4线", "油漆线"):
            group = [
                no
                for no in self.deferred_stage4_order(deferred_nos)
                if no not in assigned and self.deferred_stage4_target(no) == target
            ]
            if not group:
                continue
            packed = self.pack_deferred_stage4_group(group, remaining)
            if packed is None:
                return assigned, tuple(sorted(no for no in deferred_nos if no not in assigned))
            for line, chunk in packed:
                for no in chunk:
                    assigned[no] = line
                remaining[line] -= self.length(chunk)
        missing = tuple(sorted(no for no in deferred_nos if no not in assigned))
        return assigned, missing

    def deferred_stage4_order(self, nos: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                nos,
                key=lambda no: (
                    0 if self.deferred_stage4_target(no) == "存4线" else 1,
                    self.line_rank_for_deferred(self.meta[no]["Line"]),
                    int(self.meta[no].get("Position") or 0),
                    no,
                ),
            )
        )

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

    def pack_deferred_stage4_group(
        self,
        ordered_nos: list[str],
        remaining: dict[str, float],
    ) -> list[tuple[str, tuple[str, ...]]] | None:
        n = len(ordered_nos)
        lengths = [self.length((no,)) for no in ordered_nos]
        prefix = [0.0]
        for value in lengths:
            prefix.append(prefix[-1] + value)

        def chunk_len(left: int, right: int) -> float:
            return prefix[right] - prefix[left]

        line_order = tuple(
            sorted(
                STAGE4_STAGING_LINES,
                key=lambda line: (
                    0 if line == UNWHEEL else 1,
                    self.line_number(line),
                    line,
                ),
            )
        )
        best: tuple[tuple[int, int, float, tuple[str, ...]], list[tuple[str, tuple[str, ...]]]] | None = None

        def rec(index: int, available: tuple[str, ...], chunks: list[tuple[str, tuple[str, ...]]]) -> None:
            nonlocal best
            if index >= n:
                used = tuple(line for line, _chunk in chunks)
                score = (
                    len(chunks),
                    sum(1 for line in used if line != UNWHEEL),
                    round(sum(remaining[line] for line in used), 3),
                    used,
                )
                candidate = (score, list(chunks))
                if best is None or candidate[0] < best[0]:
                    best = candidate
                return
            if best is not None and len(chunks) >= best[0][0]:
                return
            for line in available:
                limit = remaining.get(line, 0.0)
                if limit <= rv.TOL:
                    continue
                for end in range(n, index, -1):
                    size = chunk_len(index, end)
                    if size > limit + rv.TOL:
                        continue
                    chunk = tuple(ordered_nos[index:end])
                    next_available = tuple(item for item in available if item != line)
                    chunks.append((line, chunk))
                    rec(end, next_available, chunks)
                    chunks.pop()

        rec(0, line_order, [])
        return best[1] if best else None

    def assigned_loads(
        self,
        assigned_lines: dict[str, str],
    ) -> dict[str, float]:
        loads: Counter[str] = Counter()
        for no, line in assigned_lines.items():
            loads[line] += float(self.meta[no]["Length"])
        return dict(loads)

    def assign_outer_targets(
        self,
        outer_nos: list[str],
        used_inner_lines: set[str],
    ) -> tuple[dict[str, str], dict[str, tuple[str, int]], tuple[str, ...]]:
        if not outer_nos:
            return {}, {}, ()
        fixed_load = {
            line: sum(float(car["Length"]) for car in self.fixed_cars if car["Line"] == line)
            for line in DEPOT_OUT
        }
        remaining = {
            line: float(rv.TRACK_LEN.get(line) or 0.0) - fixed_load.get(line, 0.0)
            for line in DEPOT_OUT
        }
        candidates = {
            no: tuple(sorted(self.outer_target_lines(no)))
            for no in outer_nos
        }
        ordered = sorted(
            outer_nos,
            key=lambda no: (len(candidates.get(no, ())), -self.length((no,)), no),
        )
        initial_stayers = {
            line
            for no in outer_nos
            for line in (self.meta[no]["Line"],)
            if line in set(DEPOT_OUT) and self.terminal_line_satisfied(no, line)
        }
        best: tuple[
            tuple[int, tuple[tuple[str, str], ...], tuple[tuple[str, tuple[str, int]], ...]],
            dict[str, str],
            dict[str, tuple[str, int]],
        ] | None = None
        current: dict[str, str] = {}

        def assign_forced_positions() -> dict[str, tuple[str, int]] | None:
            assigned: dict[str, tuple[str, int]] = {}
            for line in DEPOT_OUT:
                line_nos = [
                    no
                    for no in outer_nos
                    if current.get(no) == line and self.meta[no].get("_ForcePositions")
                ]
                if not line_nos:
                    continue
                fixed_positions = set(self.fixed_positioned_positions.get(line, {}))
                options = {
                    no: tuple(
                        int(position)
                        for position in self.meta[no]["_ForcePositions"]
                        if int(position) > 0 and int(position) not in fixed_positions
                    )
                    for no in line_nos
                }
                ordered_line_nos = sorted(
                    line_nos,
                    key=lambda no: (len(options[no]), no),
                )
                used: set[int] = set()
                selected: dict[str, int] = {}

                def match(index: int) -> bool:
                    if index == len(ordered_line_nos):
                        return True
                    no = ordered_line_nos[index]
                    for position in options[no]:
                        if position in used:
                            continue
                        used.add(position)
                        selected[no] = position
                        if match(index + 1):
                            return True
                        selected.pop(no)
                        used.remove(position)
                    return False

                if not match(0):
                    return None
                assigned.update(
                    {no: (line, selected[no]) for no in ordered_line_nos}
                )
            return assigned

        def rec(index: int, cost: int) -> None:
            nonlocal best
            if best is not None and cost >= best[0][0]:
                return
            if index == len(ordered):
                forced_slots = assign_forced_positions()
                if forced_slots is None:
                    return
                assignment = dict(current)
                candidate_key = (
                    cost,
                    tuple(sorted(assignment.items())),
                    tuple(sorted(forced_slots.items())),
                )
                if best is None or candidate_key < best[0]:
                    best = (candidate_key, assignment, forced_slots)
                return
            no = ordered[index]
            car_len = self.length((no,))
            car = self.meta[no]
            initial_line = car["Line"]
            is_stayer = initial_line in set(DEPOT_OUT) and self.terminal_line_satisfied(
                no,
                initial_line,
            )
            for line in candidates.get(no, ()):
                if remaining.get(line, -1.0) + rv.TOL < car_len:
                    continue
                remaining[line] -= car_len
                current[no] = line
                line_cost = self.outer_line_cost(line, used_inner_lines) * 100
                if is_stayer and line != initial_line:
                    line_cost += 10_000
                elif not is_stayer and line in initial_stayers:
                    line_cost += 1_000
                rec(index + 1, cost + line_cost)
                current.pop(no, None)
                remaining[line] += car_len

        rec(0, 0)
        if best is None:
            return {}, {}, tuple(sorted(outer_nos))
        assigned, forced_slots = best[1], best[2]
        missing = tuple(sorted(no for no in outer_nos if no not in assigned))
        return assigned, forced_slots, missing

    def outer_line_cost(self, line: str, used_inner_lines: set[str]) -> int:
        inner = DEPOT_IN_BY_OUT[line]
        return (10 if inner in used_inner_lines else 0) + self.line_number(line)

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

        clear_modes = (
            (True, False)
            if any(self.is_stage4_deferred(no) for no in self.active_nos)
            else (True,)
        )
        strategies = tuple(
            (template, layout, deferred_clear, terminal_merge, inner_clear_policy)
            for template, layout in (
                ("B", "cohesive"),
                ("A", "cohesive"),
                ("B", "cost"),
                ("A", "cost"),
            )
            for deferred_clear in clear_modes
            for terminal_merge in (True, False)
            for inner_clear_policy in ("eager", "just_in_time")
        )
        results = [
            self.validate_candidate(self.solve_template(
                template,
                layout=layout,
                deferred_clear=deferred_clear,
                terminal_merge=terminal_merge,
                inner_clear_policy=inner_clear_policy,
                allow_search=False,
            ))
            for template, layout, deferred_clear, terminal_merge, inner_clear_policy in strategies
        ]

        # Exact search is an explicit small-state strategy, not a failure
        # continuation.  Larger cases stay on the block planner so their
        # diagnostics and memory use remain bounded.
        if len(self.active_nos) <= EXACT_SEARCH_ACTIVE_LIMIT:
            exact_specs = tuple(dict.fromkeys(
                (template, layout, deferred_clear)
                for template, layout, deferred_clear, _merge, _policy in strategies
            ))
            for offset, (template, layout, deferred_clear) in enumerate(exact_specs):
                remaining_time = max(0.0, self.global_deadline - time.monotonic())
                remaining_strategies = len(exact_specs) - offset
                if remaining_time <= 0.0:
                    self.portfolio_evaluation_incomplete = True
                    break
                self.deadline = min(
                    self.global_deadline,
                    time.monotonic() + remaining_time / remaining_strategies,
                )
                results.append(self.validate_candidate(self.solve_template(
                    template,
                    layout=layout,
                    deferred_clear=deferred_clear,
                    terminal_merge=False,
                    inner_clear_policy="exact",
                    allow_search=True,
                )))

        if any(item.status == "complete" for item in results) and len(self.active_nos) > EXACT_SEARCH_ACTIVE_LIMIT:
            best_result = self.choose_result(results)
            incumbent_hooks = self.business_hook_count(best_result.ops)
            portfolio_lower_bound = min(
                (item.lower_bound for item in results if item.lower_bound is not None),
                default=incumbent_hooks,
            )
            improvable = tuple(dict.fromkeys(
                (item.template, item.layout, item.deferred_clear)
                for item in results
                if item.lower_bound is not None
                and item.lower_bound < incumbent_hooks
                and incumbent_hooks - item.lower_bound <= 2
                and (item.status == "complete" or item.reasons == ("greedy_no_completion",))
            ))
            improvement_deadline = min(
                self.global_deadline,
                time.monotonic() + IMPROVEMENT_TIME_BUDGET_SECONDS,
            )
            remaining_expansions = MAX_IMPROVEMENT_EXPANSIONS
            for offset, (template, layout, deferred_clear) in enumerate(improvable):
                if incumbent_hooks <= portfolio_lower_bound:
                    break
                remaining_time = improvement_deadline - time.monotonic()
                remaining_candidates = len(improvable) - offset
                if remaining_time <= 0.0 or remaining_expansions <= 0:
                    self.optimization_budget_exhausted = True
                    break
                self.optimization_attempted += 1
                expansion_share = max(1, remaining_expansions // remaining_candidates)
                self.deadline = time.monotonic() + remaining_time / remaining_candidates
                improved = self.validate_candidate(self.solve_template(
                    template,
                    layout=layout,
                    deferred_clear=deferred_clear,
                    terminal_merge=False,
                    inner_clear_policy="exact",
                    allow_search=True,
                    improve_below_hooks=incumbent_hooks,
                    search_expansion_limit=expansion_share,
                ))
                results.append(improved)
                self.optimization_expansions += improved.expansions
                remaining_expansions -= improved.expansions
                if (
                    improved.status == "complete"
                    and self.business_hook_count(improved.ops) < incumbent_hooks
                ):
                    incumbent_hooks = self.business_hook_count(improved.ops)
        self.deadline = self.global_deadline
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

    def solve_template(
        self,
        template: str,
        *,
        layout: str = "cost",
        deferred_clear: bool = True,
        terminal_merge: bool = True,
        inner_clear_policy: str = "eager",
        allow_search: bool = True,
        improve_below_hooks: int | None = None,
        search_expansion_limit: int = MAX_EXPANSIONS,
    ) -> SearchResult:
        started = time.monotonic()
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
                deferred_clear=deferred_clear,
                terminal_merge=terminal_merge,
                inner_clear_policy=inner_clear_policy,
                strategy_evaluated=False,
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
                deferred_clear=deferred_clear,
                terminal_merge=terminal_merge,
                inner_clear_policy=inner_clear_policy,
            )
        lower_bound_components = (
            self.exact_operation_lower_bound_components(template)
            if inner_clear_policy == "exact"
            else self.template_operation_lower_bound_components(template)
        )
        lower_bound = sum(lower_bound_components.values())

        def annotate(result: SearchResult) -> SearchResult:
            return replace(
                result,
                layout=layout,
                deferred_clear=deferred_clear,
                terminal_merge=terminal_merge,
                inner_clear_policy=inner_clear_policy,
                lower_bound=lower_bound,
                lower_bound_components=tuple(sorted(lower_bound_components.items())),
                lower_bound_scope=(
                    "assignment_independent_relaxation"
                    if inner_clear_policy == "exact"
                    else "fixed_template_layout_relaxation"
                ),
            )
        if template == "B":
            pickup_order = TEMPLATE_B_ORDER
            phase = 1
        elif template == "A":
            pickup_order = TEMPLATE_A_FIRST_ORDER
            phase = 0
        else:
            raise ValueError(f"unknown_template:{template}")

        pickup_key = (template, layout, deferred_clear)
        built = self.pickup_cache.get(pickup_key)
        if built is None:
            built_raw = self.apply_pickup_template(
                pickup_order,
                phase=phase,
                template=template,
                deferred_clear=deferred_clear,
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
            ))
        if inner_clear_policy == "exact":
            if not allow_search:
                raise ValueError("exact_strategy_requires_search")
            return annotate(self.search(
                template,
                state,
                tuple(pickup_ops),
                started,
                hook_limit=improve_below_hooks,
                expansion_limit=search_expansion_limit,
            ))
        greedy = self.greedy_finish(
            template,
            state,
            list(pickup_ops),
            started,
            terminal_merge=terminal_merge,
            inner_clear_policy=inner_clear_policy,
        )
        return annotate(greedy)

    def template_operation_lower_bound(self, template: str) -> int:
        return sum(self.template_operation_lower_bound_components(template).values())

    def exact_operation_lower_bound_components(self, template: str) -> dict[str, int]:
        """Assignment-independent relaxation for exact-search candidates."""
        must_move = {
            no
            for no in self.active_nos
            if not self.terminal_line_satisfied(no, self.meta[no]["Line"])
        }
        inner_epochs = {
            1 if template == "A" and self.meta[no]["Line"] == TEMPLATE_A_SECOND_LINE else 0
            for no in must_move
            if self.inner_target_lines(no)
        }
        non_inner = [no for no in must_move if not self.inner_target_lines(no)]
        return {
            "source_gets": len({self.meta[no]["Line"] for no in must_move}),
            "inner_puts": len(inner_epochs),
            "non_inner_puts": self.minimum_non_inner_terminal_lines(non_inner),
            "frontier_rehandle": 0,
        }

    def template_operation_lower_bound_components(self, template: str) -> dict[str, int]:
        """Cheap admissible hook bound for one fixed template/layout strategy."""
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
                set(STAGE4_STAGING_LINES)
                if self.is_stage4_deferred(no)
                else self.outer_target_lines(no)
            )
            for no in nos
        }
        ordered = sorted(nos, key=lambda no: (len(allowed[no]), -self.length((no,)), no))

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
                    return count
                remaining.update(snapshot)
        return len(lines)

    def deadline_reached(self) -> bool:
        return time.monotonic() >= self.deadline

    def greedy_finish(
        self,
        template: str,
        state: State,
        ops: list[Op],
        started: float,
        *,
        terminal_merge: bool,
        inner_clear_policy: str,
    ) -> SearchResult:
        if inner_clear_policy not in {"eager", "just_in_time"}:
            raise ValueError(f"unknown_inner_clear_policy:{inner_clear_policy}")
        seen: set[State] = set()
        for _step in range(120):
            if self.deadline_reached():
                break
            if self.complete(state):
                return SearchResult(
                    status="complete",
                    template=template,
                    state=state,
                    ops=tuple(ops),
                    cost=self.ops_cost(ops),
                    reasons=(),
                    expansions=0,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
            if state in seen:
                break
            seen.add(state)

            if template == "A" and state.phase == 0 and not state.held:
                applied_wash = self.greedy_get_template_a_second(state)
                if applied_wash:
                    op, state = applied_wash
                    ops.append(op)
                    continue
                if any(
                    no in self.task_nos
                    for no in self.line_map(state).get(TEMPLATE_A_SECOND_LINE, ())
                ):
                    break
                state = State(
                    lines=state.lines,
                    held=state.held,
                    loco=state.loco,
                    phase=1,
                    positioned_positions=state.positioned_positions,
                )
                continue

            if state.held:
                if inner_clear_policy == "eager":
                    applied = self.greedy_get_blocking_inner(state)
                    if applied:
                        op, state = applied
                        ops.append(op)
                        continue
                gate_macro = self.greedy_clear_gate_macro(state)
                if gate_macro:
                    macro_ops, state = gate_macro
                    ops.extend(macro_ops)
                    continue
                if inner_clear_policy == "just_in_time":
                    applied = self.greedy_get_tail_blocking_inner(state)
                    if applied:
                        op, state = applied
                        ops.append(op)
                        continue
                last_op = ops[-1] if ops else None
                applied = self.greedy_put_ready_inner(state)
                if not applied:
                    applied = self.greedy_put_terminal_without_new_door_debt(
                        state,
                        last_op=last_op,
                    )
                if not applied:
                    applied = self.greedy_put_alignment_blockers(
                        state,
                        avoid_line=(
                            last_op.line
                            if last_op and last_op.action == "Get"
                            else ""
                        ),
                    )
                if not applied and terminal_merge:
                    applied = self.greedy_put_terminal_compatible_suffix(state)
                if not applied:
                    applied = self.greedy_put(state, last_op=last_op)
                if not applied and inner_clear_policy == "just_in_time":
                    applied = self.greedy_get_blocking_inner(state)
                if not applied:
                    applied = self.greedy_get_blocking_outer(state)
                if not applied:
                    break
                op, state = applied
                ops.append(op)
                continue

            applied_get = self.greedy_get_outer(state)
            if not applied_get:
                break
            op, state = applied_get
            ops.append(op)

        return SearchResult(
            status="partial",
            template=template,
            state=None,
            ops=tuple(ops),
            cost=(INF_COST, 0, 0, 0),
            reasons=("greedy_no_completion",),
            expansions=0,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def greedy_get_template_a_second(self, state: State) -> tuple[Op, State] | None:
        line_map = self.line_map(state)
        pickup_nos = set(
            self.initial_source_pickup_map().get(TEMPLATE_A_SECOND_LINE, ())
        )
        wash = tuple(
            no for no in line_map.get(TEMPLATE_A_SECOND_LINE, ()) if no in pickup_nos
        )
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
            if not reject:
                return op, next_state
        return None

    def greedy_put(self, state: State, *, last_op: Op | None = None) -> tuple[Op, State] | None:
        held = state.held
        avoid_outer_line = last_op.line if last_op and last_op.action == "Get" and last_op.line in set(DEPOT_OUT) else ""
        for start in range(len(held)):
            move = held[start:]
            target = self.common_assigned_target(move)
            if not target:
                continue
            preferred_lines = self.greedy_preferred_put_lines(
                state,
                target,
                move,
                held[:start],
                avoid_outer_line=avoid_outer_line,
            )
            for line in preferred_lines:
                if not self.put_candidate_allowed(state, line, move):
                    continue
                op, next_state, reject = self.apply_put(state, line, move)
                if not reject:
                    return op, next_state
        return None

    def greedy_put_ready_inner(self, state: State) -> tuple[Op, State] | None:
        for start in range(len(state.held)):
            move = state.held[start:]
            target = self.common_assigned_target(move)
            if target not in set(DEPOT_IN):
                continue
            if not self.depot_move_is_ready(state, target, move):
                continue
            if not self.put_candidate_allowed(state, target, move):
                continue
            op, next_state, reject = self.apply_put(state, target, move)
            if not reject:
                return op, next_state
        return None

    def greedy_put_terminal_without_new_door_debt(
        self,
        state: State,
        *,
        last_op: Op | None,
    ) -> tuple[Op, State] | None:
        """Commit a terminal suffix when it cannot create another door-clear cycle."""
        line_map = self.line_map(state)
        for start in range(len(state.held)):
            move = state.held[start:]
            pending_inner = self.pending_inner_targets(state, exclude=set(move))
            candidates = [
                line
                for line in STAGE4_STAGING_LINES
                if all(self.terminal_line_satisfied(no, line) for no in move)
                and self.line_has_capacity(state, line, move)
                and not any(
                    self.assigned_line_by_no.get(no) == line
                    for no in state.held[:start]
                )
                and (
                    line == UNWHEEL
                    or DEPOT_IN_BY_OUT[line] not in pending_inner
                    or bool(line_map.get(line))
                )
            ]
            if candidates:
                shared = self.greedy_put_shared_retrieved_block(
                    state,
                    terminal_start=start,
                    last_op=last_op,
                )
                if shared:
                    return shared
                shared = self.greedy_put_shared_noninner_block(state, start)
                if shared:
                    return shared
            for line in sorted(
                candidates,
                key=lambda candidate: (
                    not bool(line_map.get(candidate)),
                    candidate == UNWHEEL,
                    self.line_number(candidate),
                    candidate,
                ),
            ):
                op, next_state, reject = self.apply_put(state, line, move)
                if not reject:
                    return replace(op, note=f"commit_terminal_without_new_door_debt:{line}"), next_state
        return None

    def greedy_put_shared_retrieved_block(
        self,
        state: State,
        *,
        terminal_start: int,
        last_op: Op | None,
    ) -> tuple[Op, State] | None:
        if (
            last_op is None
            or last_op.action != "Get"
            or last_op.line not in set(STAGE4_STAGING_LINES)
            or len(last_op.move) <= len(state.held) - terminal_start
            or state.held[-len(last_op.move):] != last_op.move
        ):
            return None
        move = last_op.move
        pending_inner = self.pending_inner_targets(state, exclude=set(move))
        for line in sorted(
            STAGE4_STAGING_LINES,
            key=lambda candidate: (
                candidate != UNWHEEL and DEPOT_IN_BY_OUT[candidate] in pending_inner,
                candidate != UNWHEEL,
                self.line_number(candidate),
                candidate,
            ),
        ):
            if line == last_op.line or not self.line_is_empty(state, line):
                continue
            if not self.line_has_capacity(state, line, move):
                continue
            op, next_state, reject = self.apply_put(state, line, move)
            if not reject:
                return replace(op, note=f"preserve_retrieved_block:{line}"), next_state
        return None

    def greedy_put_shared_noninner_block(
        self,
        state: State,
        terminal_start: int,
    ) -> tuple[Op, State] | None:
        block_start = terminal_start
        while block_start > 0:
            prior = state.held[block_start - 1]
            if self.assigned_line_by_no.get(prior, "") in set(DEPOT_IN):
                break
            block_start -= 1
        if block_start == terminal_start:
            return None
        move = state.held[block_start:]

        keep_inner_open = ""
        if block_start > 0:
            candidate = self.assigned_line_by_no.get(state.held[block_start - 1], "")
            if candidate in set(DEPOT_IN):
                keep_inner_open = candidate
        pending_inner = self.pending_inner_targets(state, exclude=set(move))
        candidate_lines = tuple(
            line
            for line in sorted(
                STAGE4_STAGING_LINES,
                key=lambda line: (
                    line != UNWHEEL and DEPOT_IN_BY_OUT[line] in pending_inner,
                    line != UNWHEEL,
                    self.line_number(line),
                    line,
                ),
            )
            if (not keep_inner_open or line != DEPOT_OUT_BY_IN[keep_inner_open])
            and self.line_is_empty(state, line)
            and self.line_has_capacity(state, line, move)
        )
        for line in candidate_lines:
            op, next_state, reject = self.apply_put(state, line, move)
            if not reject:
                return replace(op, note=f"share_noninner_finish:{line}"), next_state
        return None

    def greedy_put_alignment_blockers(
        self,
        state: State,
        *,
        avoid_line: str = "",
    ) -> tuple[Op, State] | None:
        """Buffer only the tail that hides a target line's next deepest car."""
        next_no_by_line = self.next_assigned_no_by_line(state)
        for index in range(len(state.held) - 2, -1, -1):
            no = state.held[index]
            target = self.assigned_line_by_no.get(no, "")
            if target not in set(DEPOT_IN) or next_no_by_line.get(target) != no:
                continue
            move = state.held[index + 1:]
            for line in self.alignment_buffer_lines(
                state,
                move,
                keep_inner_open=target,
                avoid_line=avoid_line,
            ):
                op, next_state, reject = self.apply_put(state, line, move)
                if not reject:
                    return replace(op, note=f"stage_alignment_block:{target}"), next_state
        return None

    def alignment_buffer_lines(
        self,
        state: State,
        move: tuple[str, ...],
        *,
        keep_inner_open: str,
        avoid_line: str,
    ) -> tuple[str, ...]:
        pending = self.pending_inner_targets(state, exclude=set(move))
        return tuple(
            line
            for line in sorted(
                STAGE4_STAGING_LINES,
                key=lambda candidate: (
                    candidate != UNWHEEL and DEPOT_IN_BY_OUT[candidate] in pending,
                    candidate != UNWHEEL,
                    self.line_number(candidate),
                    candidate,
                ),
            )
            if line not in {avoid_line, DEPOT_OUT_BY_IN[keep_inner_open]}
            and self.line_is_empty(state, line)
            and self.put_candidate_allowed(state, line, move)
        )

    def greedy_put_buffer_block(
        self,
        state: State,
        move: tuple[str, ...],
        *,
        keep_inner_open: str,
        avoid_line: str,
    ) -> tuple[Op, State] | None:
        for line in self.alignment_buffer_lines(
            state,
            move,
            keep_inner_open=keep_inner_open,
            avoid_line=avoid_line,
        ):
            op, next_state, reject = self.apply_put(state, line, move)
            if not reject:
                return replace(op, note=f"relocate_gate_block:{keep_inner_open}"), next_state
        return None

    def next_assigned_no_by_line(self, state: State) -> dict[str, str]:
        line_map = self.line_map(state)
        return {
            line: max(
                (
                    (slot[1], no)
                    for no, slot in self.assigned_slot_by_no.items()
                    if slot[0] == line and no not in set(line_map.get(line, ()))
                ),
                default=(0, ""),
            )[1]
            for line in DEPOT_IN
        }

    def greedy_put_terminal_compatible_suffix(self, state: State) -> tuple[Op, State] | None:
        """Put the longest mixed tail that can legally share one terminal line."""
        held = state.held
        for start in range(len(held)):
            move = held[start:]
            if len({self.assigned_line_by_no.get(no, "") for no in move}) <= 1:
                continue
            pending_before = held[:start]
            pending_inner_targets = {
                self.assigned_line_by_no.get(no, "")
                for no in pending_before
                if self.assigned_line_by_no.get(no, "") in set(DEPOT_IN)
            }
            candidate_lines = sorted(
                STAGE4_STAGING_LINES,
                key=lambda line: (
                    line != UNWHEEL and DEPOT_IN_BY_OUT.get(line, "") in pending_inner_targets,
                    line == UNWHEEL,
                    self.line_number(line),
                    line,
                ),
            )
            for line in candidate_lines:
                if not self.line_is_empty(state, line):
                    continue
                if not all(self.terminal_line_satisfied(no, line) for no in move):
                    continue
                if not self.put_candidate_allowed(state, line, move):
                    continue
                op, next_state, reject = self.apply_put(state, line, move)
                if not reject:
                    return replace(op, note=f"merge_terminal_blocks:{line}"), next_state
        return None

    def greedy_preferred_put_lines(
        self,
        state: State,
        target: str,
        move: tuple[str, ...],
        pending_before: tuple[str, ...],
        *,
        avoid_outer_line: str = "",
    ) -> tuple[str, ...]:
        if target in set(STAGE3_SOURCE_LINES) and all(
            no in self.restoration_nos for no in move
        ):
            return (target,)
        if target in set(DEPOT_IN) and all(self.is_stage4_deferred(no) for no in move):
            line_map = self.line_map(state)
            outer = DEPOT_OUT_BY_IN[target]
            pending_inner_targets = {
                self.assigned_line_by_no.get(no)
                for no in pending_before
                if self.assigned_line_by_no.get(no) in set(DEPOT_IN)
                and not self.is_stage4_deferred(no)
            }
            reusable_outers = tuple(
                line
                for line in DEPOT_OUT
                if line != outer
                and (
                    not line_map.get(line)
                    or all(self.terminal_line_satisfied(no, line) for no in line_map.get(line, ()))
                )
            )
            nonblocking_outers = tuple(
                line for line in reusable_outers if DEPOT_IN_BY_OUT[line] not in pending_inner_targets
            )
            blocking_outers = tuple(
                line for line in reusable_outers if DEPOT_IN_BY_OUT[line] in pending_inner_targets
            )
            alternates = (UNWHEEL, *nonblocking_outers, *blocking_outers)
            pending_same_inner = any(
                self.assigned_line_by_no.get(no) == target and not self.is_stage4_deferred(no)
                for no in pending_before
            )
            if pending_same_inner:
                return (*alternates, outer, target)
            return (target, *alternates, outer)
        if target in set(DEPOT_OUT) and not all(self.is_stage4_deferred(no) for no in move):
            pending_inner_targets = {
                self.assigned_line_by_no.get(no)
                for no in pending_before
                if self.assigned_line_by_no.get(no) in set(DEPOT_IN)
            }
            line_map = self.line_map(state)
            mergeable_staged_lines = tuple(
                line
                for line in DEPOT_OUT
                if line != avoid_outer_line
                and self.line_has_stage4_deferred(state, line)
                and all(line in self.outer_target_lines(no) for no in move)
                and self.line_has_capacity(state, line, move)
            )
            temporary_lines = tuple(
                line
                for line in STAGE4_STAGING_LINES
                if line != target and line not in set(mergeable_staged_lines)
                and self.line_is_empty(state, line)
                and self.line_has_capacity(state, line, move)
            )
            nonblocking_alternates = tuple(
                line
                for line in temporary_lines
                if line == UNWHEEL or DEPOT_IN_BY_OUT[line] not in pending_inner_targets
            )
            blocking_alternates = tuple(
                line
                for line in temporary_lines
                if line != UNWHEEL and DEPOT_IN_BY_OUT[line] in pending_inner_targets
            )
            blocking_inner = DEPOT_IN_BY_OUT[target]
            pending_same_target = any(
                self.assigned_line_by_no.get(no) == target for no in pending_before
            )
            target_first_allowed = (
                target != avoid_outer_line
                and blocking_inner not in pending_inner_targets
                and not pending_same_target
            )
            target_first = (target,) if target_first_allowed else ()
            target_later = (target,) if target != avoid_outer_line and not target_first_allowed else ()
            if any(self.assigned_line_by_no.get(no) == blocking_inner for no in pending_before):
                return (
                    *mergeable_staged_lines,
                    *target_first,
                    *nonblocking_alternates,
                    *blocking_alternates,
                    *target_later,
                    blocking_inner,
                )
            return (
                *mergeable_staged_lines,
                *target_first,
                *nonblocking_alternates,
                *blocking_alternates,
                *target_later,
                blocking_inner,
            )
        if target in set(STAGE4_DEFER_LINES) and target not in set(DEPOT_IN):
            line_map = self.line_map(state)
            pending_inner_targets = {
                self.assigned_line_by_no.get(no)
                for no in pending_before
                if self.assigned_line_by_no.get(no) in set(DEPOT_IN)
                and not self.is_stage4_deferred(no)
            }
            alternates = tuple(
                line
                for line in STAGE4_STAGING_LINES
                if line != target
                and (
                    not line_map.get(line)
                    or all(self.terminal_line_satisfied(no, line) for no in line_map.get(line, ()))
                )
            )
            def nonblocking(line: str) -> bool:
                return line == UNWHEEL or DEPOT_IN_BY_OUT.get(line, "") not in pending_inner_targets

            immediate_inner = ""
            for no in reversed(pending_before):
                if self.is_stage4_deferred(no):
                    continue
                candidate = self.assigned_line_by_no.get(no, "")
                if candidate in set(DEPOT_IN):
                    immediate_inner = candidate
                    break
            immediate_outer = DEPOT_OUT_BY_IN.get(immediate_inner, "")
            target_first = (target,) if target in set(STAGE4_STAGING_LINES) and nonblocking(target) else ()
            target_later = (target,) if target in set(STAGE4_STAGING_LINES) and not nonblocking(target) else ()
            safe = tuple(line for line in alternates if nonblocking(line))
            blocking = tuple(
                sorted(
                    (line for line in alternates if not nonblocking(line)),
                    key=lambda line: (line == immediate_outer, self.line_number(line), line),
                )
            )
            return (*target_first, *safe, *blocking, *target_later)
        outer = DEPOT_OUT_BY_IN[target]
        pending_inner_targets = self.pending_inner_targets(state, exclude=set(move))
        buffers = tuple(
            sorted(
                (
                    line
                    for line in STAGE4_STAGING_LINES
                    if line != outer
                    and line != avoid_outer_line
                    and self.line_is_empty(state, line)
                    and self.line_has_capacity(state, line, move)
                ),
                key=lambda line: (
                    line != UNWHEEL and DEPOT_IN_BY_OUT[line] in pending_inner_targets,
                    line != UNWHEEL,
                    self.line_number(line),
                    line,
                ),
            )
        )
        if self.depot_move_is_ready(state, target, move):
            return (target, *buffers, outer)
        return (*buffers, outer, target)

    def common_assigned_target(self, nos: tuple[str, ...]) -> str:
        if not nos:
            return ""
        targets = {self.assigned_line_by_no.get(no, "") for no in nos}
        if len(targets) != 1:
            return ""
        target = next(iter(targets))
        allowed = set(DEPOT_TARGETS) | set(STAGE4_DEFER_LINES) | set(STAGE3_SOURCE_LINES)
        return target if target in allowed else ""

    def greedy_get_outer(self, state: State) -> tuple[Op, State] | None:
        line_map = self.line_map(state)
        next_no_by_line = self.next_assigned_no_by_line(state)
        ready_prefixes: list[tuple[int, str, str, tuple[str, ...]]] = []
        for line in STAGE4_STAGING_LINES:
            ordered = tuple(line_map.get(line, ()))
            for index, no in enumerate(ordered):
                target = self.assigned_line_by_no.get(no, "")
                if target in set(DEPOT_IN) and next_no_by_line.get(target) == no:
                    ready_prefixes.append((index + 1, target, line, ordered[: index + 1]))
        for _length, target, line, move in sorted(ready_prefixes):
            op, next_state, reject = self.apply_get(state, line, move)
            if not reject:
                return replace(op, note=f"retrieve_alignment_ready:{target}"), next_state
        for line in (*DEPOT_OUT, UNWHEEL, *DEPOT_IN):
            nos = tuple(line_map.get(line, ()))
            if not nos:
                continue
            if (
                self.line_can_stay_terminal(state, line, nos)
                and not self.outer_blocks_held_inner(state, line)
                and not self.outer_blocks_held_position(state, line)
            ):
                continue
            op, next_state, reject = self.apply_get(state, line, nos)
            if not reject:
                return op, next_state
        return None

    def greedy_get_blocking_outer(self, state: State) -> tuple[Op, State] | None:
        if state.held:
            tail_target = self.assigned_line_by_no.get(state.held[-1], "")
            if tail_target in set(DEPOT_IN):
                outer = DEPOT_OUT_BY_IN[tail_target]
                nos = tuple(self.line_map(state).get(outer, ()))
                if nos:
                    op, next_state, reject = self.apply_get(state, outer, nos)
                    if not reject:
                        return op, next_state
        target = self.common_assigned_target(state.held)
        if target in set(DEPOT_IN):
            outer = DEPOT_OUT_BY_IN[target]
            nos = tuple(self.line_map(state).get(outer, ()))
            if nos:
                op, next_state, reject = self.apply_get(state, outer, nos)
                if not reject:
                    return op, next_state
        return self.greedy_get_outer(state)

    def greedy_get_blocking_inner(self, state: State) -> tuple[Op, State] | None:
        for line, move in self.blocking_inner_get_prefixes(state):
            op, next_state, reject = self.apply_get(state, line, move)
            if not reject:
                return op, next_state
        return None

    def greedy_get_tail_blocking_inner(self, state: State) -> tuple[Op, State] | None:
        """Clear only the inner line needed by the currently exposed tail.

        A blocker for a deeper train group is not urgent while another tail
        group can complete a different repair line.  Delaying that Get lets
        the completed line's outer lead become permanent stage-4 staging,
        avoiding the Get/Put rehandle created by eager clearing.
        """
        if not state.held:
            return None
        target = self.assigned_line_by_no.get(state.held[-1], "")
        if target not in set(DEPOT_IN):
            return None
        for line, move in self.blocking_inner_get_prefixes(state):
            if line != target:
                continue
            op, next_state, reject = self.apply_get(state, line, move)
            if not reject:
                return op, next_state
        return None

    def greedy_clear_gate_macro(
        self,
        state: State,
    ) -> tuple[tuple[Op, ...], State] | None:
        """Prove and atomically commit one bounded gate-clear transaction."""
        if not state.held:
            return None
        ready = next(
            (
                (target, move)
                for start in range(len(state.held))
                for move in (state.held[start:],)
                for target in (self.common_assigned_target(move),)
                if target in set(DEPOT_IN)
                and self.depot_move_is_ready(state, target, move)
                and not self.plan_depot_put_positions(state, target, move)[1]
            ),
            None,
        )
        if ready is None:
            return None
        target, move = ready
        outer = DEPOT_OUT_BY_IN[target]
        outer_nos = tuple(self.line_map(state).get(outer, ()))
        if not outer_nos:
            return None
        get_op, after_get, reject = self.apply_get(state, outer, outer_nos)
        if reject:
            return None

        def is_original_commit(op: Op) -> bool:
            return op.line == target and op.move == move

        def committed(
            prefix: tuple[Op, ...],
            candidate: tuple[Op, State] | None,
        ) -> tuple[tuple[Op, ...], State] | None:
            if candidate is None:
                return None
            op, next_state = candidate
            if not is_original_commit(op):
                return None
            return (
                (
                    replace(get_op, note=f"atomic_gate_clear:{target}"),
                    *prefix,
                    replace(op, note=f"atomic_inner_commit:{target}"),
                ),
                next_state,
            )

        permanent = self.greedy_put_ready_inner(after_get)
        direct = committed((), permanent)
        if direct:
            return direct
        if permanent:
            permanent_op, after_permanent = permanent
            direct = committed(
                (replace(permanent_op, note=f"atomic_gate_permanent:{permanent_op.line}"),),
                self.greedy_put_ready_inner(after_permanent),
            )
            if direct:
                return direct

        terminal = self.greedy_put_exposed_terminal_suffix(
            after_get,
            outer_nos,
            avoid_line=outer,
        )
        if terminal:
            terminal_op, after_terminal = terminal
            terminal_prefix = (
                replace(terminal_op, note=f"atomic_gate_terminal:{terminal_op.line}"),
            )
            ready_after_terminal = self.greedy_put_ready_inner(after_terminal)
            direct = committed(terminal_prefix, ready_after_terminal)
            if direct:
                return direct
            if ready_after_terminal:
                ready_op, after_ready = ready_after_terminal
                direct = committed(
                    (
                        *terminal_prefix,
                        replace(ready_op, note=f"atomic_gate_permanent:{ready_op.line}"),
                    ),
                    self.greedy_put_ready_inner(after_ready),
                )
                if direct:
                    return direct

        relocated = self.greedy_put_buffer_block(
            after_get,
            outer_nos,
            keep_inner_open=target,
            avoid_line=outer,
        )
        relocation_creates_door_debt = bool(
            relocated
            and relocated[0].line in set(DEPOT_OUT)
            and DEPOT_IN_BY_OUT[relocated[0].line]
            in self.pending_inner_targets(after_get, exclude=set(outer_nos))
        )
        nested_dependency = (
            self.nested_gate_dependency(
                after_get,
                original_target=target,
                original_outer=outer,
                first_gate=outer_nos,
            )
            if relocation_creates_door_debt
            else None
        )
        if nested_dependency is not None:
            nested = self.greedy_nested_gate_swap(
                after_get,
                original_target=target,
                original_move=move,
                original_outer=outer,
                first_gate=outer_nos,
                dependency=nested_dependency,
            )
            if nested is None:
                return None
            nested_ops, final_state = nested
            return (
                (
                    replace(get_op, note=f"atomic_gate_clear:{target}"),
                    *nested_ops,
                ),
                final_state,
            )
        if not relocated:
            return None
        relocate_op, after_relocate = relocated
        direct = self.greedy_put_ready_inner(after_relocate)
        if not direct:
            return None
        direct_op, final_state = direct
        if direct_op.line != target or direct_op.move != move:
            return None
        return (
            (
                replace(get_op, note=f"atomic_gate_clear:{target}"),
                replace(relocate_op, note=f"atomic_gate_relocate:{target}"),
                replace(direct_op, note=f"atomic_inner_commit:{target}"),
            ),
            final_state,
        )

    def nested_gate_dependency(
        self,
        state: State,
        *,
        original_target: str,
        original_outer: str,
        first_gate: tuple[str, ...],
    ) -> tuple[str, str, tuple[str, ...]] | None:
        if not first_gate or state.held[-len(first_gate):] != first_gate:
            raise ValueError("nested_gate_block_not_train_tail")
        nested_target = self.common_assigned_target(first_gate)
        if nested_target not in set(DEPOT_IN) or nested_target == original_target:
            return None
        if not self.depot_move_is_ready(state, nested_target, first_gate):
            return None
        if self.plan_depot_put_positions(state, nested_target, first_gate)[1]:
            return None
        nested_outer = DEPOT_OUT_BY_IN[nested_target]
        second_gate = tuple(self.line_map(state).get(nested_outer, ()))
        if not second_gate or nested_outer == original_outer:
            return None
        return nested_target, nested_outer, second_gate

    def greedy_nested_gate_swap(
        self,
        state: State,
        *,
        original_target: str,
        original_move: tuple[str, ...],
        original_outer: str,
        first_gate: tuple[str, ...],
        dependency: tuple[str, str, tuple[str, ...]],
    ) -> tuple[tuple[Op, ...], State] | None:
        """Clear a proved two-door dependency and restore its inner gate atomically."""
        if not first_gate or state.held[-len(first_gate):] != first_gate:
            raise ValueError("nested_gate_block_not_train_tail")
        nested_target, nested_outer, second_gate = dependency

        get_nested, after_nested_get, reject = self.apply_get(
            state,
            nested_outer,
            second_gate,
        )
        if reject:
            return None
        swap_out, after_swap_out, reject = self.apply_put(
            after_nested_get,
            original_outer,
            second_gate,
        )
        if reject:
            return None
        nested_commit = self.greedy_put_ready_inner(after_swap_out)
        if nested_commit is None:
            return None
        nested_put, after_nested_commit = nested_commit
        if nested_put.line != nested_target or nested_put.move != first_gate:
            return None

        get_swapped, after_swapped_get, reject = self.apply_get(
            after_nested_commit,
            original_outer,
            second_gate,
        )
        if reject:
            return None
        restore_gate, after_restore, reject = self.apply_put(
            after_swapped_get,
            nested_outer,
            second_gate,
        )
        if reject:
            return None
        before_positions = dict(state.positioned_positions)
        restored_positions = dict(after_restore.positioned_positions)
        if any(
            before_positions.get(no) != restored_positions.get(no)
            for no in second_gate
        ):
            return None

        original_commit = self.greedy_put_ready_inner(after_restore)
        if original_commit is None:
            return None
        original_put, final_state = original_commit
        if original_put.line != original_target or original_put.move != original_move:
            return None
        return (
            (
                replace(get_nested, note=f"atomic_nested_gate_clear:{nested_target}"),
                replace(swap_out, note=f"atomic_nested_gate_swap_out:{nested_target}"),
                replace(nested_put, note=f"atomic_nested_inner_commit:{nested_target}"),
                replace(get_swapped, note=f"atomic_nested_gate_retrieve:{nested_target}"),
                replace(restore_gate, note=f"atomic_nested_gate_restore:{nested_target}"),
                replace(original_put, note=f"atomic_inner_commit:{original_target}"),
            ),
            final_state,
        )

    def greedy_put_exposed_terminal_suffix(
        self,
        state: State,
        block: tuple[str, ...],
        *,
        avoid_line: str,
    ) -> tuple[Op, State] | None:
        if not block or state.held[-len(block):] != block:
            raise ValueError("terminal_suffix_block_not_train_tail")
        for start in range(len(block)):
            move = block[start:]
            pending_inner = self.pending_inner_targets(state, exclude=set(move))
            for line in sorted(
                STAGE4_STAGING_LINES,
                key=lambda candidate: (
                    candidate != UNWHEEL and DEPOT_IN_BY_OUT[candidate] in pending_inner,
                    candidate != UNWHEEL,
                    self.line_number(candidate),
                    candidate,
                ),
            ):
                if line == avoid_line:
                    continue
                if not all(self.terminal_line_satisfied(no, line) for no in move):
                    continue
                if not self.line_has_capacity(state, line, move):
                    continue
                op, next_state, reject = self.apply_put(state, line, move)
                if not reject:
                    return op, next_state
        return None

    def apply_pickup_template(
        self,
        pickup_order: tuple[str, ...],
        *,
        phase: int,
        template: str,
        deferred_clear: bool = True,
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

        # This is an optional transactional macro.  It is attempted only
        # after the standard pickup, so a successful clear does not strand the
        # locomotive away from still-uncollected assembly lines.  If any step
        # is infeasible, the immutable input state is retained unchanged.
        if deferred_clear:
            cleared = self.try_apply_deferred_clear_macro(state)
            if cleared is not None:
                clear_ops, cleared_state, reject = cleared
                if reject:
                    return SearchResult(
                        status="partial",
                        template=template,
                        state=None,
                        ops=tuple((*ops, *clear_ops)),
                        cost=(INF_COST, 0, 0, 0),
                        reasons=(f"deferred_clear_{reject}",),
                        expansions=0,
                        elapsed_seconds=0.0,
                    )
                ops.extend(clear_ops)
                state = cleared_state
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

    def try_apply_deferred_clear_macro(
        self,
        state: State,
    ) -> tuple[list[Op], State, str] | None:
        """Clear depot doors needed by stage 3 with the assembled train held.

        Deferred stage-4 cars on different blocking lines are collected into
        one train and put once when they share a staging destination.
        Combining compatible blockers before putting either one saves a hook;
        the caller commits the immutable state only when the whole macro works.
        """
        required_inner = {
            line
            for no, line in self.assigned_line_by_no.items()
            if line in set(DEPOT_IN) and not self.is_stage4_deferred(no)
        }
        blocking_lines = set(required_inner) | {
            DEPOT_OUT_BY_IN[line] for line in required_inner
        }
        groups: dict[str, list[str]] = {}
        line_map = self.line_map(state)
        source_order = (*reversed(DEPOT_OUT), *DEPOT_IN, UNWHEEL)
        for line in source_order:
            if line not in blocking_lines:
                continue
            ordered = tuple(line_map.get(line, ()))
            if (
                line in set(DEPOT_OUT)
                and ordered
                and all(self.is_stage4_deferred(no) for no in ordered)
                and self.pending_outer_suffix_for_line(state, line)
            ):
                # Keep the staged block in place so the immediately exposed
                # compatible outer-target block can be added and later moved
                # with one shared Get.
                continue
            prefix: list[str] = []
            target = ""
            for no in ordered:
                assigned = self.assigned_line_by_no.get(no, "")
                if not self.is_stage4_deferred(no) or assigned not in set(STAGE4_STAGING_LINES):
                    break
                if target and assigned != target:
                    break
                target = assigned
                prefix.append(no)
            if not prefix or target == line:
                continue
            initial_reachable = self.active_prefix(self.initial_active_line_map(), line)
            if initial_reachable[: len(prefix)] != tuple(prefix):
                continue
            groups.setdefault(target, []).append(line)
        if not groups:
            return None

        ops: list[Op] = []
        for target in sorted(groups, key=lambda line: (line != UNWHEEL, self.line_number(line), line)):
            collected: list[str] = []
            for line in groups[target]:
                current = tuple(self.line_map(state).get(line, ()))
                move: list[str] = []
                for no in current:
                    if self.is_stage4_deferred(no) and self.assigned_line_by_no.get(no) == target:
                        move.append(no)
                        continue
                    break
                if not move:
                    continue
                op, next_state, reject = self.apply_get(state, line, tuple(move))
                if reject:
                    return ops, state, reject
                ops.append(replace(op, note=f"collect_deferred_for:{target}"))
                state = next_state
                collected.extend(move)
            if not collected:
                continue
            move = tuple(collected)
            op, next_state, reject = self.apply_put(state, target, move)
            if reject:
                return ops, state, reject
            ops.append(replace(op, note=f"stage_deferred_to:{target}"))
            state = next_state
        return (ops, state, "") if ops else None

    def pending_outer_suffix_for_line(self, state: State, line: str) -> tuple[str, ...]:
        suffix: list[str] = []
        for no in reversed(state.held):
            if self.inner_target_lines(no) or line not in self.outer_target_lines(no):
                break
            suffix.append(no)
        move = tuple(reversed(suffix))
        if not move or not self.line_has_capacity(state, line, move):
            return ()
        return move

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
            if is_final_inner and self.has_exact_position(no):
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

    def has_exact_position(self, no: str) -> bool:
        return len(tuple(self.meta[no].get("_ForcePositions") or ())) == 1

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

    def search(
        self,
        template: str,
        start: State,
        prefix_ops: tuple[Op, ...],
        started: float,
        *,
        hook_limit: int | None = None,
        expansion_limit: int = MAX_EXPANSIONS,
    ) -> SearchResult:
        queue: list[tuple[tuple[int, int, int, int, int, int], tuple[int, int, int, int], int, State]] = []
        start_cost = self.ops_cost(prefix_ops)
        heapq.heappush(queue, (self.priority(start_cost, start), start_cost, 0, start))
        best: dict[State, tuple[int, int, int, int]] = {start: start_cost}
        prev: dict[State, tuple[State, Op]] = {}
        sequence = 1
        expansions = 0
        rejections: Counter[str] = Counter()

        while queue:
            _priority, cost, _seq, state = heapq.heappop(queue)
            if best.get(state) != cost:
                continue
            if hook_limit is not None and cost[0] >= hook_limit:
                continue
            if self.complete(state):
                ops = (*prefix_ops, *self.reconstruct(prev, state))
                return SearchResult(
                    status="complete",
                    template=template,
                    state=state,
                    ops=ops,
                    cost=cost,
                    reasons=(),
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
            if self.deadline_reached():
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=prefix_ops,
                    cost=(INF_COST, 0, 0, 0),
                    reasons=("stage3_global_time_budget_exhausted",),
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
            expansions += 1
            if expansions > expansion_limit:
                return SearchResult(
                    status="partial",
                    template=template,
                    state=None,
                    ops=prefix_ops,
                    cost=(INF_COST, 0, 0, 0),
                    reasons=("stage3_expansion_budget_exhausted",),
                    expansions=expansions,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )

            yielded = False
            for op, next_state, reject in self.neighbors(state, template):
                if reject:
                    rejections[reject] += 1
                    continue
                yielded = True
                next_cost = tuple(cost[index] + self.delta(op)[index] for index in range(4))
                if hook_limit is not None and next_cost[0] >= hook_limit:
                    continue
                if next_cost >= best.get(next_state, (INF_COST, INF_COST, INF_COST, INF_COST)):
                    continue
                best[next_state] = next_cost
                prev[next_state] = (state, op)
                heapq.heappush(queue, (self.priority(next_cost, next_state), next_cost, sequence, next_state))
                sequence += 1
            if not yielded:
                rejections["no_neighbor_from_state"] += 1

        reasons = tuple(f"{key}:{value}" for key, value in rejections.most_common(12))
        return SearchResult(
            status="partial",
            template=template,
            state=None,
            ops=prefix_ops,
            cost=(INF_COST, 0, 0, 0),
            reasons=reasons or ("stage3_no_solution",),
            expansions=expansions,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def priority(self, cost: tuple[int, int, int, int], state: State) -> tuple[int, int, int, int, int, int]:
        return (
            cost[0] + self.remaining_put_lower_bound(state),
            self.unsatisfied_active_count(state),
            -len(state.held),
            cost[1],
            cost[2],
            cost[3],
        )

    def remaining_put_lower_bound(self, state: State) -> int:
        line_map = self.line_map(state)
        if state.held:
            assigned_targets = {self.assigned_line_by_no.get(no, "") for no in state.held}
            return max(1, len({line for line in assigned_targets if line}))
        if state.phase == 0 and line_map.get(TEMPLATE_A_SECOND_LINE):
            return 1
        buffer_debt = sum(
            1
            for line in DEPOT_OUT
            if any(not self.terminal_line_satisfied(no, line) for no in line_map.get(line, ()))
        )
        pending_lines = {
            line
            for line, nos in line_map.items()
            if nos and any(not self.terminal_line_satisfied(no, line) for no in nos)
        }
        return len(pending_lines) + buffer_debt

    def unsatisfied_active_count(self, state: State) -> int:
        line_map = self.line_map(state)
        count = len(state.held)
        for line, nos in line_map.items():
            for no in nos:
                if not self.terminal_line_satisfied(no, line):
                    count += 1
        return count

    def reconstruct(self, prev: dict[State, tuple[State, Op]], state: State) -> tuple[Op, ...]:
        ops: list[Op] = []
        while state in prev:
            prior, op = prev[state]
            ops.append(op)
            state = prior
        ops.reverse()
        return tuple(ops)

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
                return self.line_is_empty(state, line) and self.line_has_capacity(state, line, move)
            if target in set(DEPOT_OUT):
                if line in set(DEPOT_OUT) and all(line in self.outer_target_lines(no) for no in move):
                    return True
                if line == DEPOT_IN_BY_OUT[target]:
                    return True
                if line in set(STAGE4_STAGING_LINES):
                    return self.line_is_empty(state, line) and self.line_has_capacity(state, line, move)
            return False
        if not self.line_is_empty(state, line):
            return False
        if line in set(DEPOT_IN) and DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
            return False
        return self.length(move) <= float(rv.TRACK_LEN.get(line) or 0.0) + rv.TOL

    def line_has_stage4_deferred(self, state: State, line: str) -> bool:
        return any(self.is_stage4_deferred(no) for no in self.line_map(state).get(line, ()))

    def line_is_empty(self, state: State, line: str) -> bool:
        return (
            not self.line_map(state).get(line)
            and not any(car["Line"] == line for car in self.fixed_cars)
        )

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
        if not any(self.has_exact_position(no) for _position, no in remaining):
            if any(self.repair_process(self.meta[no]).startswith("厂") for no in move):
                return True
            return not any(
                self.repair_process(self.meta[no]).startswith("厂")
                for _position, no in remaining
                if no not in set(move)
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
        if self.inner_target_lines(no):
            return line in self.inner_target_lines(no)
        if self.outer_target_lines(no):
            return line in self.outer_target_lines(no)
        return False

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

    def choose_result(self, results: list[SearchResult]) -> SearchResult:
        complete = [item for item in results if item.status == "complete"]
        if complete:
            return min(
                complete,
                key=lambda item: (
                    item.cost,
                    0 if item.layout == "cohesive" else 1,
                    0 if item.deferred_clear else 1,
                    0 if item.terminal_merge else 1,
                    0 if item.inner_clear_policy == "eager" else 1,
                    0 if item.template == "B" else 1,
                ),
            )
        return min(
            results,
            key=lambda item: (
                len(item.reasons),
                -item.expansions,
                item.template,
                item.layout,
                not item.deferred_clear,
                not item.terminal_merge,
                item.inner_clear_policy,
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
        portfolio_lower_bound = min(
            evaluated_bounds,
            default=None,
        )
        portfolio_gap = (
            executable_hooks - portfolio_lower_bound
            if validated_complete and portfolio_lower_bound is not None
            else None
        )
        portfolio_evaluation_complete = (
            not self.portfolio_evaluation_incomplete
            and all(item.strategy_evaluated for item in results)
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
                "strategy_evaluated": item.strategy_evaluated,
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
            "partial_response_safe": not any(
                violation.kind in {"schema", "physical", "state"}
                for violation in replay_bad
            ),
            "operation_lower_bound": lower_bound,
            "operation_lower_bound_components": lower_bound_components,
            "operation_lower_bound_scope": chosen.lower_bound_scope,
            "evaluated_strategy_portfolio_bound_scope": "evaluated_template_layout_clear_merge_policy_modes",
            "operation_lower_bound_gap": optimality_gap,
            "evaluated_strategy_portfolio_lower_bound": portfolio_lower_bound,
            "evaluated_strategy_portfolio_gap": portfolio_gap,
            "portfolio_evaluation_complete": portfolio_evaluation_complete,
            "lower_bound_validation_violations": lower_bound_validation_violations,
            "optimization_attempted": self.optimization_attempted,
            "optimization_expansions": self.optimization_expansions,
            "optimization_budget_exhausted": self.optimization_budget_exhausted,
            "optimality_status": (
                "invalid_lower_bound_certificate"
                if validated_complete
                and (optimality_gap is not None and optimality_gap < 0
                     or portfolio_gap is not None and portfolio_gap < 0
                     or lower_bound_validation_violations)
                else "portfolio_evaluation_incomplete"
                if validated_complete and not portfolio_evaluation_complete
                else "portfolio_lower_bound_reached"
                if validated_complete and portfolio_gap == 0
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


def request_paths(input_path: Path, case: str | None) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    paths = sorted(input_path.glob("*.json"))
    if case:
        target = case.upper()
        paths = [path for path in paths if case_id_from_path(path) == target]
    return paths


def unavailable_summary(case_id: str, reason: str, *, status: str = "partial") -> dict[str, Any]:
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
        "partial_response_safe": True,
        "operation_lower_bound": None,
        "operation_lower_bound_components": {},
        "operation_lower_bound_scope": "not_applicable",
        "operation_lower_bound_gap": None,
        "evaluated_strategy_portfolio_bound_scope": "not_applicable",
        "evaluated_strategy_portfolio_lower_bound": None,
        "evaluated_strategy_portfolio_gap": None,
        "portfolio_evaluation_complete": False,
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


def solve_one(path: Path, stage2_out: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    stage2_summary_path = stage2_out / f"{case_id}_summary.json"
    combined_path = stage2_out / f"{case_id}_combined_response.json"
    if not combined_path.exists():
        summary = unavailable_summary(case_id, "stage2_combined_response_missing")
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    if stage2_summary_path.exists():
        stage2_summary = read_json(stage2_summary_path)
        if stage2_summary.get("status") != "complete" and not args.include_stage2_partial:
            summary = unavailable_summary(
                case_id,
                f"stage2_not_complete:{stage2_summary.get('status')}",
            )
            write_json(out_dir / f"{case_id}_summary.json", summary)
            return summary

    solver = Stage3Solver(
        case_id,
        read_json(path),
        read_json(combined_path),
        time_budget_seconds=args.time_budget_seconds,
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
        "partial": total - complete,
        "avg_operations_complete": round(sum(ops) / len(ops), 3) if ops else 0,
        "max_operations_complete": max(ops) if ops else 0,
        "templates_complete": dict(sorted(templates.items())),
        "partial_reasons": dict(reasons.most_common()),
        "summaries": summaries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 3 simple exact-ish depot inbound solver.")
    parser.add_argument("input", type=Path, help="request JSON file or directory")
    parser.add_argument("--stage2-out", type=Path, default=Path("artifacts/stage2_simple_final"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/stage3_simple"))
    parser.add_argument("--case", help="case id filter, e.g. 0226Z")
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--include-stage2-partial", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in request_paths(args.input, args.case):
        case_id = case_id_from_path(path)
        print(case_id, flush=True)
        summaries.append(solve_one(path, args.stage2_out, args.out, args))
    agg = aggregate(summaries)
    write_json(args.out / "aggregate_summary.json", agg)
    print(
        "done "
        f"cases={agg['cases']} complete={agg['complete']} partial={agg['partial']} "
        f"avg_ops={agg['avg_operations_complete']} max_ops={agg['max_operations_complete']}",
        flush=True,
    )
    return 0 if all(item.get("status") == "complete" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
