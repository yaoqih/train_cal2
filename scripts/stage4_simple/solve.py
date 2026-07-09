#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
from solver_vnext.frontier import AccessFrontier
from solver_vnext import physical
from solver_vnext.spotting import (
    build_spotting_cross_line_repack_planlet,
)


DEFAULT_STAGE3_OUT = ROOT / "artifacts" / "four_stage_balanced_early_release_v2" / "stage3"
DEFAULT_TIME_BUDGET_SECONDS = 300.0
DEFAULT_MAX_MACROS = 160
DEFAULT_MAX_CANDIDATES_PER_STEP = 96
MAX_PREFIX_OPTIONS_PER_LINE = 10
MAX_CACHE_TRIES = 2
MAX_NO_PROGRESS_MACROS = 10
MAX_STALE_BEST_MACROS = 18
MAX_PREP_MACROS = 4

STORE4 = "存4线"
WEIGH_LINE = physical.WEIGH_LINE
OUT_OF_SCOPE_TARGET_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
SOURCE_ONLY_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
EXCLUDED_OPERATION_LINES = physical.RUNNING_LINES | {"存4南"}
SAFE_CACHE_LINES = ("存1线", "存2线", "存3线", "机走北", "调梁线北", "洗罐线北")
CORRIDOR_CACHE_LINES = ("机走棚",)
SOURCE_PRIORITY = (
    "存5线北",
    "调梁棚",
    "存3线",
    "存2线",
    "存5线南",
    "卸轮线",
    "预修线",
    "抛丸线",
    "油漆线",
    "洗罐站",
    "机库线",
    "存1线",
    "洗罐线北",
    "调梁线北",
    "存4线",
)
TARGET_TIER_1 = {"抛丸线", "洗罐站", "油漆线", "调梁棚", "机库线", "预修线"}
TARGET_TIER_2 = {"洗罐线北", "调梁线北", "机走棚"}
TARGET_TIER_3 = {"机走北", "存5线南"}
TARGET_PROCESS_ORDER = (
    "抛丸线",
    "洗罐站",
    "油漆线",
    "调梁棚",
    "机库线",
    "预修线",
    "洗罐线北",
    "调梁线北",
    "机走棚",
    "机走北",
    "存5线南",
)
LINE_READY_PREREQUISITES = {
    "调梁线北": ("调梁棚",),
    "机走北": ("调梁棚",),
    "洗罐线北": ("洗罐站",),
    "存5线北": ("存5线南",),
}
HOT_THROATS = {"渡10", "联7", "渡9", "渡8", "渡7", "渡4"}
HEAVY_REPACK_REASONS = {"closed_spotting_cross_repack"}
MOVE_MODEL_RESTRICTIONS = (
    "global_state_is_closed_no_held",
    "held_only_exists_inside_planlet_macro",
    "get_put_weigh_each_count_as_one_operation_hook",
    "depot_related_lines_are_source_only_when_target_is_front_field",
    "safe_cache_lines_are_whitelisted",
    "protected_satisfied_cars_must_remain_satisfied_after_each_macro",
    "ordinary_external_put_requires_target_ready",
    "coupled_front_lines_require_prerequisite_line_ready",
    "same_line_service_sweep_may_bypass_target_ready_for_self_restack",
    "cun5_north_south_segment_transfer_is_generated_as_an_explicit_unit",
    "spotting_cross_repack_is_a_heavy_candidate_used_after_lower_cost_progress",
)


@dataclass(frozen=True)
class MacroView:
    candidate: physical.HookCandidate
    validation: physical.PhysicalValidation
    score: tuple[Any, ...]
    reason: str
    progress_after: tuple[int, ...]
    prep_after: tuple[int, ...] = ()


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


def clone_car(car: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in car.items():
        if isinstance(value, list):
            copied[key] = list(value)
        elif isinstance(value, tuple):
            copied[key] = tuple(value)
        elif isinstance(value, set):
            copied[key] = set(value)
        elif isinstance(value, dict):
            copied[key] = dict(value)
        else:
            copied[key] = value
    return copied


def normalize_car(car: dict[str, Any]) -> dict[str, Any]:
    return physical.normalized_car(rv.ncar(car))


def final_loco_after_response(request: dict[str, Any], response: dict[str, Any]) -> str:
    current = physical.LocoLocation(physical.normalize_line((request.get("locoNode") or {}).get("Line")))
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = physical.normalize_line(row.get("Line"))
        if action == "Get":
            current = physical.LocoLocation(line)
        elif action == "Put":
            current = physical.post_put_loco_location(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            current = physical.LocoLocation(WEIGH_LINE)
    return current.line or WEIGH_LINE


def apply_generated_end_status(response: dict[str, Any], cars: list[dict[str, Any]]) -> None:
    by_no = {physical.car_no(car): car for car in cars}
    for row in rv.generated(response):
        no = str(row.get("No") or "")
        car = by_no.get(no)
        if car is None:
            continue
        car["Line"] = physical.normalize_line(row.get("Line"))
        car["Position"] = int(row.get("Position") or 0)
        if "_Weighed" in row:
            car["_Weighed"] = bool(row.get("_Weighed"))


class Stage4Solver:
    def __init__(
        self,
        case_id: str,
        original_request: dict[str, Any],
        depot_assignment: physical.DepotAssignment,
        stage3_request: dict[str, Any],
        stage3_response: dict[str, Any],
        stage3_combined_response: dict[str, Any],
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
        max_macros: int = DEFAULT_MAX_MACROS,
        max_candidates_per_step: int = DEFAULT_MAX_CANDIDATES_PER_STEP,
        heavy_repack_policy: str = "defer",
        ranking_mode: str = "focused",
    ) -> None:
        self.case_id = case_id
        self.original_request = original_request
        self.depot_assignment = depot_assignment
        self.stage3_request = stage3_request
        self.stage3_response = stage3_response
        self.stage3_combined_response = stage3_combined_response
        replayed, replay_bad = rv.replay(stage3_request, stage3_response)
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical"}]
        if hard_bad:
            detail = ";".join(f"{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:5])
            raise ValueError(f"stage3_replay_failed:{detail}")
        self.cars = [normalize_car(car) for car in rv.final_cars(stage3_response, replayed)]
        apply_generated_end_status(stage3_response, self.cars)
        self.initial_cars = [clone_car(car) for car in self.cars]
        self.stage3_final_loco = final_loco_after_response(stage3_request, stage3_response)
        self.loco = physical.LocoLocation(self.stage3_final_loco)
        self.initial_loco = self.loco
        self.graph = physical.TrackGraph()
        self.frontier = AccessFrontier()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + time_budget_seconds
        self.max_macros = max_macros
        self.max_candidates_per_step = max_candidates_per_step
        self.heavy_repack_policy = heavy_repack_policy
        self.ranking_mode = ranking_mode
        self.macro_index = 1
        self.operation_index = 1
        self.operations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.validation_cache: dict[tuple[Any, str], physical.PhysicalValidation] = {}
        self._by_no_cache: dict[str, dict[str, Any]] | None = None
        self._unsatisfied_nos_cache: set[str] | None = None
        self._state_signature_cache: tuple[Any, ...] | None = None
        self._prep_progress_cache: tuple[int, ...] | None = None
        self.target_by_no: dict[str, str] = {}
        self.target_reason_by_no: dict[str, str] = {}
        self.active_nos: set[str] = set()
        self.out_of_scope_nos: set[str] = set()
        self.excluded_line_nos: set[str] = set()
        self.initialize_targets()
        self.initial_unsatisfied_nos = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }
        self.initial_unresolved_weigh_nos = {
            physical.car_no(car)
            for car in self.cars
            if physical.car_no(car) in self.initial_unsatisfied_nos
            and car.get("IsWeigh")
            and not car.get("_Weighed")
        }
        self.protected_satisfied_nos = {
            physical.car_no(car)
            for car in self.cars
            if physical.car_no(car) not in self.initial_unsatisfied_nos
        }
        self.seen_signatures = {self.state_signature()}
        self.best_progress = self.main_progress()
        self.prep_streak = 0

    def initialize_targets(self) -> None:
        loads = physical.line_loads(self.cars)
        for car in physical.unsatisfied_cars(self.cars, self.depot_assignment):
            no = physical.car_no(car)
            target, _pos, reason = physical.planned_target_for_car(
                car,
                self.cars,
                self.depot_assignment,
                loads,
            )
            if not target:
                self.out_of_scope_nos.add(no)
                continue
            if target in EXCLUDED_OPERATION_LINES or car["Line"] in EXCLUDED_OPERATION_LINES:
                self.excluded_line_nos.add(no)
                continue
            if target in OUT_OF_SCOPE_TARGET_LINES:
                self.out_of_scope_nos.add(no)
                continue
            if target not in physical.TRACK_SPECS:
                self.out_of_scope_nos.add(no)
                continue
            self.active_nos.add(no)
            self.target_by_no[no] = target
            self.target_reason_by_no[no] = reason

        # Keep target metadata for satisfied blockers too. They may move inside a
        # closed self-restack macro, but protected damage is still a hard reject.
        loads = physical.line_loads(self.cars)
        for car in self.cars:
            no = physical.car_no(car)
            if no in self.target_by_no:
                continue
            target, _pos, reason = physical.planned_target_for_car(
                car,
                self.cars,
                self.depot_assignment,
                loads,
            )
            if target:
                self.target_by_no[no] = target
                self.target_reason_by_no[no] = reason

    def refresh_active_targets(self) -> list[dict[str, str]]:
        """Re-pick flexible target lines for still-unsatisfied active cars.

        `planned_target_for_car` uses current line loads for multi-target cars.
        Keeping the t=0 choice forever can strand cars on a target that has
        become full while another declared target remains feasible.  The active
        set stays fixed; this only updates the chosen in-scope target line.
        """
        loads = physical.line_loads(self.cars)
        unsatisfied = self.current_unsatisfied_nos() & self.active_nos
        changes: list[dict[str, str]] = []
        for no in sorted(unsatisfied):
            car = self.by_no().get(no)
            if not car:
                continue
            before = self.target_by_no.get(no, "")
            target, _pos, reason = physical.planned_target_for_car(
                car,
                self.cars,
                self.depot_assignment,
                loads,
            )
            if (
                target
                and target in physical.TRACK_SPECS
                and target not in OUT_OF_SCOPE_TARGET_LINES
                and target not in EXCLUDED_OPERATION_LINES
                and target != before
            ):
                self.target_by_no[no] = target
                self.target_reason_by_no[no] = reason
                changes.append({"no": no, "from": before, "to": target, "reason": reason})
        if changes:
            self._prep_progress_cache = None
        return changes

    def solve(self) -> dict[str, Any]:
        no_progress = 0
        stale_best = 0
        accepted_any = False
        force_progress_only = False
        while self.macro_index <= self.max_macros and not self.deadline_reached():
            target_changes = self.refresh_active_targets()
            if target_changes:
                # Target re-selection changes the metric used by main_progress.
                # Reset the monotonic guard baseline and leave a trace marker so
                # apparent progress jumps can be audited.
                self.best_progress = self.main_progress()
                no_progress = 0
                stale_best = 0
                self.trace.append({
                    "macro": self.macro_index,
                    "accepted": "",
                    "reason": "target_refresh",
                    "changes": target_changes[:20],
                    "change_count": len(target_changes),
                    "best_progress": list(self.best_progress),
                })
            debt_before = self.debt()
            if debt_before["complete"]:
                break
            views = self.ranked_valid_macros(debt_before, allow_prep=not force_progress_only)
            if not views:
                self.trace.append({
                    "macro": self.macro_index,
                    "accepted": "",
                    "reason": "no_valid_progress_macro_after_guard" if force_progress_only else "no_valid_closed_macro",
                    "debt_before": debt_before,
                })
                break
            chosen = views[0]
            before_progress = self.main_progress()
            self.accept_macro(chosen, debt_before, views[1:8])
            accepted_any = True
            after_progress = self.main_progress()
            if after_progress < self.best_progress:
                self.best_progress = after_progress
                stale_best = 0
            else:
                stale_best += 1
            if after_progress >= before_progress:
                no_progress += 1
                self.prep_streak += 1
            else:
                no_progress = 0
                self.prep_streak = 0
                force_progress_only = False
            if (
                no_progress >= MAX_NO_PROGRESS_MACROS
                or stale_best >= MAX_STALE_BEST_MACROS
                or self.prep_streak > MAX_PREP_MACROS
            ):
                if not force_progress_only:
                    self.trace.append({
                        "macro": self.macro_index,
                        "accepted": "",
                        "reason": "prep_guard_switch_to_progress_only",
                        "no_progress": no_progress,
                        "stale_best": stale_best,
                        "prep_streak": self.prep_streak,
                        "best_progress": list(self.best_progress),
                        "debt": self.debt(),
                    })
                    force_progress_only = True
                    no_progress = 0
                    stale_best = 0
                    self.prep_streak = 0
                    continue
                self.trace.append({
                    "macro": self.macro_index,
                    "accepted": "",
                    "reason": "no_progress_guard",
                    "no_progress": no_progress,
                    "stale_best": stale_best,
                    "prep_streak": self.prep_streak,
                    "best_progress": list(self.best_progress),
                    "debt": self.debt(),
                })
                break
        if not accepted_any and self.deadline_reached():
            self.trace.append({
                "macro": self.macro_index,
                "accepted": "",
                "reason": "stage4_global_time_budget_exhausted",
                "debt": self.debt(),
            })
        return self.result()

    def ranked_valid_macros(self, debt: dict[str, Any], *, allow_prep: bool = True) -> list[MacroView]:
        progress_views: list[MacroView] = []
        prep_views: list[MacroView] = []
        heavy_progress_views: list[MacroView] = []
        heavy_prep_views: list[MacroView] = []
        rejected: list[dict[str, Any]] = []
        progress_before = self.main_progress()
        prep_before = self.prep_progress()
        for candidate, reason in self.generate_macros(debt):
            if self.deadline_reached():
                break
            if len(progress_views) >= self.max_candidates_per_step:
                break
            validation = self.validate(candidate)
            if not validation.accepted:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": list(validation.reasons),
                    })
                continue
            probe = self.probe_after(candidate, validation)
            signature = probe.state_signature()
            if signature in self.seen_signatures:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": ["state_cycle"],
                    })
                continue
            damage = probe.protected_damage_nos()
            if damage:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"protected_damage:{','.join(damage[:8])}"],
                    })
                continue
            progress_after = probe.main_progress()
            prep_after = probe.prep_progress()
            if progress_after >= progress_before and prep_after >= prep_before:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": ["no_progress_or_prep_progress"],
                    })
                continue
            score = self.score_candidate(candidate, validation, progress_after, reason)
            view = MacroView(candidate, validation, score, reason, progress_after, prep_after)
            if progress_after < progress_before:
                if reason in HEAVY_REPACK_REASONS:
                    heavy_progress_views.append(view)
                else:
                    progress_views.append(view)
            elif allow_prep:
                prep_score = (
                    progress_after,
                    8,
                    prep_after,
                    len(physical.candidate_plan_steps(candidate)),
                    score,
                )
                prep_view = MacroView(candidate, validation, prep_score, reason, progress_after, prep_after)
                if reason in HEAVY_REPACK_REASONS:
                    heavy_prep_views.append(prep_view)
                else:
                    prep_views.append(prep_view)
        progress_views.sort(key=lambda item: item.score)
        prep_views.sort(key=lambda item: item.score)
        heavy_progress_views.sort(key=lambda item: item.score)
        heavy_prep_views.sort(key=lambda item: item.score)
        views = self.pick_ranked_view_layer(
            progress_views=progress_views,
            prep_views=prep_views,
            progress_before=progress_before,
            prep_before=prep_before,
        )
        if views and heavy_progress_views and self.heavy_repack_policy == "critical":
            best_light_progress = min(item.progress_after for item in views)
            critical_heavy = [
                item for item in heavy_progress_views
                if item.progress_after < best_light_progress
            ]
            if critical_heavy:
                views = sorted([*views, *critical_heavy], key=lambda item: item.score)
        if not views:
            views = self.pick_ranked_view_layer(
                progress_views=heavy_progress_views,
                prep_views=heavy_prep_views,
                progress_before=progress_before,
                prep_before=prep_before,
            )
        if not views and rejected:
            self.trace.append({
                "macro": self.macro_index,
                "accepted": "",
                "reason": "candidate_rejections",
                "debt": debt,
                "rejected": rejected[:20],
            })
        return views

    def pick_ranked_view_layer(
        self,
        *,
        progress_views: list[MacroView],
        prep_views: list[MacroView],
        progress_before: tuple[int, ...],
        prep_before: tuple[int, ...],
    ) -> list[MacroView]:
        del progress_before
        # Some closed macros must first clear a route/access bottleneck before
        # any target debt can drop.  These "critical prep" moves are promoted
        # deliberately, then bounded by the prep guard in solve() so they cannot
        # form an unbounded rebuild/cache chain.
        critical_prep_views = [
            item
            for item in prep_views
            if item.prep_after
            and any(after < before for after, before in zip(item.prep_after, prep_before))
        ]
        if critical_prep_views:
            return sorted(
                [*progress_views, *critical_prep_views],
                key=lambda item: (item.prep_after or prep_before, item.progress_after, item.score),
            )
        return progress_views or prep_views

    def generate_macros(self, debt: dict[str, Any]) -> Iterable[tuple[physical.HookCandidate, str]]:
        emitted: set[str] = set()
        for candidate, reason in self.cun5_segment_transfer_candidates(debt):
            if candidate.candidate_id in emitted:
                continue
            emitted.add(candidate.candidate_id)
            yield candidate, reason
        for line in self.active_source_lines(debt):
            for candidate, reason in self.target_rebuild_candidates(line, debt):
                if candidate.candidate_id in emitted:
                    continue
                emitted.add(candidate.candidate_id)
                yield candidate, reason
            pending = set(debt["active_unsatisfied_nos"])
            for move in self.prefix_options_for_line(line, pending):
                if not move:
                    continue
                for candidate, reason in self.service_sweep_candidates(line, move):
                    if candidate.candidate_id in emitted:
                        continue
                    emitted.add(candidate.candidate_id)
                    yield candidate, reason

    def cun5_segment_transfer_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        pending = set(debt["active_unsatisfied_nos"])
        ordered = physical.line_access_order(self.cars, "存5线北")
        if not ordered:
            return
        active_on_north = {
            no
            for no in ordered
            if no in pending and no in self.active_nos
        }
        if not active_on_north:
            return
        target_south = {
            no
            for no in active_on_north
            if self.target_by_no.get(no, "") == "存5线南"
        }
        if not target_south:
            return

        move: list[str] = []
        for no in ordered:
            move.append(no)
            if active_on_north <= set(move):
                break
        if not active_on_north <= set(move):
            return
        if not target_south <= set(move):
            return
        if not any(self.target_by_no.get(no, "") == "存5线南" for no in move):
            return

        by_no = self.by_no()
        if any(no not in by_no for no in move):
            return
        if physical.pull_equivalent([by_no[no] for no in move]) > physical.PULL_LIMIT_EQUIVALENT:
            return
        candidate = self.build_service_sweep("存5线北", tuple(move), allow_cache=False)
        if candidate:
            yield candidate, "closed_cun5_segment_transfer"

    def active_source_lines(self, debt: dict[str, Any]) -> list[str]:
        pending = set(debt["active_unsatisfied_nos"])
        lines = {
            car["Line"]
            for car in self.cars
            if physical.car_no(car) in pending
            and car["Line"]
            and car["Line"] not in physical.RUNNING_LINES
            and car["Line"] not in {"存4南"}
        }
        # Add target lines that block inbound debt; this enables self-restack and
        # target-front preparation.
        target_lines = {self.target_by_no.get(no, "") for no in pending}
        for line in target_lines:
            if line and line in physical.TRACK_SPECS:
                if self.target_front_conflict(line):
                    lines.add(line)
        return sorted(lines, key=lambda line: (self.source_rank(line), line))

    def prefix_options_for_line(self, line: str, pending: set[str] | None = None) -> list[tuple[str, ...]]:
        ordered = physical.line_access_order(self.cars, line)
        if not ordered:
            return []
        pending = pending if pending is not None else set(self.debt()["active_unsatisfied_nos"])
        by_no = self.by_no()
        options: list[tuple[str, ...]] = []
        prefix: list[str] = []
        target_changes = 0
        last_target = ""
        for no in ordered:
            car = by_no.get(no)
            if not car:
                break
            trial_cars = [by_no[item] for item in (*prefix, no) if item in by_no]
            if physical.pull_equivalent(trial_cars) > physical.PULL_LIMIT_EQUIVALENT:
                break
            target = self.target_by_no.get(no, "")
            if prefix and target != last_target:
                target_changes += 1
            last_target = target
            prefix.append(no)
            if no in pending:
                options.append(tuple(prefix))
            if len(prefix) >= 8 and target_changes >= 2:
                options.append(tuple(prefix))
            if len(options) >= MAX_PREFIX_OPTIONS_PER_LINE:
                break
            if len(prefix) >= int(physical.PULL_LIMIT_EQUIVALENT):
                break
            if car.get("IsWeigh") and not car.get("_Weighed"):
                break
        # Also allow clearing a front blocker segment before the first debt.
        first_pending_index = next((idx for idx, no in enumerate(ordered) if no in pending), -1)
        if first_pending_index > 0:
            blockers = tuple(ordered[:first_pending_index])
            if blockers and blockers not in options:
                cars = [by_no[no] for no in blockers if no in by_no]
                if physical.pull_equivalent(cars) <= physical.PULL_LIMIT_EQUIVALENT:
                    options.insert(0, blockers)
        return sorted(set(options), key=lambda item: (-len(item), item))[:MAX_PREFIX_OPTIONS_PER_LINE]

    def service_sweep_candidates(
        self,
        source_line: str,
        move: tuple[str, ...],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        candidate = self.build_service_sweep(source_line, move, allow_cache=False)
        if candidate:
            yield candidate, "closed_service_sweep_target"
        candidate = self.build_service_sweep(source_line, move, allow_cache=False, include_get_route_blockers=True)
        if candidate:
            yield candidate, "closed_service_sweep_get_unblock"
        candidate = self.build_service_sweep(source_line, move, allow_cache=False, include_put_route_blockers=True)
        if candidate:
            yield candidate, "closed_service_sweep_put_unblock"
        candidate = self.build_service_sweep(source_line, move, allow_cache=True)
        if candidate:
            yield candidate, "closed_service_sweep_cache"
        candidate = self.build_service_sweep(source_line, move, allow_cache=True, include_get_route_blockers=True)
        if candidate:
            yield candidate, "closed_service_sweep_cache_get_unblock"
        candidate = self.build_service_sweep(source_line, move, allow_cache=True, include_put_route_blockers=True)
        if candidate:
            yield candidate, "closed_service_sweep_cache_put_unblock"

    def target_rebuild_candidates(
        self,
        source_line: str,
        debt: dict[str, Any],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        pending = set(debt["active_unsatisfied_nos"])
        ordered = physical.line_access_order(self.cars, source_line)
        if not ordered:
            return
        first_no = ordered[0]
        target_line = self.target_by_no.get(first_no, "")
        if first_no not in pending or not target_line:
            return
        if target_line == source_line or target_line in OUT_OF_SCOPE_TARGET_LINES:
            return
        if target_line in EXCLUDED_OPERATION_LINES or target_line not in physical.TRACK_SPECS:
            return

        group: list[str] = []
        for no in ordered:
            if no not in pending or self.target_by_no.get(no, "") != target_line:
                break
            group.append(no)
            if len(group) > 4:
                break
            candidate = self.build_target_rebuild(source_line, tuple(group), target_line)
            if candidate:
                yield candidate, "closed_target_rebuild"
            candidate = self.build_target_rebuild_route_session(source_line, tuple(group), target_line)
            if candidate:
                yield candidate, "closed_target_rebuild_route_session"
            spotting_plan = build_spotting_cross_line_repack_planlet(
                case_id=self.case_id,
                hook_index=self.macro_index,
                source_line=source_line,
                target_line=target_line,
                source_batch=[self.by_no()[item] for item in group if item in self.by_no()],
                cars=self.cars,
                depot_assignment=self.depot_assignment,
                reason=f"stage4_closed_macro:spotting_cross_repack:{source_line}->{target_line}",
                candidate_kind="stage4_spotting_repack",
                frontier=self.frontier,
                graph=self.graph,
                loco_location=self.loco,
                serial_gate_leases={},
            )
            if spotting_plan is not None:
                yield spotting_plan.candidate, "closed_spotting_cross_repack"

    def build_target_rebuild(
        self,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if not source_group or any(no not in by_no for no in source_group):
            return None
        target_existing = tuple(
            no
            for no in physical.line_access_order(self.cars, target_line)
            if no not in set(source_group)
        )
        if not target_existing:
            return None
        if any(
            self.target_by_no.get(no, "") != target_line and not self.car_satisfied_no(no)
            for no in target_existing
        ):
            return None
        ordering = self.target_rebuild_order(source_line, source_group, target_line, target_existing)
        if ordering is None:
            return None
        target_before, target_after, target_put, target_positions = ordering
        return self.build_ordered_target_rebuild(
            source_line=source_line,
            source_group=source_group,
            target_line=target_line,
            target_existing=target_existing,
            target_before=target_before,
            target_after=target_after,
            target_put=target_put,
            target_positions=target_positions,
            reason_name="target_rebuild",
        )

    def build_ordered_target_rebuild(
        self,
        *,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
        target_existing: tuple[str, ...],
        target_before: tuple[str, ...],
        target_after: tuple[str, ...],
        target_put: tuple[str, ...],
        target_positions: dict[str, int],
        reason_name: str,
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if not source_group or any(no not in by_no for no in source_group):
            return None
        if (*target_before, *target_after) != target_existing:
            return None
        if set(target_put) != (set(target_existing) | set(source_group)):
            return None
        if len(target_positions) != len(target_put):
            return None
        moving_for_route = set(source_group) | set(target_existing)
        _static, _available, route_blocker_lines = physical.route_blocking_lines(
            self.graph,
            self.cars,
            source_line,
            target_line,
            moving_for_route,
        )
        blocker_groups: list[tuple[str, tuple[str, ...]]] = []
        for blocker_line in route_blocker_lines:
            if blocker_line in {source_line, target_line}:
                continue
            if blocker_line in EXCLUDED_OPERATION_LINES or blocker_line not in physical.TRACK_SPECS:
                continue
            blocker_nos = tuple(physical.line_access_order(self.cars, blocker_line))
            if not blocker_nos:
                continue
            if set(blocker_nos) & moving_for_route:
                continue
            blocker_groups.append((blocker_line, blocker_nos))

        all_nos: list[str] = []
        for _line, nos in blocker_groups:
            all_nos.extend(nos)
        all_nos.extend(target_existing)
        all_nos.extend(source_group)
        if len(set(all_nos)) != len(all_nos) or any(no not in by_no for no in all_nos):
            return None
        batch = [by_no[no] for no in all_nos]
        if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        carried: list[str] = []
        steps: list[physical.PlanStep] = []

        for blocker_line, blocker_nos in blocker_groups:
            steps.append(physical.plan_step("Get", blocker_line, blocker_nos))
            physical.apply_physical_get_order(planning_cars, blocker_line, blocker_nos)
            carried.extend(blocker_nos)

        if target_before:
            steps.append(physical.plan_step("Get", target_line, target_before))
            physical.apply_physical_get_order(planning_cars, target_line, target_before)
            carried.extend(target_before)

        steps.append(physical.plan_step("Get", source_line, source_group))
        physical.apply_physical_get_order(planning_cars, source_line, source_group)
        carried.extend(source_group)

        if target_after:
            steps.append(physical.plan_step("Get", target_line, target_after))
            physical.apply_physical_get_order(planning_cars, target_line, target_after)
            carried.extend(target_after)

        steps.append(physical.plan_step("Put", target_line, target_put, target_positions))
        physical.apply_physical_put_order(planning_cars, target_line, list(target_put), target_positions)
        carried = [no for no in carried if no not in set(target_put)]

        for blocker_line, blocker_nos in reversed(blocker_groups):
            positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in blocker_nos],
                target_line=blocker_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(carried),
            )
            if len(positions) != len(blocker_nos):
                return None
            steps.append(physical.plan_step("Put", blocker_line, blocker_nos, positions))
            physical.apply_physical_put_order(planning_cars, blocker_line, list(blocker_nos), positions)
            carried = [no for no in carried if no not in set(blocker_nos)]

        if carried:
            return None
        return physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            steps=tuple(steps),
            reason=f"stage4_closed_macro:{reason_name}:{source_line}->{target_line}",
            candidate_kind="stage4_closed_macro",
        )

    def target_rebuild_order(
        self,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
        target_existing: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], dict[str, int]] | None:
        del source_line
        by_no = self.by_no()
        planning_cars = [clone_car(car) for car in self.cars]
        physical.apply_physical_get_order(planning_cars, target_line, target_existing)
        source_current_line = by_no[source_group[0]]["Line"] if source_group else ""
        physical.apply_physical_get_order(planning_cars, source_current_line, source_group)
        target_batch = (*target_existing, *source_group)
        positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in target_batch],
            target_line=target_line,
            cars=planning_cars,
            depot_assignment=self.depot_assignment,
            batch_nos=set(target_batch),
        )
        if len(positions) != len(target_batch):
            return None
        target_put = tuple(sorted(
            target_batch,
            key=lambda no: (int(positions.get(no) or 0), target_batch.index(no)),
        ))
        source_positions = [target_put.index(no) for no in source_group if no in target_put]
        if len(source_positions) != len(source_group):
            return None
        first = min(source_positions)
        if source_positions != list(range(first, first + len(source_group))):
            return None
        if tuple(target_put[first : first + len(source_group)]) != source_group:
            return None
        target_before = target_put[:first]
        target_after = target_put[first + len(source_group) :]
        if (*target_before, *target_after) != target_existing:
            return None
        return target_before, target_after, target_put, positions

    def build_target_rebuild_route_session(
        self,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if not source_group or any(no not in by_no for no in source_group):
            return None
        target_existing = tuple(
            no
            for no in physical.line_access_order(self.cars, target_line)
            if no not in set(source_group)
        )
        if not target_existing:
            return None
        if any(
            self.target_by_no.get(no, "") != target_line and not self.car_satisfied_no(no)
            for no in target_existing
        ):
            return None
        ordering = self.target_rebuild_order(source_line, source_group, target_line, target_existing)
        if ordering is None:
            return None
        target_before, target_after, target_put, target_positions = ordering
        return self.build_ordered_target_rebuild_route_session(
            source_line=source_line,
            source_group=source_group,
            target_line=target_line,
            target_existing=target_existing,
            target_before=target_before,
            target_after=target_after,
            target_put=target_put,
            target_positions=target_positions,
        )

    def build_ordered_target_rebuild_route_session(
        self,
        *,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
        target_existing: tuple[str, ...],
        target_before: tuple[str, ...],
        target_after: tuple[str, ...],
        target_put: tuple[str, ...],
        target_positions: dict[str, int],
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if (*target_before, *target_after) != target_existing:
            return None
        moving_for_route = set(source_group) | set(target_existing)
        _static, _available, route_blocker_lines = physical.route_blocking_lines(
            self.graph,
            self.cars,
            source_line,
            target_line,
            moving_for_route,
        )
        blocker_groups: list[tuple[str, tuple[str, ...]]] = []
        for blocker_line in route_blocker_lines:
            if blocker_line in {source_line, target_line}:
                continue
            if blocker_line in EXCLUDED_OPERATION_LINES or blocker_line not in physical.TRACK_SPECS:
                continue
            blocker_nos = tuple(physical.line_access_order(self.cars, blocker_line))
            if not blocker_nos:
                continue
            if set(blocker_nos) & moving_for_route:
                continue
            blocker_groups.append((blocker_line, blocker_nos))
        if not blocker_groups:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = []
        all_nos: list[str] = []
        staged_blockers: list[tuple[str, str, tuple[str, ...]]] = []
        blocker_lines = {line for line, _nos in blocker_groups}

        for blocker_line, blocker_nos in blocker_groups:
            if any(no not in by_no for no in blocker_nos):
                return None
            staging_line = self.choose_route_staging_line(
                blocker_nos,
                source_line=source_line,
                target_line=target_line,
                blocked_lines=blocker_lines,
                planning_cars=planning_cars,
            )
            if not staging_line:
                return None
            steps.append(physical.plan_step("Get", blocker_line, blocker_nos))
            physical.apply_physical_get_order(planning_cars, blocker_line, blocker_nos)
            all_nos.extend(blocker_nos)
            positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in blocker_nos],
                target_line=staging_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(blocker_nos),
            )
            if len(positions) != len(blocker_nos):
                return None
            steps.append(physical.plan_step("Put", staging_line, blocker_nos, positions))
            physical.apply_physical_put_order(planning_cars, staging_line, list(blocker_nos), positions)
            staged_blockers.append((blocker_line, staging_line, blocker_nos))

        if target_before:
            steps.append(physical.plan_step("Get", target_line, target_before))
            physical.apply_physical_get_order(planning_cars, target_line, target_before)
            all_nos.extend(target_before)

        steps.append(physical.plan_step("Get", source_line, source_group))
        physical.apply_physical_get_order(planning_cars, source_line, source_group)
        all_nos.extend(source_group)

        if target_after:
            steps.append(physical.plan_step("Get", target_line, target_after))
            physical.apply_physical_get_order(planning_cars, target_line, target_after)
            all_nos.extend(target_after)

        if set(target_put) != (set(target_existing) | set(source_group)):
            return None
        steps.append(physical.plan_step("Put", target_line, target_put, target_positions))
        physical.apply_physical_put_order(planning_cars, target_line, list(target_put), target_positions)

        for blocker_line, staging_line, blocker_nos in reversed(staged_blockers):
            steps.append(physical.plan_step("Get", staging_line, blocker_nos))
            physical.apply_physical_get_order(planning_cars, staging_line, blocker_nos)
            positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in blocker_nos],
                target_line=blocker_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(blocker_nos),
            )
            if len(positions) != len(blocker_nos):
                return None
            steps.append(physical.plan_step("Put", blocker_line, blocker_nos, positions))
            physical.apply_physical_put_order(planning_cars, blocker_line, list(blocker_nos), positions)

        if len(set(all_nos)) != len(all_nos) or any(no not in by_no for no in all_nos):
            return None
        return physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=source_line,
            target_line=target_line,
            batch=[by_no[no] for no in all_nos],
            steps=tuple(steps),
            reason=f"stage4_closed_macro:target_rebuild_route_session:{source_line}->{target_line}",
            candidate_kind="stage4_closed_macro",
        )

    def build_service_sweep(
        self,
        source_line: str,
        move: tuple[str, ...],
        *,
        allow_cache: bool,
        include_get_route_blockers: bool = False,
        include_put_route_blockers: bool = False,
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if any(no not in by_no for no in move):
            return None
        batch = [by_no[no] for no in move]
        planning_cars = [clone_car(car) for car in self.cars]
        blocker_groups: list[tuple[str, tuple[str, ...]]] = []
        if include_get_route_blockers:
            blocker_groups.extend(self.get_route_blocker_groups(source_line, move))
        if include_put_route_blockers:
            blocker_groups.extend(self.put_route_blocker_groups(source_line, move))
        blocker_groups = list(dict.fromkeys(blocker_groups))
        blocker_nos = [no for _line, nos in blocker_groups for no in nos]
        if blocker_nos:
            batch = [by_no[no] for no in (*blocker_nos, *move)]
            if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
                return None
        steps: list[physical.PlanStep] = []
        carried: list[str] = []
        restore_line_by_no: dict[str, str] = {}
        for blocker_line, nos in blocker_groups:
            steps.append(physical.plan_step("Get", blocker_line, nos))
            physical.apply_physical_get_order(planning_cars, blocker_line, nos)
            carried.extend(nos)
            restore_line_by_no.update({no: blocker_line for no in nos})

        source_carried = list(physical.carried_order_after_get(
            cars=planning_cars,
            line=source_line,
            move_nos=set(move),
            carried_nos=set(carried),
        ) or move)
        carried.extend(source_carried)
        steps.append(physical.plan_step("Get", source_line, tuple(move)))
        physical.apply_physical_get_order(planning_cars, source_line, tuple(move))
        progressed: list[str] = []
        cache_puts = 0

        while carried:
            tail_target = restore_line_by_no.get(carried[-1]) or self.target_by_no.get(carried[-1], "")
            target_line = tail_target if tail_target else source_line
            start = len(carried) - 1
            while start > 0 and (
                restore_line_by_no.get(carried[start - 1])
                or self.target_by_no.get(carried[start - 1], "")
            ) == tail_target:
                start -= 1
            group = tuple(carried[start:])
            if not group:
                return None
            is_restore_group = all(no in restore_line_by_no for no in group)
            put_line = ""
            note = "target"
            prefer_cache = (
                not is_restore_group
                and
                allow_cache
                and target_line != source_line
                and target_line in physical.TRACK_SPECS
                and (
                    not self.target_ready(target_line, planning_cars)
                    or (
                        physical.is_spotting_line(target_line)
                        and any(
                            car["Line"] == target_line and physical.car_no(car) not in set(group)
                            for car in planning_cars
                        )
                    )
                )
            )
            if prefer_cache:
                put_line = self.choose_cache_line(group, source_line, planning_cars)
                if put_line:
                    note = "cache"
                    cache_puts += 1
            if is_restore_group:
                put_line = target_line
                note = "restore"
            elif not put_line and self.target_put_allowed(
                source_line=source_line,
                target_line=target_line,
                move=group,
                planning_cars=planning_cars,
            ):
                put_line = target_line
            elif not put_line and allow_cache:
                put_line = self.choose_cache_line(group, source_line, planning_cars)
                if put_line:
                    note = "cache"
                    cache_puts += 1
            if not put_line:
                return None
            group_cars = [by_no[no] for no in group if no in by_no]
            positions = physical.planned_positions_for_batch(
                batch=group_cars,
                target_line=put_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(carried),
            )
            if len(positions) != len(group):
                return None
            steps.append(physical.plan_step("Put", put_line, group, positions))
            physical.apply_physical_put_order(planning_cars, put_line, list(group), positions)
            if note == "target":
                progressed.extend(
                    no
                    for no in group
                    if no not in restore_line_by_no and self.target_by_no.get(no) == put_line
                )
            del carried[start:]
            if cache_puts > MAX_CACHE_TRIES:
                return None

        if not progressed and not allow_cache:
            return None
        if not progressed and allow_cache:
            # Pure cache-only moves are allowed only when they reduce blocking.
            before = self.blocked_active_count(self.cars)
            after = self.blocked_active_count(planning_cars)
            if after >= before:
                return None
        return physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=source_line,
            target_line=steps[-1].line,
            batch=batch,
            steps=tuple(steps),
            reason=f"stage4_closed_macro:{'cache' if allow_cache else 'target'}:{source_line}",
            candidate_kind="stage4_closed_macro",
        )

    def put_route_blocker_groups(self, source_line: str, move: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
        moving = set(move)
        groups: list[tuple[str, tuple[str, ...]]] = []
        seen_lines: set[str] = set()
        for no in move:
            target = self.target_by_no.get(no, "")
            if not target or target == source_line or target not in physical.TRACK_SPECS:
                continue
            _static, available, blockers = physical.route_blocking_lines(
                self.graph,
                self.cars,
                source_line,
                target,
                moving,
            )
            if available:
                continue
            for line in blockers:
                if line in seen_lines or line in {source_line, target}:
                    continue
                seen_lines.add(line)
                if line in EXCLUDED_OPERATION_LINES or line not in physical.TRACK_SPECS:
                    continue
                nos = tuple(physical.line_access_order(self.cars, line))
                if not nos or set(nos) & moving:
                    continue
                groups.append((line, nos))
        return groups[:2]

    def get_route_blocker_groups(self, source_line: str, move: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
        moving = set(move)
        occupied = physical.occupied_lines_for_get_route(self.cars, moving, source_line)
        raw_path = self.graph.route_avoiding_occupied(
            self.loco.line,
            source_line,
            occupied,
            source_departure_lines=physical.route_departure_lines_for_source(self.loco.line, self.cars, moving),
            target_approach_lines=physical.route_approach_lines_for_get(source_line),
            cars=self.cars,
            moving_nos=moving,
            train_length_m=0.0,
        )
        if raw_path:
            return []
        static_path = self.graph.route(self.loco.line, source_line)
        groups: list[tuple[str, tuple[str, ...]]] = []
        seen: set[str] = set()
        for line in static_path:
            if line in seen or line == source_line or line in physical.RUNNING_LINES:
                continue
            seen.add(line)
            if line not in occupied or line in EXCLUDED_OPERATION_LINES or line not in physical.TRACK_SPECS:
                continue
            nos = tuple(physical.line_access_order(self.cars, line))
            if not nos or set(nos) & moving:
                continue
            groups.append((line, nos))
        return groups[:2]

    def target_put_allowed(
        self,
        *,
        source_line: str,
        target_line: str,
        move: tuple[str, ...],
        planning_cars: list[dict[str, Any]],
    ) -> bool:
        if not target_line or target_line in EXCLUDED_OPERATION_LINES:
            return False
        if target_line not in physical.TRACK_SPECS:
            return False
        if target_line in OUT_OF_SCOPE_TARGET_LINES:
            return False
        if target_line == source_line:
            return True
        if not self.target_ready(target_line, planning_cars):
            return False
        if target_line == "存5线南":
            active_on_cun5_north = {
                physical.car_no(car)
                for car in self.cars
                if car["Line"] == "存5线北"
                and physical.car_no(car) in self.current_unsatisfied_nos()
                and physical.car_no(car) in self.active_nos
            }
            if active_on_cun5_north and (
                source_line != "存5线北" or not active_on_cun5_north <= set(move)
            ):
                return False
        group_cars = [self.by_no()[no] for no in move if no in self.by_no()]
        existing_load = sum(
            physical.car_length(car)
            for car in planning_cars
            if car["Line"] == target_line and physical.car_no(car) not in set(move)
        )
        spec = physical.TRACK_SPECS.get(target_line)
        if spec and existing_load + sum(physical.car_length(car) for car in group_cars) > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
            return False
        return True

    def choose_cache_line(
        self,
        move: tuple[str, ...],
        source_line: str,
        planning_cars: list[dict[str, Any]],
    ) -> str:
        active_targets = {self.target_by_no.get(no, "") for no in self.current_unsatisfied_nos() & self.active_nos}
        for line in (*SAFE_CACHE_LINES, *CORRIDOR_CACHE_LINES):
            if line == source_line:
                continue
            if line in active_targets and self.line_has_active_debt(line):
                continue
            if line not in physical.TRACK_SPECS:
                continue
            if line in EXCLUDED_OPERATION_LINES or line in OUT_OF_SCOPE_TARGET_LINES:
                continue
            if not self.target_ready(line, planning_cars):
                continue
            group_cars = [self.by_no()[no] for no in move if no in self.by_no()]
            spec = physical.TRACK_SPECS[line]
            load = sum(physical.car_length(car) for car in planning_cars if car["Line"] == line and physical.car_no(car) not in set(move))
            if load + sum(physical.car_length(car) for car in group_cars) > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                continue
            return line
        return ""

    def choose_route_staging_line(
        self,
        move: tuple[str, ...],
        *,
        source_line: str,
        target_line: str,
        blocked_lines: set[str],
        planning_cars: list[dict[str, Any]],
    ) -> str:
        active_targets = {self.target_by_no.get(no, "") for no in self.current_unsatisfied_nos() & self.active_nos}
        group_cars = [self.by_no()[no] for no in move if no in self.by_no()]
        group_length = sum(physical.car_length(car) for car in group_cars)
        for line in (*SAFE_CACHE_LINES, *CORRIDOR_CACHE_LINES):
            if line in {source_line, target_line} or line in blocked_lines:
                continue
            if line in active_targets and self.line_has_active_debt(line):
                continue
            if line not in physical.TRACK_SPECS:
                continue
            if line in EXCLUDED_OPERATION_LINES or line in OUT_OF_SCOPE_TARGET_LINES:
                continue
            if not self.target_ready(line, planning_cars):
                continue
            load = sum(
                physical.car_length(car)
                for car in planning_cars
                if car["Line"] == line and physical.car_no(car) not in set(move)
            )
            if load + group_length > physical.TRACK_SPECS[line].length_m + physical.LINE_LENGTH_TOLERANCE_M:
                continue
            return line
        return ""

    def target_ready(
        self,
        target: str,
        cars: list[dict[str, Any]] | None = None,
        _seen: set[str] | None = None,
    ) -> bool:
        cars = cars or self.cars
        target = physical.normalize_line(target)
        seen = set(_seen or set())
        if target in seen:
            return True
        seen.add(target)
        for prerequisite in LINE_READY_PREREQUISITES.get(target, ()):
            if not self.target_ready(prerequisite, cars, seen):
                return False
        for car in cars:
            if car["Line"] != target:
                continue
            no = physical.car_no(car)
            if no in self.active_nos:
                car_target = self.target_by_no.get(no, "")
                if car_target != target:
                    return False
                if not physical.car_is_satisfied(car, self.depot_assignment, cars):
                    return False
            elif no in self.protected_satisfied_nos and not physical.car_is_satisfied(car, self.depot_assignment, cars):
                return False
        return True

    def accept_macro(
        self,
        view: MacroView,
        debt_before: dict[str, Any],
        alternatives: list[MacroView],
    ) -> None:
        rows = physical.operation_rows(view.candidate, view.validation, self.operation_index)
        self.operations.extend(physical.response_operation(row) for row in rows)
        self.operation_index += len(rows)
        physical.apply_candidate(view.candidate, self.cars, view.validation)
        self.loco = physical.next_loco_location(view.candidate, view.validation)
        self.invalidate_caches()
        self.validation_cache.clear()
        self.seen_signatures.add(self.state_signature())
        debt_after = self.debt()
        self.trace.append({
            "macro": self.macro_index,
            "accepted": view.candidate.candidate_id,
            "reason": view.reason,
            "operations": len(rows),
            "source": view.candidate.source_line,
            "target": view.candidate.target_line,
            "move": list(view.candidate.move_car_nos),
            "score": list(view.score),
            "progress_after": list(view.progress_after),
            "prep_after": list(view.prep_after),
            "paths": [list(path) for path in view.validation.operation_paths],
            "debt_before": debt_before,
            "debt_after": debt_after,
            "alternatives": [
                {
                    "candidate_id": item.candidate.candidate_id,
                    "reason": item.reason,
                    "score": list(item.score),
                }
                for item in alternatives
            ],
        })
        self.macro_index += 1

    def validate(self, candidate: physical.HookCandidate) -> physical.PhysicalValidation:
        key = (self.macro_index, self.state_signature(), candidate.candidate_id)
        cached = self.validation_cache.get(key)
        if cached is not None:
            return cached
        validation = physical.validate_candidate(
            self.graph,
            candidate,
            self.cars,
            self.loco,
            self.depot_assignment,
        )
        self.validation_cache[key] = validation
        return validation

    def probe_after(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
    ) -> "Stage4Solver":
        clone = Stage4Solver.__new__(Stage4Solver)
        clone.case_id = self.case_id
        clone.original_request = self.original_request
        clone.depot_assignment = self.depot_assignment
        clone.stage3_request = self.stage3_request
        clone.stage3_response = self.stage3_response
        clone.stage3_combined_response = self.stage3_combined_response
        clone.cars = [clone_car(car) for car in self.cars]
        clone.initial_cars = self.initial_cars
        clone.stage3_final_loco = self.stage3_final_loco
        clone.initial_loco = self.initial_loco
        clone.loco = self.loco
        clone.graph = self.graph
        clone.frontier = self.frontier
        clone.started_at = self.started_at
        clone.deadline = self.deadline
        clone.max_macros = self.max_macros
        clone.max_candidates_per_step = self.max_candidates_per_step
        clone.heavy_repack_policy = self.heavy_repack_policy
        clone.ranking_mode = self.ranking_mode
        clone.macro_index = self.macro_index
        clone.operation_index = self.operation_index
        clone.operations = []
        clone.trace = []
        clone.validation_cache = {}
        clone._by_no_cache = None
        clone._unsatisfied_nos_cache = None
        clone._state_signature_cache = None
        clone._prep_progress_cache = None
        clone.target_by_no = dict(self.target_by_no)
        clone.target_reason_by_no = dict(self.target_reason_by_no)
        clone.active_nos = set(self.active_nos)
        clone.out_of_scope_nos = set(self.out_of_scope_nos)
        clone.excluded_line_nos = set(self.excluded_line_nos)
        clone.initial_unsatisfied_nos = set(self.initial_unsatisfied_nos)
        clone.initial_unresolved_weigh_nos = set(self.initial_unresolved_weigh_nos)
        clone.protected_satisfied_nos = set(self.protected_satisfied_nos)
        clone.seen_signatures = self.seen_signatures
        clone.best_progress = self.best_progress
        clone.prep_streak = self.prep_streak
        physical.apply_candidate(candidate, clone.cars, validation)
        clone.loco = physical.next_loco_location(candidate, validation)
        return clone

    def score_candidate(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
        progress_after: tuple[int, ...],
        reason: str,
    ) -> tuple[Any, ...]:
        route_cost = sum(len(path) for path in validation.operation_paths)
        hot_cost = sum(1 for path in validation.operation_paths for line in path if line in HOT_THROATS)
        cache_puts = sum(
            1
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(self.target_by_no.get(no, "") != step.line for no in step.move_car_nos)
        )
        not_ready_puts = sum(
            1
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and all(self.target_by_no.get(no, "") == step.line for no in step.move_car_nos)
            and not self.target_ready(step.line)
        )
        target_put_cars = sum(
            len(step.move_car_nos)
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and all(self.target_by_no.get(no, "") == step.line for no in step.move_car_nos)
        )
        source_rank = self.source_rank(candidate.source_line)
        reason_rank = 0 if reason.endswith("_target") else 3
        return (
            self.progress_score_key(progress_after),
            reason_rank,
            not_ready_puts,
            cache_puts,
            -target_put_cars,
            len(physical.candidate_plan_steps(candidate)),
            source_rank,
            hot_cost,
            route_cost,
            candidate.candidate_id,
        )

    def progress_score_key(self, progress: tuple[int, ...]) -> tuple[int, ...]:
        if self.ranking_mode == "full":
            return progress
        if progress[0] > 0:
            return progress[:2]
        if progress[2] > 0:
            return progress[:4]
        return progress

    def debt(self) -> dict[str, Any]:
        unsatisfied = self.current_unsatisfied_nos()
        active_unsatisfied = sorted(unsatisfied & self.active_nos)
        protected_damage = sorted(unsatisfied & self.protected_satisfied_nos)
        return {
            "complete": not active_unsatisfied and not protected_damage and not self.out_of_scope_current_unsatisfied(),
            "active_unsatisfied_count": len(active_unsatisfied),
            "active_unsatisfied_nos": active_unsatisfied,
            "blocked_active_count": self.blocked_active_count(self.cars),
            "protected_damage_nos": protected_damage,
            "out_of_scope_unsatisfied_nos": sorted(self.out_of_scope_current_unsatisfied()),
            "excluded_line_nos": sorted(self.excluded_line_nos),
        }

    def main_progress(self) -> tuple[int, ...]:
        unsatisfied = self.current_unsatisfied_nos()
        tier_counts = [0, 0, 0]
        tier_blocked = [0, 0, 0]
        blocked_nos = self.blocked_active_nos(self.cars)
        for no in unsatisfied & self.active_nos:
            tier = self.target_tier(self.target_by_no.get(no, ""))
            tier_counts[tier] += 1
            if no in blocked_nos:
                tier_blocked[tier] += 1
        protected_damage = len(unsatisfied & self.protected_satisfied_nos)
        return (
            tier_counts[0],
            tier_blocked[0],
            tier_counts[1],
            tier_blocked[1],
            tier_counts[2],
            tier_blocked[2],
            protected_damage,
        )

    def blocked_active_count(self, cars: list[dict[str, Any]]) -> int:
        return len(self.blocked_active_nos(cars))

    def blocked_active_nos(self, cars: list[dict[str, Any]]) -> set[str]:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos
        blocked: set[str] = set()
        for line in {car["Line"] for car in cars if car["Line"]}:
            front_has_non_debt = False
            front_debt_targets: set[str] = set()
            for no in physical.line_access_order(cars, line):
                if no in unsatisfied:
                    target = self.target_by_no.get(no, "")
                    if front_has_non_debt or any(item != target for item in front_debt_targets):
                        blocked.add(no)
                    front_debt_targets.add(target)
                    continue
                front_has_non_debt = True
        return blocked

    def prep_progress(self) -> tuple[int, ...]:
        cached = self._prep_progress_cache
        if cached is not None:
            return cached
        unsatisfied = self.current_unsatisfied_nos() & self.active_nos
        by_no = self.by_no()
        route_blocked = 0
        route_blocking_lines = 0
        target_access_blocked = 0
        hot_blocked = 0
        target_front_process_penalty = 0
        target_access_not_front_ready = 0
        for no in unsatisfied:
            car = by_no.get(no)
            if not car:
                continue
            source = car.get("Line") or ""
            target = self.target_by_no.get(no, "")
            if not target or source == target or target not in physical.TRACK_SPECS:
                continue
            _static, available, blockers = physical.route_blocking_lines(
                self.graph,
                self.cars,
                source,
                target,
                {no},
            )
            if blockers and not available:
                route_blocked += 1
                route_blocking_lines += len(blockers)
                hot_blocked += sum(1 for line in blockers if line in HOT_THROATS or line in {"调梁线北", "机走棚", "预修线"})
            if physical.is_spotting_line(target):
                existing = [
                    item
                    for item in self.cars
                    if item["Line"] == target and physical.car_no(item) != no
                ]
                if existing:
                    target_access_blocked += 1
                    if not self.source_front_ready_for_rebuild(no, source):
                        target_front_process_penalty += self.target_process_weight(target)
                        target_access_not_front_ready += 1
        cached = (
            route_blocked,
            route_blocking_lines,
            target_access_blocked,
            hot_blocked,
            target_front_process_penalty,
            target_access_not_front_ready,
        )
        self._prep_progress_cache = cached
        return cached

    def source_front_ready_for_rebuild(self, no: str, source: str) -> bool:
        if not source or source in EXCLUDED_OPERATION_LINES or source not in physical.TRACK_SPECS:
            return False
        ordered = physical.line_access_order(self.cars, source)
        return bool(ordered and ordered[0] == no)

    def target_process_weight(self, target: str) -> int:
        try:
            rank = TARGET_PROCESS_ORDER.index(target)
        except ValueError:
            rank = len(TARGET_PROCESS_ORDER)
        return max(1, 100 - rank * 8)

    def target_front_conflict(self, line: str) -> bool:
        ordered = physical.line_access_order(self.cars, line)
        if not ordered:
            return False
        target_debt = [
            no
            for no in ordered
            if no in self.active_nos and self.target_by_no.get(no) == line
            and not self.car_satisfied_no(no)
        ]
        if not target_debt:
            return False
        return ordered[0] not in target_debt

    def car_satisfied_no(self, no: str) -> bool:
        car = self.by_no().get(no)
        return bool(car and physical.car_is_satisfied(car, self.depot_assignment, self.cars))

    def protected_damage_nos(self) -> list[str]:
        unsatisfied = self.current_unsatisfied_nos()
        return sorted(unsatisfied & self.protected_satisfied_nos)

    def current_unsatisfied_nos(self) -> set[str]:
        cached = self._unsatisfied_nos_cache
        if cached is None:
            cached = {
                physical.car_no(car)
                for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
            }
            self._unsatisfied_nos_cache = cached
        return set(cached)

    def current_satisfied_nos(self) -> set[str]:
        all_nos = {physical.car_no(car) for car in self.cars}
        return all_nos - self.current_unsatisfied_nos()

    def out_of_scope_current_unsatisfied(self) -> set[str]:
        unsatisfied = self.current_unsatisfied_nos()
        return unsatisfied & self.out_of_scope_nos

    def line_has_active_debt(self, line: str) -> bool:
        unsatisfied = self.current_unsatisfied_nos() & self.active_nos
        return any(car["Line"] == line and physical.car_no(car) in unsatisfied for car in self.cars)

    def target_tier(self, target: str) -> int:
        if target in TARGET_TIER_1:
            return 0
        if target in TARGET_TIER_2:
            return 1
        return 2

    def source_rank(self, line: str) -> int:
        try:
            return SOURCE_PRIORITY.index(line)
        except ValueError:
            return 99

    def by_no(self) -> dict[str, dict[str, Any]]:
        cached = self._by_no_cache
        if cached is None:
            cached = {physical.car_no(car): car for car in self.cars}
            self._by_no_cache = cached
        return cached

    def state_signature(self) -> tuple[Any, ...]:
        cached = self._state_signature_cache
        if cached is None:
            cached = (
                self.loco.line,
                tuple(
                    (physical.car_no(car), car["Line"], int(car.get("Position") or 0), bool(car.get("_Weighed")))
                    for car in sorted(self.cars, key=lambda item: physical.car_no(item))
                ),
            )
            self._state_signature_cache = cached
        return cached

    def invalidate_caches(self) -> None:
        self._by_no_cache = None
        self._unsatisfied_nos_cache = None
        self._state_signature_cache = None
        self._prep_progress_cache = None

    def deadline_reached(self) -> bool:
        return time.monotonic() >= self.deadline

    def stage4_request(self) -> dict[str, Any]:
        return {
            "StartStatus": [self.output_car(car) for car in self.initial_cars],
            "TerminalLines": self.original_request.get("TerminalLines") or self.stage3_request.get("TerminalLines") or [],
            "locoNode": {"Line": self.initial_loco.line, "End": "North"},
        }

    def result(self) -> dict[str, Any]:
        response = {"Data": {"Operations": self.operations}}
        stage4_request = self.stage4_request()
        replayed, replay_bad = rv.replay(stage4_request, response)
        hard_bad = [v for v in replay_bad if v.kind in {"schema", "physical", "business"}]
        final_cars = [normalize_car(car) for car in rv.final_cars(response, replayed)]
        response["Data"]["GeneratedEndStatus"] = [
            self.output_car(car)
            for car in sorted(final_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), physical.car_no(item)))
        ]
        combined = {
            "Data": {
                "Operations": self.combined_operations(response),
                "GeneratedEndStatus": response["Data"]["GeneratedEndStatus"],
            }
        }
        combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        combined_hard_bad = [v for v in combined_bad if v.kind in {"schema", "physical", "business"}]
        final_unsatisfied = physical.unsatisfied_cars(final_cars, self.depot_assignment)
        final_unsat_nos = [physical.car_no(car) for car in final_unsatisfied]
        debt = self.debt()
        current_unresolved_weigh = sorted(self.current_unresolved_weigh_nos())
        reasons = list(self.blocking_reasons(debt))
        reasons.extend(f"replay_{v.kind}:{v.code}:{v.detail}" for v in hard_bad[:12])
        reasons.extend(f"combined_replay_{v.kind}:{v.code}:{v.detail}" for v in combined_hard_bad[:12])
        if final_unsatisfied:
            reasons.append(f"final_unsatisfied:{len(final_unsatisfied)}")
        complete = not hard_bad and not combined_hard_bad and not final_unsatisfied
        operation_count = len(self.operations)
        business_hooks = sum(1 for row in self.operations if row.get("Action") in {"Get", "Put", "Weigh"})
        summary = {
            "case_id": self.case_id,
            "status": "complete" if complete else "partial",
            "heavy_repack_policy": self.heavy_repack_policy,
            "stage4_ranking_mode": self.ranking_mode,
            "stage3_final_loco": self.stage3_final_loco,
            "stage4_start_loco": self.initial_loco.line,
            "operations": operation_count,
            "business_hooks": business_hooks,
            "hook_count_definition": "operation_rows_get_put_weigh",
            "active_count": len(self.active_nos),
            "active_nos": sorted(self.active_nos),
            "out_of_scope_count": len(self.out_of_scope_nos),
            "out_of_scope_nos": sorted(self.out_of_scope_nos),
            "excluded_line_count": len(self.excluded_line_nos),
            "initial_unresolved_weigh_count": len(self.initial_unresolved_weigh_nos),
            "initial_unresolved_weigh_nos": sorted(self.initial_unresolved_weigh_nos),
            "current_unresolved_weigh_count": len(current_unresolved_weigh),
            "current_unresolved_weigh_nos": current_unresolved_weigh,
            "stage4_debt": debt,
            "main_progress": list(self.main_progress()),
            "optimality": "feasible_unproved" if operation_count else "not_proved",
            "move_model_restrictions": list(MOVE_MODEL_RESTRICTIONS),
            "blocking_reasons": reasons,
            "replay_physical_ok": not hard_bad,
            "replay_violations": [v.__dict__ for v in hard_bad[:20]],
            "combined_replay_physical_ok": not combined_hard_bad,
            "combined_replay_violations": [v.__dict__ for v in combined_hard_bad[:20]],
            "final_unsatisfied_count": len(final_unsatisfied),
            "final_unsatisfied_nos": final_unsat_nos[:80],
            "expansions": self.macro_index - 1,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
        }
        return {
            "response": response,
            "combined_response": combined,
            "stage4_request": stage4_request,
            "summary": summary,
            "trace": self.trace,
        }

    def blocking_reasons(self, debt: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if debt["active_unsatisfied_count"]:
            reasons.append(f"active_unsatisfied:{debt['active_unsatisfied_count']}")
        if debt["blocked_active_count"]:
            reasons.append(f"blocked_active:{debt['blocked_active_count']}")
        if debt["protected_damage_nos"]:
            reasons.append(f"protected_damage:{len(debt['protected_damage_nos'])}")
        if debt["out_of_scope_unsatisfied_nos"]:
            reasons.append(f"out_of_scope_unsatisfied:{len(debt['out_of_scope_unsatisfied_nos'])}")
        unresolved_weigh = self.current_unresolved_weigh_nos()
        if unresolved_weigh:
            reasons.append(f"unresolved_weigh:{len(unresolved_weigh)}")
        if self.trace and self.trace[-1].get("reason"):
            reasons.append(str(self.trace[-1]["reason"]))
        return reasons

    def current_unresolved_weigh_nos(self) -> set[str]:
        unsatisfied = self.current_unsatisfied_nos()
        return {
            physical.car_no(car)
            for car in self.cars
            if physical.car_no(car) in unsatisfied
            and car.get("IsWeigh")
            and not car.get("_Weighed")
        }

    def generated_end_status(self) -> list[dict[str, Any]]:
        return [
            self.output_car(car)
            for car in sorted(self.cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), physical.car_no(item)))
        ]

    def output_car(self, car: dict[str, Any]) -> dict[str, Any]:
        out = {
            key: value
            for key, value in car.items()
            if not key.startswith("_") or key in {"_Weighed"}
        }
        out["No"] = physical.car_no(car)
        out["Line"] = car["Line"]
        out["Position"] = int(car.get("Position") or 0)
        return out

    def combined_operations(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        combined_rows: list[dict[str, Any]] = []
        for row in rv.operations(self.stage3_combined_response):
            copied = dict(row)
            copied["Index"] = len(combined_rows) + 1
            combined_rows.append(copied)
        for row in rv.operations(response):
            copied = dict(row)
            copied["Index"] = len(combined_rows) + 1
            combined_rows.append(copied)
        return combined_rows


def case_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("validation_*.json"))


def solve_one(path: Path, stage3_out: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    case_id = case_id_from_path(path)
    summary_path = stage3_out / f"{case_id}_summary.json"
    stage3_request_path = stage3_out / f"{case_id}_stage3_request.json"
    stage3_response_path = stage3_out / f"{case_id}_response.json"
    stage3_combined_path = stage3_out / f"{case_id}_combined_response.json"
    if not summary_path.exists():
        summary = error_summary(case_id, f"stage3_summary_missing:{summary_path}")
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    stage3_summary = read_json(summary_path)
    if stage3_summary.get("status") != "complete" and not args.include_stage3_partial:
        summary = error_summary(case_id, f"stage3_not_complete:{stage3_summary.get('status')}")
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary
    missing = [
        str(item)
        for item in (stage3_request_path, stage3_response_path, stage3_combined_path)
        if not item.exists()
    ]
    if missing:
        summary = error_summary(case_id, "stage3_artifact_missing:" + ",".join(missing))
        write_json(out_dir / f"{case_id}_summary.json", summary)
        return summary

    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(path)
    stage3_request = read_json(stage3_request_path)
    stage3_response = read_json(stage3_response_path)
    stage3_combined_response = read_json(stage3_combined_path)
    result = solve_one_portfolio(
        case_id=case_id,
        request=request,
        depot_assignment=depot_assignment,
        stage3_request=stage3_request,
        stage3_response=stage3_response,
        stage3_combined_response=stage3_combined_response,
        args=args,
    )
    write_case_outputs(out_dir, case_id, result)
    return result["summary"]


def solve_one_portfolio(
    *,
    case_id: str,
    request: dict[str, Any],
    depot_assignment: physical.DepotAssignment,
    stage3_request: dict[str, Any],
    stage3_response: dict[str, Any],
    stage3_combined_response: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    global_deadline = time.monotonic() + args.time_budget_seconds
    policies: list[tuple[str, str]] = [("focused", "defer")]
    if args.stage4_portfolio == "off":
        policies = [("focused", args.heavy_repack_policy)]
    elif args.stage4_portfolio == "all":
        policies = [
            ("focused", "defer"),
            ("full", "defer"),
            ("focused", "critical"),
            ("full", "critical"),
        ]

    if args.stage4_portfolio == "completion":
        policies = [
            ("focused", "defer"),
            ("full", "defer"),
            ("focused", "critical"),
            ("full", "critical"),
        ]

    for index, (ranking_mode, policy) in enumerate(policies):
        remaining = max(0.0, global_deadline - time.monotonic())
        if remaining <= 0.0:
            break
        policy_budget = portfolio_policy_budget(
            remaining=remaining,
            total_budget=args.time_budget_seconds,
            policies_left=len(policies) - index,
            portfolio_mode=args.stage4_portfolio,
        )
        result = run_policy_solver(
            case_id=case_id,
            request=request,
            depot_assignment=depot_assignment,
            stage3_request=stage3_request,
            stage3_response=stage3_response,
            stage3_combined_response=stage3_combined_response,
            args=args,
            heavy_repack_policy=policy,
            ranking_mode=ranking_mode,
            time_budget_seconds=policy_budget,
        )
        results.append(result)
        evaluated.append(portfolio_summary(result, policy, ranking_mode, policy_budget))
        if args.stage4_portfolio == "completion" and result["summary"].get("status") == "complete":
            break

    if not results:
        summary = error_summary(case_id, "stage4_global_time_budget_exhausted_before_policy")
        return {"response": {"Data": {"Operations": []}}, "combined_response": {"Data": {"Operations": []}}, "stage4_request": {}, "summary": summary, "trace": []}

    selected = min(results, key=portfolio_result_key)
    selected_policy = selected["summary"].get("heavy_repack_policy", "")
    selected_ranking = selected["summary"].get("stage4_ranking_mode", "")
    selected["summary"]["stage4_strategy"] = f"{selected_ranking}:{selected_policy}" if selected_ranking else selected_policy
    selected["summary"]["stage4_portfolio_mode"] = args.stage4_portfolio
    selected["summary"]["stage4_portfolio_evaluations"] = evaluated
    return selected


def portfolio_policy_budget(
    *,
    remaining: float,
    total_budget: float,
    policies_left: int,
    portfolio_mode: str,
) -> float:
    if portfolio_mode == "off" or policies_left <= 1:
        return remaining
    fair_share = remaining / max(1, policies_left)
    floor = min(30.0, max(5.0, total_budget * 0.10))
    cap = max(floor, total_budget * 0.50)
    return min(remaining, cap, max(floor, fair_share))


def run_policy_solver(
    *,
    case_id: str,
    request: dict[str, Any],
    depot_assignment: physical.DepotAssignment,
    stage3_request: dict[str, Any],
    stage3_response: dict[str, Any],
    stage3_combined_response: dict[str, Any],
    args: argparse.Namespace,
    heavy_repack_policy: str,
    ranking_mode: str = "focused",
    time_budget_seconds: float | None = None,
) -> dict[str, Any]:
    solver = Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage3_request,
        stage3_response,
        stage3_combined_response,
        time_budget_seconds=time_budget_seconds if time_budget_seconds is not None else args.time_budget_seconds,
        max_macros=args.max_macros,
        max_candidates_per_step=args.max_candidates_per_step,
        heavy_repack_policy=heavy_repack_policy,
        ranking_mode=ranking_mode,
    )
    return solver.solve()


def portfolio_result_key(result: dict[str, Any]) -> tuple[int, int, int, int, int]:
    summary = result["summary"]
    replay_bad = int(
        not summary.get("replay_physical_ok")
        or not summary.get("combined_replay_physical_ok")
    )
    final_unsatisfied = int(summary.get("final_unsatisfied_count") or 0)
    active_unsatisfied = int((summary.get("stage4_debt") or {}).get("active_unsatisfied_count") or 0)
    hooks = int(summary.get("business_hooks") or 0)
    complete_penalty = 0 if summary.get("status") == "complete" else 1
    return (replay_bad, final_unsatisfied, active_unsatisfied, complete_penalty, hooks)


def portfolio_summary(result: dict[str, Any], policy: str, ranking_mode: str, budget_seconds: float) -> dict[str, Any]:
    summary = result["summary"]
    return {
        "ranking_mode": ranking_mode,
        "heavy_repack_policy": policy,
        "budget_seconds": round(budget_seconds, 3),
        "elapsed_seconds": float(summary.get("elapsed_seconds") or 0.0),
        "status": summary.get("status"),
        "business_hooks": int(summary.get("business_hooks") or 0),
        "final_unsatisfied_count": int(summary.get("final_unsatisfied_count") or 0),
        "active_unsatisfied_count": int((summary.get("stage4_debt") or {}).get("active_unsatisfied_count") or 0),
        "replay_physical_ok": bool(summary.get("replay_physical_ok")),
        "combined_replay_physical_ok": bool(summary.get("combined_replay_physical_ok")),
    }


def error_summary(case_id: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": "partial",
        "heavy_repack_policy": "",
        "stage4_ranking_mode": "",
        "stage3_final_loco": "",
        "stage4_start_loco": "",
        "operations": 0,
        "business_hooks": 0,
        "hook_count_definition": "operation_rows_get_put_weigh",
        "active_count": 0,
        "active_nos": [],
        "out_of_scope_count": 0,
        "out_of_scope_nos": [],
        "excluded_line_count": 0,
        "initial_unresolved_weigh_count": 0,
        "initial_unresolved_weigh_nos": [],
        "current_unresolved_weigh_count": 0,
        "current_unresolved_weigh_nos": [],
        "stage4_debt": {
            "complete": False,
            "active_unsatisfied_count": 0,
            "active_unsatisfied_nos": [],
            "blocked_active_count": 0,
            "protected_damage_nos": [],
            "out_of_scope_unsatisfied_nos": [],
            "excluded_line_nos": [],
        },
        "main_progress": [],
        "optimality": "not_proved",
        "move_model_restrictions": list(MOVE_MODEL_RESTRICTIONS),
        "blocking_reasons": [reason],
        "replay_physical_ok": False,
        "replay_violations": [],
        "combined_replay_physical_ok": False,
        "combined_replay_violations": [],
        "final_unsatisfied_count": 0,
        "final_unsatisfied_nos": [],
        "expansions": 0,
        "elapsed_seconds": 0.0,
    }


def write_case_outputs(out_dir: Path, case_id: str, result: dict[str, Any]) -> None:
    write_json(out_dir / f"{case_id}_response.json", result["response"])
    write_json(out_dir / f"{case_id}_combined_response.json", result["combined_response"])
    write_json(out_dir / f"{case_id}_stage4_request.json", result["stage4_request"])
    write_json(out_dir / f"{case_id}_summary.json", result["summary"])
    write_json(out_dir / f"{case_id}_trace.json", result["trace"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Closed-macro fourth-stage residual solver.")
    parser.add_argument("input", type=Path, help="case JSON or directory containing validation_*.json")
    parser.add_argument("--stage3-out", type=Path, default=DEFAULT_STAGE3_OUT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case", default="")
    parser.add_argument("--time-budget-seconds", type=float, default=DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("--max-macros", type=int, default=DEFAULT_MAX_MACROS)
    parser.add_argument("--max-candidates-per-step", type=int, default=DEFAULT_MAX_CANDIDATES_PER_STEP)
    parser.add_argument(
        "--stage4-portfolio",
        choices=("completion", "all", "off"),
        default="completion",
        help="completion runs low-cost first and critical heavy-repack only when needed; all evaluates both; off uses --heavy-repack-policy.",
    )
    parser.add_argument(
        "--heavy-repack-policy",
        choices=("defer", "critical"),
        default="defer",
        help="Used only when --stage4-portfolio=off.",
    )
    parser.add_argument("--include-stage3-partial", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = case_files(args.input)
    if args.case:
        wanted = args.case.upper()
        files = [path for path in files if case_id_from_path(path) == wanted]
    if args.limit:
        files = files[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for path in files:
        case_id = case_id_from_path(path)
        try:
            summary = solve_one(path, args.stage3_out, args.out, args)
        except Exception as exc:
            summary = error_summary(case_id, f"solver_exception:{type(exc).__name__}:{exc}")
            write_json(args.out / f"{case_id}_summary.json", summary)
        summaries.append(summary)
        if args.input.is_dir():
            print(
                f"{summary['case_id']} {summary.get('status')} "
                f"hooks={summary.get('business_hooks', 0)} "
                f"unsat={summary.get('final_unsatisfied_count', 0)}",
                flush=True,
            )
    aggregate = {
        "cases": len(summaries),
        "complete": sum(1 for item in summaries if item.get("status") == "complete"),
        "feasible_unproved": sum(1 for item in summaries if item.get("optimality") == "feasible_unproved"),
        "partial": sum(1 for item in summaries if item.get("status") == "partial"),
        "avg_operations_complete": round(
            sum(int(item.get("operations") or 0) for item in summaries if item.get("status") == "complete")
            / max(1, sum(1 for item in summaries if item.get("status") == "complete")),
            3,
        ),
        "avg_business_hooks_complete": round(
            sum(int(item.get("business_hooks") or 0) for item in summaries if item.get("status") == "complete")
            / max(1, sum(1 for item in summaries if item.get("status") == "complete")),
            3,
        ),
        "partial_reasons": dict(Counter(
            str(reason).split(":", 1)[0]
            for item in summaries
            if item.get("status") != "complete"
            for reason in item.get("blocking_reasons", [])[:3]
        )),
        "summaries": summaries,
    }
    write_json(args.out / "aggregate_summary.json", aggregate)
    print(json.dumps({key: value for key, value in aggregate.items() if key != "summaries"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
