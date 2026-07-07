#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
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
STAGE1_INITIAL_DONE_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
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
# hook_index counts move batches. business_hooks in the summary counts Get/Put rows.
MAX_NO_PROGRESS_STREAK = 30
MAX_STALE_BEST_STREAK = 35
MAX_CAPACITY_DEFICIT_HOOKS = 10
DEFAULT_TIME_BUDGET_SECONDS = 300.0
DYNAMIC_VALID_WINDOW = 20
DYNAMIC_EXAMINED_WINDOW = 140


@dataclass(frozen=True)
class CandidateView:
    candidate: physical.HookCandidate
    score: tuple[Any, ...]
    reason: str


class Stage1Solver:
    def __init__(
        self,
        path: Path,
        *,
        max_hooks: int = MAX_HOOKS,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> None:
        self.case_id, self.request, self.cars, self.depot_assignment, self.loco = physical.read_case(path)
        nos = [physical.car_no(car) for car in self.cars]
        duplicates = [no for no, count in Counter(nos).items() if not no or count > 1]
        if duplicates:
            raise ValueError(f"duplicate_or_empty_car_no:{','.join(sorted(duplicates))}")
        self.graph = physical.TrackGraph()
        self.max_hooks = max_hooks
        self.hook_index = 1
        self.operation_index = 1
        self.operations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.seen_states = {physical.state_signature(self.cars, self.loco)}
        self.is_probe = False
        self.started_at = time.monotonic()
        self.time_budget_seconds = time_budget_seconds
        self.validation_cache: dict[tuple[str, str], physical.PhysicalValidation] = {}

    def solve(self) -> dict[str, Any]:
        no_progress_streak = 0
        stale_best_streak = 0
        best_progress = (10**9, 10**9)
        while self.hook_index <= self.max_hooks:
            before = self.stage1_debt()
            if before["complete"]:
                break
            if self.time_exhausted():
                self.trace.append({
                    "hook": self.hook_index,
                    "accepted": "",
                    "kind": "blocked",
                    "reason": "solve_time_budget_exhausted",
                    "candidate_count": 0,
                    "rejected": [],
                    "debt_before": before,
                    "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
                })
                break
            before_progress = (before["debt_count"], before["blocked_g_count"])
            if before_progress < best_progress:
                best_progress = before_progress
                stale_best_streak = 0
            capacity_deficit = self.depot_assembly_capacity_deficit_m()
            if (
                capacity_deficit > 0
                and (no_progress_streak >= 6 or self.hook_index > MAX_CAPACITY_DEFICIT_HOOKS)
            ):
                self.trace.append({
                    "hook": self.hook_index,
                    "accepted": "",
                    "kind": "blocked",
                    "reason": "assembly_capacity_impossible",
                    "candidate_count": 0,
                    "rejected": [],
                    "debt_before": before,
                    "capacity_deficit_m": round(capacity_deficit, 3),
                })
                break
            if no_progress_streak >= MAX_NO_PROGRESS_STREAK:
                self.trace.append({
                    "hook": self.hook_index,
                    "accepted": "",
                    "kind": "blocked",
                    "reason": "no_stage1_progress",
                    "candidate_count": 0,
                    "rejected": [],
                    "debt_before": before,
                })
                break
            if stale_best_streak >= MAX_STALE_BEST_STREAK:
                self.trace.append({
                    "hook": self.hook_index,
                    "accepted": "",
                    "kind": "blocked",
                    "reason": "no_stage1_window_progress",
                    "candidate_count": 0,
                    "rejected": [],
                    "debt_before": before,
                    "best_progress": list(best_progress),
                })
                break
            accepted = self.step(before)
            if not accepted:
                break
            after = self.stage1_debt()
            after_progress = (after["debt_count"], after["blocked_g_count"])
            if after_progress < best_progress:
                best_progress = after_progress
                stale_best_streak = 0
            else:
                stale_best_streak += 1
            if after["debt_count"] >= before["debt_count"] and after["blocked_g_count"] >= before["blocked_g_count"]:
                self.trace[-1]["progress_warning"] = "no_stage1_debt_drop"
                no_progress_streak += 1
            else:
                no_progress_streak = 0
        return self.result()

    def step(self, debt: dict[str, Any]) -> bool:
        views = sorted(self.generate_candidates(debt), key=lambda item: item.score)
        rejected: list[dict[str, Any]] = []
        if self.try_views(views, debt, rejected):
            return True

        put_blocker_views = sorted(
            self.route_blocker_candidates(debt, include_put_blockers=True),
            key=lambda item: item.score,
        )
        if put_blocker_views and self.try_views(put_blocker_views, debt, rejected):
            self.trace[-1]["fallback"] = "put_route_blocker_cleanup"
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

    def try_views(
        self,
        views: list[CandidateView],
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
    ) -> bool:
        if self.try_dynamic_views(views, debt, rejected):
            return True
        return self.try_first_valid_view(views, debt, rejected)

    def try_first_valid_view(
        self,
        views: list[CandidateView],
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
    ) -> bool:
        for view in views:
            if self.time_exhausted():
                return False
            validation = self.validate_candidate(view.candidate)
            if not validation.accepted:
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": list(validation.reasons),
                })
                continue
            probe = self.probe_after(view.candidate, validation)
            after_signature = physical.state_signature(probe.cars, probe.loco)
            if after_signature in self.seen_states:
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["state_cycle"],
                })
                continue
            after_debt = probe.stage1_debt()
            if self.large_assembly_debt_rebound(view.candidate, debt, after_debt):
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["large_assembly_debt_rebound"],
                })
                continue
            if not after_debt["complete"] and not probe.can_continue(depth=1, include_put_blockers=True):
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["dead_end_after_candidate"],
                })
                continue
            return self.accept_candidate(view, validation, after_debt, debt, rejected)
        return False

    def try_dynamic_views(
        self,
        views: list[CandidateView],
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
    ) -> bool:
        if debt["pollution_nos"]:
            return False

        get_blockers, get_blocked_nos = self.pending_get_blocker_info(debt)
        put_blockers, _put_blocked_nos = self.pending_put_blocker_info(debt)
        use_get_unlock = bool(get_blockers & set(ASSEMBLY_DEPOT))
        use_put_unlock = not get_blockers and bool(put_blockers & set(ASSEMBLY_DEPOT))
        use_debt_window = not get_blockers
        if not (use_get_unlock or use_put_unlock or use_debt_window):
            return False

        dynamic_views = views
        if use_put_unlock:
            dynamic_views = self.dynamic_release_first_views(views, debt)

        viable: list[
            tuple[
                tuple[Any, ...],
                CandidateView,
                physical.PhysicalValidation,
                dict[str, Any],
                set[str],
                set[str],
                set[str],
                set[str],
            ]
        ] = []
        examined_count = 0
        valid_count = 0
        for view in dynamic_views:
            if self.time_exhausted():
                return False
            examined_count += 1
            validation = self.validate_candidate(view.candidate)
            if not validation.accepted:
                if len(rejected) < 80:
                    rejected.append({
                        "candidate_id": view.candidate.candidate_id,
                        "reason": view.reason,
                        "violations": list(validation.reasons),
                    })
                continue
            probe = self.probe_after(view.candidate, validation)
            after_signature = physical.state_signature(probe.cars, probe.loco)
            if after_signature in self.seen_states:
                if len(rejected) < 80:
                    rejected.append({
                        "candidate_id": view.candidate.candidate_id,
                        "reason": view.reason,
                        "violations": ["state_cycle"],
                    })
                continue
            after_debt = probe.stage1_debt()
            if not after_debt["complete"] and not probe.can_continue(depth=1, include_put_blockers=True):
                if len(rejected) < 80:
                    rejected.append({
                        "candidate_id": view.candidate.candidate_id,
                        "reason": view.reason,
                        "violations": ["dead_end_after_candidate"],
                    })
                continue

            moving_nos = set(view.candidate.move_car_nos)
            improves_debt = self.progress_tuple(after_debt) < self.progress_tuple(debt)
            if get_blockers:
                after_get_blockers, _after_get_blocked_nos = probe.pending_get_blocker_info(after_debt)
                if not use_get_unlock:
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                moves_get_blocked = bool(moving_nos & get_blocked_nos)
                unlocks_get = len(after_get_blockers) < len(get_blockers)
                if not moves_get_blocked and not unlocks_get and not after_debt["complete"]:
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                if (
                    after_debt["debt_count"] > debt["debt_count"]
                    and view.candidate.source_line in ASSEMBLY_DEPOT
                    and view.candidate.target_line not in ASSEMBLY_DEPOT
                    and not unlocks_get
                ):
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                primary = (
                    0
                    if after_debt["complete"]
                    else 1
                    if unlocks_get and not moves_get_blocked
                    else 2
                    if moves_get_blocked and improves_debt
                    else 3
                    if improves_debt
                    else 4
                )
                dynamic_score = (
                    primary,
                    self.dynamic_target_group(view.candidate),
                    len(after_get_blockers),
                    max(0, after_debt["debt_count"] - debt["debt_count"]),
                    self.progress_tuple(after_debt),
                    view.score,
                )
                viable.append((
                    dynamic_score,
                    view,
                    validation,
                    after_debt,
                    get_blockers,
                    after_get_blockers,
                    put_blockers,
                    set(),
                ))
            else:
                after_put_blockers: set[str] = set()
                unlocks_put = False
                if put_blockers:
                    after_put_blockers, _after_put_blocked_nos = probe.pending_put_blocker_info(after_debt)
                    unlocks_put = bool(put_blockers & set(ASSEMBLY_DEPOT)) and len(after_put_blockers) < len(put_blockers)
                    if len(after_put_blockers) > len(put_blockers) and not after_debt["complete"]:
                        valid_count += 1
                        if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                            break
                        continue
                if unlocks_put and self.large_assembly_debt_rebound(view.candidate, debt, after_debt):
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                primary = (
                    0
                    if after_debt["complete"]
                    else 1
                    if unlocks_put
                    else 2
                    if improves_debt
                    else 5
                )
                if primary == 5 and use_put_unlock:
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                dynamic_score = (
                    primary,
                    self.dynamic_target_group(view.candidate),
                    len(after_put_blockers),
                    max(0, after_debt["debt_count"] - debt["debt_count"]),
                    self.progress_tuple(after_debt),
                    view.score,
                )
                viable.append((
                    dynamic_score,
                    view,
                    validation,
                    after_debt,
                    set(),
                    set(),
                    put_blockers,
                    after_put_blockers,
                ))

            valid_count += 1
            if after_debt["complete"] or valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                break

        if not viable:
            return False
        dynamic_score, view, validation, after_debt, before_get, after_get, before_put, after_put = min(
            viable,
            key=lambda item: item[0],
        )
        accepted = self.accept_candidate(view, validation, after_debt, debt, rejected)
        self.trace[-1]["dynamic_score"] = list(dynamic_score[:4])
        self.trace[-1]["route_blockers_before"] = sorted(before_get)
        self.trace[-1]["route_blockers_after"] = sorted(after_get)
        self.trace[-1]["put_blockers_before"] = sorted(before_put)
        self.trace[-1]["put_blockers_after"] = sorted(after_put)
        return accepted

    def dynamic_target_group(self, candidate: physical.HookCandidate) -> int:
        if candidate.target_line == "存4线":
            return 0
        if candidate.target_line in {"机南", "洗油北"}:
            return 0
        if candidate.target_line in {"机走棚", "机走北"}:
            return 1
        return 2

    def large_assembly_debt_rebound(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        return (
            candidate.source_line in ASSEMBLY_DEPOT
            and candidate.target_line not in ASSEMBLY_DEPOT
            and after_debt["debt_count"] > debt["debt_count"] + 1
        )

    def dynamic_release_first_views(
        self,
        views: list[CandidateView],
        debt: dict[str, Any],
    ) -> list[CandidateView]:
        release_views = sorted(self.release_gate_candidates(debt), key=lambda item: item.score)
        if not release_views:
            return views
        ordered: list[CandidateView] = []
        seen: set[str] = set()
        for view in [*release_views, *views]:
            candidate_id = view.candidate.candidate_id
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            ordered.append(view)
        return ordered

    def accept_candidate(
        self,
        view: CandidateView,
        validation: physical.PhysicalValidation,
        after_debt: dict[str, Any],
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
    ) -> bool:
        if self.is_probe:
            raise RuntimeError("probe_solver_cannot_accept_candidate")
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

    def validate_candidate(self, candidate: physical.HookCandidate) -> physical.PhysicalValidation:
        state_key = physical.state_signature(self.cars, self.loco)
        cache_key = (state_key, candidate.candidate_id)
        cached = self.validation_cache.get(cache_key)
        if cached is not None:
            return cached
        validation = physical.validate_candidate(
            self.graph,
            candidate,
            self.cars,
            self.loco,
            self.depot_assignment,
        )
        self.validation_cache[cache_key] = validation
        return validation

    def probe_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> "Stage1Solver":
        probe = self.fork()
        physical.apply_candidate(candidate, probe.cars, validation)
        probe.loco = physical.next_loco_location(candidate, validation)
        return probe

    def debt_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> dict[str, Any]:
        return self.probe_after(candidate, validation).stage1_debt()

    def seen_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> bool:
        probe = self.probe_after(candidate, validation)
        return physical.state_signature(probe.cars, probe.loco) in self.seen_states

    def leaves_progress(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
        *,
        depth: int,
    ) -> bool:
        probe = self.probe_after(candidate, validation)
        probe.hook_index = self.hook_index + 1
        return probe.can_continue(depth, include_put_blockers=True)

    def can_continue(self, depth: int, *, include_put_blockers: bool = False) -> bool:
        debt = self.stage1_debt()
        if debt["complete"] or depth <= 0:
            return True
        views = self.continuation_views(debt, include_put_blockers=include_put_blockers)
        for view in views:
            next_validation = self.validate_candidate(view.candidate)
            if not next_validation.accepted:
                continue
            probe = self.probe_after(view.candidate, next_validation)
            if physical.state_signature(probe.cars, probe.loco) in self.seen_states:
                continue
            if depth <= 1 or probe.can_continue(depth - 1, include_put_blockers=include_put_blockers):
                return True
        return False

    def fork(self) -> "Stage1Solver":
        clone = Stage1Solver.__new__(Stage1Solver)
        clone.case_id = self.case_id
        clone.request = self.request
        clone.cars = [self.clone_car(car) for car in self.cars]
        clone.depot_assignment = self.depot_assignment
        clone.loco = self.loco
        clone.graph = self.graph
        clone.max_hooks = self.max_hooks
        clone.hook_index = self.hook_index
        clone.operation_index = self.operation_index
        clone.operations = []
        clone.trace = []
        clone.seen_states = self.seen_states
        clone.is_probe = True
        clone.started_at = self.started_at
        clone.time_budget_seconds = self.time_budget_seconds
        clone.validation_cache = self.validation_cache
        return clone

    def clone_car(self, car: dict[str, Any]) -> dict[str, Any]:
        copied: dict[str, Any] = {}
        for key, value in car.items():
            if isinstance(value, list):
                copied[key] = list(value)
            elif isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, set):
                copied[key] = set(value)
            else:
                copied[key] = value
        return copied

    def time_exhausted(self) -> bool:
        return (time.monotonic() - self.started_at) >= self.time_budget_seconds

    def continuation_views(
        self,
        debt: dict[str, Any],
        *,
        include_put_blockers: bool,
    ) -> list[CandidateView]:
        views = list(self.generate_candidates(debt))
        if include_put_blockers:
            views.extend(self.route_blocker_candidates(debt, include_put_blockers=True))
        unique: dict[str, CandidateView] = {}
        for view in views:
            unique.setdefault(view.candidate.candidate_id, view)
        return sorted(unique.values(), key=lambda item: item.score)

    def generate_candidates(self, debt: dict[str, Any]) -> list[CandidateView]:
        if "priority_route_blocker_lines" not in debt:
            debt = {
                **debt,
                "priority_route_blocker_lines": self.priority_route_blocker_lines(debt),
            }
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
                else (*ASSEMBLY_DEPOT, *self.safe_temp_targets(source=line, debt=debt))
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
                        debt=debt,
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
                    if not self.target_allowed(target, batch):
                        continue
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
                        self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=1),
                        "clear_prefix_blocker",
                    )

    def route_blocker_candidates(
        self,
        debt: dict[str, Any],
        *,
        include_put_blockers: bool = False,
    ) -> Iterable[CandidateView]:
        by_no = self.by_no()
        for blocker_line, reason in self.route_blocker_requests(debt, include_put_blockers=include_put_blockers):
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
                        self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=1),
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
                    if not self.target_allowed(target, batch):
                        continue
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
                        self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=2),
                        "stage_boundary_cleanup",
                    )

    def route_blocker_requests(
        self,
        debt: dict[str, Any],
        *,
        include_put_blockers: bool = False,
    ) -> list[tuple[str, str]]:
        by_no = self.by_no()
        seeds: list[tuple[str, set[str], str, int]] = []
        requests: list[tuple[str, str]] = []
        seen_requests: set[tuple[str, str]] = set()
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if car:
                seeds.append((car["Line"], {no}, f"clear_route_blocker_for:{no}", 0))
                if include_put_blockers:
                    for target in self.official_stage1_targets(car):
                        for blocker_line in self.route_blockers_for_put(car["Line"], target, {no}):
                            if blocker_line in FORBIDDEN_LINES or blocker_line == car["Line"]:
                                continue
                            request_key = (blocker_line, f"clear_put_route_blocker_for:{no}")
                            if request_key not in seen_requests:
                                seen_requests.add(request_key)
                                requests.append(request_key)
        for no in debt["pollution_nos"]:
            car = by_no.get(no)
            if car:
                seeds.append((car["Line"], {no}, f"clear_route_blocker_for_pollution:{no}", 0))

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
        blockers, _status = self.route_blocker_status_for_get(line, moving_nos)
        return blockers

    def route_blocker_status_for_get(self, line: str, moving_nos: set[str]) -> tuple[tuple[str, ...], str]:
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
            return (), "open"

        endpoints = {self.loco.line, line}
        candidate_paths = self.get_static_access_paths(line)
        if not candidate_paths:
            return (), "missing_static_path"
        blocked_paths: list[tuple[int, int, list[str]]] = []
        for path in candidate_paths:
            blockers = [node for node in path if node in occupied and node not in endpoints]
            if blockers:
                blocked_paths.append((len(blockers), len(path), blockers))
        if not blocked_paths:
            return (), "non_occupancy_route_blocked"
        _count, _length, blockers = min(blocked_paths)
        return tuple(dict.fromkeys(blockers)), "occupied_blockers"

    def route_blockers_for_put(self, source: str, target: str, moving_nos: set[str]) -> tuple[str, ...]:
        occupied = physical.occupied_lines_for_route(self.cars, moving_nos)
        train_length = physical.train_length_for_nos(self.cars, moving_nos)
        available = self.graph.route_avoiding_occupied(
            source,
            target,
            occupied,
            source_departure_lines=physical.route_departure_lines_for_source(source, self.cars, moving_nos),
            target_approach_lines=physical.route_approach_lines_for_put(target, self.cars, moving_nos),
            cars=self.cars,
            moving_nos=moving_nos,
            train_length_m=train_length,
        )
        if available:
            return ()

        endpoints = {source, target}
        blockers: list[str] = []
        for path in self.get_static_put_paths(source, target):
            for node in path:
                if node in occupied and node not in endpoints and node not in blockers:
                    blockers.append(node)
            if blockers:
                break
        return tuple(blockers)

    def get_static_put_paths(self, source: str, target: str) -> list[list[str]]:
        approaches = sorted(physical.route_approach_lines_for_put(target, self.cars, set()))
        if not approaches:
            route = self.graph.route(source, target)
            return [route] if route else []
        paths: list[list[str]] = []
        for approach in approaches:
            if approach == source:
                paths.append([source, target])
                continue
            route = self.graph.route_avoiding_occupied(source, approach, {target})
            if route:
                paths.append([*route, target])
        return paths

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
                for target in ASSEMBLY_DEPOT:
                    if target == line:
                        continue
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
                        self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=0, reason_rank=3),
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
        # build_planlet_candidate inserts a Weigh step before Put when the carried tail car needs weighing.
        # Hard business/physical rules such as closed-door ordering, pull limits, route length, 15m locomotive
        # allowance, and forced spotting are owned by physical.validate_candidate.
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
        debt: dict[str, Any],
        moved_g: int,
        moved_x: int,
        reason_rank: int,
    ) -> tuple[Any, ...]:
        return (
            self.pollution_rank(candidate, debt),
            self.stage_boundary_target_penalty(candidate, debt),
            self.route_unlock_rank(candidate, debt),
            self.candidate_source_rank(candidate),
            reason_rank,
            self.candidate_target_rank(candidate),
            -moved_g,
            -moved_x if moved_g == 0 else moved_x,
            self.route_price(candidate),
            len(candidate.move_car_nos),
            candidate.candidate_id,
        )

    def pollution_rank(self, candidate: physical.HookCandidate, debt: dict[str, Any]) -> int:
        if not debt["pollution_nos"]:
            return 0
        reason = candidate.generation_reason
        if reason == "stage_boundary_cleanup":
            return 0
        if reason.startswith("clear_route_blocker_for_pollution:"):
            return 1
        return 2

    def stage_boundary_target_penalty(self, candidate: physical.HookCandidate, debt: dict[str, Any]) -> int:
        if not debt["pollution_nos"]:
            return 0
        return 1 if candidate.target_line in ASSEMBLY_DEPOT else 0

    def route_unlock_rank(self, candidate: physical.HookCandidate, debt: dict[str, Any]) -> int:
        priority_blockers = set(debt.get("priority_route_blocker_lines") or ())
        if not priority_blockers:
            return 0
        if candidate.generation_reason.startswith("clear_route_blocker_for:"):
            return 0 if candidate.source_line in priority_blockers else 1
        moving = set(candidate.move_car_nos)
        if moving.intersection(debt["pending_stage1_nos"]):
            return 0
        return 1

    def priority_route_blocker_lines(self, debt: dict[str, Any]) -> list[str]:
        return list(self.pending_route_blocker_map(debt))

    def progress_tuple(self, debt: dict[str, Any]) -> tuple[int, int]:
        return debt["debt_count"], debt["blocked_g_count"]

    def pending_get_blocker_info(self, debt: dict[str, Any]) -> tuple[set[str], set[str]]:
        blocker_map = self.pending_route_blocker_map(debt)
        blocked_nos = {
            no
            for nos in blocker_map.values()
            for no in nos
        }
        return set(blocker_map), blocked_nos

    def pending_route_blocker_map(self, debt: dict[str, Any]) -> dict[str, list[str]]:
        by_no = self.by_no()
        blocker_map: dict[str, list[str]] = {}
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if not car:
                continue
            for blocker_line in self.route_blockers_for_get(car["Line"], {no}):
                blocker_map.setdefault(blocker_line, []).append(no)
        return blocker_map

    def pending_put_blocker_info(self, debt: dict[str, Any]) -> tuple[set[str], set[str]]:
        blocker_map: dict[str, set[str]] = {}
        by_no = self.by_no()
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if not car:
                continue
            for target in self.official_stage1_targets(car):
                for blocker_line in self.route_blockers_for_put(car["Line"], target, {no}):
                    if blocker_line in FORBIDDEN_LINES or blocker_line == car["Line"]:
                        continue
                    blocker_map.setdefault(blocker_line, set()).add(no)
        blocked_nos = {
            no
            for nos in blocker_map.values()
            for no in nos
        }
        return set(blocker_map), blocked_nos

    def route_price(self, candidate: physical.HookCandidate) -> int:
        static = self.graph.route(candidate.source_line, candidate.target_line)
        hot = {"渡10", "联7", "机北1", "机北2", "预修线", "调梁线北", "存5线北"}
        return len(static) + sum(3 for line in static if line in hot)

    def depot_assembly_capacity_deficit_m(self) -> float:
        required = sum(
            physical.car_length(car)
            for car in self.cars
            if self.stage1_goal(car) == "depot_assembly"
        )
        capacity = sum(physical.TRACK_SPECS[line].length_m for line in ASSEMBLY_DEPOT)
        return max(0.0, required - capacity - physical.LINE_LENGTH_TOLERANCE_M)

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
            return target in ASSEMBLY_DEPOT
        return False

    def official_stage1_targets(self, car: dict[str, Any]) -> tuple[str, ...]:
        goal = self.stage1_goal(car)
        if goal == "存4线":
            return ("存4线",)
        if goal == "depot_assembly":
            return ASSEMBLY_DEPOT
        return ()

    def stage1_goal(self, car: dict[str, Any]) -> str:
        if car.get("_InitialLine") in STAGE1_INITIAL_DONE_LINES:
            return ""
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
        return car["Line"] in ASSEMBLY_DEPOT

    def allowed_on_stage_line(self, car: dict[str, Any], line: str) -> bool:
        goal = self.stage1_goal(car)
        if line == "存4线":
            return goal == "存4线"
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
            pending_after: list[bool] = [False] * len(ordered)
            seen_pending = False
            for index in range(len(ordered) - 1, -1, -1):
                pending_after[index] = seen_pending
                if ordered[index] in pending_set:
                    seen_pending = True
            seen_blocker = False
            for index, no in enumerate(ordered):
                car = by_no.get(no)
                if not car:
                    continue
                if no in pending_set:
                    if seen_blocker:
                        blocked_g += 1
                elif pending_after[index]:
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
        business_hooks = sum(1 for row in self.operations if row.get("Action") in {"Get", "Put"})
        summary = {
            "case_id": self.case_id,
            "status": "complete" if debt["complete"] else "partial",
            "hooks": self.hook_index - 1,
            "move_batches": self.hook_index - 1,
            "business_hooks": business_hooks,
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
        deficit = self.depot_assembly_capacity_deficit_m()
        if deficit > 0:
            reasons.append(f"assembly_capacity_deficit_m:{deficit:.1f}")
        if self.trace and self.trace[-1].get("reason") == "no_valid_candidate":
            counter = Counter(
                violation
                for item in self.trace[-1].get("rejected", [])
                for violation in item.get("violations", [])
            )
            reasons.extend(f"{key}:{count}" for key, count in counter.most_common(8))
        if self.trace and self.trace[-1].get("reason") == "assembly_capacity_impossible":
            reasons.append("assembly_capacity_impossible")
        if self.trace and self.trace[-1].get("reason") == "no_stage1_progress":
            reasons.append("no_stage1_progress")
        if self.trace and self.trace[-1].get("reason") == "no_stage1_window_progress":
            reasons.append("no_stage1_window_progress")
        if self.trace and self.trace[-1].get("reason") == "solve_time_budget_exhausted":
            reasons.append("solve_time_budget_exhausted")
        unknown_get_blocks = self.unknown_get_route_blocks(debt)
        if unknown_get_blocks:
            counts = Counter(status for _no, _line, status in unknown_get_blocks)
            reasons.extend(f"{status}:{count}" for status, count in counts.most_common())
        return reasons

    def unknown_get_route_blocks(self, debt: dict[str, Any]) -> list[tuple[str, str, str]]:
        by_no = self.by_no()
        unknowns: list[tuple[str, str, str]] = []
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if not car:
                continue
            blockers, status = self.route_blocker_status_for_get(car["Line"], {no})
            if not blockers and status not in {"open", "occupied_blockers"}:
                unknowns.append((no, car["Line"], status))
        return unknowns

    def active_lines(self) -> list[str]:
        return sorted({car["Line"] for car in self.cars if car["Line"]}, key=lambda line: (self.source_rank(line), line))

    def source_rank(self, line: str) -> int:
        return HOT_SOURCE_RANK.get(line, 50)

    def candidate_source_rank(self, candidate: physical.HookCandidate) -> int:
        rank = self.source_rank(candidate.source_line)
        moving = set(candidate.move_car_nos)
        original_pending_exists = self.has_original_pending_stage1(moving)
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

    def has_original_pending_stage1(self, excluded_nos: set[str] | None = None) -> bool:
        excluded_nos = excluded_nos or set()
        return any(
            self.stage1_goal(car)
            and not self.stage1_car_complete(car)
            and physical.car_no(car) not in excluded_nos
            and car["Line"] == car.get("_InitialLine")
            for car in self.cars
        )

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


def solve_one(
    path: Path,
    out_dir: Path,
    *,
    max_hooks: int,
    time_budget_seconds: float,
    verbose: bool = False,
) -> dict[str, Any]:
    solver = Stage1Solver(path, max_hooks=max_hooks, time_budget_seconds=time_budget_seconds)
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
            f"business_hooks={summary['business_hooks']} "
            f"move_batches={summary['hooks']} debt={summary['stage1_debt']['debt_count']}",
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
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--limit", type=int, default=0, help="limit number of cases for directory input")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = case_files(args.input)
    if args.limit:
        files = files[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in files:
        try:
            summaries.append(
                solve_one(
                    path,
                    args.out,
                    max_hooks=args.max_hooks,
                    time_budget_seconds=args.time_budget_seconds,
                    verbose=args.input.is_dir(),
                )
            )
        except Exception as exc:  # keep directory batches diagnosable when one case is malformed
            try:
                case_id = physical.case_id_from_path(path)
            except Exception:
                case_id = path.stem
            summary = {
                "case_id": case_id,
                "status": "error",
                "hooks": 0,
                "move_batches": 0,
                "business_hooks": 0,
                "operations": 0,
                "stage1_debt": {"complete": False, "debt_count": 0},
                "blocking_reasons": [f"solver_exception:{type(exc).__name__}:{exc}"],
            }
            summaries.append(summary)
            write_json(args.out / f"{case_id}_summary.json", summary)
            if args.input.is_dir():
                print(f"{case_id} error {type(exc).__name__}: {exc}", flush=True)
    aggregate = {
        "cases": len(summaries),
        "complete": sum(1 for item in summaries if item["status"] == "complete"),
        "partial": sum(1 for item in summaries if item["status"] == "partial"),
        "error": sum(1 for item in summaries if item["status"] == "error"),
        "avg_hooks": round(sum(item["hooks"] for item in summaries) / len(summaries), 3) if summaries else 0,
        "avg_business_hooks": round(sum(item["business_hooks"] for item in summaries) / len(summaries), 3) if summaries else 0,
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps({k: v for k, v in aggregate.items() if k != "summaries"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
