#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
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


CAR_LENGTH_DEFAULT_M = 14.3
INF_COST = 10**9
DEPOT_IN = tuple(f"修{i}库内" for i in range(1, 5))
DEPOT_OUT = tuple(f"修{i}库外" for i in range(1, 5))
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
STAGE3_REHOOK_LOCO = PREPICKUP_OUTER_SOURCE


@dataclass(frozen=True)
class State:
    lines: tuple[tuple[str, tuple[str, ...]], ...]
    held: tuple[str, ...]
    loco: tuple[str, ...]
    phase: int


@dataclass(frozen=True)
class Op:
    action: str
    line: str
    move: tuple[str, ...]
    path: tuple[str, ...]
    train_after: tuple[str, ...]
    note: str = ""


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
    return sorted(line for line in loco if line)[0] if any(loco) else rv.WEIGH


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
        replayed, _replay_bad = rv.replay(request, stage2_combined_response)
        self.initial_cars = [normalize_car(car) for car in rv.final_cars(stage2_combined_response, replayed)]
        self.stage2_final_loco = final_loco_after_response(request, stage2_combined_response)
        # Stage 2 finishes by putting the outbound consist on 存4线.  The stage
        # contract says the locomotive re-couples at 存4北 before depot inbound
        # work starts; using the post-Put standing point 存4南 would make the
        # north-side pickup templates unreachable behind occupied 存4/存3.
        self.initial_loco = STAGE3_REHOOK_LOCO
        self.meta = {rv.car_no(car): dict(car) for car in self.initial_cars}
        self.caps = physical.terminal_capacity_by_line(request)
        self.graph = physical.TrackGraph()
        self.time_budget_seconds = time_budget_seconds
        self.started_at = time.monotonic()
        self.deadline = self.started_at + time_budget_seconds
        self.route_cache: dict[tuple[Any, ...], tuple[tuple[str, ...], str]] = {}
        self.cars_cache: dict[State, tuple[dict[str, Any], ...]] = {}
        self.line_map_cache: dict[State, dict[str, tuple[str, ...]]] = {}
        self.duplicate_nos = self.find_duplicate_nos()
        self.depot_target_nos = self.find_depot_target_nos()
        self.active_nos = self.find_active_nos()
        self.fixed_cars = [dict(car) for car in self.initial_cars if rv.car_no(car) not in self.active_nos]
        self.fixed_by_no = {rv.car_no(car): dict(car) for car in self.fixed_cars}
        self.fixed_depot_positions = self.build_fixed_depot_positions()
        self.fixed_outer_lines = {
            car["Line"] for car in self.fixed_cars if car["Line"] in set(DEPOT_OUT)
        }
        self.assigned_slot_by_no: dict[str, tuple[str, int]] = {}
        self.assignment_reasons: tuple[str, ...] = ()
        self.assigned_line_by_no = self.build_assigned_line_by_no("B")

    def find_duplicate_nos(self) -> tuple[str, ...]:
        counts = Counter(rv.car_no(car) for car in self.initial_cars)
        return tuple(sorted(no for no, count in counts.items() if not no or count > 1))

    def find_depot_target_nos(self) -> set[str]:
        return {
            rv.car_no(car)
            for car in self.initial_cars
            if set(car.get("TargetLines") or []) & set(DEPOT_TARGETS)
        }

    def find_active_nos(self) -> set[str]:
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
            elif car["Line"] in set(STAGE4_DEFER_LINES) and targets & STAGE4_DEFER_TARGETS:
                active.add(rv.car_no(car))
        return active

    def build_fixed_depot_positions(self) -> dict[str, dict[int, str]]:
        out: dict[str, dict[int, str]] = {line: {} for line in DEPOT_IN}
        for car in self.fixed_cars:
            line = car["Line"]
            if line not in out:
                continue
            position = int(car.get("Position") or 0)
            if position > 0:
                out[line][position] = rv.car_no(car)
        return out

    def build_assigned_line_by_no(self, template: str) -> dict[str, str]:
        self.assignment_reasons = ()
        self.assigned_slot_by_no = {}
        exposure = self.template_exposure_order(template)
        exposure_time = {no: index for index, no in enumerate(exposure)}
        inner_nos = sorted(no for no in self.active_nos if self.inner_target_lines(no))
        outer_nos = sorted(no for no in self.active_nos if not self.inner_target_lines(no) and self.outer_target_lines(no))
        deferred_nos = sorted(no for no in self.active_nos if self.is_stage4_deferred(no))
        slots: list[tuple[str, int]] = []
        for line in DEPOT_IN:
            if DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
                continue
            fixed_positions = sorted(self.fixed_depot_positions.get(line, {}))
            capacity = self.caps.get(line, 5)
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
                if self.slot_allowed_for_car(car, line, position, self.caps.get(line, 5)):
                    allowed.append((line, position))
            candidates[no] = allowed

        def base_slot_cost(no: str, slot: tuple[str, int]) -> int:
            car = self.meta[no]
            line, position = slot
            line_no = self.line_number(line)
            cost = 0
            if self.repair_process(car).startswith("厂"):
                # Factory repair needs the deepest available positions. 修4 is
                # preferred as the high-capacity long-car line, with 修2/修1
                # next so scarce 修3 capacity remains available for long cars.
                cost += 0 if position == 5 else 1
                cost += {4: 0, 2: 1, 1: 2, 3: 3}.get(line_no, 4)
            elif float(car.get("Length") or 14.3) >= 17.6:
                cost += {3: 0, 4: 1}.get(line_no, 5)
                cost += position
            else:
                cost += 0 if line_no in {1, 2} else 2
                cost += position // 4
                if template == "A" and car.get("Line") == TEMPLATE_A_SECOND_LINE and line_no in {3, 4}:
                    # Template A's second trip naturally arrives late and is
                    # best used for shallow short cars on 修1/修2, leaving 修3/4
                    # deep positions for long/factory cars from the first trip.
                    cost += 20
            return cost

        ordered_nos = sorted(
            inner_nos,
            key=lambda no: (
                len(candidates.get(no, ())),
                not self.repair_process(self.meta[no]).startswith("厂"),
                float(self.meta[no].get("Length") or 14.3) < 17.6,
                exposure_time.get(no, 10**6),
                no,
            ),
        )
        owner: dict[tuple[str, int], str] = {}

        def assign(no: str, seen: set[tuple[str, int]]) -> bool:
            for slot in sorted(candidates.get(no, ()), key=lambda item: base_slot_cost(no, item)):
                if slot in seen:
                    continue
                seen.add(slot)
                other = owner.get(slot)
                if other is None or assign(other, seen):
                    owner[slot] = no
                    return True
            return False

        for no in ordered_nos:
            if not candidates.get(no):
                continue
            assign(no, set())
        assigned_inner = {no: slot for slot, no in owner.items() if no in set(inner_nos)}
        missing_inner = tuple(sorted(no for no in inner_nos if no not in assigned_inner))
        assigned_outer, missing_outer = self.assign_outer_targets(outer_nos, set(line for line, _pos in assigned_inner.values()))
        assigned_lines = {no: line for no, (line, _position) in assigned_inner.items()}
        assigned_lines.update(assigned_outer)
        assigned_deferred, missing_deferred = self.assign_deferred_stage4_targets(deferred_nos)
        assigned_lines.update(assigned_deferred)
        if missing_inner or missing_outer:
            reasons: list[str] = []
            if missing_inner:
                reasons.append(f"inner_assignment_incomplete:{template}:{','.join(missing_inner)}")
            if missing_outer:
                reasons.append(f"outer_assignment_incomplete:{template}:{','.join(missing_outer)}")
            if missing_deferred:
                reasons.append(f"stage4_defer_staging_incomplete:{template}:{','.join(missing_deferred)}")
            self.assignment_reasons = tuple(reasons)
            return assigned_lines
        if missing_deferred:
            self.assignment_reasons = (f"stage4_defer_staging_incomplete:{template}:{','.join(missing_deferred)}",)
            return assigned_lines

        self.assigned_slot_by_no = dict(assigned_inner)
        return assigned_lines

    def is_stage4_deferred(self, no: str) -> bool:
        car = self.meta.get(no, {})
        targets = set(car.get("TargetLines") or [])
        return bool(targets & STAGE4_DEFER_TARGETS) and not (targets & set(DEPOT_TARGETS))

    def deferred_stage4_preferred_line(self, no: str) -> str:
        car = self.meta[no]
        line = car["Line"]
        if line in set(STAGE4_STAGING_LINES):
            return line
        return UNWHEEL

    def deferred_stage4_target(self, no: str) -> str:
        targets = set(self.meta[no].get("TargetLines") or []) & STAGE4_DEFER_TARGETS
        if "存4线" in targets:
            return "存4线"
        if "油漆线" in targets:
            return "油漆线"
        return sorted(targets)[0] if targets else ""

    def assign_deferred_stage4_targets(self, deferred_nos: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
        if not deferred_nos:
            return {}, ()
        remaining = {
            line: float(rv.TRACK_LEN.get(line) or 0.0)
            - sum(float(car.get("Length") or CAR_LENGTH_DEFAULT_M) for car in self.fixed_cars if car["Line"] == line)
            for line in STAGE4_STAGING_LINES
        }
        assigned: dict[str, str] = {}
        for target in ("存4线", "油漆线"):
            group = [no for no in self.deferred_stage4_order(deferred_nos) if self.deferred_stage4_target(no) == target]
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

    def assign_outer_targets(self, outer_nos: list[str], used_inner_lines: set[str]) -> tuple[dict[str, str], tuple[str, ...]]:
        if not outer_nos:
            return {}, ()
        fixed_load = {
            line: sum(float(car.get("Length") or CAR_LENGTH_DEFAULT_M) for car in self.fixed_cars if car["Line"] == line)
            for line in DEPOT_OUT
        }
        remaining = {
            line: float(rv.TRACK_LEN.get(line) or 0.0) - fixed_load.get(line, 0.0)
            for line in DEPOT_OUT
        }
        candidates = {
            no: tuple(sorted(self.outer_target_lines(no), key=lambda line: self.outer_line_cost(line, used_inner_lines)))
            for no in outer_nos
        }
        ordered = sorted(
            outer_nos,
            key=lambda no: (len(candidates.get(no, ())), -self.length((no,)), no),
        )
        best: tuple[int, dict[str, str]] | None = None
        current: dict[str, str] = {}

        def rec(index: int, cost: int) -> None:
            nonlocal best
            if best is not None and cost >= best[0]:
                return
            if index == len(ordered):
                best = (cost, dict(current))
                return
            no = ordered[index]
            car_len = self.length((no,))
            for line in candidates.get(no, ()):
                if remaining.get(line, -1.0) + rv.TOL < car_len:
                    continue
                remaining[line] -= car_len
                current[no] = line
                rec(index + 1, cost + self.outer_line_cost(line, used_inner_lines))
                current.pop(no, None)
                remaining[line] += car_len

        rec(0, 0)
        if best is None:
            return {}, tuple(sorted(outer_nos))
        assigned = best[1]
        missing = tuple(sorted(no for no in outer_nos if no not in assigned))
        return assigned, missing

    def outer_line_cost(self, line: str, used_inner_lines: set[str]) -> int:
        inner = DEPOT_IN_BY_OUT[line]
        return (10 if inner in used_inner_lines else 0) + self.line_number(line)

    def template_exposure_order(self, template: str) -> tuple[str, ...]:
        line_map = self.initial_active_line_map()
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
            )
            return self.result(empty, [empty])

        first = self.solve_template("B")
        results = [first]
        if not (first.status == "complete" and first.cost[0] <= self.template_operation_lower_bound("B")):
            results.append(self.solve_template("A"))
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
        return State(lines=self.pack_lines(packed), held=(), loco=(self.initial_loco,), phase=1)

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
            if not depot_targets and not self.is_stage4_deferred(no):
                reasons.append(f"active_without_depot_target:{no}")
            if car.get("IsWeigh") and not car.get("_Weighed"):
                reasons.append(f"active_unweighed:{no}")
        return reasons

    def solve_template(self, template: str) -> SearchResult:
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
            )
        self.assigned_line_by_no = self.build_assigned_line_by_no(template)
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
            )
        if template == "B":
            pickup_order = TEMPLATE_B_ORDER
            phase = 1
        elif template == "A":
            pickup_order = TEMPLATE_A_FIRST_ORDER
            phase = 0
        else:
            raise ValueError(f"unknown_template:{template}")

        built = self.apply_pickup_template(pickup_order, phase=phase, template=template)
        if isinstance(built, SearchResult):
            return built
        state, pickup_ops = built
        if self.complete(state):
            return SearchResult(
                status="complete",
                template=template,
                state=state,
                ops=tuple(pickup_ops),
                cost=self.ops_cost(pickup_ops),
                reasons=(),
                expansions=0,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        greedy = self.greedy_finish(template, state, list(pickup_ops), started)
        if greedy.status == "complete":
            return greedy
        searched = self.search(template, state, tuple(pickup_ops), started)
        return searched

    def template_operation_lower_bound(self, template: str) -> int:
        line_map = self.initial_active_line_map()
        get_lines: list[str] = []
        if line_map.get(PREPICKUP_OUTER_SOURCE):
            get_lines.append(PREPICKUP_OUTER_SOURCE)
        get_lines.extend(line for line in DEPOT_IN if line_map.get(line))
        if template == "A":
            get_lines.extend(line for line in TEMPLATE_A_FIRST_ORDER if line_map.get(line))
            if line_map.get(TEMPLATE_A_SECOND_LINE):
                get_lines.append(TEMPLATE_A_SECOND_LINE)
        else:
            get_lines.extend(line for line in TEMPLATE_B_ORDER if line_map.get(line))
        target_lines = {
            line for line in self.assigned_line_by_no.values() if line in set(DEPOT_TARGETS)
        }
        return len(get_lines) + len(target_lines)

    def deadline_reached(self) -> bool:
        return time.monotonic() >= self.deadline

    def greedy_finish(
        self,
        template: str,
        state: State,
        ops: list[Op],
        started: float,
    ) -> SearchResult:
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
                    reasons=("greedy_upper_bound",),
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
                if self.line_map(state).get(TEMPLATE_A_SECOND_LINE):
                    break
                state = State(lines=state.lines, held=state.held, loco=state.loco, phase=1)
                continue

            if state.held:
                applied = self.greedy_get_blocking_inner(state)
                if applied:
                    op, state = applied
                    ops.append(op)
                    continue
                last_op = ops[-1] if ops else None
                applied = self.greedy_put(state, last_op=last_op)
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
        wash = tuple(line_map.get(TEMPLATE_A_SECOND_LINE, ()))
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

    def greedy_preferred_put_lines(
        self,
        state: State,
        target: str,
        move: tuple[str, ...],
        pending_before: tuple[str, ...],
        *,
        avoid_outer_line: str = "",
    ) -> tuple[str, ...]:
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
            reusable_outer_lines = tuple(
                line
                for line in DEPOT_OUT
                if line != target
                and (
                    not line_map.get(line)
                    or self.line_can_stay_terminal(line, tuple(line_map.get(line, ())))
                )
            )
            nonblocking_alternates = tuple(
                line
                for line in reusable_outer_lines
                if DEPOT_IN_BY_OUT[line] not in pending_inner_targets
            )
            blocking_alternates = tuple(
                line
                for line in reusable_outer_lines
                if DEPOT_IN_BY_OUT[line] in pending_inner_targets
            )
            blocking_inner = DEPOT_IN_BY_OUT[target]
            target_first_allowed = target != avoid_outer_line and not (
                blocking_inner in pending_inner_targets and bool(line_map.get(blocking_inner))
            )
            target_first = (target,) if target_first_allowed else ()
            target_later = (target,) if target != avoid_outer_line and not target_first_allowed else ()
            if any(self.assigned_line_by_no.get(no) == blocking_inner for no in pending_before):
                return (*target_first, *nonblocking_alternates, *blocking_alternates, *target_later, blocking_inner)
            return (*target_first, *nonblocking_alternates, *blocking_alternates, *target_later, blocking_inner)
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
        immediate_inner = ""
        for no in reversed(pending_before):
            if self.is_stage4_deferred(no):
                continue
            candidate = self.assigned_line_by_no.get(no, "")
            if candidate in set(DEPOT_IN):
                immediate_inner = candidate
                break
        immediate_outer = DEPOT_OUT_BY_IN.get(immediate_inner, "")
        alternate_buffers = tuple(
            sorted(
                (
                    line
                    for line in DEPOT_OUT
                    if line != outer and not self.line_map(state).get(line)
                ),
                key=lambda line: (line == immediate_outer, self.line_number(line), line),
            )
        )
        pending_same_target = [no for no in pending_before if self.assigned_line_by_no.get(no) == target]
        pending_factory = any(self.repair_process(self.meta[no]).startswith("厂") for no in pending_same_target)
        move_factory = any(self.repair_process(self.meta[no]).startswith("厂") for no in move)
        if pending_factory and not move_factory:
            immediate_deep = (
                bool(pending_before)
                and self.assigned_line_by_no.get(pending_before[-1]) == target
                and self.repair_process(self.meta[pending_before[-1]]).startswith("厂")
            )
            if immediate_deep:
                return (*alternate_buffers, outer, target)
            unrelated_pending = any(self.assigned_line_by_no.get(no) != target for no in pending_before)
            if unrelated_pending:
                return (outer, *alternate_buffers, target)
            return (*alternate_buffers, outer, target)
        # Once the deep factory block has been placed, shallow blocks waiting in
        # the outside line should be pulled back and pushed into the inner line.
        return (target, outer)

    def common_assigned_target(self, nos: tuple[str, ...]) -> str:
        if not nos:
            return ""
        targets = {self.assigned_line_by_no.get(no, "") for no in nos}
        if len(targets) != 1:
            return ""
        target = next(iter(targets))
        return target if target in set(DEPOT_TARGETS) | set(STAGE4_DEFER_LINES) else ""

    def greedy_get_outer(self, state: State) -> tuple[Op, State] | None:
        line_map = self.line_map(state)
        for line in (*DEPOT_OUT, UNWHEEL, *DEPOT_IN):
            nos = tuple(line_map.get(line, ()))
            if not nos:
                continue
            if self.line_can_stay_terminal(line, nos) and not self.outer_blocks_held_inner(state, line):
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

    def apply_pickup_template(
        self,
        pickup_order: tuple[str, ...],
        *,
        phase: int,
        template: str,
    ) -> tuple[State, list[Op]] | SearchResult:
        line_map = self.initial_active_line_map()
        state = State(lines=self.pack_lines(line_map), held=(), loco=(self.initial_loco,), phase=phase)
        ops: list[Op] = []
        pre_move = tuple(line_map.get(PREPICKUP_OUTER_SOURCE, ()))
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
            ops.append(op)
            state = next_state
            line_map = self.line_map(state)

        for line in pickup_order:
            move = tuple(line_map.get(line, ()))
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
            ops.append(op)
            state = next_state
            line_map = self.line_map(state)

        if template == "B":
            remaining = sorted(
                no
                for line, nos in self.line_map(state).items()
                if line in set(STAGE3_SOURCE_LINES)
                for no in nos
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
            if expansions > MAX_EXPANSIONS:
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
                ), ""

        if state.held:
            blocking_gets = list(self.blocking_inner_get_prefixes(state))
            if blocking_gets:
                for line, move in blocking_gets:
                    op, next_state, reject = self.apply_get(state, line, move)
                    yield op, next_state, reject
                return
            put_results: list[tuple[Op, State, str]] = []
            has_usable_put = False
            for line, move in self.put_suffixes(state):
                op, next_state, reject = self.apply_put(state, line, move)
                put_results.append((op, next_state, reject))
                if not reject:
                    has_usable_put = True
            for item in put_results:
                yield item
            if has_usable_put:
                return

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
        candidate_lines = (*DEPOT_IN, *DEPOT_OUT, UNWHEEL)
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
        if not move_is_deferred and self.line_has_stage4_deferred(state, line):
            return False
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
                    proposed = (*move, *self.line_map(state).get(line, ()))
                    return self.partial_depot_line_possible(line, proposed)
                return line in set(STAGE4_STAGING_LINES)
            needs_buffer = self.needs_buffer_before_pending_deep(state, target, move)
            if line == target:
                if line in set(DEPOT_IN):
                    proposed = (*move, *self.line_map(state).get(line, ()))
                    if not self.partial_depot_line_possible(line, proposed):
                        return False
                return not needs_buffer
            if target in set(DEPOT_IN) and line == DEPOT_OUT_BY_IN[target]:
                if any(self.repair_process(self.meta[no]).startswith("厂") for no in move):
                    proposed = (*move, *self.line_map(state).get(target, ()))
                    if not self.partial_depot_line_possible(target, proposed):
                        return False
                return True
            if target in set(DEPOT_OUT):
                if line in set(DEPOT_OUT) and all(line in self.outer_target_lines(no) for no in move):
                    return True
                if line == DEPOT_IN_BY_OUT[target]:
                    return True
            if needs_buffer and line in set(DEPOT_OUT) and line != DEPOT_OUT_BY_IN[target]:
                line_map = self.line_map(state)
                return not line_map.get(line) and self.length(move) <= float(rv.TRACK_LEN.get(line) or 0.0) + rv.TOL
            return False
        line_map = self.line_map(state)
        if line_map.get(line):
            return False
        if line in set(DEPOT_IN) and DEPOT_OUT_BY_IN[line] in self.fixed_outer_lines:
            return False
        return len(move) <= 3 and self.length(move) <= float(rv.TRACK_LEN.get(line) or 0.0) + rv.TOL

    def line_has_stage4_deferred(self, state: State, line: str) -> bool:
        return any(self.is_stage4_deferred(no) for no in self.line_map(state).get(line, ()))

    def needs_buffer_before_pending_deep(self, state: State, target: str, move: tuple[str, ...]) -> bool:
        if target not in set(DEPOT_IN):
            return False
        held = state.held
        if not move or held[-len(move):] != move:
            return False
        pending_before = held[: len(held) - len(move)]
        if any(self.repair_process(self.meta[no]).startswith("厂") for no in move):
            return False
        return any(
            self.assigned_line_by_no.get(no) == target
            and self.repair_process(self.meta[no]).startswith("厂")
            for no in pending_before
        )

    def get_prefixes(self, state: State) -> Iterable[tuple[str, tuple[str, ...]]]:
        line_map = self.line_map(state)
        # Outside buffers are preferred, but inner depot get-back is part of
        # the stage-3 reordering contract.  It is kept prefix-only; process
        # legality is length/route based, while final slot rules are terminal.
        for line in (*DEPOT_OUT, UNWHEEL, *DEPOT_IN):
            ordered = line_map.get(line, ())
            if not ordered:
                continue
            if self.line_can_stay_terminal(line, ordered) and not self.outer_blocks_held_inner(state, line):
                continue
            for cut in range(len(ordered), 0, -1):
                yield line, ordered[:cut]

    def line_can_stay_terminal(self, line: str, ordered: tuple[str, ...]) -> bool:
        if line in set(DEPOT_OUT):
            return all(self.terminal_line_satisfied(no, line) for no in ordered)
        if line == UNWHEEL:
            return all(self.terminal_line_satisfied(no, line) for no in ordered)
        if line in set(DEPOT_IN):
            return all(self.terminal_line_satisfied(no, line) for no in ordered) and self.partial_depot_line_possible(line, ordered)
        return False

    def outer_blocks_held_inner(self, state: State, line: str) -> bool:
        if line not in set(DEPOT_OUT):
            return False
        blocked_inner = DEPOT_IN_BY_OUT[line]
        return any(self.assigned_line_by_no.get(no) == blocked_inner for no in state.held)

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
        phase = state.phase if next_phase is None else next_phase
        next_state = State(
            lines=self.pack_lines(next_lines),
            held=held_after,
            loco=(line,),
            phase=phase,
        )
        return Op("Get", line, move, route, held_after), next_state, ""

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
        route, reject = self.route(state, "Put", line, move)
        if reject:
            return Op("Put", line, move, (), state.held), state, reject
        line_map = dict(self.line_map(state))
        line_map[line] = (*move, *line_map.get(line, ()))
        move_set = set(move)
        held_after = tuple(no for no in state.held if no not in move_set)
        post_put_loco = tuple(sorted(rv.put_loco_positions(route, line)))
        if not post_put_loco:
            post_put_loco = (line,)
        next_state = State(
            lines=self.pack_lines(line_map),
            held=held_after,
            loco=(post_put_loco[0],),
            phase=state.phase,
        )
        return Op("Put", line, move, route, held_after), next_state, ""

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
            loads[line] += float(car.get("Length") or CAR_LENGTH_DEFAULT_M)
        return tuple(sorted((line, round(load, 3)) for line, load in loads.items()))

    def complete(self, state: State) -> bool:
        if state.held:
            return False
        if state.phase == 0:
            return False
        line_map = self.line_map(state)
        for line in ASSEMBLY_LINES:
            if line_map.get(line):
                return False
        ok, _reasons, _positions = self.terminal_depot_ok(state)
        return ok

    def terminal_depot_ok(self, state: State) -> tuple[bool, list[str], dict[str, tuple[str, int]]]:
        line_map = self.line_map(state)
        positions: dict[str, tuple[str, int]] = {}
        reasons: list[str] = []
        for no in self.active_nos:
            found = [(line, nos.index(no) + 1) for line, nos in line_map.items() if no in nos]
            if not found:
                reasons.append(f"active_missing:{no}")
                continue
            line, _index = found[0]
            if not self.terminal_line_satisfied(no, line):
                reasons.append(f"active_terminal_line_violation:{no}:{line}")
            elif line in set(DEPOT_OUT) or line == UNWHEEL:
                positions[no] = (line, _index)
                car = self.meta[no]
                forced = tuple(int(value) for value in car.get("_ForcePositions") or () if int(value) > 0)
                if forced and _index not in forced:
                    reasons.append(f"outer_force_position_violation:{no}:{line}:{_index}")
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    reasons.append(f"depot_weigh_pending:{no}")

        for line in DEPOT_IN:
            active = tuple(line_map.get(line, ()))
            deferred_prefix: list[str] = []
            depot_active: list[str] = []
            seen_depot = False
            for no in active:
                if self.is_stage4_deferred(no):
                    if seen_depot:
                        reasons.append(f"deferred_after_depot_car:{no}:{line}")
                    deferred_prefix.append(no)
                else:
                    seen_depot = True
                    depot_active.append(no)
            fixed_positions = sorted(self.fixed_depot_positions.get(line, {}))
            capacity = self.caps.get(line, 5)
            usable_limit = (min(fixed_positions) - 1) if fixed_positions else capacity
            if len(active) > usable_limit:
                reasons.append(f"depot_prefix_capacity:{line}:{len(active)}>{usable_limit}")
                continue
            start_position = usable_limit - len(active) + 1
            for offset, no in enumerate(active):
                position = start_position + offset
                positions[no] = (line, position)
                car = self.meta[no]
                if self.is_stage4_deferred(no):
                    if not self.terminal_line_satisfied(no, line):
                        reasons.append(f"deferred_stage4_line_violation:{no}:{line}")
                    if car.get("IsWeigh") and not car.get("_Weighed"):
                        reasons.append(f"depot_weigh_pending:{no}")
                    continue
                if not self.inner_target_lines(no) or line not in self.inner_target_lines(no):
                    reasons.append(f"depot_target_line_violation:{no}:{line}")
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    reasons.append(f"depot_weigh_pending:{no}")
                if not self.slot_allowed_for_car(car, line, position, capacity):
                    reasons.append(f"depot_slot_rule_violation:{no}:{line}:{position}")

            factory_positions: list[int] = []
            for no in depot_active:
                if self.repair_process(self.meta[no]).startswith("厂"):
                    factory_positions.append(positions[no][1])
            for position, no in self.fixed_depot_positions.get(line, {}).items():
                fixed = self.fixed_by_no.get(no)
                if fixed and self.repair_process(fixed).startswith("厂"):
                    factory_positions.append(position)
            if factory_positions:
                factory_min = min(factory_positions)
                for no in depot_active:
                    position = positions[no][1]
                    if self.repair_process(self.meta[no]).startswith("段") and position > factory_min:
                        reasons.append(f"depot_section_after_factory:{no}:{line}:{position}>{factory_min}")
        return not reasons, reasons, positions

    def partial_depot_line_possible(self, line: str, nos: tuple[str, ...]) -> bool:
        fixed_positions = sorted(self.fixed_depot_positions.get(line, {}))
        capacity = self.caps.get(line, 5)
        usable_limit = (min(fixed_positions) - 1) if fixed_positions else capacity
        if len(nos) > usable_limit:
            return False
        start_position = usable_limit - len(nos) + 1
        positions = {no: start_position + offset for offset, no in enumerate(nos)}
        seen_depot = False
        for no, position in positions.items():
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
            for no, position in positions.items()
            if self.repair_process(self.meta[no]).startswith("厂")
        ]
        for position, fixed_no in self.fixed_depot_positions.get(line, {}).items():
            fixed = self.fixed_by_no.get(fixed_no)
            if fixed and self.repair_process(fixed).startswith("厂"):
                factory_positions.append(position)
        if factory_positions:
            factory_min = min(factory_positions)
            for no, position in positions.items():
                if self.repair_process(self.meta[no]).startswith("段") and position > factory_min:
                    return False
        return True

    def terminal_line_satisfied(self, no: str, line: str) -> bool:
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
        if float(car.get("Length") or 14.3) >= 17.6 and self.line_number(line) not in {3, 4}:
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

    def choose_result(self, results: list[SearchResult]) -> SearchResult:
        complete = [item for item in results if item.status == "complete"]
        if complete:
            return min(complete, key=lambda item: (item.cost, 0 if item.template == "B" else 1))
        return min(results, key=lambda item: (len(item.reasons), -item.expansions, item.template))

    def result(self, chosen: SearchResult, results: list[SearchResult]) -> dict[str, Any]:
        if chosen.template in {"A", "B"}:
            self.assigned_line_by_no = self.build_assigned_line_by_no(chosen.template)
        generated = self.generated_end_status(chosen.state) if chosen.state is not None else []
        response = {"Data": {"Operations": self.response_operations(chosen.ops), "GeneratedEndStatus": generated}}
        combined = {"Data": {"Operations": self.combined_operations(response), "GeneratedEndStatus": generated}}
        stage3_request = self.stage3_request()
        replayed, replay_bad = rv.replay(stage3_request, response)
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical"}]
        _combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        combined_hard_bad = [v for v in combined_bad if v.kind in {"schema", "physical"}]
        terminal_ok = chosen.state is not None and self.terminal_depot_ok(chosen.state)[0]
        if hard_bad and not chosen.reasons:
            reasons = tuple(f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12])
        else:
            reasons = chosen.reasons
        template_summaries = [
            {
                "template": item.template,
                "status": item.status,
                "operations": len(item.ops),
                "cost": list(item.cost),
                "blocking_reasons": list(item.reasons),
                "expansions": item.expansions,
                "elapsed_seconds": item.elapsed_seconds,
            }
            for item in results
        ]
        summary = {
            "case_id": self.case_id,
            "status": "complete" if chosen.status == "complete" and terminal_ok and not hard_bad else "partial",
            "template": chosen.template,
            "stage2_final_loco": self.stage2_final_loco,
            "stage3_start_loco": self.initial_loco,
            "operations": len(chosen.ops),
            "business_hooks": sum(1 for op in chosen.ops if op.action in {"Get", "Put"}),
            "active_count": len(self.active_nos),
            "active_nos": sorted(self.active_nos),
            "template_summaries": template_summaries,
            "terminal_depot_ok": bool(terminal_ok),
            "blocking_reasons": list(reasons),
            "replay_physical_ok": not hard_bad,
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
            "combined_replay_physical_ok": not combined_hard_bad,
            "combined_replay_violations": [v.__dict__ for v in combined_hard_bad[:20]],
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
            }
            for index, op in enumerate(chosen.ops, start=1)
        ]
        return {
            "response": response,
            "combined_response": combined,
            "stage3_request": stage3_request,
            "summary": summary,
            "trace": trace,
        }

    def generated_end_status(self, state: State | None) -> list[dict[str, Any]]:
        if state is None:
            return []
        _ok, _reasons, positions = self.terminal_depot_ok(state)
        positions.update(self.generated_staging_positions(state))
        rows: list[dict[str, Any]] = []
        line_map = self.line_map(state)
        for car in self.initial_cars:
            no = rv.car_no(car)
            line = car["Line"]
            position = int(car.get("Position") or 0)
            if no in positions:
                line, position = positions[no]
            elif no in self.active_nos:
                for candidate_line, nos in line_map.items():
                    if no in nos:
                        line = candidate_line
                        position = nos.index(no) + 1
                        break
            rows.append({"No": no, "Line": line, "Position": position})
        return sorted(rows, key=lambda item: item["No"])

    def generated_staging_positions(self, state: State) -> dict[str, tuple[str, int]]:
        line_map = self.line_map(state)
        out: dict[str, tuple[str, int]] = {}
        for line in STAGE4_STAGING_LINES:
            active = list(line_map.get(line, ()))
            fixed = [
                rv.car_no(car)
                for car in sorted(
                    (item for item in self.fixed_cars if item["Line"] == line),
                    key=lambda item: (int(item.get("Position") or 0), rv.car_no(item)),
                )
            ]
            if not active and not fixed:
                continue
            # Stage 4 pulls from the north/outside end.  Deferred oil/store4
            # staging cars must therefore stay in front of fixed unwheel/depot
            # cars that are already satisfied on the same holding line.
            for position, no in enumerate((*active, *fixed), start=1):
                out[no] = (line, position)
        return out

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
        for line, nos in state.lines:
            for position, no in enumerate(nos, start=1):
                car = dict(self.meta[no])
                car["Line"] = line
                car["Position"] = position
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
        existing = sum(float(self.meta[no].get("Length") or CAR_LENGTH_DEFAULT_M) for no in line_map.get(line, ()))
        fixed = sum(float(car.get("Length") or CAR_LENGTH_DEFAULT_M) for car in self.fixed_cars if car["Line"] == line)
        incoming = self.length(move)
        return existing + fixed + incoming <= limit + rv.TOL

    def pull_equivalent(self, nos: Iterable[str]) -> int:
        return sum(4 if self.meta[no].get("IsHeavy") else 1 for no in nos)

    def length(self, nos: Iterable[str]) -> float:
        return sum(float(self.meta[no].get("Length") or CAR_LENGTH_DEFAULT_M) for no in nos)

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


def request_paths(input_path: Path, case: str | None) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    paths = sorted(input_path.glob("*.json"))
    if case:
        target = case.upper()
        paths = [path for path in paths if case_id_from_path(path) == target]
    return paths


def solve_one(path: Path, stage2_out: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    stage2_summary_path = stage2_out / f"{case_id}_summary.json"
    combined_path = stage2_out / f"{case_id}_combined_response.json"
    if not combined_path.exists():
        summary = {
            "case_id": case_id,
            "status": "partial",
            "template": "none",
            "operations": 0,
            "business_hooks": 0,
            "active_count": 0,
            "blocking_reasons": ["stage2_combined_response_missing"],
            "replay_physical_ok": False,
            "replay_violations": [],
            "expansions": 0,
            "elapsed_seconds": 0.0,
        }
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    if stage2_summary_path.exists():
        stage2_summary = read_json(stage2_summary_path)
        if stage2_summary.get("status") != "complete" and not args.include_stage2_partial:
            summary = {
                "case_id": case_id,
                "status": "partial",
                "template": "none",
                "operations": 0,
                "business_hooks": 0,
                "active_count": 0,
                "blocking_reasons": [f"stage2_not_complete:{stage2_summary.get('status')}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
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
        try:
            summaries.append(solve_one(path, args.stage2_out, args.out, args))
        except Exception as exc:  # keep batch runs diagnosable
            summary = {
                "case_id": case_id,
                "status": "error",
                "template": "none",
                "operations": 0,
                "business_hooks": 0,
                "active_count": 0,
                "blocking_reasons": [f"{type(exc).__name__}:{exc}"],
                "replay_physical_ok": False,
                "replay_violations": [],
                "expansions": 0,
                "elapsed_seconds": 0.0,
            }
            summaries.append(summary)
            write_json(args.out / f"{case_id}_summary.json", summary)
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
