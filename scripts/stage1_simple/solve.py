#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical


ASSEMBLY_DEPOT = ("机南", "洗油北", "机走棚", "机走北")
ASSEMBLY_ALL = ("存4线", *ASSEMBLY_DEPOT)
FORBIDDEN_LINES = physical.DEPOT_TARGET_LINES | physical.DEPOT_OUTSIDE_LINES | physical.RUNNING_LINES
HOT_SOURCE_RANK = {
    "油漆线": 0,
    "洗罐站": 1,
    "洗罐线北": 2,
    "抛丸线": 3,
    "预修线": 4,
    "调梁棚": 5,
    "调梁线北": 6,
    "存5线南": 7,
    "存5线北": 8,
}
CACHE_LINES = (
    "存1线",
    "存2线",
    "存3线",
    "存5线北",
    "存5线南",
    "调梁线北",
    "调梁棚",
    "预修线",
    "洗罐线北",
    "洗罐站",
    "油漆线",
    "抛丸线",
)
MAX_HOOKS = 80


@dataclass(frozen=True)
class CandidateView:
    candidate: physical.HookCandidate
    score: tuple[Any, ...]
    reason: str


class Stage1Solver:
    def __init__(self, path: Path, *, max_hooks: int = MAX_HOOKS) -> None:
        self.case_id, self.request, self.cars, self.depot_assignment, self.loco = physical.read_case(path)
        self.graph = physical.TrackGraph()
        self.max_hooks = max_hooks
        self.hook_index = 1
        self.operation_index = 1
        self.operations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.seen_states = {physical.state_signature(self.cars, self.loco)}

    def solve(self) -> dict[str, Any]:
        while self.hook_index <= self.max_hooks:
            before = self.stage1_debt()
            if before["complete"]:
                break
            accepted = self.step(before)
            if not accepted:
                break
            after = self.stage1_debt()
            if after["debt_count"] >= before["debt_count"] and after["blocked_g_count"] >= before["blocked_g_count"]:
                self.trace[-1]["progress_warning"] = "no_stage1_debt_drop"
        return self.result()

    def step(self, debt: dict[str, Any]) -> bool:
        views = sorted(self.generate_candidates(debt), key=lambda item: item.score)
        rejected: list[dict[str, Any]] = []
        for view in views:
            validation = physical.validate_candidate(
                self.graph,
                view.candidate,
                self.cars,
                self.loco,
                self.depot_assignment,
            )
            if not validation.accepted:
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": list(validation.reasons),
                })
                continue
            after_debt = self.debt_after(view.candidate, validation)
            if not after_debt["complete"] and not self.leaves_progress(view.candidate, validation, depth=1):
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["dead_end_after_candidate"],
                })
                continue
            if self.seen_after(view.candidate, validation):
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["state_cycle"],
                })
                continue

            rows = physical.operation_rows(view.candidate, validation, self.operation_index)
            self.operations.extend(physical.response_operation(row) for row in rows)
            self.operation_index += len(rows)
            physical.apply_candidate(view.candidate, self.cars, validation)
            self.loco = physical.next_loco_location(view.candidate, validation)
            self.seen_states.add(physical.state_signature(self.cars, self.loco))
            self.trace.append({
                "hook": self.hook_index,
                "accepted": view.candidate.candidate_id,
                "kind": view.candidate.candidate_kind,
                "reason": view.reason,
                "source": view.candidate.source_line,
                "target": view.candidate.target_line,
                "move": list(view.candidate.move_car_nos),
                "score": list(view.score),
                "paths": [list(path) for path in validation.operation_paths],
                "rejected_before_accept": rejected[:8],
                "debt_before": debt,
                "debt_after": after_debt,
            })
            self.hook_index += 1
            return True

        self.trace.append({
            "hook": self.hook_index,
            "accepted": "",
            "kind": "blocked",
            "reason": "no_valid_candidate",
            "candidate_count": len(views),
            "rejected": rejected[:30],
            "debt_before": debt,
        })
        return False

    def debt_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> dict[str, Any]:
        probe = self.fork()
        physical.apply_candidate(candidate, probe.cars, validation)
        probe.loco = physical.next_loco_location(candidate, validation)
        return probe.stage1_debt()

    def seen_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> bool:
        probe = self.fork()
        physical.apply_candidate(candidate, probe.cars, validation)
        probe.loco = physical.next_loco_location(candidate, validation)
        return physical.state_signature(probe.cars, probe.loco) in self.seen_states

    def leaves_progress(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
        *,
        depth: int,
    ) -> bool:
        probe = self.fork()
        physical.apply_candidate(candidate, probe.cars, validation)
        probe.loco = physical.next_loco_location(candidate, validation)
        probe.hook_index = self.hook_index + 1
        return probe.can_continue(depth)

    def can_continue(self, depth: int) -> bool:
        debt = self.stage1_debt()
        if debt["complete"] or depth <= 0:
            return True
        for view in sorted(self.generate_candidates(debt), key=lambda item: item.score):
            next_validation = physical.validate_candidate(
                self.graph,
                view.candidate,
                self.cars,
                self.loco,
                self.depot_assignment,
            )
            if next_validation.accepted and self.leaves_progress(view.candidate, next_validation, depth=depth - 1):
                return True
        return False

    def fork(self) -> "Stage1Solver":
        clone = Stage1Solver.__new__(Stage1Solver)
        clone.case_id = self.case_id
        clone.request = self.request
        clone.cars = [dict(car) for car in self.cars]
        clone.depot_assignment = self.depot_assignment
        clone.loco = self.loco
        clone.graph = self.graph
        clone.max_hooks = self.max_hooks
        clone.hook_index = self.hook_index
        clone.operation_index = self.operation_index
        clone.operations = list(self.operations)
        clone.trace = list(self.trace)
        clone.seen_states = set(self.seen_states)
        return clone

    def generate_candidates(self, debt: dict[str, Any]) -> list[CandidateView]:
        candidates: list[CandidateView] = []
        candidates.extend(self.direct_assembly_candidates(debt))
        candidates.extend(self.blocker_candidates(debt))
        candidates.extend(self.route_blocker_candidates(debt))
        candidates.extend(self.cleanup_candidates(debt))
        candidates.extend(self.release_gate_candidates(debt))
        unique: dict[str, CandidateView] = {}
        for item in candidates:
            unique.setdefault(item.candidate.candidate_id, item)
        return list(unique.values())

    def direct_assembly_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        by_no = self.by_no()
        for line in self.active_lines():
            if line in FORBIDDEN_LINES:
                continue
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            first = ordered[0]
            first_goal = self.stage1_goal(first)
            if not first_goal or self.stage1_car_complete(first):
                continue
            target_group = "unwheel" if first_goal == "存4线" else "depot"
            max_batch: list[dict[str, Any]] = []
            for car in ordered:
                goal = self.stage1_goal(car)
                if not goal or self.stage1_car_complete(car):
                    break
                if ("unwheel" if goal == "存4线" else "depot") != target_group:
                    break
                trial = [*max_batch, car]
                if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                max_batch = trial
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    break
            if not max_batch:
                continue
            targets = (
                ("存4线",)
                if target_group == "unwheel"
                else (*ASSEMBLY_DEPOT, "存4线", *self.safe_temp_targets(source=line, debt=debt))
            )
            for batch in self.prefix_options(max_batch):
                for target in targets:
                    if self.disallow_stage1_temp_to_temp(line, target, batch):
                        continue
                    if not self.target_allowed(target, batch):
                        continue
                    candidate = self.make_candidate(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="vnext_depot_inbound_assembly_session",
                        reason="direct_assembly",
                    )
                    if not candidate:
                        continue
                    candidates_score = self.score_candidate(
                        candidate,
                        moved_g=sum(1 for car in batch if self.stage1_goal(car)),
                        moved_x=0,
                        reason_rank=0,
                    )
                    yield CandidateView(candidate, candidates_score, "direct_assembly")

    def blocker_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        by_no = self.by_no()
        pending = set(debt["pending_stage1_nos"])
        for line in self.active_lines():
            if line in FORBIDDEN_LINES:
                continue
            ordered_nos = physical.line_access_order(self.cars, line)
            if not any(no in pending for no in ordered_nos):
                continue
            first_pending = next((idx for idx, no in enumerate(ordered_nos) if no in pending), -1)
            if first_pending <= 0:
                continue
            blockers = [by_no[no] for no in ordered_nos[:first_pending] if no in by_no]
            for batch in self.prefix_options(blockers):
                for target in self.blocker_targets(batch, source=line, debt=debt):
                    candidate = self.make_candidate(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="clear_prefix_blocker",
                    )
                    if not candidate:
                        continue
                    yield CandidateView(
                        candidate,
                        self.score_candidate(candidate, moved_g=0, moved_x=len(batch), reason_rank=1),
                        "clear_prefix_blocker",
                    )

    def route_blocker_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        by_no = self.by_no()
        for blocker_line, reason in self.route_blocker_requests(debt):
            if blocker_line in FORBIDDEN_LINES:
                continue
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, blocker_line) if no in by_no]
            if not ordered:
                continue
            for batch in self.prefix_options(ordered):
                for target in self.blocker_targets(batch, source=blocker_line, debt=debt):
                    if not self.target_allowed(target, batch):
                        continue
                    candidate = self.make_candidate(
                        source=blocker_line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason=reason,
                    )
                    if not candidate:
                        continue
                    yield CandidateView(
                        candidate,
                        self.score_candidate(candidate, moved_g=0, moved_x=len(batch), reason_rank=1),
                        reason,
                    )

    def cleanup_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        by_no = self.by_no()
        for line in ASSEMBLY_ALL:
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            polluted_prefixes = [
                ordered[: index + 1]
                for index, car in enumerate(ordered)
                if not self.allowed_on_stage_line(car, line)
            ]
            for batch in polluted_prefixes:
                if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
                    continue
                for target in self.blocker_targets(batch, source=line, debt=debt):
                    candidate = self.make_candidate(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="stage_boundary_cleanup",
                    )
                    if not candidate:
                        continue
                    yield CandidateView(
                        candidate,
                        self.score_candidate(candidate, moved_g=0, moved_x=len(batch), reason_rank=2),
                        "stage_boundary_cleanup",
                    )

    def route_blocker_requests(self, debt: dict[str, Any]) -> list[tuple[str, str]]:
        by_no = self.by_no()
        seeds: list[tuple[str, set[str], str, int]] = []
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if car:
                seeds.append((car["Line"], {no}, f"clear_route_blocker_for:{no}", 0))
        for no in debt["pollution_nos"]:
            car = by_no.get(no)
            if car:
                seeds.append((car["Line"], {no}, f"clear_route_blocker_for_pollution:{no}", 0))

        requests: list[tuple[str, str]] = []
        seen_requests: set[tuple[str, str]] = set()
        seen_seeds: set[tuple[str, tuple[str, ...], int]] = set()
        while seeds:
            source, moving_nos, reason, depth = seeds.pop(0)
            seed_key = (source, tuple(sorted(moving_nos)), depth)
            if seed_key in seen_seeds:
                continue
            seen_seeds.add(seed_key)
            for blocker_line in self.route_blockers_for_get(source, moving_nos):
                if blocker_line in FORBIDDEN_LINES or blocker_line == source:
                    continue
                request_key = (blocker_line, reason)
                if request_key not in seen_requests:
                    seen_requests.add(request_key)
                    requests.append(request_key)
                if depth >= 1:
                    continue
                blocker_nos = tuple(physical.line_access_order(self.cars, blocker_line)[:1])
                if blocker_nos:
                    seeds.append((blocker_line, set(blocker_nos), f"clear_route_blocker_for:{source}", depth + 1))
        return requests

    def route_blockers_for_get(self, line: str, moving_nos: set[str]) -> tuple[str, ...]:
        occupied = physical.occupied_lines_for_get_route(self.cars, moving_nos, line)
        available = self.graph.route_avoiding_occupied(
            self.loco.line,
            line,
            occupied,
            source_departure_lines=physical.route_departure_lines_for_source(self.loco.line, self.cars, moving_nos),
            target_approach_lines=physical.route_approach_lines_for_get(line),
            cars=self.cars,
            moving_nos=moving_nos,
            train_length_m=0.0,
        )
        if available:
            return ()

        endpoints = {self.loco.line, line}
        candidate_paths = self.get_static_access_paths(line)
        blocked_paths: list[tuple[int, int, list[str]]] = []
        for path in candidate_paths:
            blockers = [node for node in path if node in occupied and node not in endpoints]
            if blockers:
                blocked_paths.append((len(blockers), len(path), blockers))
        if not blocked_paths:
            return ()
        _count, _length, blockers = min(blocked_paths)
        return tuple(dict.fromkeys(blockers))

    def get_static_access_paths(self, line: str) -> list[list[str]]:
        approaches = sorted(physical.route_approach_lines_for_get(line))
        if not approaches:
            route = self.graph.route(self.loco.line, line)
            return [route] if route else []
        paths: list[list[str]] = []
        for approach in approaches:
            if approach == self.loco.line:
                paths.append([self.loco.line, line])
                continue
            route = self.graph.route_avoiding_occupied(
                self.loco.line,
                approach,
                {line},
            )
            if route:
                paths.append([*route, line])
        return paths

    def release_gate_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not debt["pending_stage1_nos"]:
            return
        if not any(
            self.stage1_goal(car)
            and not self.stage1_car_complete(car)
            and car["Line"] == car.get("_InitialLine")
            for car in self.cars
        ):
            return
        by_no = self.by_no()
        for line in ("洗油北", "机走棚", "机走北"):
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            movable = [car for car in ordered if self.stage1_goal(car)]
            if not movable:
                continue
            for batch in self.prefix_options(movable[:2]):
                for target in self.safe_temp_targets(source=line, debt=debt):
                    if not self.target_allowed(target, batch):
                        continue
                    candidate = self.make_candidate(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="release_gate_assembly",
                    )
                    if not candidate:
                        continue
                    yield CandidateView(
                        candidate,
                        self.score_candidate(candidate, moved_g=0, moved_x=len(batch), reason_rank=3),
                        "release_gate_assembly",
                    )

    def blocker_targets(self, batch: list[dict[str, Any]], *, source: str, debt: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        direct = self.common_current_target(batch)
        if (
            direct
            and direct not in FORBIDDEN_LINES
            and direct != source
            and not self.would_pollute_stage_boundary(direct, batch)
        ):
            targets.append(direct)
        targets.extend(self.safe_temp_targets(source=source, debt=debt))
        return list(dict.fromkeys(targets))

    def safe_temp_targets(self, *, source: str, debt: dict[str, Any]) -> list[str]:
        blocked_lines = set(debt["lines_with_pending_stage1"])
        targets: list[str] = []
        for line in CACHE_LINES:
            if line == source or line in FORBIDDEN_LINES or line in blocked_lines or line == physical.WEIGH_LINE:
                continue
            targets.append(line)
        return targets

    def would_pollute_stage_boundary(self, target: str, batch: list[dict[str, Any]]) -> bool:
        if target not in ASSEMBLY_ALL:
            return False
        return any(not self.allowed_on_stage_line(car, target) for car in batch)

    def prefix_options(self, cars: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        prefix: list[dict[str, Any]] = []
        for car in cars:
            trial = [*prefix, car]
            if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix = trial
            if car.get("IsWeigh") and not car.get("_Weighed"):
                break
        return [prefix[:size] for size in range(len(prefix), 0, -1)]

    def make_candidate(
        self,
        *,
        source: str,
        target: str,
        batch: list[dict[str, Any]],
        kind: str,
        reason: str,
    ) -> physical.HookCandidate | None:
        if source in FORBIDDEN_LINES or target in FORBIDDEN_LINES:
            return None
        nos = tuple(physical.car_no(car) for car in batch)
        steps = (
            physical.plan_step("Get", source, nos),
            physical.plan_step("Put", target, nos),
        )
        return physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.hook_index,
            source_line=source,
            target_line=target,
            batch=batch,
            steps=steps,
            reason=reason,
            candidate_kind=kind,
        )

    def score_candidate(
        self,
        candidate: physical.HookCandidate,
        *,
        moved_g: int,
        moved_x: int,
        reason_rank: int,
    ) -> tuple[Any, ...]:
        return (
            self.candidate_source_rank(candidate),
            reason_rank,
            self.candidate_target_rank(candidate),
            -moved_g,
            moved_x,
            self.route_price(candidate),
            len(candidate.move_car_nos),
            candidate.candidate_id,
        )

    def route_price(self, candidate: physical.HookCandidate) -> int:
        static = self.graph.route(candidate.source_line, candidate.target_line)
        hot = {"渡10", "联7", "机北1", "机北2", "预修线", "调梁线北", "存5线北"}
        return len(static) + sum(3 for line in static if line in hot)

    def target_allowed(self, target: str, batch: list[dict[str, Any]]) -> bool:
        if target not in physical.TRACK_SPECS:
            return False
        batch_nos = {physical.car_no(car) for car in batch}
        return physical.line_has_length_capacity(target, self.cars, batch, batch_nos)

    def common_current_target(self, batch: list[dict[str, Any]]) -> str:
        targets = []
        loads = physical.line_loads(self.cars)
        for car in batch:
            target, _pos, _reason = physical.planned_target_for_car(car, self.cars, self.depot_assignment, loads)
            targets.append(target)
        return targets[0] if targets and all(target == targets[0] for target in targets) else ""

    def disallow_stage1_temp_to_temp(self, source: str, target: str, batch: list[dict[str, Any]]) -> bool:
        for car in batch:
            if not self.stage1_goal(car):
                continue
            if self.official_stage1_target(car, target):
                continue
            if car.get("_InitialLine") == source:
                continue
            return True
        return False

    def official_stage1_target(self, car: dict[str, Any], target: str) -> bool:
        goal = self.stage1_goal(car)
        if goal == "存4线":
            return target == "存4线"
        if goal == "depot_assembly":
            return target in ASSEMBLY_DEPOT or target == "存4线"
        return False

    def stage1_goal(self, car: dict[str, Any]) -> str:
        if car["Line"] in physical.DEPOT_TARGET_LINES:
            return ""
        targets = set(car.get("TargetLines") or ())
        if "卸轮线" in targets:
            return "存4线"
        if targets & physical.DEPOT_TARGET_LINES:
            return "depot_assembly"
        return ""

    def stage1_car_complete(self, car: dict[str, Any]) -> bool:
        goal = self.stage1_goal(car)
        if not goal:
            return True
        if car.get("IsWeigh") and not car.get("_Weighed"):
            return False
        if goal == "存4线":
            return car["Line"] == "存4线"
        return car["Line"] in ASSEMBLY_DEPOT or car["Line"] == "存4线"

    def allowed_on_stage_line(self, car: dict[str, Any], line: str) -> bool:
        goal = self.stage1_goal(car)
        if line == "存4线":
            return goal in {"存4线", "depot_assembly"}
        if line in ASSEMBLY_DEPOT:
            return goal == "depot_assembly"
        return True

    def stage1_debt(self) -> dict[str, Any]:
        pending: list[str] = []
        blocked_g = 0
        lines_with_pending: set[str] = set()
        pollution: list[str] = []
        by_no = self.by_no()
        for car in self.cars:
            if self.stage1_goal(car) and not self.stage1_car_complete(car):
                no = physical.car_no(car)
                pending.append(no)
                lines_with_pending.add(car["Line"])
        pending_set = set(pending)
        for line in self.active_lines():
            ordered = physical.line_access_order(self.cars, line)
            seen_blocker = False
            for no in ordered:
                car = by_no.get(no)
                if not car:
                    continue
                if no in pending_set:
                    if seen_blocker:
                        blocked_g += 1
                elif pending_set.intersection(ordered[ordered.index(no) + 1:]):
                    seen_blocker = True
        for line in ASSEMBLY_ALL:
            for no in physical.line_access_order(self.cars, line):
                car = by_no.get(no)
                if car and not self.allowed_on_stage_line(car, line):
                    pollution.append(no)
        return {
            "complete": not pending and not pollution,
            "debt_count": len(pending) + len(pollution),
            "pending_stage1_nos": pending,
            "blocked_g_count": blocked_g,
            "pollution_nos": pollution,
            "lines_with_pending_stage1": sorted(line for line in lines_with_pending if line),
        }

    def result(self) -> dict[str, Any]:
        debt = self.stage1_debt()
        final_status = [
            self.output_car(car)
            for car in sorted(self.cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), physical.car_no(item)))
        ]
        response = {"Data": {"Operations": self.operations, "GeneratedEndStatus": final_status}}
        summary = {
            "case_id": self.case_id,
            "status": "complete" if debt["complete"] else "partial",
            "hooks": self.hook_index - 1,
            "operations": len(self.operations),
            "stage1_debt": debt,
            "blocking_reasons": self.blocking_reasons(debt),
        }
        return {"response": response, "summary": summary, "trace": self.trace}

    def blocking_reasons(self, debt: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if debt["pending_stage1_nos"]:
            reasons.append(f"pending_stage1_cars:{len(debt['pending_stage1_nos'])}")
        if debt["pollution_nos"]:
            reasons.append(f"stage_boundary_pollution:{len(debt['pollution_nos'])}")
        if self.trace and self.trace[-1].get("reason") == "no_valid_candidate":
            counter = Counter(
                violation
                for item in self.trace[-1].get("rejected", [])
                for violation in item.get("violations", [])
            )
            reasons.extend(f"{key}:{count}" for key, count in counter.most_common(8))
        return reasons

    def active_lines(self) -> list[str]:
        return sorted({car["Line"] for car in self.cars if car["Line"]}, key=lambda line: (self.source_rank(line), line))

    def source_rank(self, line: str) -> int:
        return HOT_SOURCE_RANK.get(line, 50)

    def candidate_source_rank(self, candidate: physical.HookCandidate) -> int:
        rank = self.source_rank(candidate.source_line)
        moving = set(candidate.move_car_nos)
        original_pending_exists = any(
            self.stage1_goal(car)
            and not self.stage1_car_complete(car)
            and physical.car_no(car) not in moving
            and car["Line"] == car.get("_InitialLine")
            for car in self.cars
        )
        if not original_pending_exists:
            return rank
        by_no = self.by_no()
        if any(
            no in by_no
            and self.stage1_goal(by_no[no])
            and by_no[no]["Line"] != by_no[no].get("_InitialLine")
            for no in moving
        ):
            rank += 80
        return rank

    def target_rank(self, line: str) -> int:
        if line == "存4线":
            return 0
        if line in ASSEMBLY_DEPOT:
            return 10 + ASSEMBLY_DEPOT.index(line)
        if line in CACHE_LINES:
            return 50 + CACHE_LINES.index(line)
        return 100

    def candidate_target_rank(self, candidate: physical.HookCandidate) -> int:
        rank = self.target_rank(candidate.target_line)
        if candidate.target_line in {"洗油北", "机走棚", "机走北"}:
            moving = set(candidate.move_car_nos)
            if any(
                self.stage1_goal(car) and not self.stage1_car_complete(car) and physical.car_no(car) not in moving
                for car in self.cars
            ):
                rank += 60
        if candidate.target_line != "存4线":
            return rank
        by_no = self.by_no()
        if any(self.stage1_goal(by_no[no]) == "depot_assembly" for no in candidate.move_car_nos if no in by_no):
            return 40
        return 0

    def by_no(self) -> dict[str, dict[str, Any]]:
        return {physical.car_no(car): car for car in self.cars}

    def output_car(self, car: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                **car,
                "No": physical.car_no(car),
                "Line": car["Line"],
                "Position": int(car.get("Position") or 0),
            }.items()
            if not key.startswith("_") or key in {"_Weighed"}
        }


def solve_one(path: Path, out_dir: Path, *, max_hooks: int, verbose: bool = False) -> dict[str, Any]:
    solver = Stage1Solver(path, max_hooks=max_hooks)
    result = solver.solve()
    out_dir.mkdir(parents=True, exist_ok=True)
    case_id = result["summary"]["case_id"]
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    if verbose:
        summary = result["summary"]
        print(
            f"{summary['case_id']} {summary['status']} "
            f"hooks={summary['hooks']} debt={summary['stage1_debt']['debt_count']}",
            flush=True,
        )
    return result["summary"]


def case_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("validation_*.json"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple first-stage depot inbound assembly solver.")
    parser.add_argument("input", type=Path, help="case JSON or directory containing validation_*.json")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--max-hooks", type=int, default=MAX_HOOKS)
    parser.add_argument("--limit", type=int, default=0, help="limit number of cases for directory input")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = case_files(args.input)
    if args.limit:
        files = files[: args.limit]
    summaries = [solve_one(path, args.out, max_hooks=args.max_hooks, verbose=args.input.is_dir()) for path in files]
    aggregate = {
        "cases": len(summaries),
        "complete": sum(1 for item in summaries if item["status"] == "complete"),
        "partial": sum(1 for item in summaries if item["status"] != "complete"),
        "avg_hooks": round(sum(item["hooks"] for item in summaries) / len(summaries), 3) if summaries else 0,
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps({k: v for k, v in aggregate.items() if k != "summaries"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
