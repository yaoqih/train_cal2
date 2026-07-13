#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical  # noqa: E402


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
REAL_TARGET_STATIC_LINES = {"机库线", "预修线", "油漆线", "洗罐站", "抛丸线"}
STAGE3_TEMPLATE_B_ORDER = ("机走北", "机走棚", "洗油北", "机南")
STAGE3_TEMPLATE_A_FIRST_ORDER = ("机走北", "机走棚", "机南")
STAGE3_TEMPLATE_A_SECOND_LINE = "洗油北"
STAGE4_IGNORED_TARGET_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
HIGH_VALUE_STAGE4_TARGETS = {"预修线", "调梁棚", "调梁线北"}
SERVICE_LINES = (
    "抛丸线",
    "油漆线",
    "洗罐站",
    "洗罐线北",
    "机库线",
    "调梁棚",
    "调梁线北",
    "预修线",
    "存1线",
    "存2线",
    "存3线",
    "存5线南",
    "存5线北",
)
SERVICE_LINE_SET = set(SERVICE_LINES)
IGNORED_SERVICE_TARGETS = {"机走棚", "机走北"}
NON_SERVICE_TARGETS = set(physical.DEPOT_TARGET_LINES) | {"卸轮线"}
DIRECT_DELIVERY_MAX_STATIC_ROUTE_LEN = 10
DIRECT_DELIVERY_PRIORITY_MIN_BATCH = 2
SERVICE_CLOSURE_MAX_BUSINESS_HOOKS = 12
SERVICE_CLOSURE_VALID_WINDOW = 48
SERVICE_CLOSURE_EXAMINED_WINDOW = 240
SERVICE_CLOSURE_LOOKAHEAD_WIDTH = 8
SERVICE_CLOSURE_LOOKAHEAD_EXAMINED = 80
SERVICE_CLOSURE_GAIN_SCALE = 1
SERVICE_CLOSURE_HOOK_COST = 1
SERVICE_CLOSURE_REHANDLE_COST = 2
DEEP_CONTINUATION_DEBT_THRESHOLD = 5
STACK_ACCESS_GAIN_THRESHOLD = 2
SERVICE_CLOSURE_MONOTONE_KEYS = (
    "service_satisfied_count",
    "service_forced_position_satisfied_count",
    "service_south_contiguous_count",
)
ROLLING_SESSION_MAX_STEPS = 12
ROLLING_SUPPORT_MAX_STEPS = 6
ROLLING_SESSION_BEAM_WIDTH = 24
ROLLING_SESSION_STATES_PER_SHAPE = 4
ROLLING_SESSION_GET_BRANCH = 8
ROLLING_SESSION_PUT_BRANCH = 6
ROLLING_SESSION_PREFIXES_PER_LINE = 5
ROLLING_SESSION_MAX_CANDIDATES = 64
ROLLING_SESSION_ENDPOINTS_PER_SHAPE = 48
ROLLING_SESSION_MAX_REGET_STEPS = 0
STACK_CLOSURE_MAX_STEPS = 16
STACK_CLOSURE_PREFIXES_PER_LINE = 6
STAGE1_COMPOUND_SESSION_REASONS = {
    "stage1_rolling_session",
    "stage1_stack_closure",
}
TARGET_REBUILD_MAX_CANDIDATES = 48
FORCED_REBUILD_MAX_CANDIDATES = 24
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


MAX_HOOKS = 80
# hook_index counts move batches. business_hooks in the summary counts Get/Put rows.
MAX_NO_PROGRESS_STREAK = 30
MAX_STALE_BEST_STREAK = 35
MAX_CAPACITY_DEFICIT_HOOKS = 10
DEFAULT_TIME_BUDGET_SECONDS = 300.0
CONTEXTUAL_VALID_WINDOW = 20
CONTEXTUAL_EXAMINED_WINDOW = 140
CONTEXTUAL_COMPLETE_VALID_WINDOW = 8
DEBT_REBOUND_PRIMARY = 7
DIRECT_DELIVERY_MAX_HOOKS = 5


def downstream_quality_vector(quality: dict[str, Any]) -> tuple[int, ...]:
    service = (
        -int(quality.get("service_forced_position_satisfied_count") or 0),
        -int(quality.get("service_south_contiguous_count") or 0),
        -int(quality.get("service_satisfied_count") or 0),
        int(quality.get("service_prefix_blocked_count") or 0),
        int(quality.get("service_extra_target_fragment_count") or 0),
    )
    layout = (
        int(quality.get("stage4_extra_target_fragment_count") or 0),
        int(quality.get("stage4_target_run_count") or 0),
        -int(quality.get("stage4_south_settled_tail_count") or 0),
    )
    stage3 = (
        int(quality.get("stage3_extra_fragment_count") or 0),
        int(quality.get("stage3_group_run_count") or 0),
        int(quality.get("stage3_prefix_blocked_count") or 0),
    )
    stage4 = (
        int(quality.get("stage4_access_blocked_debt_count") or 0),
        int(quality.get("stage4_lower_bound") or 0),
        int(quality.get("stage4_tail_debt_count") or 0),
    )
    return (*stage3, *stage4, *layout, *service)


@dataclass(frozen=True)
class CandidateView:
    candidate: physical.HookCandidate
    score: tuple[Any, ...]
    reason: str


@dataclass(frozen=True)
class SessionMetrics:
    business_hooks: int
    flow_count: int
    redundant_put_count: int
    retains_across_put_then_get: bool
    stack_valid: bool


@dataclass
class OpenSessionState:
    cars: list[dict[str, Any]]
    carried: tuple[str, ...]
    carried_origins: tuple[str, ...]
    carried_targets: tuple[tuple[str, ...], ...]
    steps: tuple[physical.PlanStep, ...]
    moved_order: tuple[str, ...]
    source_lines: frozenset[str]
    touched_lines: frozenset[str]
    loco: physical.LocoLocation
    route_cost: int
    reget_steps: int
    service_count: int
    forced_count: int
    completed_g_count: int
    carried_g_count: int
    carried_service_count: int


@dataclass(frozen=True)
class RollingGetOption:
    rank: tuple[Any, ...]
    source: str
    batch: tuple[dict[str, Any], ...]
    stage1_gain: int
    service_gain: int
    support_gain: int
    minimum_puts: int
    target_options: tuple[tuple[str, ...], ...]
    shared_put_targets: tuple[str, ...]


@dataclass(frozen=True)
class CandidateEvaluation:
    view: CandidateView
    validation: physical.PhysicalValidation
    probe: "Stage1Solver"
    after_debt: dict[str, Any]
    moving_nos: set[str]
    downstream_quality: tuple[int, ...]


@dataclass(frozen=True)
class GetBlockerInfo:
    lines: frozenset[str]
    blocked_nos: frozenset[str]
    pressure: tuple[int, int, int]


@dataclass(frozen=True)
class SelectionContext:
    mode: str
    get_blockers: GetBlockerInfo
    put_blockers: set[str]


@dataclass(frozen=True)
class RankedCandidate:
    score: tuple[Any, ...]
    evaluation: CandidateEvaluation
    before_get_blockers: GetBlockerInfo
    after_get_blockers: GetBlockerInfo
    before_put_blockers: set[str]
    after_put_blockers: set[str]


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
        self.continuation_cache: dict[tuple[Any, ...], bool] = {}
        self.downstream_quality_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.rolling_session_cache: dict[tuple[Any, ...], tuple[CandidateView, ...]] = {}
        self.forced_rebuild_word_cache: dict[
            tuple[Any, ...],
            tuple[
                tuple[tuple[str, str, tuple[str, ...]], ...],
                dict[str, int],
            ] | None,
        ] = {}
        self.service_support_cache: dict[str, set[str]] = {}
        self.get_count_by_no: Counter[str] = Counter()
        self.stage1_completion_business_hooks: int | None = None
        self.initial_service_quality = self.service_quality()

    def solve(self) -> dict[str, Any]:
        no_progress_streak = 0
        stale_best_streak = 0
        best_progress = (10**9, 10**9)
        while self.hook_index <= self.max_hooks:
            before = self.stage1_debt()
            if before["complete"]:
                if self.stage1_completion_business_hooks is None:
                    self.stage1_completion_business_hooks = self.current_business_hooks()
                closure_hooks = (
                    self.current_business_hooks()
                    - self.stage1_completion_business_hooks
                )
                if closure_hooks >= SERVICE_CLOSURE_MAX_BUSINESS_HOOKS:
                    break
                if not self.step(before, service_closure=True):
                    break
                continue
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
            accepted = self.step(before, service_closure=False)
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

    def step(
        self,
        debt: dict[str, Any],
        *,
        service_closure: bool = False,
    ) -> bool:
        service_quality_before = self.service_quality() if service_closure else None
        views = sorted(
            self.generate_candidates(debt, service_closure=service_closure),
            key=lambda item: item.score,
        )
        rejected: list[dict[str, Any]] = []
        if self.choose_and_accept_candidate(views, debt, rejected):
            if service_closure:
                self.trace[-1].update({
                    "phase": "service_closure",
                    "service_quality_before": service_quality_before,
                    "service_quality_after": self.service_quality(),
                    "service_business_hooks": self.candidate_business_hook_count(
                        next(
                            view.candidate
                            for view in views
                            if view.candidate.candidate_id == self.trace[-1]["accepted"]
                        )
                    ),
                })
            return True

        if service_closure:
            return False
        timed_out = self.time_exhausted()
        self.trace.append({
            "hook": self.hook_index,
            "accepted": "",
            "kind": "blocked",
            "reason": "solve_time_budget_exhausted" if timed_out else "no_valid_candidate",
            "candidate_count": len(views),
            "rejected": rejected[:30],
            "debt_before": debt,
            **(
                {"elapsed_seconds": round(time.monotonic() - self.started_at, 3)}
                if timed_out
                else {}
            ),
        })
        return False

    def choose_and_accept_candidate(
        self,
        views: list[CandidateView],
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
    ) -> bool:
        context = self.selection_context(debt)
        ordered_views = self.order_views_for_context(views, context)
        ranked: list[RankedCandidate] = []
        examined_count = 0
        valid_count = 0
        complete_count = 0

        for view in ordered_views:
            if self.time_exhausted():
                return False
            examined_count += 1
            evaluation = self.evaluate_candidate(
                view,
                debt,
                rejected,
                reject_limit=80 if context else None,
                reject_sideways=context is None,
                reject_large_rebound=context is None,
                reject_dead_end=True,
                reject_uncontextualized_compound=context is None,
                compute_downstream_quality=context is not None,
            )
            if evaluation is None:
                if context and examined_count >= CONTEXTUAL_EXAMINED_WINDOW:
                    break
                continue

            if context is None:
                return self.accept_candidate(
                    evaluation.view,
                    evaluation.validation,
                    evaluation.after_debt,
                    debt,
                    rejected,
                )

            valid_count += 1
            complete_count += int(evaluation.after_debt["complete"])
            pick, violations = self.rank_contextual_candidate(context, evaluation, debt)
            if pick is not None:
                ranked.append(pick)
            else:
                self.reject_candidate(rejected, view, violations, limit=80)

            if (
                (
                    debt["complete"]
                    and (
                        valid_count >= SERVICE_CLOSURE_VALID_WINDOW
                        or examined_count >= SERVICE_CLOSURE_EXAMINED_WINDOW
                    )
                )
                or (
                    not debt["complete"]
                    and complete_count >= CONTEXTUAL_COMPLETE_VALID_WINDOW
                )
                or (
                    not debt["complete"]
                    and
                    valid_count >= CONTEXTUAL_VALID_WINDOW
                    and ranked
                    and min(item.score[0] for item in ranked) < DEBT_REBOUND_PRIMARY
                )
                or (
                    not debt["complete"]
                    and examined_count >= CONTEXTUAL_EXAMINED_WINDOW
                )
            ):
                break

        if not ranked:
            return False

        if debt["complete"]:
            shortlist = self.service_closure_shortlist(ranked)
            pick = min(
                shortlist,
                key=self.service_closure_continuation_rank,
            )
        else:
            pick = min(ranked, key=lambda item: item.score)
        accepted = self.accept_candidate(
            pick.evaluation.view,
            pick.evaluation.validation,
            pick.evaluation.after_debt,
            debt,
            rejected,
        )
        self.trace[-1]["selection_context"] = context.mode
        self.trace[-1]["selection_primary"] = self.score_for_trace(pick.score[0])
        self.trace[-1]["selection_score"] = self.score_for_trace(pick.score[:5])
        self.trace[-1]["downstream_quality"] = list(pick.evaluation.downstream_quality)
        self.trace[-1]["route_blockers_before"] = sorted(pick.before_get_blockers.lines)
        self.trace[-1]["route_blockers_after"] = sorted(pick.after_get_blockers.lines)
        self.trace[-1]["route_blocker_pressure_before"] = list(
            pick.before_get_blockers.pressure
        )
        self.trace[-1]["route_blocker_pressure_after"] = list(
            pick.after_get_blockers.pressure
        )
        self.trace[-1]["put_blockers_before"] = sorted(pick.before_put_blockers)
        self.trace[-1]["put_blockers_after"] = sorted(pick.after_put_blockers)
        return accepted

    def service_closure_shortlist(
        self,
        ranked: list[RankedCandidate],
    ) -> list[RankedCandidate]:
        ordered = sorted(ranked, key=lambda item: item.score)
        representatives: list[RankedCandidate] = []
        seen_families: set[str] = set()
        for item in ordered:
            reason = item.evaluation.view.reason
            family = (
                "stage1_service_direct"
                if reason.startswith("stage1_service_direct:")
                else reason
            )
            if family in seen_families:
                continue
            seen_families.add(family)
            representatives.append(item)
        selected_ids = {
            item.evaluation.view.candidate.candidate_id
            for item in representatives
        }
        representatives.extend(
            item
            for item in ordered
            if item.evaluation.view.candidate.candidate_id not in selected_ids
        )
        return representatives[:SERVICE_CLOSURE_LOOKAHEAD_WIDTH]

    def service_closure_continuation_rank(
        self,
        item: RankedCandidate,
    ) -> tuple[Any, ...]:
        candidate = item.evaluation.view.candidate
        first_hooks = self.candidate_business_hook_count(candidate)
        first_rehandles = len(self.candidate_rehandled_nos(candidate))
        used_after = (
            self.current_business_hooks()
            - int(self.stage1_completion_business_hooks or 0)
            + first_hooks
        )
        remaining_hooks = SERVICE_CLOSURE_MAX_BUSINESS_HOOKS - used_after
        probe = item.evaluation.probe
        best = self.service_closure_path_rank(
            probe.service_quality(),
            business_hooks=first_hooks,
            rehandles=first_rehandles,
            tie_break=(item.score,),
        )
        if remaining_hooks < 2 or self.time_exhausted():
            return best

        debt = probe.stage1_debt()
        context = probe.selection_context(debt)
        if context is None:
            return best
        examined = 0
        for view in sorted(
            probe.service_closure_candidates(debt),
            key=lambda candidate_view: candidate_view.score,
        ):
            if self.time_exhausted() or examined >= SERVICE_CLOSURE_LOOKAHEAD_EXAMINED:
                break
            examined += 1
            hooks = probe.candidate_business_hook_count(view.candidate)
            if hooks > remaining_hooks:
                continue
            evaluation = probe.evaluate_candidate(
                view,
                debt,
                [],
                reject_dead_end=False,
            )
            if evaluation is None or not evaluation.after_debt["complete"]:
                continue
            ranked, _violations = probe.rank_service_closure_candidate(
                context,
                evaluation,
            )
            if ranked is None:
                continue
            best = min(
                best,
                self.service_closure_path_rank(
                    evaluation.probe.service_quality(),
                    business_hooks=first_hooks + hooks,
                    rehandles=(
                        first_rehandles
                        + len(probe.candidate_rehandled_nos(view.candidate))
                    ),
                    tie_break=(item.score, ranked.score),
                ),
            )
        return best

    def service_closure_path_rank(
        self,
        final_quality: dict[str, int],
        *,
        business_hooks: int,
        rehandles: int,
        tie_break: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        gain = self.service_closure_weighted_gain(
            self.service_quality(),
            final_quality,
        )
        cost = (
            SERVICE_CLOSURE_HOOK_COST * business_hooks
            + SERVICE_CLOSURE_REHANDLE_COST * rehandles
        )
        surplus = SERVICE_CLOSURE_GAIN_SCALE * gain - cost
        return (
            -surplus,
            self.service_quality_vector(final_quality),
            rehandles,
            business_hooks,
            tie_break,
        )

    @classmethod
    def score_for_trace(cls, value: Any) -> Any:
        if isinstance(value, Fraction):
            return [value.numerator, value.denominator]
        if isinstance(value, tuple):
            return [cls.score_for_trace(item) for item in value]
        if isinstance(value, list):
            return [cls.score_for_trace(item) for item in value]
        return value

    def reject_candidate(
        self,
        rejected: list[dict[str, Any]],
        view: CandidateView,
        violations: Iterable[str],
        *,
        limit: int | None = None,
    ) -> None:
        if limit is not None and len(rejected) >= limit:
            return
        rejected.append({
            "candidate_id": view.candidate.candidate_id,
            "reason": view.reason,
            "violations": list(violations),
        })

    def evaluate_candidate(
        self,
        view: CandidateView,
        debt: dict[str, Any],
        rejected: list[dict[str, Any]],
        *,
        reject_limit: int | None = None,
        reject_sideways: bool = False,
        reject_large_rebound: bool = False,
        reject_dead_end: bool = True,
        reject_uncontextualized_compound: bool = False,
        compute_downstream_quality: bool = False,
    ) -> CandidateEvaluation | None:
        validation = self.validate_candidate(view.candidate)
        if not validation.accepted:
            self.reject_candidate(rejected, view, validation.reasons, limit=reject_limit)
            return None

        probe = self.probe_after(view.candidate, validation)
        after_signature = physical.state_signature(probe.cars, probe.loco)
        if after_signature in self.seen_states:
            self.reject_candidate(rejected, view, ["state_cycle"], limit=reject_limit)
            return None

        after_debt = probe.stage1_debt()
        if (
            reject_uncontextualized_compound
            and self.is_compound_stage1_session(view.candidate)
            and not after_debt["complete"]
        ):
            self.reject_candidate(
                rejected,
                view,
                ["compound_session_requires_selection_context"],
                limit=reject_limit,
            )
            return None
        if self.is_compound_stage1_session(view.candidate) and not after_debt["complete"]:
            before_get_blockers = self.pending_get_blocker_info(debt)
            before_put_blockers, _ = self.pending_put_blocker_info(debt)
            after_get_blockers = probe.pending_get_blocker_info(after_debt)
            after_put_blockers, _ = probe.pending_put_blocker_info(after_debt)
            unlocks = (
                after_get_blockers.pressure < before_get_blockers.pressure
                or len(after_put_blockers) < len(before_put_blockers)
            )
            improves_debt = self.progress_tuple(after_debt) < self.progress_tuple(debt)
            if not self.compound_progress_allowed(
                improves_debt=improves_debt,
                unlocks=unlocks,
            ):
                self.reject_candidate(
                    rejected,
                    view,
                    ["compound_assembly_requires_unlock_or_completion"],
                    limit=reject_limit,
                )
                return None
        if reject_sideways and self.disallow_sideways_stage1_move(view.candidate, debt, after_debt):
            self.reject_candidate(rejected, view, ["sideways_stage1_move"], limit=reject_limit)
            return None

        if reject_large_rebound and self.large_assembly_debt_rebound(view.candidate, debt, after_debt):
            self.reject_candidate(rejected, view, ["large_assembly_debt_rebound"], limit=reject_limit)
            return None

        if (
            reject_dead_end
            and not after_debt["complete"]
            and not probe.can_continue(
                depth=(
                    2
                    if (
                        self.is_compound_stage1_session(view.candidate)
                        or after_debt["debt_count"] <= DEEP_CONTINUATION_DEBT_THRESHOLD
                    )
                    else 1
                ),
                avoid_new_get_blockers=self.is_compound_stage1_session(view.candidate),
            )
        ):
            self.reject_candidate(rejected, view, ["dead_end_after_candidate"], limit=reject_limit)
            return None

        downstream_quality = probe.downstream_quality_tuple() if compute_downstream_quality else ()
        return CandidateEvaluation(
            view=view,
            validation=validation,
            probe=probe,
            after_debt=after_debt,
            moving_nos=set(view.candidate.move_car_nos),
            downstream_quality=downstream_quality,
        )

    def selection_context(self, debt: dict[str, Any]) -> SelectionContext | None:
        if debt["pollution_nos"]:
            return None
        get_blockers = self.pending_get_blocker_info(debt)
        put_blockers, _put_blocked_nos = self.pending_put_blocker_info(debt)
        if get_blockers.lines & set(ASSEMBLY_DEPOT):
            mode = "get_unlock"
        elif not get_blockers.lines and put_blockers & set(ASSEMBLY_DEPOT):
            mode = "put_unlock"
        elif not get_blockers.lines:
            mode = "debt_window"
        else:
            return None
        return SelectionContext(
            mode=mode,
            get_blockers=get_blockers,
            put_blockers=put_blockers,
        )

    def order_views_for_context(
        self,
        views: list[CandidateView],
        context: SelectionContext | None,
    ) -> list[CandidateView]:
        if context and context.mode == "put_unlock":
            return sorted(
                views,
                key=lambda view: (view.reason != "release_gate_assembly", view.score),
            )
        return views

    def rank_contextual_candidate(
        self,
        context: SelectionContext,
        evaluation: CandidateEvaluation,
        debt: dict[str, Any],
    ) -> tuple[RankedCandidate | None, tuple[str, ...]]:
        service_barriers = self.new_service_landing_barriers(
            evaluation.view.candidate,
            evaluation.probe,
        )
        if (
            service_barriers
            and evaluation.after_debt["debt_count"] >= debt["debt_count"]
        ):
            return None, tuple(
                f"new_service_landing_barrier:{line}"
                for line in sorted(service_barriers)
            )
        if debt["complete"]:
            return self.rank_service_closure_candidate(context, evaluation)
        if context.mode == "get_unlock":
            return self.rank_get_unlock_candidate(context, evaluation, debt)
        return self.rank_put_or_debt_candidate(context, evaluation, debt)

    def rank_service_closure_candidate(
        self,
        context: SelectionContext,
        evaluation: CandidateEvaluation,
    ) -> tuple[RankedCandidate | None, tuple[str, ...]]:
        before_quality = self.service_quality()
        after_quality = evaluation.probe.service_quality()
        regressed = tuple(
            key
            for key in SERVICE_CLOSURE_MONOTONE_KEYS
            if after_quality[key] < before_quality[key]
        )
        if regressed:
            return None, tuple(f"service_quality_regression:{key}" for key in regressed)
        before_vector = self.service_quality_vector(before_quality)
        after_vector = self.service_quality_vector(after_quality)
        if after_vector >= before_vector:
            return None, ("service_quality_not_improved",)
        weighted_gain = self.service_closure_weighted_gain(
            before_quality,
            after_quality,
        )
        candidate = evaluation.view.candidate
        rehandled = len(self.candidate_rehandled_nos(candidate))
        action_cost = (
            SERVICE_CLOSURE_HOOK_COST
            * self.candidate_business_hook_count(candidate)
            + SERVICE_CLOSURE_REHANDLE_COST * rehandled
        )
        if weighted_gain <= 0:
            return None, ("service_closure_marginal_gain_too_low",)
        surplus = SERVICE_CLOSURE_GAIN_SCALE * weighted_gain - action_cost
        if surplus < 0:
            return None, ("service_closure_negative_net_gain",)
        after_get_blockers = evaluation.probe.pending_get_blocker_info(
            evaluation.after_debt
        )
        after_put_blockers, _ = evaluation.probe.pending_put_blocker_info(
            evaluation.after_debt
        )
        return RankedCandidate(
            score=(
                -surplus,
                after_vector,
                Fraction(action_cost, weighted_gain),
                rehandled,
                self.candidate_business_hook_count(candidate),
                self.route_price(candidate),
                evaluation.view.score,
            ),
            evaluation=evaluation,
            before_get_blockers=context.get_blockers,
            after_get_blockers=after_get_blockers,
            before_put_blockers=context.put_blockers,
            after_put_blockers=after_put_blockers,
        ), ()

    def rank_get_unlock_candidate(
        self,
        context: SelectionContext,
        evaluation: CandidateEvaluation,
        debt: dict[str, Any],
    ) -> tuple[RankedCandidate | None, tuple[str, ...]]:
        after_debt = evaluation.after_debt
        after_get_blockers = evaluation.probe.pending_get_blocker_info(after_debt)
        after_put_blockers, _after_put_blocked_nos = evaluation.probe.pending_put_blocker_info(after_debt)
        moves_get_blocked = bool(
            evaluation.moving_nos & context.get_blockers.blocked_nos
        )
        unlocks_get = after_get_blockers.pressure < context.get_blockers.pressure
        worsens_get_blockers = (
            after_get_blockers.pressure > context.get_blockers.pressure
            and not after_debt["complete"]
        )
        worsens_put_blockers = (
            len(after_put_blockers) > len(context.put_blockers)
            and not after_debt["complete"]
        )
        debt_rebound = after_debt["debt_count"] > debt["debt_count"]
        improves_debt = self.progress_tuple(after_debt) < self.progress_tuple(debt)
        improves_stack_access = (
            debt["blocked_g_count"] - after_debt["blocked_g_count"]
            >= STACK_ACCESS_GAIN_THRESHOLD
            and not debt_rebound
        )
        if (
            self.disallow_sideways_stage1_move(evaluation.view.candidate, debt, after_debt)
            and not unlocks_get
        ):
            return None, ("sideways_stage1_move",)
        if (
            self.large_assembly_debt_rebound(evaluation.view.candidate, debt, after_debt)
            and not unlocks_get
        ):
            return None, ("large_assembly_debt_rebound",)
        if worsens_get_blockers:
            return None, ("worsens_get_route_blockers",)
        if (
            worsens_put_blockers
            and (not improves_debt or not context.put_blockers)
        ):
            return None, ("worsens_put_route_blockers",)
        if (
            self.is_compound_stage1_session(evaluation.view.candidate)
            and not after_debt["complete"]
            and not self.compound_progress_allowed(
                improves_debt=improves_debt,
                unlocks=unlocks_get,
            )
        ):
            return None, ("compound_assembly_requires_unlock_or_completion",)
        if after_debt["complete"]:
            primary = 0
        elif unlocks_get and not moves_get_blocked and not debt_rebound:
            primary = 1
        elif moves_get_blocked and improves_debt:
            primary = 2
        elif improves_stack_access:
            primary = 2
        elif improves_debt:
            primary = 3
        elif debt_rebound:
            primary = DEBT_REBOUND_PRIMARY
        elif moves_get_blocked or unlocks_get:
            primary = 4
        else:
            primary = 6
        return RankedCandidate(
            score=self.compose_contextual_score(
                primary=primary,
                candidate=evaluation.view.candidate,
                debt=debt,
                after_debt=after_debt,
                blocker_count=len(after_get_blockers.lines) + len(after_put_blockers),
                service_regression=self.service_regression_rank(evaluation.probe),
                service_quality=evaluation.probe.service_quality(),
                downstream_quality=evaluation.downstream_quality,
                view_score=evaluation.view.score,
            ),
            evaluation=evaluation,
            before_get_blockers=context.get_blockers,
            after_get_blockers=after_get_blockers,
            before_put_blockers=context.put_blockers,
            after_put_blockers=after_put_blockers,
        ), ()

    def rank_put_or_debt_candidate(
        self,
        context: SelectionContext,
        evaluation: CandidateEvaluation,
        debt: dict[str, Any],
    ) -> tuple[RankedCandidate | None, tuple[str, ...]]:
        after_debt = evaluation.after_debt
        after_get_blockers = evaluation.probe.pending_get_blocker_info(after_debt)
        after_put_blockers, _after_put_blocked_nos = evaluation.probe.pending_put_blocker_info(after_debt)
        worsens_get_blockers = (
            after_get_blockers.pressure > context.get_blockers.pressure
            and not after_debt["complete"]
        )
        unlocks_put = (
            bool(context.put_blockers & set(ASSEMBLY_DEPOT))
            and len(after_put_blockers) < len(context.put_blockers)
        )
        worsens_put_blockers = (
            len(after_put_blockers) > len(context.put_blockers)
            and not after_debt["complete"]
            and (
                bool(context.put_blockers)
                or self.is_compound_stage1_session(evaluation.view.candidate)
            )
        )
        large_rebound = self.large_assembly_debt_rebound(
            evaluation.view.candidate,
            debt,
            after_debt,
        )
        improves_debt = self.progress_tuple(after_debt) < self.progress_tuple(debt)
        improves_stack_access = (
            debt["blocked_g_count"] - after_debt["blocked_g_count"]
            >= STACK_ACCESS_GAIN_THRESHOLD
            and after_debt["debt_count"] <= debt["debt_count"]
        )
        if worsens_get_blockers:
            return None, ("worsens_get_route_blockers",)
        if unlocks_put and large_rebound:
            return None, ("large_assembly_debt_rebound",)
        if (
            worsens_put_blockers
            and (not improves_debt or not context.put_blockers)
        ):
            return None, ("worsens_put_route_blockers",)
        if (
            self.is_compound_stage1_session(evaluation.view.candidate)
            and not after_debt["complete"]
            and not self.compound_progress_allowed(
                improves_debt=improves_debt,
                unlocks=unlocks_put,
            )
        ):
            return None, ("compound_assembly_requires_unlock_or_completion",)
        direct_delivery_priority = (
            not context.put_blockers
            and (
                self.direct_delivery_priority_candidate(
                    evaluation.view.candidate,
                    debt,
                    after_debt,
                )
                or self.forced_position_gain(evaluation.probe)
            )
        )
        if (
            self.disallow_sideways_stage1_move(evaluation.view.candidate, debt, after_debt)
            and not unlocks_put
        ):
            return None, ("sideways_stage1_move",)
        debt_rebound = after_debt["debt_count"] > debt["debt_count"]
        if after_debt["complete"]:
            primary = 0
        elif direct_delivery_priority:
            primary = 1
        elif unlocks_put and not debt_rebound:
            primary = 2
        elif improves_stack_access:
            primary = 2
        elif improves_debt:
            primary = 3
        elif unlocks_put and debt_rebound:
            primary = DEBT_REBOUND_PRIMARY
        elif unlocks_put:
            primary = 4
        elif context.mode == "put_unlock":
            primary = 6
        else:
            primary = 5
        if primary == 6 and large_rebound:
            return None, ("large_assembly_debt_rebound",)
        return RankedCandidate(
            score=self.compose_contextual_score(
                primary=primary,
                candidate=evaluation.view.candidate,
                debt=debt,
                after_debt=after_debt,
                blocker_count=len(after_put_blockers),
                service_regression=self.service_regression_rank(evaluation.probe),
                service_quality=evaluation.probe.service_quality(),
                downstream_quality=evaluation.downstream_quality,
                view_score=evaluation.view.score,
            ),
            evaluation=evaluation,
            before_get_blockers=context.get_blockers,
            after_get_blockers=after_get_blockers,
            before_put_blockers=context.put_blockers,
            after_put_blockers=after_put_blockers,
        ), ()

    def compose_contextual_score(
        self,
        *,
        primary: int,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
        blocker_count: int,
        service_regression: tuple[int, int, int],
        service_quality: dict[str, int],
        downstream_quality: tuple[int, ...],
        view_score: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        return (
            primary,
            service_regression,
            self.contextual_target_group(candidate, debt),
            self.assembly_landing_rank(candidate),
            self.candidate_source_phase_gap(candidate, debt),
            blocker_count,
            max(0, after_debt["debt_count"] - debt["debt_count"]),
            self.progress_tuple(after_debt),
            self.rehandling_value_rank(candidate, after_quality=service_quality),
            self.service_quality_vector(service_quality),
            downstream_quality,
            (
                -self.candidate_closure_savings(candidate),
                self.candidate_business_hook_count(candidate),
            ),
            self.real_target_contextual_rank(candidate, debt, after_debt),
            view_score,
        )

    def rehandling_value_rank(
        self,
        candidate: physical.HookCandidate,
        *,
        after_quality: dict[str, int],
    ) -> tuple[int, int, int]:
        before = self.service_quality()
        gain = max(0, self.service_closure_weighted_gain(before, after_quality))
        rehandled = len(self.candidate_rehandled_nos(candidate))
        return (
            max(0, rehandled - gain),
            -gain,
            rehandled,
        )

    def is_compound_stage1_session(self, candidate: physical.HookCandidate) -> bool:
        return candidate.generation_reason in STAGE1_COMPOUND_SESSION_REASONS

    def compound_progress_allowed(
        self,
        *,
        improves_debt: bool,
        unlocks: bool,
    ) -> bool:
        return improves_debt or unlocks

    def service_regression_rank(self, probe: "Stage1Solver") -> tuple[int, int, int]:
        before = self.downstream_quality()
        after = probe.downstream_quality()
        return tuple(
            max(0, int(before[key]) - int(after[key]))
            for key in (
                "service_forced_position_satisfied_count",
                "service_south_contiguous_count",
                "service_satisfied_count",
            )
        )

    def forced_position_gain(self, probe: "Stage1Solver") -> bool:
        before = self.downstream_quality()
        after = probe.downstream_quality()
        return int(after["service_forced_position_satisfied_count"]) > int(
            before["service_forced_position_satisfied_count"]
        )

    def assembly_landing_rank(self, candidate: physical.HookCandidate) -> int:
        by_no = self.by_no()
        destinations = self.candidate_put_line_by_no(candidate)
        ranks = [
            ASSEMBLY_DEPOT.index(destinations[no])
            for no in candidate.move_car_nos
            if no in by_no
            and self.stage1_goal(by_no[no]) == "depot_assembly"
            and destinations.get(no) in ASSEMBLY_DEPOT
        ]
        return max(ranks, default=0)

    def contextual_target_group(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
    ) -> int:
        put_lines = set(self.candidate_put_line_by_no(candidate).values())
        if "存4线" in put_lines:
            return 0
        if put_lines & {"机南", "洗油北"}:
            return 0
        if put_lines & {"机走棚", "机走北"}:
            remaining = set(debt["pending_stage1_nos"]) - set(candidate.move_car_nos)
            return 3 if remaining else 1
        return 2

    def candidate_source_phase_gap(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
    ) -> int:
        pending_nos = set(debt["pending_stage1_nos"])
        pending_lines = {
            car["Line"]
            for car in self.cars
            if physical.car_no(car) in pending_nos
        }
        source_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Get" and step.line in pending_lines
        }
        source_ranks = [self.source_rank(line) for line in source_lines]
        if len(source_ranks) < 2:
            return 0
        low, high = min(source_ranks), max(source_ranks)
        return sum(
            low < self.source_rank(line) < high
            for line in pending_lines - source_lines
        )

    def real_target_contextual_rank(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> int:
        if after_debt["debt_count"] > debt["debt_count"]:
            return 1
        return 0 if self.clean_real_target_put(candidate) else 1

    def large_assembly_debt_rebound(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        by_no = self.by_no()
        destinations = self.candidate_put_line_by_no(candidate)
        return (
            any(
                by_no.get(no, {}).get("Line") in ASSEMBLY_DEPOT
                and destinations.get(no) not in ASSEMBLY_DEPOT
                for no in candidate.move_car_nos
            )
            and after_debt["debt_count"] > debt["debt_count"] + 1
        )

    def disallow_sideways_stage1_move(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        if self.progress_tuple(after_debt) < self.progress_tuple(debt):
            return False
        by_no = self.by_no()
        destinations = self.candidate_put_line_by_no(candidate)
        for no in candidate.move_car_nos:
            car = by_no.get(no)
            if not car or not self.stage1_goal(car):
                continue
            if not self.official_stage1_target(car, destinations.get(no, "")):
                return True
        return False

    @staticmethod
    def service_closure_weighted_gain(
        before: dict[str, int],
        after: dict[str, int],
    ) -> int:
        return (
            after["service_satisfied_count"]
            - before["service_satisfied_count"]
            + after["service_south_contiguous_count"]
            - before["service_south_contiguous_count"]
            + 2 * (
                after["service_forced_position_satisfied_count"]
                - before["service_forced_position_satisfied_count"]
            )
        )

    def service_closure_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        yield from self.forced_position_rebuild_candidates(debt)
        yield from self.spotting_repack_candidates(debt)
        yield from self.service_prefix_cleanup_candidates(debt)
        yield from self.target_rebuild_candidates(debt)
        yield from self.rolling_session_candidates(debt, service_only=True)
        yield from self.direct_delivery_candidates(
            debt,
            enforce_hook_limit=False,
            enforce_route_limit=False,
        )

    def forced_position_rebuild_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[CandidateView]:
        if not debt["complete"] or debt["pollution_nos"]:
            return
        views: list[tuple[tuple[Any, ...], CandidateView]] = []
        for target in SERVICE_LINES:
            if not physical.is_spotting_line(target):
                continue
            existing = self.line_ordered_cars(target)
            if any(not self.target_satisfied(car) for car in existing):
                continue
            pending_forced_nos = {
                physical.car_no(car)
                for car in self.cars
                if car["Line"] != target
                and self.service_eligible(car)
                and target in set(car.get("TargetLines") or ())
                and bool(physical.force_positions(car))
                and not self.target_satisfied(car)
            }
            if not pending_forced_nos:
                continue
            for source_batches in self.forced_rebuild_source_sets(
                target,
                existing,
                pending_forced_nos,
            ):
                incoming = [
                    car
                    for _source, batch in source_batches
                    for car in batch
                ]
                candidate = self.compile_forced_position_rebuild(
                    target,
                    existing,
                    source_batches,
                )
                if not candidate:
                    continue
                view = CandidateView(
                    candidate,
                    self.score_candidate(
                        candidate,
                        debt=debt,
                        moved_g=0,
                        moved_x=len(incoming),
                        reason_rank=1,
                    ),
                    "stage1_service_forced_rebuild",
                )
                views.append((
                    (
                        -len(pending_forced_nos),
                        -len(incoming),
                        self.candidate_business_hook_count(candidate),
                        self.route_price(candidate),
                    ),
                    view,
                ))
        for _rank, view in sorted(views, key=lambda item: item[0])[
            :FORCED_REBUILD_MAX_CANDIDATES
        ]:
            yield view

    def forced_rebuild_source_sets(
        self,
        target: str,
        existing: list[dict[str, Any]],
        pending_forced_nos: set[str],
    ) -> list[tuple[tuple[str, list[dict[str, Any]]], ...]]:
        capacity = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target, 0)
        remaining_capacity = capacity - len(existing)
        if remaining_capacity <= 0:
            return []
        available: list[tuple[str, list[dict[str, Any]]]] = []
        for source in self.active_lines():
            if source == target or source in FORBIDDEN_LINES or source in ASSEMBLY_ALL:
                continue
            batch = self.service_prefix_for_target(source, target)
            if batch and not any(self.pending_weigh(car) for car in batch):
                available.append((source, batch))
        available_nos = {
            physical.car_no(car)
            for _source, batch in available
            for car in batch
        }
        if not pending_forced_nos <= available_nos:
            return []

        mandatory = tuple(
            item
            for item in available
            if any(
                physical.car_no(car) in pending_forced_nos
                for car in item[1]
            )
        )
        mandatory_count = sum(len(batch) for _source, batch in mandatory)
        if mandatory_count > remaining_capacity:
            return []
        optional = [item for item in available if item not in mandatory]
        optional_capacity = remaining_capacity - mandatory_count
        best_by_count: dict[int, tuple[tuple[str, list[dict[str, Any]]], ...]] = {0: ()}
        for item in optional:
            size = len(item[1])
            updates = dict(best_by_count)
            for count, selected in best_by_count.items():
                next_count = count + size
                if next_count > optional_capacity:
                    continue
                candidate = (*selected, item)
                previous = updates.get(next_count)
                if previous is None or self.forced_rebuild_source_set_rank(
                    candidate
                ) < self.forced_rebuild_source_set_rank(previous):
                    updates[next_count] = candidate
            best_by_count = updates

        result = {
            tuple(source for source, _batch in (*mandatory, *selected)): (
                *mandatory,
                *selected,
            )
            for _count, selected in best_by_count.items()
        }
        return sorted(
            result.values(),
            key=lambda selected: (
                -sum(len(batch) for _source, batch in selected),
                self.forced_rebuild_source_set_rank(selected),
            ),
        )

    def forced_rebuild_source_set_rank(
        self,
        selected: tuple[tuple[str, list[dict[str, Any]]], ...],
    ) -> tuple[Any, ...]:
        return (
            len(selected),
            sum(len(self.graph.route(self.loco.line, source)) for source, _batch in selected),
            tuple(source for source, _batch in selected),
        )

    def compile_forced_position_rebuild(
        self,
        target: str,
        existing: list[dict[str, Any]],
        source_batches: tuple[tuple[str, list[dict[str, Any]]], ...],
    ) -> physical.HookCandidate | None:
        existing_nos = tuple(physical.car_no(car) for car in existing)
        incoming = [
            car
            for _source, batch in source_batches
            for car in batch
        ]
        source_nos = tuple(
            (
                source,
                tuple(physical.car_no(car) for car in batch),
            )
            for source, batch in source_batches
        )
        target_route_blockers = (
            self.retainable_service_route_blockers(target, set(existing_nos))
            if existing_nos
            else []
        )
        source_route_blockers: list[tuple[str, list[dict[str, Any]]]] = []
        for source, move_nos in source_nos:
            blockers = self.retainable_service_route_blockers(source, set(move_nos))
            if blockers is None:
                return None
            source_route_blockers.extend(blockers)
        if target_route_blockers is None:
            return None
        source_lines = {source for source, _move_nos in source_nos}
        route_blockers = [
            (line, cars)
            for line, cars in dict(
                (*target_route_blockers, *source_route_blockers)
            ).items()
            if line not in source_lines | {target}
        ]
        blocker_cars = [
            car
            for _line, cars in route_blockers
            for car in cars
        ]
        if (
            physical.pull_equivalent([*blocker_cars, *existing])
            > physical.PULL_LIMIT_EQUIVALENT
        ):
            return None

        fixed_hooks = 2 * len(route_blockers) + int(bool(existing_nos))
        max_word_steps = SERVICE_CLOSURE_MAX_BUSINESS_HOOKS - fixed_hooks
        optimistic_gain = (
            2 * len(incoming)
            + 2 * sum(bool(physical.force_positions(car)) for car in incoming)
        )
        historical_rehandles = sum(
            self.get_count_by_no[physical.car_no(car)] > 0
            for car in [*blocker_cars, *existing, *incoming]
        )
        direct_hook_lower_bound = fixed_hooks + len(source_nos) + 1
        if optimistic_gain < (
            SERVICE_CLOSURE_HOOK_COST * direct_hook_lower_bound
            + SERVICE_CLOSURE_REHANDLE_COST * historical_rehandles
        ):
            return None
        plans: list[tuple[
            tuple[tuple[str, str, tuple[str, ...]], ...],
            dict[str, int],
            str,
        ]] = []
        direct_word = self.forced_position_rebuild_word(
            target=target,
            existing_nos=existing_nos,
            source_nos=source_nos,
            blocker_cars=blocker_cars,
            max_steps=max_word_steps,
            allow_staging=False,
        )
        if direct_word is not None:
            plans.append((*direct_word, ""))
        staged_word = None
        staged_hook_lower_bound = direct_hook_lower_bound + 2
        if optimistic_gain >= (
            SERVICE_CLOSURE_HOOK_COST * staged_hook_lower_bound
            + SERVICE_CLOSURE_REHANDLE_COST * historical_rehandles
        ):
            staged_word = self.forced_position_rebuild_word(
                target=target,
                existing_nos=existing_nos,
                source_nos=source_nos,
                blocker_cars=blocker_cars,
                max_steps=max_word_steps,
                allow_staging=True,
            )
        if staged_word is not None and (
            direct_word is None or staged_word[0] != direct_word[0]
        ):
            staging_lines = [
                line
                for line in sorted(STORAGE_CACHE_LINES, key=self.target_rank)
                if line not in source_lines | {target}
                and line not in {blocker_line for blocker_line, _cars in route_blockers}
            ]
            plans.extend((*staged_word, line) for line in staging_lines)

        for actions, planned_positions, staging_line in plans:
            working_cars = [self.clone_car(car) for car in self.cars]
            steps: list[physical.PlanStep] = []
            for blocker_line, cars in route_blockers:
                blocker_nos = tuple(physical.car_no(car) for car in cars)
                physical.apply_physical_get_order(
                    working_cars,
                    blocker_line,
                    blocker_nos,
                )
                steps.append(physical.plan_step("Get", blocker_line, blocker_nos))
            if existing_nos:
                physical.apply_physical_get_order(working_cars, target, existing_nos)
                steps.append(physical.plan_step("Get", target, existing_nos))
            plan_valid = True
            for action, abstract_line, move_nos in actions:
                line = abstract_line or staging_line
                if not line:
                    plan_valid = False
                    break
                if action == "Get":
                    physical.apply_physical_get_order(working_cars, line, move_nos)
                    steps.append(physical.plan_step("Get", line, move_nos))
                    continue
                if line == target:
                    move_positions = {
                        no: planned_positions[no]
                        for no in move_nos
                    }
                    physical.apply_physical_put_order(
                        working_cars,
                        line,
                        list(move_nos),
                        move_positions,
                    )
                    steps.append(physical.plan_step(
                        "Put",
                        line,
                        move_nos,
                        move_positions,
                    ))
                    continue
                put_step = self.planned_session_put_step(
                    working_cars,
                    line,
                    move_nos,
                )
                if put_step is None:
                    plan_valid = False
                    break
                steps.append(put_step)
            if not plan_valid:
                continue
            for blocker_line, cars in reversed(route_blockers):
                blocker_nos = tuple(physical.car_no(car) for car in cars)
                put_step = self.planned_session_put_step(
                    working_cars,
                    blocker_line,
                    blocker_nos,
                )
                if put_step is None:
                    plan_valid = False
                    break
                steps.append(put_step)
            if not plan_valid or not steps:
                continue
            candidate = self.make_planlet_candidate(
                source=steps[0].line,
                target=steps[-1].line,
                batch=[*blocker_cars, *existing, *incoming],
                steps=tuple(steps),
                kind="vnext_depot_inbound_mixed_extraction_session",
                reason="stage1_service_forced_rebuild",
            )
            if candidate is not None and self.validate_candidate(candidate).accepted:
                return candidate
        return None

    def forced_position_rebuild_word(
        self,
        *,
        target: str,
        existing_nos: tuple[str, ...],
        source_nos: tuple[tuple[str, tuple[str, ...]], ...],
        blocker_cars: list[dict[str, Any]],
        max_steps: int,
        allow_staging: bool,
    ) -> tuple[
        tuple[tuple[str, str, tuple[str, ...]], ...],
        dict[str, int],
    ] | None:
        if max_steps <= 0:
            return None
        cache_key = (
            target,
            existing_nos,
            source_nos,
            tuple(physical.car_no(car) for car in blocker_cars),
            max_steps,
            allow_staging,
        )
        if cache_key in self.forced_rebuild_word_cache:
            return self.forced_rebuild_word_cache[cache_key]
        by_no = self.by_no()
        class_by_no = {
            no: (
                physical.force_positions(car),
                bool(car.get("IsHeavy")),
                round(physical.car_length(car), 3),
                bool(car.get("IsClosedDoor")),
            )
            for no, car in by_no.items()
        }
        blocker_equivalent = physical.pull_equivalent(blocker_cars)
        source_lines = tuple(source for source, _nos in source_nos)
        initial = (
            tuple(nos for _source, nos in source_nos),
            (),
            (),
            existing_nos,
        )
        frontier = deque([(initial, ())])

        def state_key(
            state: tuple[
                tuple[tuple[str, ...], ...],
                tuple[str, ...],
                tuple[str, ...],
                tuple[str, ...],
            ],
        ) -> tuple[Any, ...]:
            remaining, staged, placed, carried = state
            return (
                tuple(tuple(class_by_no[no] for no in line) for line in remaining),
                tuple(class_by_no[no] for no in staged),
                tuple(class_by_no[no] for no in placed),
                tuple(class_by_no[no] for no in carried),
            )

        seen = {state_key(initial)}
        while frontier:
            if self.time_exhausted():
                return None
            (
                remaining_by_source,
                staged,
                placed,
                carried,
            ), actions = frontier.popleft()
            if not any(remaining_by_source) and not staged and not carried:
                positions = self.spotting_positions_for_order(target, placed)
                if positions is not None:
                    result = (actions, positions)
                    self.forced_rebuild_word_cache[cache_key] = result
                    return result
            if len(actions) >= max_steps:
                continue

            for source_index, remaining in enumerate(remaining_by_source):
                for size in range(len(remaining), 0, -1):
                    move_nos = remaining[:size]
                    train = [
                        *(by_no[no] for no in carried if no in by_no),
                        *(by_no[no] for no in move_nos if no in by_no),
                    ]
                    if (
                        blocker_equivalent + physical.pull_equivalent(train)
                        > physical.PULL_LIMIT_EQUIVALENT
                    ):
                        continue
                    next_remaining = list(remaining_by_source)
                    next_remaining[source_index] = remaining[size:]
                    state = (
                        tuple(next_remaining),
                        staged,
                        placed,
                        (*carried, *move_nos),
                    )
                    key = state_key(state)
                    if key in seen:
                        continue
                    seen.add(key)
                    frontier.append((state, (*actions, (
                        "Get",
                        source_lines[source_index],
                        move_nos,
                    ))))

            if allow_staging:
                for size in range(len(staged), 0, -1):
                    move_nos = staged[:size]
                    train = [
                        *(by_no[no] for no in carried if no in by_no),
                        *(by_no[no] for no in move_nos if no in by_no),
                    ]
                    if (
                        blocker_equivalent + physical.pull_equivalent(train)
                        > physical.PULL_LIMIT_EQUIVALENT
                    ):
                        continue
                    state = (
                        remaining_by_source,
                        staged[size:],
                        placed,
                        (*carried, *move_nos),
                    )
                    key = state_key(state)
                    if key in seen:
                        continue
                    seen.add(key)
                    frontier.append((state, (*actions, ("Get", "", move_nos))))

            for size in range(len(carried), 0, -1):
                move_nos = carried[-size:]
                state = (
                    remaining_by_source,
                    staged,
                    (*move_nos, *placed),
                    carried[:-size],
                )
                key = state_key(state)
                if key not in seen:
                    seen.add(key)
                    frontier.append((state, (*actions, ("Put", target, move_nos))))
                if allow_staging:
                    staging_state = (
                        remaining_by_source,
                        (*move_nos, *staged),
                        placed,
                        carried[:-size],
                    )
                    staging_key = state_key(staging_state)
                    if staging_key not in seen:
                        seen.add(staging_key)
                        frontier.append((staging_state, (*actions, (
                            "Put",
                            "",
                            move_nos,
                        ))))
        self.forced_rebuild_word_cache[cache_key] = None
        return None

    def spotting_positions_for_order(
        self,
        target: str,
        ordered_nos: tuple[str, ...],
    ) -> dict[str, int] | None:
        capacity = physical.SPOTTING_LINE_TOTAL_POSITIONS.get(target, 0)
        by_no = self.by_no()
        if not capacity or len(ordered_nos) > capacity:
            return None
        memo: dict[tuple[int, int], tuple[int, ...] | None] = {}

        def assign(index: int, previous: int) -> tuple[int, ...] | None:
            key = (index, previous)
            if key in memo:
                return memo[key]
            if index >= len(ordered_nos):
                return ()
            car = by_no.get(ordered_nos[index])
            if car is None:
                memo[key] = None
                return None
            forced = physical.force_positions(car)
            allowed = forced or tuple(range(1, capacity + 1))
            remaining_count = len(ordered_nos) - index - 1
            for position in allowed:
                if position <= previous or capacity - position < remaining_count:
                    continue
                suffix = assign(index + 1, position)
                if suffix is not None:
                    memo[key] = (position, *suffix)
                    return memo[key]
            memo[key] = None
            return None

        positions = assign(0, 0)
        if positions is None:
            return None
        return dict(zip(ordered_nos, positions))

    def retainable_service_route_blockers(
        self,
        source: str,
        moving_nos: set[str],
    ) -> list[tuple[str, list[dict[str, Any]]]] | None:
        blocker_lines = self.route_blockers_for_get(source, moving_nos)
        if not blocker_lines:
            return []
        retainable: list[tuple[str, list[dict[str, Any]]]] = []
        for line in blocker_lines:
            if line in FORBIDDEN_LINES or line == "存4线" or line == source:
                continue
            cars = self.line_ordered_cars(line)
            if (
                not cars
                or any(
                    self.pending_weigh(car)
                    or not (
                        self.target_satisfied(car)
                        or self.service_eligible(car)
                        or (
                            line in ASSEMBLY_DEPOT
                            and bool(self.stage1_goal(car))
                            and self.stage1_car_complete(car)
                        )
                    )
                    for car in cars
                )
                or physical.pull_equivalent(cars) > physical.PULL_LIMIT_EQUIVALENT
            ):
                continue
            retainable.append((line, cars))
        for size in range(1, len(retainable) + 1):
            for selected in combinations(retainable, size):
                selected_cars = [car for _line, cars in selected for car in cars]
                if physical.pull_equivalent(selected_cars) > physical.PULL_LIMIT_EQUIVALENT:
                    continue
                selected_nos = {physical.car_no(car) for car in selected_cars}
                route_moving_nos = moving_nos | selected_nos
                occupied = physical.occupied_lines_for_get_route(
                    self.cars,
                    route_moving_nos,
                    source,
                )
                route = self.graph.route_avoiding_occupied(
                    self.loco.line,
                    source,
                    occupied,
                    source_departure_lines=physical.route_departure_lines_for_source(
                        self.loco.line,
                        self.cars,
                        route_moving_nos,
                    ),
                    target_approach_lines=physical.route_approach_lines_for_get(source),
                    cars=self.cars,
                    moving_nos=route_moving_nos,
                    train_length_m=physical.train_length_for_nos(
                        self.cars,
                        selected_nos,
                    ),
                )
                if route:
                    return list(selected)
        return None

    def candidate_business_hook_count(self, candidate: physical.HookCandidate) -> int:
        return sum(
            1
            for step in physical.candidate_plan_steps(candidate)
            if step.action in {"Get", "Put"}
        )

    def current_business_hooks(self) -> int:
        return sum(
            row.get("Action") in {"Get", "Put"}
            for row in self.operations
        )

    def candidate_get_nos(self, candidate: physical.HookCandidate) -> tuple[str, ...]:
        return tuple(
            no
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Get"
            for no in step.move_car_nos
        )

    def candidate_rehandled_nos(self, candidate: physical.HookCandidate) -> set[str]:
        candidate_get_counts = Counter(self.candidate_get_nos(candidate))
        return {
            no
            for no, count in candidate_get_counts.items()
            if self.get_count_by_no[no] + count > 1
        }

    def new_service_landing_barriers(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage1Solver",
    ) -> set[str]:
        before_by_no = self.by_no()
        after_by_no = probe.by_no()
        landed_by_line: dict[str, set[str]] = {}
        for no, target in self.candidate_put_line_by_no(candidate).items():
            if target not in SERVICE_LINE_SET:
                continue
            before = before_by_no.get(no)
            after = after_by_no.get(no)
            if after is None or not probe.target_satisfied(after):
                continue
            if (
                before is not None
                and before.get("Line") == target
                and self.target_satisfied(before)
            ):
                continue
            landed_by_line.setdefault(target, set()).add(no)

        barriers: set[str] = set()
        for line, landed_nos in landed_by_line.items():
            landed_seen = False
            for car in probe.line_ordered_cars(line):
                no = physical.car_no(car)
                if no in landed_nos:
                    landed_seen = True
                    continue
                if landed_seen and not probe.target_satisfied(car):
                    barriers.add(line)
                    break
        return barriers

    def session_metrics(
        self,
        steps: Iterable[physical.PlanStep],
    ) -> SessionMetrics:
        carried: list[str] = []
        get_step_by_no: dict[str, int] = {}
        get_line_by_no: dict[str, str] = {}
        flows: set[tuple[int, int]] = set()
        business_hooks = 0
        put_group = -1
        previous_action = ""
        previous_put_line = ""
        redundant_put_count = 0
        retained_after_put = False
        put_then_get = False
        stack_valid = True

        for step_index, step in enumerate(steps):
            if step.action == "Weigh":
                previous_action = step.action
                previous_put_line = ""
                continue
            if step.action == "Get":
                business_hooks += 1
                if retained_after_put and carried:
                    put_then_get = True
                for no in step.move_car_nos:
                    if no in get_step_by_no:
                        stack_valid = False
                        continue
                    get_step_by_no[no] = step_index
                    get_line_by_no[no] = step.line
                    carried.append(no)
                retained_after_put = False
                previous_action = step.action
                previous_put_line = ""
                continue
            if step.action != "Put":
                stack_valid = False
                previous_action = step.action
                previous_put_line = ""
                continue

            business_hooks += 1
            if previous_action == "Put" and previous_put_line == step.line:
                redundant_put_count += 1
            else:
                put_group += 1
            move_nos = list(step.move_car_nos)
            if (
                not move_nos
                or len(move_nos) > len(carried)
                or carried[-len(move_nos):] != move_nos
            ):
                stack_valid = False
                continue
            for no in move_nos:
                get_step = get_step_by_no.pop(no, None)
                get_line = get_line_by_no.pop(no, "")
                if get_step is None:
                    stack_valid = False
                    continue
                if get_line != step.line:
                    flows.add((get_step, put_group))
            del carried[-len(move_nos):]
            retained_after_put = bool(carried)
            previous_action = step.action
            previous_put_line = step.line

        stack_valid = (
            stack_valid
            and not carried
            and not get_step_by_no
            and not get_line_by_no
        )
        return SessionMetrics(
            business_hooks=business_hooks,
            flow_count=len(flows),
            redundant_put_count=redundant_put_count,
            retains_across_put_then_get=put_then_get,
            stack_valid=stack_valid,
        )

    def candidate_closure_savings(self, candidate: physical.HookCandidate) -> int:
        metrics = self.session_metrics(physical.candidate_plan_steps(candidate))
        if not metrics.stack_valid:
            return -metrics.business_hooks
        return 2 * metrics.flow_count - metrics.business_hooks

    def service_quality_vector(self, quality: dict[str, int] | None = None) -> tuple[int, ...]:
        quality = quality or self.service_quality()
        return (
            -quality["service_forced_position_satisfied_count"],
            -(
                quality["service_south_contiguous_count"]
                + quality["service_satisfied_count"]
            ),
            -quality["service_south_contiguous_count"],
            -quality["service_satisfied_count"],
            quality["service_prefix_blocked_count"],
            quality["service_extra_target_fragment_count"],
            quality["service_target_run_count"],
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
        get_nos = self.candidate_get_nos(view.candidate)
        rehandled_nos = self.candidate_rehandled_nos(view.candidate)
        rows = physical.operation_rows(view.candidate, validation, self.operation_index)
        self.operations.extend(physical.response_operation(row) for row in rows)
        self.operation_index += len(rows)
        physical.apply_candidate(view.candidate, self.cars, validation)
        self.loco = physical.next_loco_location(view.candidate, validation)
        self.get_count_by_no.update(get_nos)
        self.service_support_cache.clear()
        self.seen_states.add(physical.state_signature(self.cars, self.loco))
        self.trace.append({
            "hook": self.hook_index,
            "accepted": view.candidate.candidate_id,
            "kind": view.candidate.candidate_kind,
            "reason": view.reason,
            "source": view.candidate.source_line,
            "target": view.candidate.target_line,
            "move": list(view.candidate.move_car_nos),
            "rehandled_nos": sorted(rehandled_nos),
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
        probe.get_count_by_no.update(self.candidate_get_nos(candidate))
        return probe

    def can_continue(
        self,
        depth: int,
        *,
        avoid_new_get_blockers: bool = False,
    ) -> bool:
        debt = self.stage1_debt()
        if debt["complete"] or depth <= 0:
            return True
        cache_key = (
            physical.state_signature(self.cars, self.loco),
            self.hook_index,
            depth,
            avoid_new_get_blockers,
        )
        cached = self.continuation_cache.get(cache_key)
        if cached is not None:
            return cached
        if "priority_route_blocker_lines" not in debt:
            debt = {
                **debt,
                "priority_route_blocker_lines": self.priority_route_blocker_lines(debt),
            }
        candidate_groups: tuple[Iterable[CandidateView], ...] = (
            self.stack_closure_candidates(debt),
            self.direct_assembly_candidates(debt),
            self.blocker_candidates(debt),
            self.route_blocker_candidates(debt),
            self.cleanup_candidates(debt),
            self.release_gate_candidates(debt),
            self.direct_delivery_candidates(debt),
        )
        if not avoid_new_get_blockers:
            candidate_groups = (
                *candidate_groups,
                self.rolling_session_candidates(debt, service_only=False),
            )
        continuation_context = self.selection_context(debt)
        before_get_blockers = self.pending_get_blocker_info(debt)
        seen: set[str] = set()
        for group in candidate_groups:
            for view in sorted(group, key=lambda item: item.score):
                candidate_id = view.candidate.candidate_id
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                next_validation = self.validate_candidate(view.candidate)
                if not next_validation.accepted:
                    continue
                probe = self.probe_after(view.candidate, next_validation)
                if physical.state_signature(probe.cars, probe.loco) in self.seen_states:
                    continue
                after_debt = probe.stage1_debt()
                if avoid_new_get_blockers and not after_debt["complete"]:
                    after_get_blockers = probe.pending_get_blocker_info(after_debt)
                    if after_get_blockers.pressure > before_get_blockers.pressure:
                        continue
                if continuation_context is not None:
                    continuation = CandidateEvaluation(
                        view=view,
                        validation=next_validation,
                        probe=probe,
                        after_debt=after_debt,
                        moving_nos=set(view.candidate.move_car_nos),
                        downstream_quality=(),
                    )
                    ranked, _violations = self.rank_contextual_candidate(
                        continuation_context,
                        continuation,
                        debt,
                    )
                    if ranked is None:
                        continue
                if depth <= 1 or probe.can_continue(
                    depth - 1,
                    avoid_new_get_blockers=avoid_new_get_blockers,
                ):
                    self.continuation_cache[cache_key] = True
                    return True
        self.continuation_cache[cache_key] = False
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
        clone.continuation_cache = self.continuation_cache
        clone.downstream_quality_cache = self.downstream_quality_cache
        clone.rolling_session_cache = self.rolling_session_cache
        clone.forced_rebuild_word_cache = self.forced_rebuild_word_cache
        clone.service_support_cache = {}
        clone.get_count_by_no = Counter(self.get_count_by_no)
        clone.stage1_completion_business_hooks = self.stage1_completion_business_hooks
        clone.initial_service_quality = dict(self.initial_service_quality)
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

    def generate_candidates(
        self,
        debt: dict[str, Any],
        *,
        service_closure: bool = False,
    ) -> list[CandidateView]:
        if "priority_route_blocker_lines" not in debt:
            debt = {
                **debt,
                "priority_route_blocker_lines": self.priority_route_blocker_lines(debt),
            }
        candidates: list[CandidateView] = []
        if service_closure:
            remaining_hooks = (
                SERVICE_CLOSURE_MAX_BUSINESS_HOOKS
                - (
                    self.current_business_hooks()
                    - int(self.stage1_completion_business_hooks or 0)
                )
            )
            candidates.extend(
                view
                for view in self.service_closure_candidates(debt)
                if self.candidate_business_hook_count(view.candidate) <= remaining_hooks
            )
            unique: dict[str, CandidateView] = {}
            for item in candidates:
                unique.setdefault(item.candidate.candidate_id, item)
            return list(unique.values())
        candidates.extend(self.stack_closure_candidates(debt))
        candidates.extend(self.rolling_session_candidates(debt, service_only=False))
        candidates.extend(self.direct_assembly_candidates(debt))
        candidates.extend(self.blocker_candidates(debt))
        candidates.extend(self.route_blocker_candidates(debt))
        candidates.extend(self.cleanup_candidates(debt))
        candidates.extend(self.release_gate_candidates(debt))
        candidates.extend(self.direct_delivery_candidates(debt))
        unique: dict[str, CandidateView] = {}
        for item in candidates:
            unique.setdefault(item.candidate.candidate_id, item)
        return list(unique.values())

    def collect_homogeneous_north_prefix(
        self,
        ordered: list[dict[str, Any]],
        key_for_car: Callable[[dict[str, Any]], str],
        *,
        stop_after_pending_weigh: bool,
    ) -> tuple[str, list[dict[str, Any]]]:
        key = ""
        prefix: list[dict[str, Any]] = []
        pull_equivalent = 0
        for car in ordered:
            car_key = key_for_car(car)
            if not car_key:
                break
            if key and car_key != key:
                break
            next_pull = pull_equivalent + (4 if bool(car.get("IsHeavy")) else 1)
            if next_pull > physical.PULL_LIMIT_EQUIVALENT:
                break
            key = car_key
            prefix.append(car)
            pull_equivalent = next_pull
            if stop_after_pending_weigh and self.pending_weigh(car):
                break
        return key, prefix

    def direct_assembly_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        for line in self.active_lines():
            if line in FORBIDDEN_LINES:
                continue
            ordered = self.line_ordered_cars(line)
            if not ordered:
                continue

            def direct_assembly_key(car: dict[str, Any]) -> str:
                goal = self.stage1_goal(car)
                if not goal or self.stage1_car_complete(car):
                    return ""
                return "unwheel" if goal == "存4线" else "depot"

            target_group, max_batch = self.collect_homogeneous_north_prefix(
                ordered,
                direct_assembly_key,
                stop_after_pending_weigh=True,
            )
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
                    view = self.single_put_view(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="vnext_depot_inbound_assembly_session",
                        reason="direct_assembly",
                        debt=debt,
                        moved_g=sum(1 for car in batch if self.stage1_goal(car)),
                        moved_x=0,
                        reason_rank=0,
                    )
                    if view:
                        yield view

    def direct_delivery_candidates(
        self,
        debt: dict[str, Any],
        *,
        enforce_hook_limit: bool = True,
        enforce_route_limit: bool = True,
    ) -> Iterable[CandidateView]:
        if enforce_hook_limit and self.direct_delivery_hooks_used() >= DIRECT_DELIVERY_MAX_HOOKS:
            return
        if debt["pollution_nos"]:
            return
        ready_targets = {
            target: self.real_target_line_ready(
                target,
                respect_forced_reservation=False,
            )
            for target in SERVICE_LINES
        }
        if not any(ready_targets.values()):
            return
        for line in self.active_lines():
            if line in FORBIDDEN_LINES:
                continue
            ordered = self.line_ordered_cars(line)
            if not ordered:
                continue

            def direct_delivery_key(car: dict[str, Any]) -> str:
                car_target = self.direct_delivery_target_for_car(car)
                if not car_target or car_target == line or not ready_targets.get(car_target):
                    return ""
                return car_target

            target, max_batch = self.collect_homogeneous_north_prefix(
                ordered,
                direct_delivery_key,
                stop_after_pending_weigh=True,
            )
            if not target or not max_batch:
                continue
            if enforce_route_limit and not self.direct_delivery_route_low_cost(line, target):
                continue
            for batch in self.prefix_options(max_batch):
                if not self.real_target_line_ready(
                    target,
                    excluded_nos={physical.car_no(car) for car in batch},
                ):
                    continue
                reason = f"stage1_service_direct:{target}"
                view = self.single_put_view(
                    source=line,
                    target=target,
                    batch=batch,
                    kind="blocker_relocation",
                    reason=reason,
                    debt=debt,
                    moved_g=0,
                    moved_x=len(batch),
                    reason_rank=4,
                )
                if view:
                    yield view
                retained_view = self.retained_put_route_view(
                    source=line,
                    target=target,
                    batch=batch,
                    debt=debt,
                    reason=reason,
                )
                if retained_view:
                    yield retained_view

    def retained_put_route_view(
        self,
        *,
        source: str,
        target: str,
        batch: list[dict[str, Any]],
        debt: dict[str, Any],
        reason: str,
    ) -> CandidateView | None:
        moving_nos = {physical.car_no(car) for car in batch}
        blocker_lines = self.route_blockers_for_put(
            source,
            target,
            moving_nos,
        )
        blockers: list[tuple[str, list[dict[str, Any]]]] = []
        for line in blocker_lines:
            if line in FORBIDDEN_LINES or line in {source, target, "存4线"}:
                return None
            cars = self.line_ordered_cars(line)
            if (
                not cars
                or any(
                    self.pending_weigh(car)
                    or not self.target_satisfied(car)
                    for car in cars
                )
            ):
                return None
            blockers.append((line, cars))
        if not blockers:
            return None
        blocker_cars = [car for _line, cars in blockers for car in cars]
        if (
            physical.pull_equivalent([*blocker_cars, *batch])
            > physical.PULL_LIMIT_EQUIVALENT
        ):
            return None

        working_cars = [self.clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = []
        for line, cars in blockers:
            nos = tuple(physical.car_no(car) for car in cars)
            physical.apply_physical_get_order(working_cars, line, nos)
            steps.append(physical.plan_step("Get", line, nos))
        batch_nos = tuple(physical.car_no(car) for car in batch)
        physical.apply_physical_get_order(working_cars, source, batch_nos)
        steps.append(physical.plan_step("Get", source, batch_nos))
        target_put = self.planned_session_put_step(
            working_cars,
            target,
            batch_nos,
        )
        if target_put is None:
            return None
        steps.append(target_put)
        for line, cars in reversed(blockers):
            nos = tuple(physical.car_no(car) for car in cars)
            blocker_put = self.planned_session_put_step(
                working_cars,
                line,
                nos,
            )
            if blocker_put is None:
                return None
            steps.append(blocker_put)
        candidate = self.make_planlet_candidate(
            source=steps[0].line,
            target=steps[-1].line,
            batch=[*blocker_cars, *batch],
            steps=tuple(steps),
            kind="vnext_depot_inbound_mixed_extraction_session",
            reason=reason,
        )
        if candidate is None:
            return None
        return CandidateView(
            candidate,
            self.score_candidate(
                candidate,
                debt=debt,
                moved_g=0,
                moved_x=len(blocker_cars) + len(batch),
                reason_rank=4,
            ),
            reason,
        )

    def stack_closure_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[CandidateView]:
        if debt["pollution_nos"]:
            return
        pending = set(debt["pending_stage1_nos"])
        for source in self.active_lines():
            if source in FORBIDDEN_LINES or source in ASSEMBLY_ALL:
                continue
            ordered = self.line_ordered_cars(source)
            pending_indexes = [
                index
                for index, car in enumerate(ordered)
                if physical.car_no(car) in pending
            ]
            if not pending_indexes or min(pending_indexes) == 0:
                continue
            pull_equivalent = 0
            max_prefix_length = 0
            for car in ordered:
                pull_equivalent += 4 if bool(car.get("IsHeavy")) else 1
                if pull_equivalent > physical.PULL_LIMIT_EQUIVALENT:
                    break
                max_prefix_length += 1
            if max_prefix_length <= 0:
                continue
            prefix = ordered[:max_prefix_length]
            signatures = [
                self.stack_closure_targets(car, source)
                for car in prefix
            ]
            lengths = {max_prefix_length}
            lengths.update(
                index
                for index in range(1, max_prefix_length)
                if signatures[index - 1] != signatures[index]
            )
            pending_in_prefix = [
                index + 1
                for index, car in enumerate(prefix)
                if physical.car_no(car) in pending
            ]
            lengths.update(pending_in_prefix)
            for length in sorted(lengths, reverse=True)[
                :STACK_CLOSURE_PREFIXES_PER_LINE
            ]:
                batch = prefix[:length]
                if (
                    any(self.pending_weigh(car) for car in batch)
                    or any(not targets for targets in signatures[:length])
                ):
                    continue
                steps, moved_nos = self.compile_stack_closure(source, batch)
                if not steps or len(steps) <= 2:
                    continue
                by_no = self.by_no()
                moved = [by_no[no] for no in moved_nos if no in by_no]
                candidate = self.make_planlet_candidate(
                    source=steps[0].line,
                    target=steps[-1].line,
                    batch=moved,
                    steps=steps,
                    kind="vnext_depot_inbound_mixed_extraction_session",
                    reason="stage1_stack_closure",
                )
                if candidate is None or self.candidate_closure_savings(candidate) < 0:
                    continue
                moved_g = sum(
                    bool(self.stage1_goal(car)) and not self.stage1_car_complete(car)
                    for car in moved
                )
                yield CandidateView(
                    candidate,
                    self.score_candidate(
                        candidate,
                        debt=debt,
                        moved_g=moved_g,
                        moved_x=len(moved) - moved_g,
                        reason_rank=0,
                    ),
                    "stage1_stack_closure",
                )

    def compile_stack_closure(
        self,
        source: str,
        batch: list[dict[str, Any]],
    ) -> tuple[tuple[physical.PlanStep, ...], tuple[str, ...]]:
        working_cars = [self.clone_car(car) for car in self.cars]
        initial_nos = tuple(physical.car_no(car) for car in batch)
        physical.apply_physical_get_order(working_cars, source, initial_nos)
        steps: list[physical.PlanStep] = [
            physical.plan_step("Get", source, initial_nos)
        ]
        carried = list(initial_nos)
        origins = [source] * len(initial_nos)
        moved_order = list(initial_nos)
        touched = set(initial_nos)

        while carried and len(steps) < STACK_CLOSURE_MAX_STEPS:
            put_options = self.stack_closure_put_options(
                working_cars,
                carried,
                origins,
            )
            put_applied = False
            for _rank, start, target in put_options:
                move_nos = tuple(carried[start:])
                trial_cars = [self.clone_car(car) for car in working_cars]
                put_step = self.planned_session_put_step(
                    trial_cars,
                    target,
                    move_nos,
                )
                if put_step is None:
                    continue
                working_cars = trial_cars
                steps.append(put_step)
                del carried[start:]
                del origins[start:]
                put_applied = True
                break
            if put_applied:
                continue

            target = self.stack_closure_blocked_target(
                working_cars,
                carried[-1],
                origins[-1],
            )
            blocker_batch = (
                self.stack_closure_blocker_prefix(working_cars, target)
                if target
                else []
            )
            blocker_nos = tuple(physical.car_no(car) for car in blocker_batch)
            if (
                not blocker_batch
                or touched & set(blocker_nos)
                or any(self.pending_weigh(car) for car in blocker_batch)
                or any(
                    not self.stack_closure_targets(car, target)
                    for car in blocker_batch
                )
                or physical.pull_equivalent([
                    *self.cars_for_nos(working_cars, carried),
                    *blocker_batch,
                ]) > physical.PULL_LIMIT_EQUIVALENT
            ):
                spill = self.stack_closure_spill(
                    working_cars,
                    carried,
                    origins,
                    blocked_target=target,
                    source=source,
                )
                if spill is None:
                    return (), ()
                spill_step, spill_start, working_cars = spill
                steps.append(spill_step)
                del carried[spill_start:]
                del origins[spill_start:]
                continue
            physical.apply_physical_get_order(
                working_cars,
                target,
                blocker_nos,
            )
            steps.append(physical.plan_step("Get", target, blocker_nos))
            carried.extend(blocker_nos)
            origins.extend([target] * len(blocker_nos))
            moved_order.extend(blocker_nos)
            touched.update(blocker_nos)

        if carried:
            return (), ()
        return tuple(steps), tuple(moved_order)

    def stack_closure_spill(
        self,
        working_cars: list[dict[str, Any]],
        carried: list[str],
        origins: list[str],
        *,
        blocked_target: str,
        source: str,
    ) -> tuple[physical.PlanStep, int, list[dict[str, Any]]] | None:
        original_by_no = self.by_no()
        start = len(carried) - 1
        while start > 0:
            car = original_by_no.get(carried[start - 1])
            if car is None or blocked_target not in self.stack_closure_targets(
                car,
                origins[start - 1],
            ):
                break
            start -= 1
        move_nos = tuple(carried[start:])
        move_cars = [
            original_by_no[no]
            for no in move_nos
            if no in original_by_no
        ]
        if len(move_cars) != len(move_nos):
            return None
        reserved_targets = {
            target
            for no, origin in zip(carried[:start], origins[:start])
            if (car := original_by_no.get(no)) is not None
            for target in self.stack_closure_targets(car, origin)
        }
        pending_lines = set(self.stage1_debt()["lines_with_pending_stage1"])
        excluded = set()
        if any(
            car.get("Line") == source
            and self.stage1_goal(car)
            and not self.stage1_car_complete(car)
            for car in working_cars
        ):
            excluded.add(source)
        candidates = sorted(
            STORAGE_CACHE_LINES
            - reserved_targets
            - (pending_lines - {source})
            - {blocked_target}
            - excluded,
            key=lambda line: (line != source, self.target_rank(line), line),
        )
        for target in candidates:
            if not self.target_allowed(target, move_cars):
                continue
            trial_cars = [self.clone_car(car) for car in working_cars]
            put_step = self.planned_session_put_step(
                trial_cars,
                target,
                move_nos,
            )
            if put_step is not None:
                return put_step, start, trial_cars
        return None

    def stack_closure_put_options(
        self,
        working_cars: list[dict[str, Any]],
        carried: list[str],
        origins: list[str],
    ) -> list[tuple[tuple[Any, ...], int, str]]:
        original_by_no = self.by_no()
        common: set[str] | None = None
        options: list[tuple[tuple[Any, ...], int, str]] = []
        for start in range(len(carried) - 1, -1, -1):
            no = carried[start]
            car = original_by_no.get(no)
            if car is None:
                break
            targets = set(self.stack_closure_targets(car, origins[start]))
            common = targets if common is None else common & targets
            if not common:
                break
            move_nos = tuple(carried[start:])
            move_origins = tuple(origins[start:])
            for target in sorted(common, key=lambda line: (self.target_rank(line), line)):
                if self.spotting_target_has_pending_forced_outside(
                    target,
                    set(move_nos),
                ):
                    continue
                if self.target_reserved_for_forced_cars_in(
                    working_cars,
                    target,
                    set(move_nos),
                ):
                    continue
                if self.put_would_close_dirty_service_target(
                    working_cars,
                    target=target,
                    move_nos=move_nos,
                    origins=move_origins,
                ):
                    continue
                options.append((
                    (-len(move_nos), self.target_rank(target), target),
                    start,
                    target,
                ))
        return sorted(options, key=lambda item: item[0])

    def spotting_target_has_pending_forced_outside(
        self,
        target: str,
        included_nos: set[str],
    ) -> bool:
        return self.spotting_target_has_pending_forced_outside_in(
            self.cars,
            target,
            included_nos,
        )

    def spotting_target_has_pending_forced_outside_in(
        self,
        cars: list[dict[str, Any]],
        target: str,
        included_nos: set[str],
    ) -> bool:
        if not physical.is_spotting_line(target):
            return False
        return any(
            physical.car_no(car) not in included_nos
            and self.service_eligible(car)
            and target in set(car.get("TargetLines") or ())
            and bool(physical.force_positions(car))
            and not self.target_satisfied(car)
            for car in cars
        )

    def stack_closure_blocked_target(
        self,
        working_cars: list[dict[str, Any]],
        no: str,
        origin: str,
    ) -> str:
        car = self.by_no().get(no)
        if car is None:
            return ""
        for target in self.stack_closure_targets(car, origin):
            if (
                target in SERVICE_LINE_SET
                and self.put_would_close_dirty_service_target(
                    working_cars,
                    target=target,
                    move_nos=(no,),
                    origins=(origin,),
                )
            ):
                return target
        return ""

    def stack_closure_blocker_prefix(
        self,
        cars: list[dict[str, Any]],
        target: str,
    ) -> list[dict[str, Any]]:
        ordered = self.line_ordered_cars_in(cars, target)
        bad_indexes = [
            index
            for index, car in enumerate(ordered)
            if not self.target_satisfied(car)
        ]
        return ordered[: max(bad_indexes) + 1] if bad_indexes else []

    def stack_closure_targets(
        self,
        car: dict[str, Any],
        origin: str,
    ) -> tuple[str, ...]:
        if self.stage1_goal(car) and not self.stage1_car_complete(car):
            return self.official_stage1_targets(car)
        if self.service_eligible(car):
            if self.target_satisfied(car):
                return (origin,)
            return self.service_target_options(car)
        if self.protected_satisfied_car(car):
            return (origin,)
        return ()

    @staticmethod
    def cars_for_nos(
        cars: list[dict[str, Any]],
        nos: Iterable[str],
    ) -> list[dict[str, Any]]:
        by_no = {physical.car_no(car): car for car in cars}
        return [by_no[no] for no in nos if no in by_no]

    @staticmethod
    def line_ordered_cars_in(
        cars: list[dict[str, Any]],
        line: str,
    ) -> list[dict[str, Any]]:
        by_no = {physical.car_no(car): car for car in cars}
        return [
            by_no[no]
            for no in physical.line_access_order(cars, line)
            if no in by_no
        ]

    def rolling_session_candidates(
        self,
        debt: dict[str, Any],
        *,
        service_only: bool,
    ) -> Iterable[CandidateView]:
        if debt["pollution_nos"]:
            return
        if service_only and not debt["complete"]:
            return

        cache_key = (
            physical.state_signature(self.cars, self.loco),
            self.hook_index,
            service_only,
        )
        cached = self.rolling_session_cache.get(cache_key)
        if cached is not None:
            yield from cached
            return

        endpoints: list[tuple[tuple[Any, ...], CandidateView]] = []
        support_lanes = (False,)
        if not service_only and any(
            self.stage1_route_support_nos(line)
            for line in ASSEMBLY_ALL
        ):
            support_lanes = (False, True)
        initial_service, initial_forced = self.rolling_service_counts(self.cars)
        for allow_stage1_support in support_lanes:
            frontier = [OpenSessionState(
                cars=[dict(car) for car in self.cars],
                carried=(),
                carried_origins=(),
                carried_targets=(),
                steps=(),
                moved_order=(),
                source_lines=frozenset(),
                touched_lines=frozenset(),
                loco=self.loco,
                route_cost=0,
                reget_steps=0,
                service_count=initial_service,
                forced_count=initial_forced,
                completed_g_count=0,
                carried_g_count=0,
                carried_service_count=0,
            )]
            max_steps = (
                ROLLING_SUPPORT_MAX_STEPS
                if allow_stage1_support
                else ROLLING_SESSION_MAX_STEPS
            )
            for _depth in range(max_steps):
                open_states: list[OpenSessionState] = []
                for state in frontier:
                    successors = (
                        self.rolling_get_successors(
                            state,
                            service_only=service_only,
                            allow_stage1_support=allow_stage1_support,
                        )
                        if not state.carried
                        else [
                            *self.rolling_put_successors(
                                state,
                                service_only=service_only,
                            ),
                            *self.rolling_get_successors(
                                state,
                                service_only=service_only,
                                allow_stage1_support=allow_stage1_support,
                            ),
                        ]
                    )
                    for successor in successors:
                        uses_stage1_support = bool(
                            successor.source_lines & set(ASSEMBLY_ALL)
                        )
                        if (
                            allow_stage1_support
                            and len(successor.source_lines) >= 2
                            and not uses_stage1_support
                        ):
                            continue
                        if successor.carried:
                            if (
                                len(successor.steps)
                                + self.rolling_minimum_puts(successor)
                                > max_steps
                            ):
                                continue
                            open_states.append(successor)
                            continue
                        if allow_stage1_support and not uses_stage1_support:
                            continue
                        endpoint = self.rolling_endpoint_view(
                            successor,
                            debt=debt,
                            service_only=service_only,
                        )
                        if endpoint is not None:
                            endpoints.append(endpoint)
                if not open_states:
                    break
                frontier = self.prune_rolling_states(
                    open_states,
                    service_only=service_only,
                )

        unique: dict[str, tuple[tuple[Any, ...], CandidateView]] = {}
        for ranked in sorted(endpoints, key=lambda item: item[0]):
            unique.setdefault(ranked[1].candidate.candidate_id, ranked)

        endpoint_groups: dict[
            tuple[Any, ...],
            list[tuple[tuple[Any, ...], CandidateView]],
        ] = {}
        for ranked in unique.values():
            endpoint_groups.setdefault(
                self.rolling_endpoint_shape(ranked[1]),
                [],
            ).append(ranked)

        valid_groups: list[list[tuple[tuple[Any, ...], CandidateView]]] = []
        for group in endpoint_groups.values():
            valid = [
                ranked
                for ranked in sorted(group, key=lambda item: item[0])[
                    :ROLLING_SESSION_ENDPOINTS_PER_SHAPE
                ]
                if self.validate_candidate(ranked[1].candidate).accepted
            ]
            if valid:
                valid_groups.append(valid)

        # Interleave structural shapes so a long retained session cannot displace
        # all simpler split or gather alternatives before contextual selection.
        selected = sorted(
            (
                (group_index, ranked[0], ranked[1])
                for group in valid_groups
                for group_index, ranked in enumerate(group)
            ),
            key=lambda item: (item[0], item[1]),
        )[:ROLLING_SESSION_MAX_CANDIDATES]
        result = tuple(item[2] for item in selected)
        self.rolling_session_cache[cache_key] = result
        yield from result

    def rolling_get_successors(
        self,
        state: OpenSessionState,
        *,
        service_only: bool,
        allow_stage1_support: bool,
    ) -> list[OpenSessionState]:
        if len(state.steps) >= ROLLING_SESSION_MAX_STEPS - 1:
            return []
        successors: list[OpenSessionState] = []
        options = self.rolling_get_options(
            state,
            service_only=service_only,
            allow_stage1_support=allow_stage1_support,
        )
        option_groups: dict[tuple[bool, int], list[RollingGetOption]] = {}
        for option in options:
            has_stage1 = any(
                self.stage1_goal(car)
                for car in option.batch
            )
            option_groups.setdefault(
                (bool(has_stage1), option.minimum_puts),
                [],
            ).append(option)
        selected_options = sorted(
            (
                (group_index, option.rank, option)
                for group in option_groups.values()
                for group_index, option in enumerate(group)
            ),
            key=lambda item: (item[0], item[1]),
        )
        moved_nos = set(state.moved_order)
        for _group_index, _option_rank, option in selected_options:
            if len(successors) >= ROLLING_SESSION_GET_BRANCH:
                break
            source = option.source
            batch = list(option.batch)
            move_nos = tuple(physical.car_no(car) for car in batch)
            reget_step = any(no in moved_nos for no in move_nos)
            if reget_step and state.reget_steps >= ROLLING_SESSION_MAX_REGET_STEPS:
                continue
            transition = self.rolling_route_transition(
                state,
                action="Get",
                line=source,
                move_nos=move_nos,
            )
            if transition is None:
                continue
            next_loco, route_cost = transition
            working_cars = [dict(car) for car in state.cars]
            targets = option.target_options
            if any(not options for options in targets):
                continue
            physical.apply_physical_get_order(working_cars, source, move_nos)
            moved_order = (*state.moved_order, *(
                no for no in move_nos if no not in moved_nos
            ))
            carried = (*state.carried, *move_nos)
            (
                service_count,
                forced_count,
                completed_g_count,
                carried_g_count,
                carried_service_count,
            ) = (
                self.rolling_state_counts(working_cars, moved_order, carried)
            )
            successor = OpenSessionState(
                cars=working_cars,
                carried=carried,
                carried_origins=(*state.carried_origins, *((source,) * len(move_nos))),
                carried_targets=(*state.carried_targets, *targets),
                steps=(*state.steps, physical.plan_step("Get", source, move_nos)),
                moved_order=moved_order,
                source_lines=state.source_lines | {source},
                touched_lines=state.touched_lines | {source},
                loco=next_loco,
                route_cost=state.route_cost + route_cost,
                reget_steps=state.reget_steps + int(reget_step),
                service_count=service_count,
                forced_count=forced_count,
                completed_g_count=completed_g_count,
                carried_g_count=carried_g_count,
                carried_service_count=carried_service_count,
            )
            successors.append(successor)
        return successors

    def rolling_get_options(
        self,
        state: OpenSessionState,
        *,
        service_only: bool,
        allow_stage1_support: bool,
    ) -> list[RollingGetOption]:
        by_no = {physical.car_no(car): car for car in state.cars}
        options: list[RollingGetOption] = []
        active_lines = sorted({str(car.get("Line") or "") for car in state.cars if car.get("Line")})
        unresolved_sources = {
            str(car.get("Line") or "")
            for car in state.cars
            if self.stage1_goal(car) and not self.stage1_car_complete(car)
        }
        last_get_line = (
            state.steps[-1].line
            if state.steps and state.steps[-1].action == "Get"
            else ""
        )
        for source in active_lines:
            if (
                source in FORBIDDEN_LINES
                or source == last_get_line
                or (
                    source in ASSEMBLY_ALL
                    and not (
                        allow_stage1_support
                        and self.stage1_route_support_nos(source)
                    )
                )
            ):
                continue
            prefix: list[dict[str, Any]] = []
            for no in physical.line_access_order(state.cars, source):
                car = by_no.get(no)
                if car is None or self.pending_weigh(car):
                    break
                if self.rolling_crosses_source_dependency(
                    car,
                    source=source,
                    unresolved_sources=unresolved_sources,
                ):
                    break
                if not (
                    self.rolling_car_actionable(
                        car,
                        source=source,
                        service_only=service_only,
                        allow_stage1_support=allow_stage1_support,
                    )
                    or self.protected_satisfied_car(car)
                ):
                    break
                prefix.append(car)
            if not prefix:
                continue

            line_options: list[RollingGetOption] = []
            for batch in self.prefix_options(prefix):
                actionable = [
                    car
                    for car in batch
                    if self.rolling_car_actionable(
                        car,
                        source=source,
                        service_only=service_only,
                        allow_stage1_support=allow_stage1_support,
                    )
                ]
                if not actionable:
                    continue
                train = [
                    *(by_no[no] for no in state.carried if no in by_no),
                    *batch,
                ]
                if physical.pull_equivalent(train) > physical.PULL_LIMIT_EQUIVALENT:
                    continue
                stage1_count = sum(
                    bool(self.stage1_goal(car)) and not self.stage1_car_complete(car)
                    for car in batch
                )
                service_count = sum(
                    self.service_eligible(car) and not self.target_satisfied(car)
                    for car in batch
                )
                support_nos = (
                    self.service_support_nos(source)
                    if service_only
                    else self.stage1_route_support_nos(source)
                )
                if service_only:
                    support_count = sum(
                        physical.car_no(car) in support_nos
                        and not (
                            self.service_eligible(car)
                            and not self.target_satisfied(car)
                        )
                        for car in batch
                    )
                else:
                    support_count = sum(
                        physical.car_no(car) in support_nos
                        for car in batch
                    )
                excluded_nos = {physical.car_no(item) for item in batch}
                target_options = tuple(
                    self.rolling_car_put_targets(
                        car,
                        source=source,
                        service_only=service_only,
                        allow_stage1_support=allow_stage1_support,
                        cars=state.cars,
                        excluded_nos=excluded_nos,
                    )
                    for car in batch
                )
                minimum_puts = self.rolling_batch_minimum_puts(target_options)
                if minimum_puts <= 0:
                    continue
                direct_targets = self.rolling_feasible_shared_put_targets(
                    state.cars,
                    (*state.carried_targets, *target_options),
                    train,
                )
                blocker_count = len(batch) - len(actionable)
                static_route = self.graph.route(state.loco.line, source)
                rank = (
                    -service_count if service_only else -stage1_count,
                    -service_count,
                    -support_count,
                    blocker_count,
                    -len(batch),
                    len(static_route),
                    self.source_rank(source),
                    tuple(physical.car_no(car) for car in batch),
                )
                line_options.append(RollingGetOption(
                    rank=rank,
                    source=source,
                    batch=tuple(batch),
                    stage1_gain=stage1_count,
                    service_gain=service_count,
                    support_gain=support_count,
                    minimum_puts=minimum_puts,
                    target_options=target_options,
                    shared_put_targets=direct_targets,
                ))
            structural_lengths = self.rolling_structural_prefix_lengths(
                prefix,
                source=source,
                service_only=service_only,
                allow_stage1_support=allow_stage1_support,
            )
            frontier = [
                option
                for option in line_options
                if not any(
                    self.rolling_prefix_dominates(other, option)
                    for other in line_options
                )
            ]
            structural = [
                option
                for option in line_options
                if len(option.batch) in structural_lengths
            ]
            preferred = {
                tuple(physical.car_no(car) for car in option.batch): option
                for option in (*frontier, *structural)
            }
            options.extend(sorted(
                preferred.values(),
                key=lambda item: item.rank,
            )[:ROLLING_SESSION_PREFIXES_PER_LINE])
        return sorted(options, key=lambda item: item.rank)

    def rolling_structural_prefix_lengths(
        self,
        cars: list[dict[str, Any]],
        *,
        source: str,
        service_only: bool,
        allow_stage1_support: bool,
    ) -> set[int]:
        lengths = {len(cars)}
        signatures = [
            (
                self.rolling_car_put_targets(
                    car,
                    source=source,
                    service_only=service_only,
                    allow_stage1_support=allow_stage1_support,
                ),
                bool(physical.force_positions(car)),
                self.target_satisfied(car),
            )
            for car in cars
        ]
        lengths.update(
            index
            for index in range(1, len(signatures))
            if signatures[index - 1] != signatures[index]
        )
        return lengths

    @staticmethod
    def rolling_prefix_dominates(
        other: RollingGetOption,
        option: RollingGetOption,
    ) -> bool:
        other_targets = set(other.shared_put_targets)
        option_targets = set(option.shared_put_targets)
        if not other_targets >= option_targets:
            return False
        other_value = (
            other.stage1_gain,
            other.service_gain,
            other.support_gain,
            -other.minimum_puts,
        )
        option_value = (
            option.stage1_gain,
            option.service_gain,
            option.support_gain,
            -option.minimum_puts,
        )
        no_worse = all(left >= right for left, right in zip(other_value, option_value))
        return no_worse and (
            other_targets != option_targets
            or other_value != option_value
            or other.rank < option.rank
        )

    def rolling_feasible_shared_put_targets(
        self,
        cars: list[dict[str, Any]],
        target_options: tuple[tuple[str, ...], ...],
        train: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        if not target_options:
            return ()
        common = set(target_options[0])
        for targets in target_options[1:]:
            common &= set(targets)
        moving_nos = {physical.car_no(car) for car in train}
        return tuple(
            target
            for target in sorted(common, key=lambda line: (self.target_rank(line), line))
            if physical.line_has_length_capacity(
                target,
                cars,
                train,
                moving_nos,
            )
        )

    def rolling_crosses_source_dependency(
        self,
        car: dict[str, Any],
        *,
        source: str,
        unresolved_sources: set[str],
    ) -> bool:
        if self.stage1_goal(car):
            return False
        targets = set(car.get("TargetLines") or ())
        return bool((targets - {source}) & unresolved_sources)

    def rolling_batch_minimum_puts(
        self,
        targets: tuple[tuple[str, ...], ...],
    ) -> int:
        if any(not options for options in targets):
            return 0
        return self.rolling_target_group_count(targets)

    def rolling_car_actionable(
        self,
        car: dict[str, Any],
        *,
        source: str,
        service_only: bool,
        allow_stage1_support: bool,
    ) -> bool:
        if not service_only and self.stage1_goal(car) and not self.stage1_car_complete(car):
            return True
        if (
            allow_stage1_support
            and not service_only
            and physical.car_no(car) in self.stage1_route_support_nos(source)
        ):
            return True
        if self.service_eligible(car) and not self.target_satisfied(car):
            return True
        return (
            service_only
            and physical.car_no(car) in self.service_support_nos(source)
        )

    def rolling_car_put_targets(
        self,
        car: dict[str, Any],
        *,
        source: str,
        service_only: bool,
        allow_stage1_support: bool,
        cars: list[dict[str, Any]] | None = None,
        excluded_nos: set[str] | None = None,
    ) -> tuple[str, ...]:
        if not service_only and self.stage1_goal(car) and not self.stage1_car_complete(car):
            return self.official_stage1_targets(car)
        if (
            allow_stage1_support
            and not service_only
            and physical.car_no(car) in self.stage1_route_support_nos(source)
        ):
            return self.official_stage1_targets(car)
        if self.service_eligible(car):
            if self.target_satisfied(car):
                return (source,)
            state_cars = self.cars if cars is None else cars
            real_targets = list(self.service_target_options(car))
            ready_targets = [
                target
                for target in real_targets
                if self.real_target_line_ready_in(
                    state_cars,
                    target,
                    excluded_nos=excluded_nos,
                    respect_forced_reservation=False,
                )
            ]
            targets = list(real_targets)
            if (
                service_only
                and physical.car_no(car) in self.service_peel_support_nos(source)
                and source not in targets
            ):
                targets.append(source)
            if (
                physical.car_no(car) in self.service_support_nos(source)
                and (not service_only or not ready_targets)
            ):
                targets.extend(
                    line
                    for line in sorted(STORAGE_CACHE_LINES, key=self.target_rank)
                    if line != source
                    and line not in targets
                    and line not in real_targets
                )
            if (
                allow_stage1_support
                and not service_only
                and physical.car_no(car) in self.stage1_prefix_support_nos(source)
            ):
                targets.extend(
                    line
                    for line in sorted(STORAGE_CACHE_LINES, key=self.target_rank)
                    if line != source
                    and line not in targets
                    and line not in real_targets
                )
            return tuple(targets)
        if self.protected_satisfied_car(car):
            return (source,)
        if (
            service_only
            and physical.car_no(car) in self.service_support_nos(source)
        ):
            return tuple(
                line
                for line in sorted(STORAGE_CACHE_LINES, key=self.target_rank)
                if line != source
            )
        return ()

    def stage1_route_support_nos(self, source: str) -> set[str]:
        cache_key = f"stage1-route:{source}"
        cached = self.service_support_cache.get(cache_key)
        if cached is not None:
            return cached
        blocker_key = "stage1-route:lines"
        blockers = self.service_support_cache.get(blocker_key)
        if blockers is None:
            blockers = set(self.pending_get_blocker_info(self.stage1_debt()).lines)
            self.service_support_cache[blocker_key] = blockers
        result = {
            physical.car_no(car)
            for car in self.cars
            if car.get("Line") == source
            and source in blockers
            and bool(self.stage1_goal(car))
            and self.stage1_car_complete(car)
        }
        self.service_support_cache[cache_key] = result
        return result

    def stage1_prefix_support_nos(self, source: str) -> set[str]:
        cache_key = f"stage1-prefix:{source}"
        cached = self.service_support_cache.get(cache_key)
        if cached is not None:
            return cached
        pending = set(self.stage1_debt()["pending_stage1_nos"])
        ordered = self.line_ordered_cars(source)
        first_pending = next(
            (
                index
                for index, car in enumerate(ordered)
                if physical.car_no(car) in pending
            ),
            -1,
        )
        if first_pending <= 0:
            result: set[str] = set()
        else:
            result = {
                physical.car_no(car)
                for car in ordered[:first_pending]
                if self.service_eligible(car) and not self.target_satisfied(car)
            }
        self.service_support_cache[cache_key] = result
        return result

    def service_peel_support_nos(self, source: str) -> set[str]:
        cache_key = f"peel:{source}"
        cached = self.service_support_cache.get(cache_key)
        if cached is not None:
            return cached
        ordered = self.line_ordered_cars(source)
        result: set[str] = set()
        for index, car in enumerate(ordered[1:], start=1):
            if not self.service_eligible(car) or self.target_satisfied(car):
                continue
            prefix = ordered[: index + 1]
            moving_nos = {physical.car_no(item) for item in prefix}
            reachable_target = any(
                target != source
                and self.real_target_line_ready(
                    target,
                    excluded_nos=moving_nos,
                    respect_forced_reservation=False,
                )
                and not self.route_blockers_for_put(source, target, moving_nos)
                for target in self.service_target_options(car)
            )
            blockers = ordered[:index]
            if not reachable_target or any(
                self.pending_weigh(blocker)
                or not (
                    self.target_satisfied(blocker)
                    or self.service_eligible(blocker)
                )
                for blocker in blockers
            ):
                continue
            result.update(
                physical.car_no(blocker)
                for blocker in blockers
                if not self.target_satisfied(blocker)
            )
        self.service_support_cache[cache_key] = result
        return result

    def service_support_nos(self, source: str) -> set[str]:
        cached = self.service_support_cache.get(source)
        if cached is not None:
            return cached
        ordered = self.line_ordered_cars(source)
        result: set[str] = set()
        bad_indexes = [
            index
            for index, car in enumerate(ordered)
            if not self.target_satisfied(car)
        ]
        if bad_indexes:
            first_bad = min(bad_indexes)
            suffix = ordered[first_bad : max(bad_indexes) + 1]
            joins_satisfied_segments = (
                not any(self.target_satisfied(car) for car in suffix)
                and any(
                    self.target_satisfied(car)
                    for car in ordered[max(bad_indexes) + 1 :]
                )
            )
            if joins_satisfied_segments:
                result.update(physical.car_no(car) for car in suffix)

        # A clean spotting line may need to be lifted temporarily so a pending
        # forced-position block can be rebuilt from the access end.
        if (
            ordered
            and physical.is_spotting_line(source)
            and self.real_target_line_ready(
                source,
                respect_forced_reservation=False,
            )
            and self.target_reserved_for_forced_cars(source)
        ):
            result.update(physical.car_no(car) for car in ordered)

        self.service_support_cache[source] = result
        return result

    def rolling_put_successors(
        self,
        state: OpenSessionState,
        *,
        service_only: bool,
    ) -> list[OpenSessionState]:
        if not state.carried:
            return []
        options: list[tuple[tuple[Any, ...], int, str]] = []
        common_targets: set[str] | None = None
        for start in range(len(state.carried) - 1, -1, -1):
            targets = set(state.carried_targets[start])
            common_targets = targets if common_targets is None else common_targets & targets
            if not common_targets:
                break
            move_nos = state.carried[start:]
            origins = state.carried_origins[start:]
            for target in sorted(common_targets, key=lambda line: (self.target_rank(line), line)):
                if target in FORBIDDEN_LINES:
                    continue
                simple_return = (
                    len(state.steps) == 1
                    and all(origin == target for origin in origins)
                )
                if simple_return:
                    continue
                options.append((
                    (
                        -len(move_nos),
                        self.target_rank(target),
                        target,
                        move_nos,
                    ),
                    start,
                    target,
                ))

        successors: list[OpenSessionState] = []
        for _option_rank, start, target in sorted(options, key=lambda item: item[0]):
            if len(successors) >= ROLLING_SESSION_PUT_BRANCH:
                break
            move_nos = state.carried[start:]
            transition = self.rolling_route_transition(
                state,
                action="Put",
                line=target,
                move_nos=move_nos,
            )
            if transition is None:
                continue
            next_loco, route_cost = transition
            working_cars = [dict(car) for car in state.cars]
            put_step = self.planned_session_put_step(working_cars, target, move_nos)
            if put_step is None:
                continue
            if self.put_creates_service_landing_barrier(
                working_cars,
                target=target,
                move_nos=move_nos,
                origins=state.carried_origins[start:],
            ):
                continue
            if (
                not service_only
                and self.rolling_put_closes_unresolved_source(
                    working_cars,
                    target=target,
                    move_nos=move_nos,
                    origins=state.carried_origins[start:],
                )
            ):
                continue
            carried = state.carried[:start]
            (
                service_count,
                forced_count,
                completed_g_count,
                carried_g_count,
                carried_service_count,
            ) = self.rolling_state_counts(working_cars, state.moved_order, carried)
            if (
                self.target_reserved_for_forced_cars_in(
                    state.cars,
                    target,
                    set(move_nos),
                )
                and forced_count <= state.forced_count
            ):
                continue
            successor = OpenSessionState(
                cars=working_cars,
                carried=carried,
                carried_origins=state.carried_origins[:start],
                carried_targets=state.carried_targets[:start],
                steps=(*state.steps, put_step),
                moved_order=state.moved_order,
                source_lines=state.source_lines,
                touched_lines=state.touched_lines | {target},
                loco=next_loco,
                route_cost=state.route_cost + route_cost,
                reget_steps=state.reget_steps,
                service_count=service_count,
                forced_count=forced_count,
                completed_g_count=completed_g_count,
                carried_g_count=carried_g_count,
                carried_service_count=carried_service_count,
            )
            successors.append(successor)
        return successors

    def put_would_close_dirty_service_target(
        self,
        cars: list[dict[str, Any]],
        *,
        target: str,
        move_nos: tuple[str, ...],
        origins: tuple[str, ...],
    ) -> bool:
        if target not in SERVICE_LINE_SET:
            return False
        by_no = {physical.car_no(car): car for car in cars}
        original_by_no = self.by_no()
        creates_new_landing = any(
            no in by_no
            and target in set(by_no[no].get("TargetLines") or ())
            and not (
                origin == target
                and no in original_by_no
                and self.target_satisfied(original_by_no[no])
            )
            for no, origin in zip(move_nos, origins)
        )
        return creates_new_landing and not self.real_target_line_ready_in(
            cars,
            target,
            respect_forced_reservation=False,
        )

    def put_creates_service_landing_barrier(
        self,
        cars: list[dict[str, Any]],
        *,
        target: str,
        move_nos: tuple[str, ...],
        origins: tuple[str, ...],
    ) -> bool:
        if target not in SERVICE_LINE_SET:
            return False
        original_by_no = self.by_no()
        landed_nos = {
            no
            for no, origin in zip(move_nos, origins)
            if no in original_by_no
            and target in set(original_by_no[no].get("TargetLines") or ())
            and not (
                origin == target
                and self.target_satisfied(original_by_no[no])
            )
        }
        if not landed_nos:
            return False
        landed_seen = False
        for car in self.line_ordered_cars_in(cars, target):
            if physical.car_no(car) in landed_nos:
                landed_seen = True
                continue
            if landed_seen and not self.target_satisfied(car):
                return True
        return False

    def rolling_put_closes_unresolved_source(
        self,
        cars: list[dict[str, Any]],
        *,
        target: str,
        move_nos: tuple[str, ...],
        origins: tuple[str, ...],
    ) -> bool:
        if not any(
            car["Line"] == target
            and self.stage1_goal(car)
            and not self.stage1_car_complete(car)
            for car in cars
        ):
            return False
        by_no = {physical.car_no(car): car for car in cars}
        return any(
            origin != target
            and no in by_no
            and self.service_eligible(by_no[no])
            for no, origin in zip(move_nos, origins)
        )

    def rolling_route_transition(
        self,
        state: OpenSessionState,
        *,
        action: str,
        line: str,
        move_nos: tuple[str, ...],
    ) -> tuple[physical.LocoLocation, int] | None:
        carried = set(state.carried)
        if action == "Get":
            moving_nos = carried | set(move_nos)
            train_length = physical.train_length_for_nos(state.cars, carried)
            occupied = physical.occupied_lines_for_get_route(
                state.cars,
                moving_nos,
                line,
            )
            raw_path = self.graph.route_avoiding_occupied(
                state.loco.line,
                line,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(
                    state.loco.line,
                    state.cars,
                    moving_nos,
                ),
                target_approach_lines=physical.route_approach_lines_for_get(line),
                cars=state.cars,
                moving_nos=moving_nos,
                train_length_m=train_length,
            )
            path = tuple(physical.route_with_line_prefix(state.loco.line, raw_path))
            next_loco = physical.operation_stand_location(path, line)
        elif action == "Put":
            if not set(move_nos) <= carried:
                return None
            moving_nos = carried
            train_length = physical.train_length_for_nos(state.cars, carried)
            occupied = physical.occupied_lines_for_route(state.cars, moving_nos)
            raw_path = self.graph.route_avoiding_occupied(
                state.loco.line,
                line,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(
                    state.loco.line,
                    state.cars,
                    moving_nos,
                ),
                target_approach_lines=physical.route_approach_lines_for_put(
                    line,
                    state.cars,
                    moving_nos,
                ),
                cars=state.cars,
                moving_nos=moving_nos,
                train_length_m=train_length,
            )
            path = tuple(physical.route_with_line_prefix(state.loco.line, raw_path))
            next_loco = physical.post_put_loco_location(path, line)
        else:
            return None
        if not path:
            return None
        if physical.route_line_length_reasons(path, train_length):
            return None
        if physical.pre_repair_reversal_reasons(
            path,
            state.cars,
            moving_nos,
            train_length,
        ):
            return None
        return next_loco, len(physical.route_for_output(path))

    def rolling_endpoint_view(
        self,
        state: OpenSessionState,
        *,
        debt: dict[str, Any],
        service_only: bool,
    ) -> tuple[tuple[Any, ...], CandidateView] | None:
        if not state.steps or state.carried:
            return None
        metrics = self.session_metrics(state.steps)
        if not metrics.stack_valid:
            return None
        get_count = sum(step.action == "Get" for step in state.steps)
        put_count = sum(step.action == "Put" for step in state.steps)
        by_no = self.by_no()
        batch = [by_no[no] for no in state.moved_order if no in by_no]
        moved_g = sum(
            bool(self.stage1_goal(car)) and not self.stage1_car_complete(car)
            for car in batch
        )
        before_service, before_forced = self.rolling_service_counts(self.cars)
        after_service, after_forced = state.service_count, state.forced_count
        if after_forced < before_forced or after_service < before_service:
            return None
        if not service_only and moved_g <= 0 and after_forced <= before_forced:
            return None
        if service_only and (after_forced, after_service) <= (before_forced, before_service):
            return None
        if get_count == put_count == 1:
            target = state.steps[-1].line
            moving_nos = set(state.steps[-1].move_car_nos)
            if moved_g or self.real_target_line_ready(target, excluded_nos=moving_nos):
                return None

        reason = (
            "stage1_rolling_session"
            if moved_g
            else "stage1_service_rolling_session"
        )
        candidate = self.make_planlet_candidate(
            source=state.steps[0].line,
            target=state.steps[-1].line,
            batch=batch,
            steps=state.steps,
            kind="vnext_depot_inbound_mixed_extraction_session",
            reason=reason,
        )
        if candidate is None or self.candidate_closure_savings(candidate) < 0:
            return None
        view = CandidateView(
            candidate,
            self.score_candidate(
                candidate,
                debt=debt,
                moved_g=moved_g,
                moved_x=len(batch) - moved_g,
                reason_rank=0 if moved_g else 2,
            ),
            reason,
        )
        economy_rank = (
            (
                state.reget_steps,
                metrics.business_hooks,
                -self.candidate_closure_savings(candidate),
            )
        )
        rank = (
            -state.completed_g_count if not service_only else -(after_forced - before_forced),
            -(after_forced - before_forced),
            -(after_service - before_service),
            *economy_rank,
            state.route_cost,
            view.score,
        )
        return rank, view

    def rolling_service_counts(self, cars: list[dict[str, Any]]) -> tuple[int, int]:
        service = sum(
            self.service_eligible(car) and self.target_satisfied(car)
            for car in cars
        )
        forced = sum(
            self.service_eligible(car)
            and bool(physical.force_positions(car))
            and self.target_satisfied(car)
            for car in cars
        )
        return service, forced

    def rolling_state_counts(
        self,
        cars: list[dict[str, Any]],
        moved_order: tuple[str, ...],
        carried: tuple[str, ...],
    ) -> tuple[int, int, int, int, int]:
        service, forced = self.rolling_service_counts(cars)
        moved = set(moved_order)
        carried_set = set(carried)
        completed_g = sum(
            bool(self.stage1_goal(car)) and self.stage1_car_complete(car)
            for car in cars
            if physical.car_no(car) in moved
        )
        carried_g = sum(
            bool(self.stage1_goal(car))
            for car in cars
            if physical.car_no(car) in carried_set
        )
        carried_service = sum(
            self.service_eligible(car)
            for car in cars
            if physical.car_no(car) in carried_set
        )
        return service, forced, completed_g, carried_g, carried_service

    def prune_rolling_states(
        self,
        states: list[OpenSessionState],
        *,
        service_only: bool,
    ) -> list[OpenSessionState]:
        feasible = [
            state
            for state in states
            if len(state.steps) + self.rolling_minimum_puts(state) <= ROLLING_SESSION_MAX_STEPS
        ]
        unique: dict[tuple[Any, ...], OpenSessionState] = {}
        for state in sorted(
            feasible,
            key=lambda item: self.rolling_state_rank(item, service_only=service_only),
        ):
            unique.setdefault(self.rolling_state_signature(state), state)

        groups: dict[tuple[Any, ...], list[OpenSessionState]] = {}
        for state in unique.values():
            groups.setdefault(self.rolling_state_shape(state), []).append(state)
        interleaved = sorted(
            (
                (
                    group_index,
                    self.rolling_state_rank(state, service_only=service_only),
                    state,
                )
                for group in groups.values()
                for group_index, state in enumerate(group[:ROLLING_SESSION_STATES_PER_SHAPE])
            ),
            key=lambda item: (item[0], item[1]),
        )
        return [item[2] for item in interleaved[:ROLLING_SESSION_BEAM_WIDTH]]

    def rolling_state_rank(
        self,
        state: OpenSessionState,
        *,
        service_only: bool,
    ) -> tuple[Any, ...]:
        minimum_puts = self.rolling_minimum_puts(state)
        potential_g = state.completed_g_count + state.carried_g_count
        projected_business_hooks = len(state.steps) + minimum_puts
        return (
            -state.forced_count if service_only else -potential_g,
            -state.carried_service_count if service_only else -state.completed_g_count,
            -state.service_count,
            -potential_g,
            state.reget_steps,
            projected_business_hooks,
            minimum_puts,
            len(state.steps),
            -len(state.source_lines),
            state.route_cost,
            state.carried,
        )

    def rolling_state_shape(self, state: OpenSessionState) -> tuple[Any, ...]:
        has_put = any(step.action == "Put" for step in state.steps)
        by_no = {physical.car_no(car): car for car in state.cars}
        carried_cars = [by_no[no] for no in state.carried if no in by_no]
        shared_put_targets = self.rolling_feasible_shared_put_targets(
            state.cars,
            state.carried_targets,
            carried_cars,
        )
        return (
            bool(state.source_lines & set(ASSEMBLY_ALL)),
            has_put,
            state.steps[-1].action,
            bool(state.carried_g_count),
            bool(state.carried_service_count),
            min(len(state.source_lines), 4),
            min(self.rolling_minimum_puts(state), 4),
            shared_put_targets,
        )

    def rolling_endpoint_shape(self, view: CandidateView) -> tuple[Any, ...]:
        steps = physical.candidate_plan_steps(view.candidate)
        metrics = self.session_metrics(steps)
        source_count = len({step.line for step in steps if step.action == "Get"})
        put_count = sum(step.action == "Put" for step in steps)
        return (
            any(step.action == "Get" and step.line in ASSEMBLY_ALL for step in steps),
            metrics.retains_across_put_then_get,
            min(source_count, 4),
            min(put_count, 4),
        )

    def rolling_minimum_puts(self, state: OpenSessionState) -> int:
        return self.rolling_target_group_count(state.carried_targets)

    def rolling_target_group_count(
        self,
        target_options: tuple[tuple[str, ...], ...],
    ) -> int:
        if not target_options:
            return 0
        groups = 1
        common = set(target_options[-1])
        for targets in reversed(target_options[:-1]):
            overlap = common & set(targets)
            if overlap:
                common = overlap
                continue
            groups += 1
            common = set(targets)
        return groups

    def rolling_state_signature(self, state: OpenSessionState) -> tuple[Any, ...]:
        lines = []
        for line in sorted(state.touched_lines):
            rows = sorted(
                (
                    int(car.get("Position") or 0),
                    physical.car_no(car),
                )
                for car in state.cars
                if car.get("Line") == line
            )
            lines.append((line, tuple(rows)))
        return (
            state.loco.line,
            state.carried,
            state.carried_targets,
            tuple(lines),
        )

    def planned_session_put_step(
        self,
        working_cars: list[dict[str, Any]],
        target: str,
        move_nos: tuple[str, ...],
    ) -> physical.PlanStep | None:
        working_by_no = {physical.car_no(car): car for car in working_cars}
        batch = [working_by_no[no] for no in move_nos if no in working_by_no]
        if len(batch) != len(move_nos):
            return None
        if not physical.line_has_length_capacity(
            target,
            working_cars,
            batch,
            set(move_nos),
        ):
            return None
        planned_positions: dict[str, int] = {}
        if physical.is_spotting_line(target) and any(
            physical.force_positions(car) for car in batch
        ):
            planned_positions = physical.planned_positions_for_batch(
                batch,
                target,
                working_cars,
                self.depot_assignment,
                set(move_nos),
            )
            if len(planned_positions) != len(move_nos):
                return None
        physical.apply_physical_put_order(
            working_cars,
            target,
            list(move_nos),
            planned_positions,
        )
        return physical.plan_step("Put", target, move_nos, planned_positions)

    def service_prefix_for_target(self, source: str, target: str) -> list[dict[str, Any]]:
        prefix: list[dict[str, Any]] = []
        pull_equivalent = 0
        for car in self.line_ordered_cars(source):
            if (
                not self.service_eligible(car)
                or self.target_satisfied(car)
                or target not in self.service_target_options(car)
                or self.pending_weigh(car)
            ):
                break
            pull_equivalent += 4 if bool(car.get("IsHeavy")) else 1
            if pull_equivalent > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
        return prefix

    def service_prefix_cleanup_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[CandidateView]:
        if not debt["complete"] or debt["pollution_nos"]:
            return
        for source in SERVICE_LINES:
            ordered = self.line_ordered_cars(source)
            bad_indexes = [
                index
                for index, car in enumerate(ordered)
                if not self.target_satisfied(car)
            ]
            if not bad_indexes:
                continue
            first_bad = min(bad_indexes)
            clear_batch = ordered[: max(bad_indexes) + 1]
            retained_prefix = clear_batch[:first_bad]
            blocker_suffix = clear_batch[first_bad:]
            if (
                not retained_prefix
                or any(not self.target_satisfied(car) for car in retained_prefix)
                or any(self.target_satisfied(car) for car in blocker_suffix)
                or physical.pull_equivalent(clear_batch) > physical.PULL_LIMIT_EQUIVALENT
            ):
                continue
            clear_nos = tuple(physical.car_no(car) for car in clear_batch)
            retained_nos = tuple(physical.car_no(car) for car in retained_prefix)
            blocker_nos = tuple(physical.car_no(car) for car in blocker_suffix)
            for target in sorted(
                STORAGE_CACHE_LINES - {source},
                key=lambda line: (self.target_rank(line), line),
            ):
                working_cars = [self.clone_car(car) for car in self.cars]
                physical.apply_physical_get_order(working_cars, source, clear_nos)
                blocker_put = self.planned_session_put_step(
                    working_cars,
                    target,
                    blocker_nos,
                )
                if blocker_put is None:
                    continue
                retained_put = self.planned_session_put_step(
                    working_cars,
                    source,
                    retained_nos,
                )
                if retained_put is None:
                    continue
                steps = (
                    physical.plan_step("Get", source, clear_nos),
                    blocker_put,
                    retained_put,
                )
                candidate = self.make_planlet_candidate(
                    source=source,
                    target=target,
                    batch=clear_batch,
                    steps=steps,
                    kind="vnext_depot_inbound_mixed_extraction_session",
                    reason="stage1_service_prefix_cleanup",
                )
                if candidate is None:
                    continue
                yield CandidateView(
                    candidate,
                    self.score_candidate(
                        candidate,
                        debt=debt,
                        moved_g=0,
                        moved_x=len(clear_batch),
                        reason_rank=2,
                    ),
                    "stage1_service_prefix_cleanup",
                )

    def spotting_repack_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not debt["complete"] or debt["pollution_nos"]:
            return
        for target in SERVICE_LINES:
            if not physical.is_spotting_line(target):
                continue
            batch = self.line_ordered_cars(target)
            wrong_position_count = sum(
                1
                for car in batch
                if self.service_forced_position_debt(car)
            )
            if not wrong_position_count or not batch:
                continue
            if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
                continue
            move_nos = tuple(physical.car_no(car) for car in batch)
            route_blockers = self.retainable_service_route_blockers(
                target,
                set(move_nos),
            )
            if route_blockers is None:
                continue
            retained_blockers = [
                car
                for _line, cars in route_blockers
                for car in cars
            ]
            if (
                physical.pull_equivalent([*retained_blockers, *batch])
                > physical.PULL_LIMIT_EQUIVALENT
            ):
                continue
            working_cars = [self.clone_car(car) for car in self.cars]
            steps: list[physical.PlanStep] = []
            for blocker_line, blocker_cars in route_blockers:
                blocker_nos = tuple(physical.car_no(car) for car in blocker_cars)
                physical.apply_physical_get_order(
                    working_cars,
                    blocker_line,
                    blocker_nos,
                )
                steps.append(physical.plan_step("Get", blocker_line, blocker_nos))
            physical.apply_physical_get_order(working_cars, target, move_nos)
            steps.append(physical.plan_step("Get", target, move_nos))
            target_put = self.planned_session_put_step(
                working_cars,
                target,
                move_nos,
            )
            if target_put is None:
                continue
            steps.append(target_put)
            valid = True
            for blocker_line, blocker_cars in reversed(route_blockers):
                blocker_nos = tuple(physical.car_no(car) for car in blocker_cars)
                blocker_put = self.planned_session_put_step(
                    working_cars,
                    blocker_line,
                    blocker_nos,
                )
                if blocker_put is None:
                    valid = False
                    break
                steps.append(blocker_put)
            if not valid:
                continue
            candidate = self.make_planlet_candidate(
                source=steps[0].line,
                target=steps[-1].line,
                batch=[*retained_blockers, *batch],
                steps=tuple(steps),
                kind="vnext_depot_inbound_mixed_extraction_session",
                reason="stage1_service_spotting_repack",
            )
            if not candidate:
                continue
            yield CandidateView(
                candidate,
                self.score_candidate(
                    candidate,
                    debt=debt,
                    moved_g=0,
                    moved_x=len(retained_blockers) + len(batch),
                    reason_rank=1,
                ),
                "stage1_service_spotting_repack",
            )

    def target_rebuild_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        if not debt["complete"] or debt["pollution_nos"]:
            return
        views: list[tuple[tuple[Any, ...], CandidateView]] = []
        for target in SERVICE_LINES:
            target_cars = self.line_ordered_cars(target)
            bad_indexes = [
                index
                for index, car in enumerate(target_cars)
                if not self.target_satisfied(car)
            ]
            if not bad_indexes:
                continue
            clear_batch = target_cars[: max(bad_indexes) + 1]
            first_bad = min(bad_indexes)
            retained_prefix = clear_batch[:first_bad]
            blocker_suffix = clear_batch[first_bad:]
            if (
                not blocker_suffix
                or any(self.target_satisfied(car) for car in blocker_suffix)
            ):
                continue
            clear_nos = {physical.car_no(car) for car in clear_batch}
            if not self.real_target_line_ready(
                target,
                excluded_nos=clear_nos,
                respect_forced_reservation=False,
            ):
                continue

            for source in self.active_lines():
                if source in FORBIDDEN_LINES or source in ASSEMBLY_ALL or source == target:
                    continue
                incoming = self.service_prefix_for_target(source, target)
                if not incoming:
                    continue
                for incoming_batch in self.prefix_options(incoming):
                    for staging in self.safe_temp_targets(source=target, debt=debt):
                        if staging in {source, target} or not self.target_allowed(staging, blocker_suffix):
                            continue
                        steps = self.build_target_rebuild_steps(
                            target=target,
                            clear_batch=clear_batch,
                            retained_prefix=retained_prefix,
                            blocker_suffix=blocker_suffix,
                            staging=staging,
                            source=source,
                            incoming=incoming_batch,
                        )
                        if not steps:
                            continue
                        batch = [*clear_batch, *incoming_batch]
                        candidate = self.make_planlet_candidate(
                            source=target,
                            target=target,
                            batch=batch,
                            steps=steps,
                            kind="vnext_depot_inbound_mixed_extraction_session",
                            reason="stage1_service_target_rebuild",
                        )
                        if not candidate:
                            continue
                        service_gain = self.candidate_service_delivery_count(candidate)
                        business_hooks = self.candidate_business_hook_count(candidate)
                        if service_gain <= 0 or service_gain * 2 < business_hooks:
                            continue
                        view = CandidateView(
                            candidate,
                            self.score_candidate(
                                candidate,
                                debt=debt,
                                moved_g=0,
                                moved_x=len(batch),
                                reason_rank=2,
                            ),
                            "stage1_service_target_rebuild",
                        )
                        views.append((
                            (
                                -service_gain,
                                len(blocker_suffix),
                                self.route_price(candidate),
                                view.score,
                            ),
                            view,
                        ))
        seen: set[str] = set()
        for _rank, view in sorted(views, key=lambda item: item[0]):
            if view.candidate.candidate_id in seen:
                continue
            seen.add(view.candidate.candidate_id)
            yield view
            if len(seen) >= TARGET_REBUILD_MAX_CANDIDATES:
                break

    def build_target_rebuild_steps(
        self,
        *,
        target: str,
        clear_batch: list[dict[str, Any]],
        retained_prefix: list[dict[str, Any]],
        blocker_suffix: list[dict[str, Any]],
        staging: str,
        source: str,
        incoming: list[dict[str, Any]],
    ) -> tuple[physical.PlanStep, ...]:
        clear_nos = tuple(physical.car_no(car) for car in clear_batch)
        retained_nos = tuple(physical.car_no(car) for car in retained_prefix)
        blocker_nos = tuple(physical.car_no(car) for car in blocker_suffix)
        incoming_nos = tuple(physical.car_no(car) for car in incoming)
        working_cars = [self.clone_car(car) for car in self.cars]
        physical.apply_physical_get_order(working_cars, target, clear_nos)
        physical.apply_physical_put_order(working_cars, staging, list(blocker_nos))
        physical.apply_physical_get_order(working_cars, source, incoming_nos)
        planned_positions = self.planned_service_put_positions(
            working_cars=working_cars,
            target=target,
            move_nos=incoming_nos,
        )
        if planned_positions is None:
            return ()
        steps = [
            physical.plan_step("Get", target, clear_nos),
            physical.plan_step("Put", staging, blocker_nos),
            physical.plan_step("Get", source, incoming_nos),
            physical.plan_step("Put", target, incoming_nos, planned_positions),
        ]
        physical.apply_physical_put_order(
            working_cars,
            target,
            list(incoming_nos),
            planned_positions,
        )
        if retained_nos:
            retained_positions = self.planned_service_put_positions(
                working_cars=working_cars,
                target=target,
                move_nos=retained_nos,
            )
            if retained_positions is None:
                return ()
            steps.append(physical.plan_step("Put", target, retained_nos, retained_positions))
        return tuple(steps)

    def planned_service_put_positions(
        self,
        *,
        working_cars: list[dict[str, Any]],
        target: str,
        move_nos: tuple[str, ...],
    ) -> dict[str, int] | None:
        working_by_no = {physical.car_no(car): car for car in working_cars}
        batch = [working_by_no[no] for no in move_nos if no in working_by_no]
        if len(batch) != len(move_nos):
            return None
        if not (
            physical.is_spotting_line(target)
            and any(physical.force_positions(car) for car in batch)
        ):
            return {}
        planned = physical.planned_positions_for_batch(
            batch,
            target,
            working_cars,
            self.depot_assignment,
            set(move_nos),
        )
        return planned if len(planned) == len(move_nos) else None

    def blocker_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        by_no = self.by_no()
        pending = set(debt["pending_stage1_nos"])
        deferred_sources = set(self.deferred_stage1_source_dependencies(debt))
        for line in self.active_lines():
            if line in FORBIDDEN_LINES or line in deferred_sources:
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
                    view = self.single_put_view(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="clear_prefix_blocker",
                        debt=debt,
                        moved_g=0,
                        moved_x=len(batch),
                        reason_rank=1,
                    )
                    if view:
                        yield view

    def deferred_stage1_source_dependencies(
        self,
        debt: dict[str, Any],
    ) -> dict[str, str]:
        unresolved = set(debt["lines_with_pending_stage1"])
        pending = set(debt["pending_stage1_nos"])
        by_no = self.by_no()
        dependencies: dict[str, str] = {}
        for source in unresolved:
            ordered_nos = physical.line_access_order(self.cars, source)
            first_pending = next(
                (index for index, no in enumerate(ordered_nos) if no in pending),
                -1,
            )
            if first_pending <= 0:
                continue
            first_blocker = by_no.get(ordered_nos[0])
            if not first_blocker:
                continue
            target = self.common_current_target([first_blocker])
            if target in unresolved and target != source:
                dependencies[source] = target

        cycle_nodes: set[str] = set()
        for start in dependencies:
            path: list[str] = []
            path_index: dict[str, int] = {}
            node = start
            while node in dependencies:
                if node in path_index:
                    cycle_nodes.update(path[path_index[node]:])
                    break
                path_index[node] = len(path)
                path.append(node)
                node = dependencies[node]
        return {
            source: target
            for source, target in dependencies.items()
            if source not in cycle_nodes
        }

    def route_blocker_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[CandidateView]:
        for blocker_line, reason in self.route_blocker_requests(debt):
            if blocker_line in FORBIDDEN_LINES:
                continue
            ordered = self.line_ordered_cars(blocker_line)
            if not ordered:
                continue
            for batch in self.prefix_options(ordered):
                for target in self.blocker_targets(
                    batch,
                    source=blocker_line,
                    debt=debt,
                ):
                    view = self.single_put_view(
                        source=blocker_line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason=reason,
                        debt=debt,
                        moved_g=0,
                        moved_x=len(batch),
                        reason_rank=1,
                    )
                    if view:
                        yield view

    def cleanup_candidates(self, debt: dict[str, Any]) -> Iterable[CandidateView]:
        for line in ASSEMBLY_ALL:
            ordered = self.line_ordered_cars(line)
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
                    view = self.single_put_view(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="stage_boundary_cleanup",
                        debt=debt,
                        moved_g=0,
                        moved_x=len(batch),
                        reason_rank=2,
                    )
                    if view:
                        yield view

    def route_blocker_requests(
        self,
        debt: dict[str, Any],
    ) -> list[tuple[str, str]]:
        by_no = self.by_no()
        seeds: list[tuple[str, set[str], str, int]] = []
        requests: list[tuple[str, str]] = []
        seen_requests: set[tuple[str, str]] = set()
        for no in debt["pending_stage1_nos"]:
            car = by_no.get(no)
            if car:
                seeds.append((car["Line"], {no}, f"clear_route_blocker_for:{no}", 0))
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
        for line in ("洗油北", "机走棚", "机走北"):
            ordered = self.line_ordered_cars(line)
            if not ordered:
                continue
            movable = [car for car in ordered if self.stage1_goal(car)]
            if not movable:
                continue
            for batch in self.prefix_options(movable[:2]):
                for target in ASSEMBLY_DEPOT:
                    if target == line:
                        continue
                    view = self.single_put_view(
                        source=line,
                        target=target,
                        batch=batch,
                        kind="blocker_relocation",
                        reason="release_gate_assembly",
                        debt=debt,
                        moved_g=0,
                        moved_x=0,
                        reason_rank=3,
                    )
                    if view:
                        yield view

    def blocker_targets(
        self,
        batch: list[dict[str, Any]],
        *,
        source: str,
        debt: dict[str, Any],
    ) -> list[str]:
        targets: list[str] = []
        direct = self.common_current_target(batch)
        unresolved_sources = set(debt["lines_with_pending_stage1"])
        if (
            direct
            and direct not in FORBIDDEN_LINES
            and direct != source
            and direct not in unresolved_sources
            and self.stage1_real_target_available(direct)
            and not self.target_reserved_for_forced_cars(
                direct,
                {physical.car_no(car) for car in batch},
            )
            and not self.would_pollute_stage_boundary(direct, batch)
        ):
            targets.append(direct)
        targets.extend(self.grouped_cache_targets(batch, source=source, debt=debt))
        targets.extend(self.safe_temp_targets(source=source, debt=debt))
        return [
            target
            for target in dict.fromkeys(targets)
            if not self.pollutes_clean_service_line(target, batch)
        ]

    def pollutes_clean_service_line(
        self,
        target: str,
        batch: list[dict[str, Any]],
    ) -> bool:
        if target not in SERVICE_LINE_SET or target in STORAGE_CACHE_LINES:
            return False
        if any(target in set(car.get("TargetLines") or ()) for car in batch):
            return False
        existing = self.line_ordered_cars(target)
        return not existing or all(self.target_satisfied(car) for car in existing)

    def grouped_cache_targets(self, batch: list[dict[str, Any]], *, source: str, debt: dict[str, Any]) -> list[str]:
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
        pull_equivalent = 0
        for car in cars:
            pull_equivalent += 4 if bool(car.get("IsHeavy")) else 1
            if pull_equivalent > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
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

    def single_put_view(
        self,
        *,
        source: str,
        target: str,
        batch: list[dict[str, Any]],
        kind: str,
        reason: str,
        debt: dict[str, Any],
        moved_g: int,
        moved_x: int,
        reason_rank: int,
    ) -> CandidateView | None:
        if not self.target_allowed(target, batch):
            return None
        candidate = self.make_candidate(
            source=source,
            target=target,
            batch=batch,
            kind=kind,
            reason=reason,
        )
        if not candidate:
            return None
        return CandidateView(
            candidate,
            self.score_candidate(
                candidate,
                debt=debt,
                moved_g=moved_g,
                moved_x=moved_x,
                reason_rank=reason_rank,
            ),
            reason,
        )

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
        planned_positions: dict[str, int] = {}
        if physical.is_spotting_line(target) and any(
            physical.force_positions(car) for car in batch
        ):
            planned_positions = physical.planned_positions_for_batch(
                batch,
                target,
                self.cars,
                self.depot_assignment,
                set(nos),
            )
            if len(planned_positions) != len(batch):
                return None
        steps = (
            physical.plan_step("Get", source, nos),
            physical.plan_step("Put", target, nos, planned_positions),
        )
        return self.make_planlet_candidate(
            source=source,
            target=target,
            batch=batch,
            steps=steps,
            kind=kind,
            reason=reason,
        )

    def make_planlet_candidate(
        self,
        *,
        source: str,
        target: str,
        batch: list[dict[str, Any]],
        steps: tuple[physical.PlanStep, ...],
        kind: str,
        reason: str,
    ) -> physical.HookCandidate | None:
        if source in FORBIDDEN_LINES or target in FORBIDDEN_LINES:
            return None
        steps = self.coalesce_adjacent_put_steps(steps)
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

    def coalesce_adjacent_put_steps(
        self,
        steps: tuple[physical.PlanStep, ...],
    ) -> tuple[physical.PlanStep, ...]:
        by_no = self.by_no()
        normalized: list[physical.PlanStep] = []
        for step in steps:
            if not normalized:
                normalized.append(step)
                continue
            previous = normalized[-1]
            if (
                previous.action != "Put"
                or step.action != "Put"
                or previous.line != step.line
            ):
                normalized.append(step)
                continue
            combined_nos = (*step.move_car_nos, *previous.move_car_nos)
            if any(
                no in by_no and self.pending_weigh(by_no[no])
                for no in combined_nos
            ):
                normalized.append(step)
                continue
            combined_positions = {
                **previous.planned_positions,
                **step.planned_positions,
            }
            if combined_positions and set(combined_positions) != set(combined_nos):
                normalized.append(step)
                continue
            normalized[-1] = physical.plan_step(
                "Put",
                step.line,
                combined_nos,
                combined_positions,
            )
        return tuple(normalized)

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
            -len(set(candidate.move_car_nos) & set(debt["pollution_nos"])),
            self.stage_boundary_target_penalty(candidate, debt),
            self.route_unlock_rank(candidate, debt),
            self.candidate_source_rank(candidate),
            reason_rank,
            -self.candidate_service_delivery_count(candidate),
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
        moved = [car for car in self.cars if physical.car_no(car) in set(candidate.move_car_nos)]
        if not moved:
            return 1
        if any(self.stage1_goal(car) for car in moved):
            return 1
        destinations = self.candidate_put_line_by_no(candidate)
        direct = [
            car
            for car in moved
            if self.stage4_target_line_satisfied_by(
                car,
                destinations.get(physical.car_no(car), ""),
            )
        ]
        if len(direct) == len(moved):
            return 0 if set(destinations.values()) & HIGH_VALUE_STAGE4_TARGETS else 1
        if direct:
            return 2
        return 3

    def real_target_static_rank(self, candidate: physical.HookCandidate) -> int:
        return 0 if self.safe_static_real_target_put(candidate) else 1

    def candidate_moved_cars(self, candidate: physical.HookCandidate) -> list[dict[str, Any]]:
        by_no = self.by_no()
        moved = [by_no[no] for no in candidate.move_car_nos if no in by_no]
        return moved if len(moved) == len(candidate.move_car_nos) else []

    def candidate_put_line_by_no(self, candidate: physical.HookCandidate) -> dict[str, str]:
        destinations: dict[str, str] = {}
        for step in physical.candidate_plan_steps(candidate):
            if step.action != "Put":
                continue
            for no in step.move_car_nos:
                destinations[no] = step.line
        return destinations

    def candidate_service_delivery_count(self, candidate: physical.HookCandidate) -> int:
        destinations = self.candidate_put_line_by_no(candidate)
        return sum(
            1
            for car in self.candidate_moved_cars(candidate)
            if self.service_eligible(car)
            and not self.target_satisfied(car)
            and destinations.get(physical.car_no(car)) in set(car.get("TargetLines") or ())
        )

    def clean_real_target_put(self, candidate: physical.HookCandidate) -> bool:
        moving_nos = set(candidate.move_car_nos)
        if not moving_nos:
            return False
        moved = self.candidate_moved_cars(candidate)
        if not moved:
            return False
        if any(self.stage1_goal(car) for car in moved):
            return False
        destinations = self.candidate_put_line_by_no(candidate)
        if any(
            not self.car_can_use_real_target(
                car,
                destinations.get(physical.car_no(car), ""),
                allow_pending_weigh=True,
                require_unsatisfied=False,
            )
            for car in moved
        ):
            return False
        return all(
            self.real_target_line_ready(target, excluded_nos=moving_nos)
            for target in set(destinations.values())
        )

    def safe_static_real_target_put(self, candidate: physical.HookCandidate) -> bool:
        if candidate.candidate_kind not in {
            "blocker_relocation",
            "vnext_depot_inbound_mixed_extraction_session",
        }:
            return False
        put_lines = set(self.candidate_put_line_by_no(candidate).values())
        if not put_lines or not put_lines <= REAL_TARGET_STATIC_LINES:
            return False
        if any(not self.stage1_real_target_available(line) for line in put_lines):
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
        put_lines = set(self.candidate_put_line_by_no(candidate).values())
        return 1 if put_lines & set(ASSEMBLY_DEPOT) else 0

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

    def pending_get_blocker_info(self, debt: dict[str, Any]) -> GetBlockerInfo:
        blocker_map = self.pending_route_blocker_map(debt)
        lines = frozenset(blocker_map)
        blocked_nos = frozenset({
            no
            for nos in blocker_map.values()
            for no in nos
        })
        blocker_cars = [car for car in self.cars if car.get("Line") in lines]
        completed_stage1_cars = sum(
            bool(self.stage1_goal(car)) and self.stage1_car_complete(car)
            for car in blocker_cars
        )
        return GetBlockerInfo(
            lines=lines,
            blocked_nos=blocked_nos,
            pressure=(len(lines), completed_stage1_cars, len(blocker_cars)),
        )

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
        hot = {"渡10", "联7", "机北1", "机北2", "预修线", "调梁线北", "存5线北"}
        operation_lines = [self.loco.line, *(
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action in {"Get", "Put"}
        )]
        price = 0
        for source, target in zip(operation_lines, operation_lines[1:]):
            route = self.graph.route(source, target)
            price += len(route) + sum(3 for line in route if line in hot)
        return price

    def satisfied_break_penalty(self, candidate: physical.HookCandidate) -> int:
        by_no = self.by_no()
        destinations = self.candidate_put_line_by_no(candidate)
        penalty = 0
        for no in candidate.move_car_nos:
            car = by_no.get(no)
            if not car or not self.protected_satisfied_car(car):
                continue
            if self.stage4_target_line_satisfied_by(car, destinations.get(no, "")):
                continue
            penalty += 1
        return penalty

    def protected_satisfied_car(self, car: dict[str, Any]) -> bool:
        if car["Line"] in ASSEMBLY_DEPOT:
            return False
        return bool(self.stage4_target_key(car)) and self.target_satisfied(car)

    def cache_group_rank(self, candidate: physical.HookCandidate) -> tuple[int, int, int, int, int]:
        if candidate.target_line not in CACHE_LINES:
            return (0, 0, 0, 0, 0)
        by_no = self.by_no()
        batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
        target = self.common_stage4_cache_target(batch)
        if not target:
            return (9, 0, 0, 0, 0)
        return self.cache_line_rank_for_target(candidate.target_line, target, set(candidate.move_car_nos))

    def direct_delivery_target_for_car(self, car: dict[str, Any]) -> str:
        for target in self.service_target_options(car):
            if self.car_can_use_real_target(car, target, allow_pending_weigh=False):
                return target
        return ""

    def service_target_options(self, car: dict[str, Any]) -> tuple[str, ...]:
        if not self.service_eligible(car):
            return ()
        targets = set(car.get("TargetLines") or ()) & SERVICE_LINE_SET
        ordered = (str(car.get("Line") or ""), *SERVICE_LINES)
        return tuple(dict.fromkeys(line for line in ordered if line in targets))

    def real_target_line_ready(
        self,
        target: str,
        *,
        excluded_nos: set[str] | None = None,
        respect_forced_reservation: bool = True,
    ) -> bool:
        return self.real_target_line_ready_in(
            self.cars,
            target,
            excluded_nos=excluded_nos,
            respect_forced_reservation=respect_forced_reservation,
        )

    def real_target_line_ready_in(
        self,
        cars: list[dict[str, Any]],
        target: str,
        *,
        excluded_nos: set[str] | None = None,
        respect_forced_reservation: bool = True,
    ) -> bool:
        if target == physical.WEIGH_LINE and any(
            self.pending_weigh(car) for car in cars
        ):
            return False
        excluded_nos = excluded_nos or set()
        if (
            respect_forced_reservation
            and self.target_reserved_for_forced_cars_in(
                cars,
                target,
                excluded_nos,
            )
        ):
            return False
        existing = [
            car
            for car in cars
            if car["Line"] == target and physical.car_no(car) not in excluded_nos
        ]
        return all(self.target_satisfied(car) for car in existing)

    def target_reserved_for_forced_cars(
        self,
        target: str,
        excluded_nos: set[str] | None = None,
    ) -> bool:
        return self.target_reserved_for_forced_cars_in(
            self.cars,
            target,
            excluded_nos,
        )

    def target_reserved_for_forced_cars_in(
        self,
        cars: list[dict[str, Any]],
        target: str,
        excluded_nos: set[str] | None = None,
    ) -> bool:
        excluded_nos = excluded_nos or set()
        pending = [
            car
            for car in cars
            if self.service_eligible(car)
            and target in set(car.get("TargetLines") or ())
            and bool(physical.force_positions(car))
            and not self.target_satisfied(car)
        ]
        if len(pending) < 2:
            return False
        source_lines = {str(car.get("Line") or "") for car in pending}
        if len(source_lines) != 1:
            return False
        source = next(iter(source_lines))
        ordered_nos = physical.line_access_order(cars, source)
        indexes = sorted(ordered_nos.index(physical.car_no(car)) for car in pending)
        by_no = {physical.car_no(car): car for car in cars}
        pending_block = [
            by_no[no]
            for no in ordered_nos[indexes[0] : indexes[-1] + 1]
        ]
        if any(
            self.target_satisfied(car)
            or target not in set(car.get("TargetLines") or ())
            for car in pending_block
        ):
            return False
        pending_nos = {physical.car_no(car) for car in pending}
        return not pending_nos <= excluded_nos

    def car_can_use_real_target(
        self,
        car: dict[str, Any],
        target: str,
        *,
        allow_pending_weigh: bool,
        require_unsatisfied: bool = True,
    ) -> bool:
        if self.stage1_goal(car):
            return False
        if require_unsatisfied and self.target_satisfied(car):
            return False
        if not allow_pending_weigh and self.pending_weigh(car):
            return False
        if not self.stage1_real_target_available(target):
            return False
        return target in set(car.get("TargetLines") or ())

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

    def direct_delivery_route_low_cost(self, source: str, target: str) -> bool:
        route = self.graph.route(source, target)
        return bool(route and len(route) <= DIRECT_DELIVERY_MAX_STATIC_ROUTE_LEN)

    def direct_delivery_priority_candidate(
        self,
        candidate: physical.HookCandidate,
        debt: dict[str, Any],
        after_debt: dict[str, Any],
    ) -> bool:
        return (
            candidate.generation_reason.startswith("stage1_service_direct:")
            and self.candidate_service_delivery_count(candidate) >= DIRECT_DELIVERY_PRIORITY_MIN_BATCH
            and self.progress_tuple(after_debt) <= self.progress_tuple(debt)
        )

    def direct_delivery_hooks_used(self) -> int:
        return sum(
            1
            for item in self.trace
            if str(item.get("reason") or "").startswith("stage1_service_direct:")
        )

    def service_eligible(self, car: dict[str, Any]) -> bool:
        targets = set(car.get("TargetLines") or ())
        return (
            bool(targets)
            and not bool(targets & IGNORED_SERVICE_TARGETS)
            and not bool(targets & NON_SERVICE_TARGETS)
            and bool(targets & SERVICE_LINE_SET)
        )

    def service_forced_position_debt(self, car: dict[str, Any]) -> bool:
        return (
            self.service_eligible(car)
            and bool(physical.force_positions(car))
            and car["Line"] in set(car.get("TargetLines") or ())
            and not self.target_satisfied(car)
        )

    def service_quality(self) -> dict[str, int]:
        eligible = [car for car in self.cars if self.service_eligible(car)]
        satisfied_count = sum(1 for car in eligible if self.target_satisfied(car))
        forced = [car for car in eligible if physical.force_positions(car)]
        forced_position_satisfied_count = sum(
            1 for car in forced if self.target_satisfied(car)
        )
        forced_wrong_position_count = sum(
            1 for car in forced if self.service_forced_position_debt(car)
        )
        south_contiguous_count = 0
        target_run_count = 0
        extra_target_fragment_count = 0
        for line in SERVICE_LINES:
            south_to_north = list(reversed(self.line_ordered_cars(line)))
            for car in south_to_north:
                targets = set(car.get("TargetLines") or ())
                if targets & (IGNORED_SERVICE_TARGETS | NON_SERVICE_TARGETS):
                    continue
                if self.service_eligible(car) and self.target_satisfied(car):
                    south_contiguous_count += 1
                    continue
                if not targets and self.target_satisfied(car):
                    continue
                break

            target_keys = [
                self.stage4_target_key(car)
                for car in south_to_north
                if self.service_eligible(car)
            ]
            run_keys = self.compressed(target_keys)
            target_run_count += len(run_keys)
            extra_target_fragment_count += sum(
                max(0, count - 1)
                for count in Counter(run_keys).values()
            )

        prefix_blocked_count = 0
        for line in self.active_lines():
            barrier_seen = False
            for car in self.line_ordered_cars(line):
                if self.service_eligible(car):
                    if not self.target_satisfied(car) and barrier_seen:
                        prefix_blocked_count += 1
                    if physical.force_positions(car):
                        barrier_seen = True
                    continue
                if self.stage1_goal(car):
                    continue
                barrier_seen = True

        return {
            "service_eligible_count": len(eligible),
            "service_satisfied_count": satisfied_count,
            "service_forced_position_eligible_count": len(forced),
            "service_forced_position_satisfied_count": forced_position_satisfied_count,
            "service_forced_wrong_position_count": forced_wrong_position_count,
            "service_south_contiguous_count": south_contiguous_count,
            "service_prefix_blocked_count": prefix_blocked_count,
            "service_target_run_count": target_run_count,
            "service_extra_target_fragment_count": extra_target_fragment_count,
        }

    def downstream_quality_tuple(self) -> tuple[int, ...]:
        return downstream_quality_vector(self.downstream_quality())

    def downstream_quality(self) -> dict[str, Any]:
        cache_key = physical.state_signature(self.cars, self.loco)
        cached = self.downstream_quality_cache.get(cache_key)
        if cached is not None:
            return cached
        stage3 = self.stage3_quality()
        stage4 = self.stage4_quality()
        service = self.service_quality()
        quality = {
            **service,
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
        return True

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
        service_quality = self.service_quality()
        final_status = [
            self.output_car(car)
            for car in sorted(self.cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), physical.car_no(item)))
        ]
        response = {"Data": {"Operations": self.operations, "GeneratedEndStatus": final_status}}
        business_hooks = sum(1 for row in self.operations if row.get("Action") in {"Get", "Put"})
        primary_business_hooks = min(
            business_hooks,
            (
                self.stage1_completion_business_hooks
                if self.stage1_completion_business_hooks is not None
                else business_hooks
            ),
        )
        service_closure_business_hooks = business_hooks - primary_business_hooks
        service_closure_planlets = sum(
            item.get("phase") == "service_closure"
            for item in self.trace
        )
        rehandled_nos = sorted(
            no
            for no, count in self.get_count_by_no.items()
            if count > 1
        )
        summary = {
            "case_id": self.case_id,
            "status": "complete" if debt["complete"] else "partial",
            "hooks": self.hook_index - 1,
            "move_batches": self.hook_index - 1,
            "business_hooks": business_hooks,
            "primary_business_hooks": primary_business_hooks,
            "operations": len(self.operations),
            "stage1_debt": debt,
            "service_quality": service_quality,
            "service_gain": {
                key: service_quality[key] - int(self.initial_service_quality.get(key) or 0)
                for key in (
                    "service_satisfied_count",
                    "service_forced_position_satisfied_count",
                    "service_south_contiguous_count",
                )
            },
            "service_closure_planlets": service_closure_planlets,
            "service_closure_business_hooks": service_closure_business_hooks,
            "rehandled_car_count": len(rehandled_nos),
            "excess_get_count": sum(
                max(0, count - 1)
                for count in self.get_count_by_no.values()
            ),
            "rehandled_nos": rehandled_nos,
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

    def line_ordered_cars(self, line: str) -> list[dict[str, Any]]:
        by_no = self.by_no()
        return [by_no[no] for no in physical.line_access_order(self.cars, line) if no in by_no]

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
        put_lines = set(self.candidate_put_line_by_no(candidate).values())
        rank = min((self.target_rank(line) for line in put_lines), default=100)
        if put_lines & {"洗油北", "机走棚", "机走北"}:
            moving = set(candidate.move_car_nos)
            if any(
                self.stage1_goal(car) and not self.stage1_car_complete(car) and physical.car_no(car) not in moving
                for car in self.cars
            ):
                rank += 60
        if "存4线" not in put_lines:
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
    solver = Stage1Solver(
        path,
        max_hooks=max_hooks,
        time_budget_seconds=time_budget_seconds,
    )
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
    if not path.is_dir():
        raise FileNotFoundError(f"input path does not exist: {path}")
    files = sorted(path.glob("validation_*.json"))
    if not files:
        raise ValueError(f"input directory has no validation_*.json files: {path}")
    return files


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple first-stage depot inbound assembly solver.")
    parser.add_argument("input", type=Path, help="case JSON or directory containing validation_*.json")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--max-hooks", type=int, default=MAX_HOOKS)
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--jobs", type=int, default=1, help="number of cases to solve in parallel")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise ValueError("jobs must be at least 1")
    files = case_files(args.input)
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []

    def record_error(path: Path, exc: Exception) -> None:
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
            "primary_business_hooks": 0,
            "service_closure_business_hooks": 0,
            "service_closure_planlets": 0,
            "operations": 0,
            "stage1_debt": {"complete": False, "debt_count": 0},
            "blocking_reasons": [f"solver_exception:{type(exc).__name__}:{exc}"],
        }
        summaries.append(summary)
        write_json(args.out / f"{case_id}_summary.json", summary)
        if args.input.is_dir():
            print(f"{case_id} error {type(exc).__name__}: {exc}", flush=True)

    solve_kwargs = {
        "max_hooks": args.max_hooks,
        "time_budget_seconds": args.time_budget_seconds,
    }
    if args.jobs == 1 or len(files) <= 1:
        for path in files:
            try:
                summaries.append(
                    solve_one(path, args.out, verbose=args.input.is_dir(), **solve_kwargs)
                )
            except Exception as exc:  # keep directory batches diagnosable when one case is malformed
                record_error(path, exc)
    else:
        worker_count = min(args.jobs, len(files))
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            pending = {
                executor.submit(solve_one, path, args.out, verbose=False, **solve_kwargs): path
                for path in files
            }
            for future in as_completed(pending):
                path = pending[future]
                try:
                    summary = future.result()
                    summaries.append(summary)
                    if args.input.is_dir():
                        print(
                            f"{summary['case_id']} {summary['status']} "
                            f"business_hooks={summary['business_hooks']} "
                            f"move_batches={summary['hooks']} "
                            f"debt={summary['stage1_debt']['debt_count']}",
                            flush=True,
                        )
                except Exception as exc:
                    record_error(path, exc)
    summaries.sort(key=lambda item: item["case_id"])
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
    return 0 if aggregate["complete"] == aggregate["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
