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
STORAGE_CACHE_LINES = {"存1线", "存2线", "存3线", "存5线北", "存5线南"}
REAL_TARGET_STATIC_LINES = {"机库线", "预修线", "油漆线", "洗罐站", "抛丸线", "调梁棚"}
STAGE3_TEMPLATE_B_ORDER = ("机走北", "机走棚", "洗油北", "机南")
STAGE3_TEMPLATE_A_FIRST_ORDER = ("机走北", "机走棚", "机南")
STAGE3_TEMPLATE_A_SECOND_LINE = "洗油北"
STAGE4_IGNORED_TARGET_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
HIGH_VALUE_STAGE4_TARGETS = {"预修线", "调梁棚", "调梁线北"}
DIRECT_DELIVERY_TARGETS = ("机库线", "油漆线", "抛丸线", "洗罐站", "预修线", "调梁棚")
DIRECT_DELIVERY_MAX_STATIC_ROUTE_LEN = 10
DIRECT_DELIVERY_PRIORITY_MIN_BATCH = 2
STAGE4_EARLY_RELEASE_TARGETS = (
    "抛丸线",
    "洗罐站",
    "洗罐线北",
    "油漆线",
    "调梁棚",
    "机库线",
)
STAGE4_EARLY_RELEASE_SOURCE_LINES = (
    "存5线北",
    "存5线南",
    "存3线",
    "存2线",
    "存1线",
    "调梁棚",
    "预修线",
    "卸轮线",
    "油漆线",
    "洗罐线北",
    "洗罐站",
    "抛丸线",
    "机库线",
    "机走棚",
    "机走北",
    "洗油北",
)
STAGE4_EARLY_RELEASE_MAX_STATIC_ROUTE_LEN = 12
SATISFIED_RETURN_MAX_HOOKS = 2
STAGE4_LAYOUT_QUALITY_BASE_LINES = (
    "洗罐站",
    "油漆线",
    "调梁棚",
    "存1线",
    "存2线",
    "存3线",
    "存5线南",
    "存5线北",
)
STAGE4_LAYOUT_QUALITY_CONDITIONAL_LINES = ("机库线",)
STAGE4_CACHE_GROUP_TARGETS = {
    "机库线",
    "预修线",
    "油漆线",
    "洗罐站",
    "抛丸线",
    "调梁棚",
    "调梁线北",
    "机走棚",
    "存1线",
    "存2线",
    "存3线",
    "存5线南",
}
STAGE4_CACHE_GROUP_LINES = ("存5线北", "存3线", "存2线", "存1线", "存5线南")
PROFILE_CONFIGS: dict[str, dict[str, Any]] = {
    "baseline": {
        "downstream": False,
        "target_aware": False,
        "direct_delivery": False,
        "direct_delivery_max_hooks": 0,
        "early_release": False,
        "early_release_max_hooks": 0,
        "quality_order": "none",
    },
    "balanced": {
        "downstream": True,
        "target_aware": True,
        "direct_delivery": True,
        "direct_delivery_max_hooks": 5,
        "early_release": True,
        "early_release_max_hooks": 4,
        "quality_order": "balanced",
    },
    "stage3": {
        "downstream": True,
        "target_aware": True,
        "direct_delivery": True,
        "direct_delivery_max_hooks": 5,
        "early_release": True,
        "early_release_max_hooks": 4,
        "quality_order": "stage3",
    },
    "stage4": {
        "downstream": True,
        "target_aware": True,
        "direct_delivery": True,
        "direct_delivery_max_hooks": 5,
        "early_release": True,
        "early_release_max_hooks": 4,
        "quality_order": "stage4",
    },
}
DEFAULT_PROFILE = "baseline"
DEFAULT_PORTFOLIO_PROFILES = ("baseline", "balanced", "stage3", "stage4")
PORTFOLIO_OBJECTIVES = ("balanced", "stage3", "stage4")
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
        profile: str = DEFAULT_PROFILE,
    ) -> None:
        if profile not in PROFILE_CONFIGS:
            raise ValueError(f"unknown_stage1_profile:{profile}")
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
        self.profile = profile
        self.downstream_quality_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.initial_stage4_satisfied_nos = {
            physical.car_no(car)
            for car in self.cars
            if self.stage4_target_key(car) and self.target_satisfied(car)
        }

    def solve(self) -> dict[str, Any]:
        no_progress_streak = 0
        stale_best_streak = 0
        best_progress = (10**9, 10**9)
        while self.hook_index <= self.max_hooks:
            before = self.stage1_debt()
            if before["complete"]:
                if self.try_satisfied_return_cleanup(before):
                    no_progress_streak = 0
                    stale_best_streak = 0
                    continue
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
            if self.disallow_profile_sideways_stage1_move(view.candidate, debt, after_debt):
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["profile_sideways_stage1_move"],
                })
                continue
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
            downstream_quality = probe.downstream_quality_tuple()
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
                    self.disallow_profile_sideways_stage1_move(view.candidate, debt, after_debt)
                    and not unlocks_get
                ):
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
                    downstream_quality,
                    self.real_target_dynamic_rank(view.candidate, debt, after_debt),
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
                direct_delivery_priority = (
                    not put_blockers
                    and self.direct_delivery_priority_candidate(view.candidate, debt, after_debt)
                )
                if (
                    self.disallow_profile_sideways_stage1_move(view.candidate, debt, after_debt)
                    and not unlocks_put
                ):
                    valid_count += 1
                    if valid_count >= DYNAMIC_VALID_WINDOW or examined_count >= DYNAMIC_EXAMINED_WINDOW:
                        break
                    continue
                primary = (
                    0
                    if after_debt["complete"]
                    else 1
                    if direct_delivery_priority
                    else 2
                    if unlocks_put
                    else 3
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
                    downstream_quality,
                    self.real_target_dynamic_rank(view.candidate, debt, after_debt),
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
        self.trace[-1]["downstream_quality"] = list(dynamic_score[5]) if len(dynamic_score) > 5 else []
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

    def real_target_dynamic_rank(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> int:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return 1
        if after_debt["debt_count"] > debt["debt_count"]:
            return 1
        return 0 if self.clean_real_target_put(candidate) else 1

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

    def disallow_profile_sideways_stage1_move(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return False
        if self.progress_tuple(after_debt) < self.progress_tuple(debt):
            return False
        by_no = self.by_no()
        for no in candidate.move_car_nos:
            car = by_no.get(no)
            if not car or not self.stage1_goal(car):
                continue
            if not self.official_stage1_target(car, candidate.target_line):
                return True
        return False

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

    def try_satisfied_return_cleanup(self, debt: dict[str, Any]) -> bool:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return False
        if debt["pollution_nos"]:
            return False
        if self.satisfied_return_hooks_used() >= SATISFIED_RETURN_MAX_HOOKS:
            return False
        views = sorted(self.satisfied_return_candidates(debt), key=lambda item: item.score)
        rejected: list[dict[str, Any]] = []
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
            after_debt = probe.stage1_debt()
            if not after_debt["complete"]:
                rejected.append({
                    "candidate_id": view.candidate.candidate_id,
                    "reason": view.reason,
                    "violations": ["satisfied_return_reopens_stage1_debt"],
                })
                continue
            accepted = self.accept_candidate(view, validation, after_debt, debt, rejected)
            self.trace[-1]["post_stage1_cleanup"] = True
            return accepted
        return False

    def satisfied_return_hooks_used(self) -> int:
        return sum(
            1
            for item in self.trace
            if str(item.get("reason") or "").startswith("stage1_satisfied_return:")
        )

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
        clone.profile = self.profile
        clone.downstream_quality_cache = self.downstream_quality_cache
        clone.initial_stage4_satisfied_nos = set(self.initial_stage4_satisfied_nos)
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
        candidates.extend(self.satisfied_return_candidates(debt))
        candidates.extend(self.direct_delivery_candidates(debt))
        candidates.extend(self.stage4_early_release_candidates(debt))
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

    def direct_delivery_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not PROFILE_CONFIGS[self.profile]["direct_delivery"]:
            return
        if self.direct_delivery_hooks_used() >= int(PROFILE_CONFIGS[self.profile]["direct_delivery_max_hooks"]):
            return
        if debt["pollution_nos"]:
            return
        ready_targets = {
            target: self.direct_delivery_target_ready(target)
            for target in DIRECT_DELIVERY_TARGETS
        }
        if not any(ready_targets.values()):
            return
        by_no = self.by_no()
        for line in self.active_lines():
            if line in FORBIDDEN_LINES:
                continue
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            target = ""
            max_batch: list[dict[str, Any]] = []
            for car in ordered:
                car_target = self.direct_delivery_target_for_car(car)
                if not car_target or car_target == line or not ready_targets.get(car_target):
                    break
                if target and car_target != target:
                    break
                trial = [*max_batch, car]
                if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                target = car_target
                max_batch = trial
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    break
            if not target or not max_batch:
                continue
            for batch in self.prefix_options(max_batch):
                if not self.direct_delivery_batch_allowed(target, batch):
                    continue
                if not self.target_allowed(target, batch):
                    continue
                if not self.direct_delivery_route_low_cost(line, target):
                    continue
                candidate = self.make_candidate(
                    source=line,
                    target=target,
                    batch=batch,
                    kind="vnext_stage1_direct_delivery",
                    reason=f"stage1_direct_delivery:{target}",
                )
                if not candidate:
                    continue
                yield CandidateView(
                    candidate,
                    self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=4),
                    f"stage1_direct_delivery:{target}",
                )

    def stage4_early_release_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not PROFILE_CONFIGS[self.profile].get("early_release"):
            return
        if self.stage4_early_release_hooks_used() >= int(PROFILE_CONFIGS[self.profile]["early_release_max_hooks"]):
            return
        if debt["pollution_nos"]:
            return
        ready_targets = {
            target: self.stage4_early_release_target_ready(target)
            for target in STAGE4_EARLY_RELEASE_TARGETS
        }
        if not any(ready_targets.values()):
            return
        by_no = self.by_no()
        source_lines = [line for line in STAGE4_EARLY_RELEASE_SOURCE_LINES if any(car["Line"] == line for car in self.cars)]
        for line in source_lines:
            if line in FORBIDDEN_LINES:
                continue
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            target = ""
            max_batch: list[dict[str, Any]] = []
            for car in ordered:
                car_target = self.stage4_early_release_target_for_car(car)
                if not car_target or car_target == line or not ready_targets.get(car_target):
                    break
                if target and car_target != target:
                    break
                trial = [*max_batch, car]
                if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                target = car_target
                max_batch = trial
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    break
            if not target or not max_batch:
                continue
            for batch in self.prefix_options(max_batch):
                if not self.stage4_early_release_batch_allowed(target, batch):
                    continue
                if not self.target_allowed(target, batch):
                    continue
                if not self.stage4_early_release_route_low_cost(line, target):
                    continue
                candidate = self.make_candidate(
                    source=line,
                    target=target,
                    batch=batch,
                    kind="vnext_stage1_direct_delivery",
                    reason=f"stage1_early_release:{target}",
                )
                if not candidate:
                    continue
                yield CandidateView(
                    candidate,
                    self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=4),
                    f"stage1_early_release:{target}",
                )

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

    def satisfied_return_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return
        if debt["pollution_nos"]:
            return
        by_no = self.by_no()
        for line in self.active_lines():
            if line in FORBIDDEN_LINES or line in ASSEMBLY_ALL:
                continue
            ordered = [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]
            if not ordered:
                continue
            target = ""
            max_batch: list[dict[str, Any]] = []
            for car in ordered:
                car_target = self.satisfied_return_target_for_car(car)
                if not car_target or car_target == line:
                    break
                if target and car_target != target:
                    break
                if not self.real_target_ready(car_target):
                    break
                trial = [*max_batch, car]
                if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                target = car_target
                max_batch = trial
            if not target or not max_batch:
                continue
            for batch in self.prefix_options(max_batch):
                if not all(self.satisfied_return_target_for_car(car) == target for car in batch):
                    continue
                if not self.target_allowed(target, batch):
                    continue
                candidate = self.make_candidate(
                    source=line,
                    target=target,
                    batch=batch,
                    kind="vnext_stage1_satisfied_return",
                    reason=f"stage1_satisfied_return:{target}",
                )
                if not candidate:
                    continue
                yield CandidateView(
                    candidate,
                    self.score_candidate(candidate, debt=debt, moved_g=0, moved_x=len(batch), reason_rank=3),
                    f"stage1_satisfied_return:{target}",
                )

    def blocker_targets(self, batch: list[dict[str, Any]], *, source: str, debt: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        direct = self.common_current_target(batch)
        if (
            direct
            and direct not in FORBIDDEN_LINES
            and direct != source
            and self.stage1_real_target_available(direct)
            and not self.would_pollute_stage_boundary(direct, batch)
        ):
            targets.append(direct)
        targets.extend(self.grouped_cache_targets(batch, source=source, debt=debt))
        targets.extend(self.safe_temp_targets(source=source, debt=debt))
        return list(dict.fromkeys(targets))

    def grouped_cache_targets(self, batch: list[dict[str, Any]], *, source: str, debt: dict[str, Any]) -> list[str]:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return []
        target = self.common_stage4_cache_target(batch)
        if not target:
            return []
        blocked_lines = set(debt["lines_with_pending_stage1"])
        moving_nos = {physical.car_no(car) for car in batch}
        ranked: list[tuple[tuple[int, int, int, int, int], str]] = []
        for line in STAGE4_CACHE_GROUP_LINES:
            if line == source or line in blocked_lines or line in FORBIDDEN_LINES:
                continue
            if not self.target_allowed(line, batch):
                continue
            ranked.append((self.cache_line_rank_for_target(line, target, moving_nos), line))
        ranked.sort()
        return [line for _rank, line in ranked]

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

    def common_stage4_cache_target(self, batch: list[dict[str, Any]]) -> str:
        targets: list[str] = []
        for car in batch:
            if self.stage1_goal(car):
                return ""
            target = self.stage4_target_key(car)
            if target not in STAGE4_CACHE_GROUP_TARGETS:
                return ""
            targets.append(target)
        return targets[0] if targets and all(target == targets[0] for target in targets) else ""

    def cache_line_rank_for_target(
        self,
        line: str,
        target: str,
        moving_nos: set[str],
    ) -> tuple[int, int, int, int, int]:
        existing_targets = [
            self.stage4_target_key(car)
            for car in self.cars
            if car["Line"] == line
            and physical.car_no(car) not in moving_nos
            and self.is_stage4_debt(car)
        ]
        target_types = {item for item in existing_targets if item}
        if not existing_targets:
            group_state = 2
        elif target_types == {target}:
            group_state = 0
        elif target in target_types:
            group_state = 1
        else:
            group_state = 3
        new_target_type = 0 if target in target_types or not target_types else 1
        mixed_count = len(target_types - {target})
        current_load = len(existing_targets)
        storage_bias = STAGE4_CACHE_GROUP_LINES.index(line) if line in STAGE4_CACHE_GROUP_LINES else 99
        return (group_state, new_target_type, mixed_count, current_load, storage_bias)

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
            self.tail_target_rank(candidate),
            self.real_target_static_rank(candidate),
            self.cache_group_rank(candidate),
            self.candidate_target_rank(candidate),
            self.satisfied_break_penalty(candidate),
            -moved_g,
            -moved_x if moved_g == 0 else moved_x,
            self.route_price(candidate),
            len(candidate.move_car_nos),
            candidate.candidate_id,
        )

    def tail_target_rank(self, candidate: physical.HookCandidate) -> int:
        if not PROFILE_CONFIGS[self.profile]["target_aware"]:
            return 1
        moved = [car for car in self.cars if physical.car_no(car) in set(candidate.move_car_nos)]
        if not moved:
            return 1
        if any(self.stage1_goal(car) for car in moved):
            return 1
        direct = [car for car in moved if self.stage4_target_line_satisfied_by(car, candidate.target_line)]
        if len(direct) == len(moved):
            return 0 if candidate.target_line in HIGH_VALUE_STAGE4_TARGETS else 1
        if direct:
            return 2
        return 3

    def real_target_static_rank(self, candidate: physical.HookCandidate) -> int:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return 1
        return 0 if self.safe_static_real_target_put(candidate) else 1

    def clean_real_target_put(self, candidate: physical.HookCandidate) -> bool:
        moving_nos = set(candidate.move_car_nos)
        if not moving_nos:
            return False
        if not self.stage1_real_target_available(candidate.target_line):
            return False
        by_no = self.by_no()
        moved = [by_no[no] for no in candidate.move_car_nos if no in by_no]
        if len(moved) != len(candidate.move_car_nos):
            return False
        if any(self.stage1_goal(car) for car in moved):
            return False
        if any(not self.stage4_target_line_satisfied_by(car, candidate.target_line) for car in moved):
            return False
        existing = [
            car
            for car in self.cars
            if car["Line"] == candidate.target_line and physical.car_no(car) not in moving_nos
        ]
        return all(self.target_satisfied(car) and not physical.force_positions(car) for car in existing)

    def safe_static_real_target_put(self, candidate: physical.HookCandidate) -> bool:
        if candidate.candidate_kind not in {
            "blocker_relocation",
            "vnext_stage1_direct_delivery",
            "vnext_stage1_satisfied_return",
        }:
            return False
        if candidate.target_line not in REAL_TARGET_STATIC_LINES:
            return False
        if not self.stage1_real_target_available(candidate.target_line):
            return False
        if not self.clean_real_target_put(candidate):
            return False
        if len(candidate.move_car_nos) >= 2:
            return True
        return candidate.source_line not in STORAGE_CACHE_LINES

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

    def satisfied_break_penalty(self, candidate: physical.HookCandidate) -> int:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return 0
        by_no = self.by_no()
        penalty = 0
        for no in candidate.move_car_nos:
            car = by_no.get(no)
            if not car or not self.protected_satisfied_car(car):
                continue
            if self.stage4_target_line_satisfied_by(car, candidate.target_line):
                continue
            penalty += 1
        return penalty

    def protected_satisfied_car(self, car: dict[str, Any]) -> bool:
        if car["Line"] in ASSEMBLY_DEPOT:
            return False
        return bool(self.stage4_target_key(car)) and self.target_satisfied(car)

    def cache_group_rank(self, candidate: physical.HookCandidate) -> tuple[int, int, int, int, int]:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return (0, 0, 0, 0, 0)
        if candidate.target_line not in CACHE_LINES:
            return (0, 0, 0, 0, 0)
        by_no = self.by_no()
        batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
        target = self.common_stage4_cache_target(batch)
        if not target:
            return (9, 0, 0, 0, 0)
        return self.cache_line_rank_for_target(candidate.target_line, target, set(candidate.move_car_nos))

    def direct_delivery_target_for_car(self, car: dict[str, Any]) -> str:
        if self.stage1_goal(car) or self.target_satisfied(car):
            return ""
        if physical.force_positions(car):
            return ""
        if car.get("IsWeigh") and not car.get("_Weighed"):
            return ""
        targets = set(car.get("TargetLines") or ())
        for target in DIRECT_DELIVERY_TARGETS:
            if not self.stage1_real_target_available(target):
                continue
            if target in targets and self.stage4_target_line_satisfied_by(car, target):
                return target
        return ""

    def satisfied_return_target_for_car(self, car: dict[str, Any]) -> str:
        no = physical.car_no(car)
        if no not in self.initial_stage4_satisfied_nos:
            return ""
        if self.stage1_goal(car) or self.target_satisfied(car):
            return ""
        if physical.force_positions(car):
            return ""
        target = self.stage4_target_key(car)
        if target not in REAL_TARGET_STATIC_LINES:
            return ""
        if not self.stage1_real_target_available(target):
            return ""
        return target if self.stage4_target_line_satisfied_by(car, target) else ""

    def direct_delivery_target_ready(self, target: str) -> bool:
        if target not in DIRECT_DELIVERY_TARGETS:
            return False
        if not self.stage1_real_target_available(target):
            return False
        return self.real_target_ready(target)

    def real_target_ready(self, target: str) -> bool:
        if not self.stage1_real_target_available(target):
            return False
        existing = [car for car in self.cars if car["Line"] == target]
        return all(self.target_satisfied(car) and not physical.force_positions(car) for car in existing)

    def stage1_real_target_available(self, target: str) -> bool:
        if target != physical.WEIGH_LINE:
            return True
        return not self.has_pending_weigh_cars()

    def has_pending_weigh_cars(self) -> bool:
        return any(self.pending_weigh(car) for car in self.cars)

    def pending_weigh_count(self) -> int:
        return sum(1 for car in self.cars if self.pending_weigh(car))

    def pending_weigh(self, car: dict[str, Any]) -> bool:
        return bool(car.get("IsWeigh")) and not bool(car.get("_Weighed"))

    def direct_delivery_batch_allowed(self, target: str, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return False
        return all(self.direct_delivery_target_for_car(car) == target for car in batch)

    def direct_delivery_route_low_cost(self, source: str, target: str) -> bool:
        route = self.graph.route(source, target)
        return bool(route and len(route) <= DIRECT_DELIVERY_MAX_STATIC_ROUTE_LEN)

    def stage4_early_release_target_for_car(self, car: dict[str, Any]) -> str:
        if self.stage1_goal(car) or self.target_satisfied(car):
            return ""
        if physical.force_positions(car):
            return ""
        if car.get("IsWeigh") and not car.get("_Weighed"):
            return ""
        targets = set(car.get("TargetLines") or ())
        for target in STAGE4_EARLY_RELEASE_TARGETS:
            if target not in targets:
                continue
            if not self.stage4_early_release_target_ready(target):
                continue
            if self.stage4_target_line_satisfied_by(car, target):
                return target
        return ""

    def stage4_early_release_target_ready(self, target: str) -> bool:
        if target not in STAGE4_EARLY_RELEASE_TARGETS:
            return False
        if not self.stage1_real_target_available(target):
            return False
        return self.real_target_ready(target)

    def stage4_early_release_batch_allowed(self, target: str, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return False
        return all(self.stage4_early_release_target_for_car(car) == target for car in batch)

    def stage4_early_release_route_low_cost(self, source: str, target: str) -> bool:
        route = self.graph.route(source, target)
        return bool(route and len(route) <= STAGE4_EARLY_RELEASE_MAX_STATIC_ROUTE_LEN)

    def is_direct_delivery_candidate(self, candidate: physical.HookCandidate) -> bool:
        return candidate.candidate_kind == "vnext_stage1_direct_delivery"

    def direct_delivery_priority_candidate(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        return (
            self.is_direct_delivery_candidate(candidate)
            and len(candidate.move_car_nos) >= DIRECT_DELIVERY_PRIORITY_MIN_BATCH
            and self.progress_tuple(after_debt) <= self.progress_tuple(debt)
        )

    def direct_delivery_hooks_used(self) -> int:
        return sum(
            1
            for item in self.trace
            if str(item.get("reason") or "").startswith("stage1_direct_delivery:")
        )

    def stage4_early_release_hooks_used(self) -> int:
        return sum(
            1
            for item in self.trace
            if str(item.get("reason") or "").startswith("stage1_early_release:")
        )

    def downstream_quality_tuple(self) -> tuple[int, ...]:
        if not PROFILE_CONFIGS[self.profile]["downstream"]:
            return ()
        quality = self.downstream_quality()
        order = PROFILE_CONFIGS[self.profile]["quality_order"]
        layout = (
            quality["stage4_extra_target_fragment_count"],
            quality["stage4_target_run_count"],
            -quality["stage4_south_settled_tail_count"],
        )
        if order == "stage3":
            return (
                quality["stage3_extra_fragment_count"],
                quality["stage3_group_run_count"],
                quality["stage3_prefix_blocked_count"],
                quality["stage4_access_blocked_debt_count"],
                quality["stage4_lower_bound"],
                quality["stage4_tail_debt_count"],
                *layout,
            )
        if order == "stage4":
            return (
                quality["stage4_access_blocked_debt_count"],
                quality["stage4_lower_bound"],
                quality["stage4_tail_debt_count"],
                *layout,
                quality["stage3_extra_fragment_count"],
                quality["stage3_group_run_count"],
                quality["stage3_prefix_blocked_count"],
            )
        return (
            quality["stage3_extra_fragment_count"],
            quality["stage3_group_run_count"],
            quality["stage3_prefix_blocked_count"],
            quality["stage4_access_blocked_debt_count"],
            quality["stage4_lower_bound"],
            quality["stage4_tail_debt_count"],
            *layout,
        )

    def downstream_quality(self) -> dict[str, Any]:
        cache_key = physical.state_signature(self.cars, self.loco)
        cached = self.downstream_quality_cache.get(cache_key)
        if cached is not None:
            return cached
        stage3 = self.stage3_quality()
        stage4 = self.stage4_quality()
        quality = {
            **stage3,
            **stage4,
        }
        self.downstream_quality_cache[cache_key] = quality
        return quality

    def stage3_quality(self) -> dict[str, int]:
        active_nos = {
            physical.car_no(car)
            for car in self.cars
            if car["Line"] in set(STAGE3_TEMPLATE_B_ORDER)
            and set(car.get("TargetLines") or ()) & physical.DEPOT_TARGET_LINES
        }
        if not active_nos:
            return {
                "stage3_active_count": 0,
                "stage3_group_count": 0,
                "stage3_group_run_count": 0,
                "stage3_extra_fragment_count": 0,
                "stage3_prefix_blocked_count": 0,
            }
        by_no = self.by_no()
        group_by_no = {
            no: self.depot_stage3_group_key(by_no[no])
            for no in active_nos
            if no in by_no
        }
        best: dict[str, int] | None = None
        for template in ("A", "B"):
            exposure = [no for no in self.stage3_template_exposure_order(template) if no in active_nos]
            keys = [group_by_no.get(no, "UNGROUPED") for no in exposure]
            run_keys = self.compressed(keys)
            group_counts = Counter(run_keys)
            extra_fragments = sum(max(0, count - 1) for key, count in group_counts.items() if key != "UNGROUPED")
            metric = {
                "stage3_active_count": len(active_nos),
                "stage3_group_count": len({key for key in keys if key != "UNGROUPED"}),
                "stage3_group_run_count": len(run_keys),
                "stage3_extra_fragment_count": extra_fragments,
                "stage3_prefix_blocked_count": self.stage3_prefix_blocked_count(active_nos),
            }
            if best is None or (
                metric["stage3_extra_fragment_count"],
                metric["stage3_group_run_count"],
                metric["stage3_prefix_blocked_count"],
            ) < (
                best["stage3_extra_fragment_count"],
                best["stage3_group_run_count"],
                best["stage3_prefix_blocked_count"],
            ):
                best = metric
        return best or {
            "stage3_active_count": len(active_nos),
            "stage3_group_count": 0,
            "stage3_group_run_count": 0,
            "stage3_extra_fragment_count": 0,
            "stage3_prefix_blocked_count": 0,
        }

    def stage3_template_exposure_order(self, template: str) -> tuple[str, ...]:
        if template == "A":
            first: list[str] = []
            for line in STAGE3_TEMPLATE_A_FIRST_ORDER:
                first.extend(physical.line_access_order(self.cars, line))
            second = physical.line_access_order(self.cars, STAGE3_TEMPLATE_A_SECOND_LINE)
            return (*reversed(first), *reversed(second))
        all_nos: list[str] = []
        for line in STAGE3_TEMPLATE_B_ORDER:
            all_nos.extend(physical.line_access_order(self.cars, line))
        return tuple(reversed(all_nos))

    def stage3_prefix_blocked_count(self, active_nos: set[str]) -> int:
        blocked = 0
        for line in STAGE3_TEMPLATE_B_ORDER:
            ordered = physical.line_access_order(self.cars, line)
            active_on_line = [no for no in ordered if no in active_nos]
            if not active_on_line:
                continue
            reachable_prefix = set(ordered[: len(active_on_line)])
            blocked += sum(1 for no in active_on_line if no not in reachable_prefix)
        return blocked

    def depot_stage3_group_key(self, car: dict[str, Any]) -> str:
        targets = set(car.get("TargetLines") or ())
        inner = sorted(targets & physical.DEPOT_LINES)
        outer = sorted(targets & physical.DEPOT_OUTSIDE_LINES)
        if inner and outer:
            target_part = "IO:" + "/".join((*inner, *outer))
        elif inner:
            target_part = "I:修1-4" if set(inner) == physical.DEPOT_LINES else "I:" + "/".join(inner)
        elif outer:
            target_part = "O:" + "/".join(outer)
        else:
            target_part = "D:UNKNOWN"
        repair = str(car.get("RepairProcess") or "")
        length = float(car.get("Length") or 14.3)
        if repair.startswith("厂"):
            class_part = "厂修"
        elif length >= 17.6:
            class_part = "长车"
        else:
            class_part = "段修短"
        forced = physical.force_positions(car)
        force_part = f":F{min(forced)}-{max(forced)}" if forced else ""
        return f"{target_part}:{class_part}{force_part}"

    def stage4_quality(self) -> dict[str, int]:
        by_no = self.by_no()
        debt_nos = {physical.car_no(car) for car in self.cars if self.is_stage4_debt(car)}
        source_lines = {by_no[no]["Line"] for no in debt_nos if no in by_no}
        target_keys = {self.stage4_target_key(by_no[no]) for no in debt_nos if no in by_no}
        target_keys.discard("")
        blocked_debt = 0
        for line in sorted({car["Line"] for car in self.cars if car["Line"]}):
            ordered = physical.line_access_order(self.cars, line)
            suffix_has_debt = [False] * (len(ordered) + 1)
            for index in range(len(ordered) - 1, -1, -1):
                suffix_has_debt[index] = suffix_has_debt[index + 1] or ordered[index] in debt_nos
            seen_blocker = False
            for index, no in enumerate(ordered):
                if no in debt_nos:
                    if seen_blocker:
                        blocked_debt += 1
                    continue
                if suffix_has_debt[index + 1]:
                    car = by_no.get(no)
                    if car and not self.target_satisfied(car):
                        seen_blocker = True
        return {
            "stage4_tail_debt_count": len(debt_nos),
            "stage4_access_blocked_debt_count": blocked_debt,
            "stage4_lower_bound": len(source_lines) + len(target_keys),
            **self.stage4_layout_quality(),
        }

    def stage4_layout_quality(self) -> dict[str, int]:
        by_no = self.by_no()
        lines = self.stage4_layout_quality_lines()
        south_settled_tail_count = 0
        target_run_count = 0
        extra_target_fragment_count = 0
        for line in lines:
            south_to_north = list(reversed(physical.line_access_order(self.cars, line)))
            for no in south_to_north:
                car = by_no.get(no)
                if car and self.stage4_target_key(car) and self.target_satisfied(car):
                    south_settled_tail_count += 1
                    continue
                break
            target_keys = [
                self.stage4_target_key(car)
                for no in south_to_north
                if (car := by_no.get(no)) and self.stage4_target_key(car)
            ]
            run_keys = self.compressed(target_keys)
            target_run_count += len(run_keys)
            extra_target_fragment_count += sum(
                max(0, count - 1)
                for count in Counter(run_keys).values()
            )
        return {
            "stage4_layout_quality_line_count": len(lines),
            "stage4_pending_weigh_count": self.pending_weigh_count(),
            "stage4_south_settled_tail_count": south_settled_tail_count,
            "stage4_target_run_count": target_run_count,
            "stage4_extra_target_fragment_count": extra_target_fragment_count,
        }

    def stage4_layout_quality_lines(self) -> tuple[str, ...]:
        lines = list(STAGE4_LAYOUT_QUALITY_BASE_LINES)
        if not self.has_pending_weigh_cars():
            lines.extend(STAGE4_LAYOUT_QUALITY_CONDITIONAL_LINES)
        return tuple(line for line in lines if line in physical.TRACK_SPECS)

    def is_stage4_debt(self, car: dict[str, Any]) -> bool:
        targets = set(car.get("TargetLines") or ())
        return bool(targets) and not bool(targets & STAGE4_IGNORED_TARGET_LINES) and not self.target_satisfied(car)

    def target_satisfied(self, car: dict[str, Any]) -> bool:
        targets = set(car.get("TargetLines") or ())
        if not targets:
            return True
        if car["Line"] not in targets:
            return False
        forced = physical.force_positions(car)
        if forced and int(car.get("Position") or 0) not in forced:
            return False
        if car["Line"] == "存4线" and car.get("IsClosedDoor") and int(car.get("Position") or 0) <= 3:
            return False
        return True

    def stage4_target_key(self, car: dict[str, Any]) -> str:
        targets = sorted(set(car.get("TargetLines") or ()) - STAGE4_IGNORED_TARGET_LINES)
        return targets[0] if len(targets) == 1 else "/".join(targets)

    def stage4_target_line_satisfied_by(self, car: dict[str, Any], line: str) -> bool:
        if line in STAGE4_IGNORED_TARGET_LINES:
            return False
        targets = set(car.get("TargetLines") or ())
        if line not in targets:
            return False
        forced = physical.force_positions(car)
        return not forced

    def compressed(self, items: list[str]) -> list[str]:
        out: list[str] = []
        for item in items:
            if not out or out[-1] != item:
                out.append(item)
        return out

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
            "profile": self.profile,
            "status": "complete" if debt["complete"] else "partial",
            "hooks": self.hook_index - 1,
            "move_batches": self.hook_index - 1,
            "business_hooks": business_hooks,
            "operations": len(self.operations),
            "stage1_debt": debt,
            "downstream_quality": self.downstream_quality(),
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
    profile: str,
    portfolio_profiles: tuple[str, ...],
    portfolio_objective: str,
    verbose: bool = False,
) -> dict[str, Any]:
    profiles = portfolio_profiles or (profile,)
    results: list[dict[str, Any]] = []
    per_profile_time_budget = time_budget_seconds if len(profiles) == 1 else max(5.0, time_budget_seconds / len(profiles))
    for item_profile in profiles:
        solver = Stage1Solver(
            path,
            max_hooks=max_hooks,
            time_budget_seconds=per_profile_time_budget,
            profile=item_profile,
        )
        results.append(solver.solve())
    result = min(results, key=lambda item: portfolio_selection_key(item, portfolio_objective))
    out_dir.mkdir(parents=True, exist_ok=True)
    case_id = result["summary"]["case_id"]
    if len(results) > 1:
        portfolio = {
            "case_id": case_id,
            "selected_profile": result["summary"].get("profile", ""),
            "objective": portfolio_objective,
            "profiles": [item["summary"] for item in results],
        }
        result["summary"]["portfolio_profiles"] = [item["summary"].get("profile", "") for item in results]
        result["summary"]["selected_profile"] = result["summary"].get("profile", "")
        write_json(out_dir / f"{case_id}_portfolio.json", portfolio)
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])
    if verbose:
        summary = result["summary"]
        print(
            f"{summary['case_id']} {summary['status']} "
            f"profile={summary.get('selected_profile') or summary.get('profile', '')} "
            f"business_hooks={summary['business_hooks']} "
            f"move_batches={summary['hooks']} debt={summary['stage1_debt']['debt_count']}",
            flush=True,
        )
    return result["summary"]


def portfolio_selection_key(result: dict[str, Any], objective: str = "balanced") -> tuple[Any, ...]:
    summary = result["summary"]
    debt = summary.get("stage1_debt") or {}
    quality = summary.get("downstream_quality") or {}
    status_rank = 0 if summary.get("status") == "complete" else 1
    common = (
        status_rank,
        int(debt.get("debt_count") or 0),
        int(debt.get("blocked_g_count") or 0),
        len(debt.get("pollution_nos") or ()),
    )
    tail = int(quality.get("stage4_tail_debt_count") or 0)
    lower_bound = int(quality.get("stage4_lower_bound") or 0)
    access_blocked = int(quality.get("stage4_access_blocked_debt_count") or 0)
    layout_fragments = int(quality.get("stage4_extra_target_fragment_count") or 0)
    layout_runs = int(quality.get("stage4_target_run_count") or 0)
    south_settled = int(quality.get("stage4_south_settled_tail_count") or 0)
    fragments = int(quality.get("stage3_extra_fragment_count") or 0)
    runs = int(quality.get("stage3_group_run_count") or 0)
    prefix_blocked = int(quality.get("stage3_prefix_blocked_count") or 0)
    business_hooks = int(summary.get("business_hooks") or 0)
    profile_name = str(summary.get("profile") or "")
    if objective == "stage4":
        return (
            *common,
            access_blocked,
            lower_bound,
            tail,
            layout_fragments,
            layout_runs,
            -south_settled,
            fragments,
            runs,
            prefix_blocked,
            business_hooks,
            profile_name,
        )
    if objective == "stage3":
        return (
            *common,
            fragments,
            runs,
            prefix_blocked,
            access_blocked,
            lower_bound,
            tail,
            layout_fragments,
            layout_runs,
            -south_settled,
            business_hooks,
            profile_name,
        )
    return (
        *common,
        fragments,
        runs,
        prefix_blocked,
        access_blocked,
        lower_bound,
        tail,
        layout_fragments,
        layout_runs,
        -south_settled,
        business_hooks,
        profile_name,
    )


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
    parser.add_argument("--profile", choices=sorted(PROFILE_CONFIGS), default=DEFAULT_PROFILE)
    parser.add_argument("--portfolio-objective", choices=PORTFOLIO_OBJECTIVES, default="balanced")
    parser.add_argument(
        "--portfolio-profiles",
        default="",
        help=(
            "comma-separated profiles to run and select from; use 'default' for "
            f"{','.join(DEFAULT_PORTFOLIO_PROFILES)}"
        ),
    )
    return parser.parse_args()


def parse_portfolio_profiles(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    if text == "default":
        return DEFAULT_PORTFOLIO_PROFILES
    profiles = tuple(item.strip() for item in text.split(",") if item.strip())
    unknown = [item for item in profiles if item not in PROFILE_CONFIGS]
    if unknown:
        raise ValueError(f"unknown_stage1_profiles:{','.join(unknown)}")
    return profiles


def main() -> None:
    args = parse_args()
    files = case_files(args.input)
    if args.limit:
        files = files[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    portfolio_profiles = parse_portfolio_profiles(args.portfolio_profiles)
    for path in files:
        try:
            summaries.append(
                solve_one(
                    path,
                    args.out,
                    max_hooks=args.max_hooks,
                    time_budget_seconds=args.time_budget_seconds,
                    profile=args.profile,
                    portfolio_profiles=portfolio_profiles,
                    portfolio_objective=args.portfolio_objective,
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
                "profile": args.profile,
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
        "selected_profile_counts": dict(Counter(item.get("selected_profile") or item.get("profile", "") for item in summaries)),
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps({k: v for k, v in aggregate.items() if k != "summaries"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
