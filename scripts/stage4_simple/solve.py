#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from itertools import combinations, permutations, product
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
    build_spotting_same_line_repack_planlet,
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
MAX_SOURCE_FRONT_OPTIONS = 4
MAX_GLOBAL_SESSION_CANDIDATES = 24
HEAVY_SINGLE_CAR_LAYOUT_MIN_STEPS = 10

STORE4 = "存4线"
WEIGH_LINE = physical.WEIGH_LINE
OUT_OF_SCOPE_TARGET_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
SOURCE_ONLY_LINES = physical.DEPOT_TARGET_LINES | {"卸轮线"}
EXCLUDED_OPERATION_LINES = physical.RUNNING_LINES | {"存4南"}
SAFE_CACHE_LINES = ("存1线", "存2线", "存3线", "机走北", "调梁线北", "洗罐线北")
CORRIDOR_CACHE_LINES = ("机走棚",)
ASSEMBLY_CACHE_LINES = ("机库线",)
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
SERVICE_GATE_LEASES = {
    "抛丸线": frozenset({"机南", "机走棚"}),
    "油漆线": frozenset({"洗油北", "机走棚"}),
    "洗罐站": frozenset({"洗罐线北", "洗油北", "机走棚"}),
    "洗罐线北": frozenset({"洗油北", "机走棚"}),
    "机南": frozenset({"机走棚"}),
    "洗油北": frozenset({"机走棚"}),
    "机走棚": frozenset({"机走北"}),
    "调梁棚": frozenset({"调梁线北", "机北2"}),
    "机库线": frozenset({"调梁线北", "机北2"}),
    "存5线南": frozenset({"存5线北"}),
}
HOT_THROATS = {"渡10", "联7", "渡9", "渡8", "渡7", "渡4"}
LAYOUT_REPACK_REASONS = {"closed_spotting_cross_repack", "closed_spotting_same_line_repack"}
RESULT_HARD_REPLAY_KINDS = {"schema", "physical", "business", "state"}
MOVE_MODEL_RESTRICTIONS = (
    "global_state_is_closed_no_held",
    "held_only_exists_inside_planlet_macro",
    "get_put_weigh_each_count_as_one_operation_hook",
    "depot_related_lines_are_source_only_when_target_is_front_field",
    "safe_cache_lines_are_whitelisted",
    "persistent_put_must_not_create_new_route_locks",
    "persistent_put_must_not_create_new_source_access_locks",
    "persistent_gate_put_must_not_expand_an_active_service_gate",
    "hard_single_target_capacity_infeasibility_is_proved_before_search",
    "protected_satisfied_cars_must_remain_satisfied_after_each_macro",
    "ordinary_external_put_requires_target_ready",
    "coupled_front_lines_require_prerequisite_line_ready",
    "same_line_service_sweep_may_bypass_target_ready_for_self_restack",
    "cun5_north_south_segment_transfer_is_generated_as_an_explicit_unit",
    "layout_repack_is_ranked_by_structural_cost_in_one_candidate_pool",
    "multi_source_same_target_sessions_are_bounded_to_three_sources",
    "partial_drop_continue_get_sessions_end_with_empty_carry",
)


@dataclass(frozen=True)
class MacroView:
    candidate: physical.HookCandidate
    validation: physical.PhysicalValidation
    score: tuple[Any, ...]
    reason: str
    progress_after: tuple[int, ...]
    prep_after: tuple[int, ...] = ()
    target_key: tuple[str, ...] = ()
    target_window_rank: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True)
class LayoutChunk:
    origin_line: str
    nos: tuple[str, ...]
    to_target: bool


@dataclass(frozen=True)
class SourceRun:
    source_line: str
    target_line: str
    nos: tuple[str, ...]


@dataclass(frozen=True)
class PendingRouteState:
    service_available: bool
    service_blockers: tuple[str, ...]
    access_available: bool
    access_blockers: tuple[str, ...]


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
        self.macro_index = 1
        self.operation_index = 1
        self.operations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.validation_cache: dict[tuple[Any, str], physical.PhysicalValidation] = {}
        self._by_no_cache: dict[str, dict[str, Any]] | None = None
        self._unsatisfied_nos_cache: set[str] | None = None
        self._state_signature_cache: tuple[Any, ...] | None = None
        self._prep_progress_cache: tuple[int, ...] | None = None
        self._pending_route_states_cache: dict[tuple[Any, ...], dict[str, PendingRouteState]] = {}
        self.target_by_no: dict[str, str] = {}
        self.target_reason_by_no: dict[str, str] = {}
        self.active_nos: set[str] = set()
        self.capacity_overflow_by_line = self.hard_capacity_overflow_by_line()
        self.infeasible_lines = set(self.capacity_overflow_by_line)
        self.capacity_holdout_count_by_line = self.hard_capacity_holdout_counts()
        self.infeasible_nos = self.hard_capacity_infeasible_nos()
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
            declared_targets = {
                physical.normalize_line(line)
                for line in physical.target_lines(car)
                if physical.normalize_line(line)
            }
            if no in self.infeasible_nos and declared_targets == {target}:
                self.target_by_no[no] = target
                self.target_reason_by_no[no] = reason
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

    def exact_single_target_cars_by_line(self) -> dict[str, list[dict[str, Any]]]:
        exact_by_line: dict[str, list[dict[str, Any]]] = {}
        for car in self.cars:
            targets = {
                physical.normalize_line(line)
                for line in physical.target_lines(car)
                if physical.normalize_line(line) in physical.TRACK_SPECS
            }
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            if target in OUT_OF_SCOPE_TARGET_LINES or target in EXCLUDED_OPERATION_LINES:
                continue
            exact_by_line.setdefault(target, []).append(car)
        return exact_by_line

    def hard_capacity_overflow_by_line(self) -> dict[str, float]:
        overflow_by_line: dict[str, float] = {}
        for line, cars in self.exact_single_target_cars_by_line().items():
            capacity = physical.TRACK_SPECS[line].length_m + physical.LINE_LENGTH_TOLERANCE_M
            overflow = sum(physical.car_length(car) for car in cars) - capacity
            if overflow > 1e-9:
                overflow_by_line[line] = overflow
        return overflow_by_line

    def hard_capacity_holdout_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        exact_by_line = self.exact_single_target_cars_by_line()
        for target in self.infeasible_lines:
            cars = exact_by_line.get(target, [])
            overflow = self.capacity_overflow_by_line[target]
            removed = 0.0
            for count, length in enumerate(
                sorted((physical.car_length(car) for car in cars), reverse=True),
                start=1,
            ):
                removed += length
                if removed + 1e-9 >= overflow:
                    counts[target] = count
                    break
        return counts

    def hard_capacity_infeasible_nos(self) -> set[str]:
        exact_by_line = self.exact_single_target_cars_by_line()
        infeasible: set[str] = set()
        for target in self.infeasible_lines:
            cars = exact_by_line.get(target, [])
            external = [car for car in cars if car["Line"] != target]
            if physical.is_spotting_line(target) and len(external) > 1:
                # Choosing the overflow member is part of the spotting layout;
                # preselecting one here can make an otherwise feasible window impossible.
                continue
            capacity = physical.TRACK_SPECS[target].length_m + physical.LINE_LENGTH_TOLERANCE_M
            required = sum(physical.car_length(car) for car in cars)
            overflow = required - capacity
            if overflow <= 0.0:
                continue
            best: tuple[Any, ...] | None = None
            best_group: tuple[dict[str, Any], ...] = ()
            for size in range(1, len(external) + 1):
                for group in combinations(external, size):
                    removed = sum(physical.car_length(car) for car in group)
                    if removed + 1e-9 < overflow:
                        continue
                    key = (
                        round(removed - overflow, 6),
                        sum(
                            self.source_rank(car.get("Line") or "") * 1000
                            + int(car.get("Position") or 0)
                            for car in group
                        ),
                        tuple(sorted(physical.car_no(car) for car in group)),
                    )
                    if best is None or key < best:
                        best = key
                        best_group = group
                if best is not None:
                    break
            infeasible.update(physical.car_no(car) for car in best_group)
        return infeasible

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
            if debt_before["actionable_complete"]:
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
        ranked_views: list[MacroView] = []
        direct_progress_count = 0
        rejected: list[dict[str, Any]] = []
        progress_before = self.main_progress()
        prep_before = self.prep_progress()
        for candidate, reason in self.generate_macros(debt):
            if self.deadline_reached():
                break
            if direct_progress_count >= self.max_candidates_per_step:
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
            buried = self.newly_buried_by_target_put(candidate, probe)
            if buried and not self.target_put_burial_recoverable(candidate, probe, buried):
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"new_target_put_burial:{','.join(sorted(buried)[:8])}"],
                    })
                continue
            access_locked = self.newly_access_locked_by_persistent_put(candidate, probe)
            if access_locked:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"new_source_access_lock:{','.join(sorted(access_locked)[:8])}"],
                    })
                continue
            gate_locked = self.persistent_gate_lease_damage(candidate, probe)
            if gate_locked:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"new_gate_lease_lock:{','.join(sorted(gate_locked)[:8])}"],
                    })
                continue
            inaccessible_cache = self.inaccessible_persistent_cache_nos(candidate, probe)
            if inaccessible_cache:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"persistent_cache_not_retrievable:{','.join(sorted(inaccessible_cache)[:8])}"],
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
            score = self.score_candidate(
                candidate,
                validation,
                progress_before,
                progress_after,
                prep_after,
                reason,
            )
            pending_before = self.current_unsatisfied_nos() & self.active_nos
            target_key = self.candidate_business_target_key(candidate, pending_before)
            target_window_rank = self.candidate_target_window_rank(
                candidate,
                probe,
                pending_before,
            )
            resolved_count = sum(
                max(0, progress_before[index] - progress_after[index])
                for index in (0, 2, 4)
            )
            heavy_single_car_layout = (
                reason == "closed_prefix_target_layout_restore_session"
                and len(physical.candidate_plan_steps(candidate))
                >= HEAVY_SINGLE_CAR_LAYOUT_MIN_STEPS
                and resolved_count <= 1
            )
            structural_cost = int(
                reason in LAYOUT_REPACK_REASONS or heavy_single_car_layout
            )
            # One lexicographic pool: progress precedes preparation inside
            # each structural-cost class; layout repacks remain more costly.
            if progress_after < progress_before:
                selection_score = (structural_cost, 0, score)
                ranked_views.append(
                    MacroView(
                        candidate,
                        validation,
                        selection_score,
                        reason,
                        progress_after,
                        prep_after,
                        target_key,
                        target_window_rank,
                    )
                )
                if structural_cost == 0:
                    direct_progress_count += 1
            elif allow_prep:
                prep_score = (
                    progress_after,
                    8,
                    prep_after,
                    len(physical.candidate_plan_steps(candidate)),
                    score,
                )
                selection_score = (structural_cost, 1, prep_score)
                ranked_views.append(
                    MacroView(
                        candidate,
                        validation,
                        selection_score,
                        reason,
                        progress_after,
                        prep_after,
                        target_key,
                        target_window_rank,
                    )
                )
        ranked_views = self.rank_target_variants(ranked_views)
        ranked_views.sort(key=lambda item: item.score)
        if not ranked_views and rejected:
            self.trace.append({
                "macro": self.macro_index,
                "accepted": "",
                "reason": "candidate_rejections",
                "debt": debt,
                "rejected": rejected[:20],
            })
        return ranked_views

    def rank_target_variants(self, views: list[MacroView]) -> list[MacroView]:
        groups: dict[tuple[int, int, tuple[str, ...]], list[MacroView]] = {}
        for view in views:
            structural_cost = int(view.score[0])
            progress_class = int(view.score[1])
            target_key = view.target_key or (view.candidate.candidate_id,)
            groups.setdefault((structural_cost, progress_class, target_key), []).append(view)

        ranked: list[MacroView] = []
        for group in groups.values():
            anchor_view = min(group, key=lambda view: view.score[2])
            anchor = anchor_view.score[2]
            compare_window = len(group) > 1 and bool(group[0].target_key)
            for view in group:
                improves_window = (
                    compare_window
                    and view.target_window_rank[2] < anchor_view.target_window_rank[2]
                    and view.target_window_rank[1] < anchor_view.target_window_rank[1]
                    and view.target_window_rank[0] < anchor_view.target_window_rank[0]
                )
                variant_class = 0 if view is anchor_view or improves_window else 1
                variant_rank = view.target_window_rank if compare_window else (0, 0, 0)
                selection_score = (
                    view.score[0],
                    view.score[1],
                    anchor,
                    variant_class,
                    variant_rank,
                    view.score[2],
                )
                ranked.append(MacroView(
                    view.candidate,
                    view.validation,
                    selection_score,
                    view.reason,
                    view.progress_after,
                    view.prep_after,
                    view.target_key,
                    view.target_window_rank,
                ))
        return ranked

    def generate_macros(self, debt: dict[str, Any]) -> Iterable[tuple[physical.HookCandidate, str]]:
        emitted: set[str] = set()
        for candidate, reason in self.same_line_spotting_repack_candidates(debt):
            if candidate.candidate_id in emitted:
                continue
            emitted.add(candidate.candidate_id)
            yield candidate, reason
        for candidate, reason in self.cun5_segment_transfer_candidates(debt):
            if candidate.candidate_id in emitted:
                continue
            emitted.add(candidate.candidate_id)
            yield candidate, reason
        for candidate, reason in self.multi_source_same_target_candidates(debt):
            if candidate.candidate_id in emitted:
                continue
            emitted.add(candidate.candidate_id)
            yield candidate, reason
        for candidate, reason in self.partial_drop_continue_get_candidates(debt):
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

    def same_line_spotting_repack_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        pending = set(debt["active_unsatisfied_nos"])
        lines = {
            car["Line"]
            for car in self.cars
            if physical.car_no(car) in pending
            and car["Line"] == self.target_by_no.get(physical.car_no(car), "")
            and physical.is_spotting_line(car["Line"])
        }
        for line in sorted(lines, key=lambda item: (self.source_rank(item), item)):
            plan = build_spotting_same_line_repack_planlet(
                case_id=self.case_id,
                hook_index=self.macro_index,
                line=line,
                cars=self.cars,
                depot_assignment=self.depot_assignment,
                reason=f"stage4_closed_macro:spotting_same_line_repack:{line}",
                candidate_kind="stage4_spotting_repack",
                frontier=self.frontier,
                graph=self.graph,
                loco_location=self.loco,
                serial_gate_leases={},
            )
            if plan is not None:
                yield plan.candidate, "closed_spotting_same_line_repack"

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
        capacity_holds = {
            no
            for no in ordered
            if no in self.infeasible_nos
            and self.target_by_no.get(no, "") == "存5线南"
        }
        required_on_north = active_on_north | capacity_holds

        move: list[str] = []
        for no in ordered:
            move.append(no)
            if required_on_north <= set(move):
                break
        if not required_on_north <= set(move):
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

    def source_front_group_options(
        self,
        pending: set[str],
    ) -> dict[str, dict[str, tuple[tuple[str, ...], ...]]]:
        """Return bounded same-target prefixes available at each source end."""
        by_no = self.by_no()
        by_target: dict[str, dict[str, tuple[tuple[str, ...], ...]]] = {}
        source_lines = {
            car["Line"]
            for car in self.cars
            if physical.car_no(car) in pending and car["Line"]
        }
        for source_line in sorted(source_lines, key=lambda line: (self.source_rank(line), line)):
            ordered = physical.line_access_order(self.cars, source_line)
            if not ordered or ordered[0] not in pending:
                continue
            target_line = self.target_by_no.get(ordered[0], "")
            if (
                not target_line
                or target_line == source_line
                or target_line in OUT_OF_SCOPE_TARGET_LINES
                or target_line in EXCLUDED_OPERATION_LINES
                or target_line not in physical.TRACK_SPECS
            ):
                continue
            prefixes: list[tuple[str, ...]] = []
            group: list[str] = []
            for no in ordered:
                if no not in pending or self.target_by_no.get(no, "") != target_line:
                    break
                car = by_no.get(no)
                if car is None:
                    break
                trial = [by_no[item] for item in (*group, no)]
                if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                group.append(no)
                prefixes.append(tuple(group))
                if car.get("IsWeigh") and not car.get("_Weighed"):
                    break
            if not prefixes:
                continue
            options = tuple(sorted(
                prefixes,
                key=lambda nos: (-len(nos), nos),
            )[:MAX_SOURCE_FRONT_OPTIONS])
            by_target.setdefault(target_line, {})[source_line] = options
        return by_target

    def source_front_runs(
        self,
        source_line: str,
        pending: set[str],
        *,
        max_runs: int = 6,
    ) -> tuple[SourceRun, ...]:
        """Split one all-active source prefix into consecutive target runs."""
        by_no = self.by_no()
        runs: list[SourceRun] = []
        current_target = ""
        current_nos: list[str] = []
        moved: list[str] = []
        for no in physical.line_access_order(self.cars, source_line):
            if no not in pending or no not in self.active_nos:
                break
            target_line = self.target_by_no.get(no, "")
            if (
                not target_line
                or target_line == source_line
                or target_line in OUT_OF_SCOPE_TARGET_LINES
                or target_line in EXCLUDED_OPERATION_LINES
                or target_line not in physical.TRACK_SPECS
            ):
                break
            car = by_no.get(no)
            if car is None:
                break
            trial = [by_no[item] for item in (*moved, no)]
            if physical.pull_equivalent(trial) > physical.PULL_LIMIT_EQUIVALENT:
                break
            if current_nos and target_line != current_target:
                runs.append(SourceRun(source_line, current_target, tuple(current_nos)))
                if len(runs) >= max_runs:
                    current_nos = []
                    break
                current_nos = []
            current_target = target_line
            current_nos.append(no)
            moved.append(no)
            if car.get("IsWeigh") and not car.get("_Weighed"):
                break
        if current_nos and len(runs) < max_runs:
            runs.append(SourceRun(source_line, current_target, tuple(current_nos)))
        return tuple(runs)

    def multi_source_same_target_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        pending = set(debt["active_unsatisfied_nos"])
        options_by_target = self.source_front_group_options(pending)
        emitted = 0
        for target_line in sorted(
            options_by_target,
            key=lambda line: (self.target_tier(line), self.target_process_weight(line) * -1, line),
        ):
            by_source = options_by_target[target_line]
            source_lines = sorted(by_source, key=lambda line: (self.source_rank(line), line))
            if len(source_lines) < 2:
                continue
            for source_count in (3, 2):
                for selected_lines in combinations(source_lines, source_count):
                    option_sets = [by_source[line] for line in selected_lines]
                    for selected_groups in product(*option_sets):
                        source_groups = dict(zip(selected_lines, selected_groups))
                        candidate = self.build_multi_source_same_target_session(
                            source_groups,
                            target_line,
                        )
                        if candidate is None:
                            continue
                        yield candidate, "closed_multi_source_same_target_session_target"
                        emitted += 1
                        if emitted >= MAX_GLOBAL_SESSION_CANDIDATES:
                            return

    def build_multi_source_same_target_session(
        self,
        source_groups: dict[str, tuple[str, ...]],
        target_line: str,
    ) -> physical.HookCandidate | None:
        """Get independent source fronts in final target order and rebuild once."""
        if len(source_groups) < 2 or len(source_groups) > 3:
            return None
        if (
            not target_line
            or target_line in OUT_OF_SCOPE_TARGET_LINES
            or target_line in EXCLUDED_OPERATION_LINES
            or target_line not in physical.TRACK_SPECS
        ):
            return None
        pending = self.current_unsatisfied_nos() & self.active_nos
        by_no = self.by_no()
        for source_line, group in source_groups.items():
            if not group or source_line == target_line:
                return None
            if tuple(physical.line_access_order(self.cars, source_line)[: len(group)]) != group:
                return None
            if any(
                no not in pending
                or no not in by_no
                or self.target_by_no.get(no, "") != target_line
                for no in group
            ):
                return None

        target_existing = tuple(physical.line_access_order(self.cars, target_line))
        if any(
            self.target_by_no.get(no, "") != target_line and not self.car_satisfied_no(no)
            for no in target_existing
        ):
            return None
        origins: dict[str, tuple[str, ...]] = {}
        if target_existing:
            origins[target_line] = target_existing
        origins.update(source_groups)
        moved_nos = tuple(no for nos in origins.values() for no in nos)
        if len(set(moved_nos)) != len(moved_nos) or any(no not in by_no for no in moved_nos):
            return None
        batch = [by_no[no] for no in moved_nos]
        if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        for source_line, nos in origins.items():
            physical.apply_physical_get_order(planning_cars, source_line, nos)
        target_positions = physical.planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=planning_cars,
            depot_assignment=self.depot_assignment,
            batch_nos=set(moved_nos),
        )
        if len(target_positions) != len(moved_nos):
            return None
        target_put = tuple(sorted(
            moved_nos,
            key=lambda no: (int(target_positions.get(no) or 0), moved_nos.index(no)),
        ))

        origin_by_no = {
            no: source_line
            for source_line, nos in origins.items()
            for no in nos
        }
        chunks: list[tuple[str, tuple[str, ...]]] = []
        for no in target_put:
            source_line = origin_by_no.get(no, "")
            if not source_line:
                return None
            if chunks and chunks[-1][0] == source_line:
                chunks[-1] = (source_line, (*chunks[-1][1], no))
            else:
                chunks.append((source_line, (no,)))
        for source_line, origin_nos in origins.items():
            reconstructed = tuple(
                no
                for chunk_line, chunk_nos in chunks
                if chunk_line == source_line
                for no in chunk_nos
            )
            if reconstructed != origin_nos:
                return None

        planning_cars = [clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = []
        for source_line, chunk_nos in chunks:
            steps.append(physical.plan_step("Get", source_line, chunk_nos))
            physical.apply_physical_get_order(planning_cars, source_line, chunk_nos)
        external_source = next(iter(source_groups))
        if not self.target_put_allowed(
            source_line=external_source,
            target_line=target_line,
            move=target_put,
            planning_cars=planning_cars,
        ):
            return None
        steps.append(physical.plan_step("Put", target_line, target_put, target_positions))
        physical.apply_physical_put_order(
            planning_cars,
            target_line,
            list(target_put),
            target_positions,
        )

        candidate = physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=external_source,
            target_line=target_line,
            batch=batch,
            steps=tuple(steps),
            reason=(
                f"stage4_closed_macro:multi_source_same_target:"
                f"{'+'.join(source_groups)}->{target_line}"
            ),
            candidate_kind="stage4_closed_macro",
        )
        return candidate if self.validate(candidate).accepted else None

    def partial_drop_continue_get_candidates(
        self,
        debt: dict[str, Any],
    ) -> Iterable[tuple[physical.HookCandidate, str]]:
        pending = set(debt["active_unsatisfied_nos"])
        options_by_target = self.source_front_group_options(pending)
        source_lines = sorted(
            {car["Line"] for car in self.cars if physical.car_no(car) in pending and car["Line"]},
            key=lambda line: (self.source_rank(line), line),
        )
        emitted = 0
        for first_source in source_lines:
            runs = self.source_front_runs(first_source, pending)
            if len(runs) < 2:
                continue
            for join_index in range(len(runs) - 1):
                join_target = runs[join_index].target_line
                second_sources = options_by_target.get(join_target, {})
                for second_source in sorted(second_sources, key=lambda line: (self.source_rank(line), line)):
                    if second_source == first_source:
                        continue
                    for second_group in second_sources[second_source]:
                        candidate = self.build_partial_drop_continue_get_session(
                            first_runs=runs,
                            join_index=join_index,
                            second_source=second_source,
                            second_group=second_group,
                        )
                        if candidate is None:
                            continue
                        yield candidate, "closed_partial_drop_continue_get_session_target"
                        emitted += 1
                        if emitted >= MAX_GLOBAL_SESSION_CANDIDATES:
                            return

    def build_partial_drop_continue_get_session(
        self,
        *,
        first_runs: tuple[SourceRun, ...],
        join_index: int,
        second_source: str,
        second_group: tuple[str, ...],
    ) -> physical.HookCandidate | None:
        """Drop a tail run, keep the head, then get and merge a fresh source front."""
        if len(first_runs) < 2 or not 0 <= join_index < len(first_runs) - 1:
            return None
        first_source = first_runs[0].source_line
        if any(run.source_line != first_source for run in first_runs):
            return None
        if second_source == first_source or not second_group:
            return None
        first_move = tuple(no for run in first_runs for no in run.nos)
        if tuple(physical.line_access_order(self.cars, first_source)[: len(first_move)]) != first_move:
            return None
        if tuple(physical.line_access_order(self.cars, second_source)[: len(second_group)]) != second_group:
            return None

        pending = self.current_unsatisfied_nos() & self.active_nos
        join_target = first_runs[join_index].target_line
        by_no = self.by_no()
        all_nos = (*first_move, *second_group)
        if len(set(all_nos)) != len(all_nos) or any(no not in by_no for no in all_nos):
            return None
        if any(no not in pending for no in all_nos):
            return None
        if any(self.target_by_no.get(no, "") != join_target for no in second_group):
            return None
        if physical.pull_equivalent([by_no[no] for no in first_move]) > physical.PULL_LIMIT_EQUIVALENT:
            return None
        retained_nos = tuple(
            no
            for run in first_runs[: join_index + 1]
            for no in run.nos
        )
        if physical.pull_equivalent(
            [by_no[no] for no in (*retained_nos, *second_group)]
        ) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = [physical.plan_step("Get", first_source, first_move)]
        physical.apply_physical_get_order(planning_cars, first_source, first_move)

        def append_put(
            target_line: str,
            move: tuple[str, ...],
            source_hint: str,
        ) -> bool:
            if not self.target_put_allowed(
                source_line=source_hint,
                target_line=target_line,
                move=move,
                planning_cars=planning_cars,
            ):
                return False
            positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in move],
                target_line=target_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(all_nos),
            )
            if len(positions) != len(move):
                return False
            put_order = tuple(sorted(
                move,
                key=lambda no: (int(positions.get(no) or 0), move.index(no)),
            ))
            if put_order != move:
                return False
            steps.append(physical.plan_step("Put", target_line, move, positions))
            physical.apply_physical_put_order(
                planning_cars,
                target_line,
                list(move),
                positions,
            )
            return True

        for run in reversed(first_runs[join_index + 1 :]):
            if not append_put(run.target_line, run.nos, first_source):
                return None

        steps.append(physical.plan_step("Get", second_source, second_group))
        physical.apply_physical_get_order(planning_cars, second_source, second_group)
        joined = (*first_runs[join_index].nos, *second_group)
        if not append_put(join_target, joined, second_source):
            return None

        for run in reversed(first_runs[:join_index]):
            if not append_put(run.target_line, run.nos, first_source):
                return None

        candidate = physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=first_source,
            target_line=join_target,
            batch=[by_no[no] for no in all_nos],
            steps=tuple(steps),
            reason=(
                f"stage4_closed_macro:partial_drop_continue_get:"
                f"{first_source}+{second_source}->{join_target}"
            ),
            candidate_kind="stage4_closed_macro",
        )
        return candidate if self.validate(candidate).accepted else None

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
        candidate = self.build_prefix_extract_restore_session(source_line, move)
        if candidate:
            yield candidate, "closed_prefix_extract_restore_session"
        candidate = self.build_prefix_ordered_target_restore_session(source_line, move)
        if candidate:
            yield candidate, "closed_prefix_ordered_target_restore_session"
        candidate = self.build_prefix_target_layout_restore_session(source_line, move)
        if candidate:
            yield candidate, "closed_prefix_target_layout_restore_session"
        candidate = self.build_source_corridor_release_session(source_line, move)
        if candidate:
            yield candidate, "closed_source_corridor_release_session"
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

    def build_prefix_extract_restore_session(
        self,
        source_line: str,
        move: tuple[str, ...],
    ) -> physical.HookCandidate | None:
        """Deliver a buried active suffix, then restore its satisfied source prefix."""
        if not move:
            return None
        ordered = tuple(physical.line_access_order(self.cars, source_line))
        if ordered[: len(move)] != move:
            return None
        pending = self.current_unsatisfied_nos() & self.active_nos
        first_active = next((index for index, no in enumerate(move) if no in pending), -1)
        if first_active <= 0:
            return None
        protected_prefix = move[:first_active]
        service_suffix = move[first_active:]
        if not service_suffix or any(no not in pending for no in service_suffix):
            return None
        if any(
            no not in self.protected_satisfied_nos or not self.car_satisfied_no(no)
            for no in protected_prefix
        ):
            return None

        by_no = self.by_no()
        if any(no not in by_no for no in move):
            return None
        batch = [by_no[no] for no in move]
        if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = [physical.plan_step("Get", source_line, move)]
        physical.apply_physical_get_order(planning_cars, source_line, move)
        carried = list(move)
        progressed: list[str] = []

        while len(carried) > len(protected_prefix):
            target_line = self.target_by_no.get(carried[-1], "")
            if (
                not target_line
                or target_line == source_line
                or target_line in OUT_OF_SCOPE_TARGET_LINES
                or target_line in EXCLUDED_OPERATION_LINES
                or target_line not in physical.TRACK_SPECS
            ):
                return None
            start = len(carried) - 1
            while (
                start > len(protected_prefix)
                and self.target_by_no.get(carried[start - 1], "") == target_line
            ):
                start -= 1
            group = tuple(carried[start:])
            if not self.target_put_allowed(
                source_line=source_line,
                target_line=target_line,
                move=group,
                planning_cars=planning_cars,
            ):
                return None
            positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in group],
                target_line=target_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(carried),
            )
            if len(positions) != len(group):
                return None
            steps.append(physical.plan_step("Put", target_line, group, positions))
            physical.apply_physical_put_order(planning_cars, target_line, list(group), positions)
            progressed.extend(group)
            del carried[start:]

        restore_positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in protected_prefix],
            target_line=source_line,
            cars=planning_cars,
            depot_assignment=self.depot_assignment,
            batch_nos=set(protected_prefix),
        )
        if len(restore_positions) != len(protected_prefix):
            return None
        steps.append(physical.plan_step("Put", source_line, protected_prefix, restore_positions))
        physical.apply_physical_put_order(
            planning_cars,
            source_line,
            list(protected_prefix),
            restore_positions,
        )
        if set(progressed) != set(service_suffix):
            return None

        candidate = physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=source_line,
            target_line=source_line,
            batch=batch,
            steps=tuple(steps),
            reason=f"stage4_closed_macro:prefix_extract_restore:{source_line}",
            candidate_kind="stage4_closed_macro",
        )
        return candidate if self.validate(candidate).accepted else None

    def build_prefix_target_layout_restore_session(
        self,
        source_line: str,
        move: tuple[str, ...],
    ) -> physical.HookCandidate | None:
        """Rebuild a blocked target while restoring satisfied cars ahead of the source group."""
        if not move:
            return None
        pending = self.current_unsatisfied_nos() & self.active_nos
        first_active = next((index for index, no in enumerate(move) if no in pending), -1)
        if first_active <= 0:
            return None
        protected_prefix = move[:first_active]
        service_suffix = move[first_active:]
        if not service_suffix or any(no not in pending for no in service_suffix):
            return None
        if any(
            no not in self.protected_satisfied_nos or not self.car_satisfied_no(no)
            for no in protected_prefix
        ):
            return None
        targets = {self.target_by_no.get(no, "") for no in service_suffix}
        if len(targets) != 1:
            return None
        target_line = next(iter(targets))
        if not target_line or target_line == source_line:
            return None
        return self.build_layout_rebuild_session(source_line, service_suffix, target_line)

    def build_prefix_ordered_target_restore_session(
        self,
        source_line: str,
        move: tuple[str, ...],
    ) -> physical.HookCandidate | None:
        """Rebuild one occupied target while restoring a satisfied source prefix."""
        if not move:
            return None
        source_order = tuple(physical.line_access_order(self.cars, source_line))
        if source_order[: len(move)] != move:
            return None

        pending = self.current_unsatisfied_nos() & self.active_nos
        first_active = next((index for index, no in enumerate(move) if no in pending), -1)
        if first_active <= 0:
            return None
        source_restore = move[:first_active]
        source_group = move[first_active:]
        if not source_group or any(no not in pending for no in source_group):
            return None
        if any(
            no not in self.protected_satisfied_nos
            or not self.car_satisfied_no(no)
            for no in source_restore
        ):
            return None

        targets = {self.target_by_no.get(no, "") for no in source_group}
        if len(targets) != 1:
            return None
        target_line = next(iter(targets))
        if (
            not target_line
            or target_line == source_line
            or target_line in OUT_OF_SCOPE_TARGET_LINES
            or target_line in EXCLUDED_OPERATION_LINES
            or target_line not in physical.TRACK_SPECS
        ):
            return None
        target_existing = tuple(physical.line_access_order(self.cars, target_line))
        if not target_existing:
            return None
        if any(
            self.target_by_no.get(no, "") != target_line and not self.car_satisfied_no(no)
            for no in target_existing
        ):
            return None

        ordering = self.target_rebuild_order(
            source_line,
            source_group,
            target_line,
            target_existing,
        )
        if ordering is None:
            return None
        target_before, target_after, target_put, target_positions = ordering
        if set(target_put) != set(target_existing) | set(source_group):
            return None

        by_no = self.by_no()
        moved_nos = (*target_existing, *move)
        if len(set(moved_nos)) != len(moved_nos) or any(no not in by_no for no in moved_nos):
            return None
        batch = [by_no[no] for no in moved_nos]
        if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        planning_cars = [clone_car(car) for car in self.cars]
        steps: list[physical.PlanStep] = []
        if target_before:
            steps.append(physical.plan_step("Get", target_line, target_before))
            physical.apply_physical_get_order(planning_cars, target_line, target_before)

        steps.append(physical.plan_step("Get", source_line, move))
        physical.apply_physical_get_order(planning_cars, source_line, move)

        if target_after:
            steps.append(physical.plan_step("Get", target_line, target_after))
            physical.apply_physical_get_order(planning_cars, target_line, target_after)

        deep_target = (*source_group, *target_after)
        deep_positions = {no: target_positions[no] for no in deep_target}
        steps.append(physical.plan_step("Put", target_line, deep_target, deep_positions))
        physical.apply_physical_put_order(
            planning_cars,
            target_line,
            list(deep_target),
            deep_positions,
        )

        restore_positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in source_restore],
            target_line=source_line,
            cars=planning_cars,
            depot_assignment=self.depot_assignment,
            batch_nos=set(source_restore),
        )
        if len(restore_positions) != len(source_restore):
            return None
        steps.append(physical.plan_step("Put", source_line, source_restore, restore_positions))
        physical.apply_physical_put_order(
            planning_cars,
            source_line,
            list(source_restore),
            restore_positions,
        )

        if target_before:
            before_positions = {no: target_positions[no] for no in target_before}
            steps.append(physical.plan_step("Put", target_line, target_before, before_positions))
            physical.apply_physical_put_order(
                planning_cars,
                target_line,
                list(target_before),
                before_positions,
            )

        candidate = physical.build_planlet_candidate(
            case_id=self.case_id,
            hook_index=self.macro_index,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            steps=tuple(steps),
            reason=(
                f"stage4_closed_macro:prefix_ordered_target_restore:"
                f"{source_line}->{target_line}"
            ),
            candidate_kind="stage4_closed_macro",
        )
        return candidate if self.validate(candidate).accepted else None

    def build_source_corridor_release_session(
        self,
        source_line: str,
        move: tuple[str, ...],
    ) -> physical.HookCandidate | None:
        """Temporarily lift a satisfied source tail that blocks a serial target."""
        ordered = tuple(physical.line_access_order(self.cars, source_line))
        if not move or len(move) >= len(ordered) or ordered[: len(move)] != move:
            return None
        tail = ordered[len(move) :]
        if not tail:
            return None
        target_lines = {self.target_by_no.get(no, "") for no in move}
        if len(target_lines) != 1:
            return None
        target_line = next(iter(target_lines))
        if (
            not target_line
            or target_line == source_line
            or target_line not in physical.TRACK_SPECS
            or target_line in OUT_OF_SCOPE_TARGET_LINES
            or target_line in EXCLUDED_OPERATION_LINES
        ):
            return None
        if any(
            self.target_by_no.get(no, "") != source_line or not self.car_satisfied_no(no)
            for no in tail
        ):
            return None
        _static, available, blockers = physical.route_blocking_lines(
            self.graph,
            self.cars,
            source_line,
            target_line,
            set(move),
        )
        if available or blockers:
            return None
        by_no = self.by_no()
        full_move = (*move, *tail)
        if physical.pull_equivalent([by_no[no] for no in full_move]) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        for staging_line in self.temporary_staging_lines(
            excluded_lines={source_line, target_line},
        ):
            planning_cars = [clone_car(car) for car in self.cars]
            steps: list[physical.PlanStep] = [
                physical.plan_step("Get", source_line, full_move),
            ]
            physical.apply_physical_get_order(planning_cars, source_line, full_move)
            staging_positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in tail],
                target_line=staging_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(full_move),
            )
            if len(staging_positions) != len(tail):
                continue
            steps.append(physical.plan_step("Put", staging_line, tail, staging_positions))
            physical.apply_physical_put_order(
                planning_cars,
                staging_line,
                list(tail),
                staging_positions,
            )
            target_positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in move],
                target_line=target_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(move),
            )
            if len(target_positions) != len(move):
                continue
            target_put = tuple(sorted(
                move,
                key=lambda no: (int(target_positions.get(no) or 0), move.index(no)),
            ))
            steps.append(physical.plan_step("Put", target_line, target_put, target_positions))
            physical.apply_physical_put_order(
                planning_cars,
                target_line,
                list(target_put),
                target_positions,
            )
            steps.append(physical.plan_step("Get", staging_line, tail))
            physical.apply_physical_get_order(planning_cars, staging_line, tail)
            restore_positions = physical.planned_positions_for_batch(
                batch=[by_no[no] for no in tail],
                target_line=source_line,
                cars=planning_cars,
                depot_assignment=self.depot_assignment,
                batch_nos=set(tail),
            )
            if len(restore_positions) != len(tail):
                continue
            steps.append(physical.plan_step("Put", source_line, tail, restore_positions))

            candidate = physical.build_planlet_candidate(
                case_id=self.case_id,
                hook_index=self.macro_index,
                source_line=source_line,
                target_line=source_line,
                batch=[by_no[no] for no in full_move],
                steps=tuple(steps),
                reason=(
                    f"stage4_closed_macro:source_corridor_release_session:"
                    f"{source_line}->{target_line}"
                ),
                candidate_kind="stage4_closed_macro",
            )
            if self.validate(candidate).accepted:
                return candidate
        return None

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
            candidate = self.build_layout_rebuild_session(source_line, tuple(group), target_line)
            if candidate:
                yield candidate, "closed_layout_rebuild_session"
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

    def build_layout_rebuild_session(
        self,
        source_line: str,
        source_group: tuple[str, ...],
        target_line: str,
    ) -> physical.HookCandidate | None:
        """Stage all participating lines, then rebuild a target in one final layout."""
        by_no = self.by_no()
        if not source_group or any(no not in by_no for no in source_group):
            return None
        if any(self.target_by_no.get(no, "") != target_line for no in source_group):
            return None
        source_order = tuple(physical.line_access_order(self.cars, source_line))
        source_indexes = [source_order.index(no) for no in source_group if no in source_order]
        if len(source_indexes) != len(source_group):
            return None
        source_move = source_order[: max(source_indexes) + 1]
        if tuple(no for no in source_move if no in set(source_group)) != source_group:
            return None
        source_restore = tuple(no for no in source_move if no not in set(source_group))
        if any(
            no not in self.protected_satisfied_nos
            or not self.car_satisfied_no(no)
            for no in source_restore
        ):
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

        target_nos = (*target_existing, *source_group)
        target_planning = [clone_car(car) for car in self.cars]
        physical.apply_physical_get_order(target_planning, target_line, target_existing)
        physical.apply_physical_get_order(target_planning, source_line, source_move)
        target_positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in target_nos],
            target_line=target_line,
            cars=target_planning,
            depot_assignment=self.depot_assignment,
            batch_nos=set(target_nos),
        )
        if len(target_positions) != len(target_nos):
            return None
        target_put = tuple(sorted(
            target_nos,
            key=lambda no: (int(target_positions.get(no) or 0), target_nos.index(no)),
        ))

        origins: dict[str, tuple[str, ...]] = {
            target_line: target_existing,
            source_line: source_move,
        }
        blocker_groups = [
            *self.get_route_blocker_groups(target_line, target_existing),
            *self.get_route_blocker_groups(source_line, source_group),
            *self.put_route_blocker_groups(source_line, source_group),
        ]
        for blocker_line, blocker_nos in blocker_groups:
            if blocker_line == target_line:
                continue
            full_line = tuple(physical.line_access_order(self.cars, blocker_line))
            if not full_line:
                continue
            if blocker_line in origins:
                origins[blocker_line] = full_line
            else:
                origins[blocker_line] = full_line

        moved_nos = tuple(dict.fromkeys(no for nos in origins.values() for no in nos))
        if any(no not in by_no for no in moved_nos):
            return None
        if physical.pull_equivalent([by_no[no] for no in moved_nos]) > physical.PULL_LIMIT_EQUIVALENT:
            return None

        chunks = self.layout_rebuild_chunks(
            origins=origins,
            target_put=target_put,
            target_nos=set(target_nos),
        )
        if not chunks or len(chunks) > 7:
            return None
        chunk_by_no = {
            no: index
            for index, chunk in enumerate(chunks)
            for no in chunk.nos
        }
        if set(chunk_by_no) != set(moved_nos):
            return None
        target_chunk_order = self.chunk_order_for_nos(target_put, chunk_by_no)
        origin_chunk_orders = {
            line: self.chunk_order_for_nos(nos, chunk_by_no)
            for line, nos in origins.items()
        }

        assignments = self.layout_staging_assignments(
            chunks=chunks,
            origins=origins,
        )
        if not assignments:
            return None
        origin_orders = list(permutations(origins.keys()))
        all_moved = set(moved_nos)
        for assigned_lines in assignments:
            for origin_order in origin_orders:
                planning_cars = [clone_car(car) for car in self.cars]
                steps: list[physical.PlanStep] = []
                feasible = True
                for line in origin_order:
                    move = origins[line]
                    steps.append(physical.plan_step("Get", line, move))
                    physical.apply_physical_get_order(planning_cars, line, move)
                    for chunk_index in reversed(origin_chunk_orders[line]):
                        chunk = chunks[chunk_index]
                        staging_line = assigned_lines[chunk_index]
                        positions = physical.planned_positions_for_batch(
                            batch=[by_no[no] for no in chunk.nos],
                            target_line=staging_line,
                            cars=planning_cars,
                            depot_assignment=self.depot_assignment,
                            batch_nos=all_moved,
                        )
                        if len(positions) != len(chunk.nos):
                            feasible = False
                            break
                        steps.append(physical.plan_step("Put", staging_line, chunk.nos, positions))
                        physical.apply_physical_put_order(
                            planning_cars,
                            staging_line,
                            list(chunk.nos),
                            positions,
                        )
                    if not feasible:
                        break
                if not feasible:
                    continue

                for chunk_index in target_chunk_order:
                    chunk = chunks[chunk_index]
                    steps.append(physical.plan_step("Get", assigned_lines[chunk_index], chunk.nos))
                    physical.apply_physical_get_order(
                        planning_cars,
                        assigned_lines[chunk_index],
                        chunk.nos,
                    )
                steps.append(physical.plan_step("Put", target_line, target_put, target_positions))
                physical.apply_physical_put_order(
                    planning_cars,
                    target_line,
                    list(target_put),
                    target_positions,
                )

                for line, chunk_order in origin_chunk_orders.items():
                    restore_chunks = [index for index in chunk_order if not chunks[index].to_target]
                    if not restore_chunks:
                        continue
                    restore_nos: list[str] = []
                    for chunk_index in restore_chunks:
                        chunk = chunks[chunk_index]
                        steps.append(physical.plan_step("Get", assigned_lines[chunk_index], chunk.nos))
                        physical.apply_physical_get_order(
                            planning_cars,
                            assigned_lines[chunk_index],
                            chunk.nos,
                        )
                        restore_nos.extend(chunk.nos)
                    positions = physical.planned_positions_for_batch(
                        batch=[by_no[no] for no in restore_nos],
                        target_line=line,
                        cars=planning_cars,
                        depot_assignment=self.depot_assignment,
                        batch_nos=set(restore_nos),
                    )
                    if len(positions) != len(restore_nos):
                        feasible = False
                        break
                    steps.append(physical.plan_step("Put", line, tuple(restore_nos), positions))
                    physical.apply_physical_put_order(planning_cars, line, restore_nos, positions)
                if not feasible:
                    continue

                candidate = physical.build_planlet_candidate(
                    case_id=self.case_id,
                    hook_index=self.macro_index,
                    source_line=origin_order[0],
                    target_line=target_line,
                    batch=[by_no[no] for no in moved_nos],
                    steps=tuple(steps),
                    reason=(
                        f"stage4_closed_macro:layout_rebuild_session:"
                        f"{source_line}->{target_line};chunks={len(chunks)}"
                    ),
                    candidate_kind="stage4_closed_macro",
                )
                if self.validate(candidate).accepted:
                    return candidate
        return None

    def layout_rebuild_chunks(
        self,
        *,
        origins: dict[str, tuple[str, ...]],
        target_put: tuple[str, ...],
        target_nos: set[str],
    ) -> tuple[LayoutChunk, ...]:
        origin_by_no = {
            no: line
            for line, nos in origins.items()
            for no in nos
        }
        chunks: list[LayoutChunk] = []
        for no in target_put:
            line = origin_by_no.get(no, "")
            if not line:
                return ()
            origin = origins[line]
            if (
                chunks
                and chunks[-1].to_target
                and chunks[-1].origin_line == line
                and origin.index(chunks[-1].nos[-1]) + 1 == origin.index(no)
            ):
                previous = chunks[-1]
                chunks[-1] = LayoutChunk(line, (*previous.nos, no), True)
            else:
                chunks.append(LayoutChunk(line, (no,), True))

        for line, nos in origins.items():
            current: list[str] = []
            for no in nos:
                if no in target_nos:
                    if current:
                        chunks.append(LayoutChunk(line, tuple(current), False))
                        current = []
                    continue
                current.append(no)
            if current:
                chunks.append(LayoutChunk(line, tuple(current), False))
        return tuple(chunks)

    def chunk_order_for_nos(
        self,
        nos: tuple[str, ...],
        chunk_by_no: dict[str, int],
    ) -> tuple[int, ...]:
        order: list[int] = []
        for no in nos:
            index = chunk_by_no.get(no)
            if index is None:
                return ()
            if not order or order[-1] != index:
                order.append(index)
        return tuple(order)

    def layout_staging_assignments(
        self,
        *,
        chunks: tuple[LayoutChunk, ...],
        origins: dict[str, tuple[str, ...]],
        limit: int = 360,
    ) -> list[tuple[str, ...]]:
        planning_cars = [clone_car(car) for car in self.cars]
        for line, nos in origins.items():
            physical.apply_physical_get_order(planning_cars, line, nos)
        excluded = set(origins)
        staging_lines = self.temporary_staging_lines(excluded_lines=excluded)[:12]
        options: list[tuple[str, ...]] = []
        by_no = self.by_no()
        for chunk in chunks:
            chunk_length = sum(physical.car_length(by_no[no]) for no in chunk.nos)
            candidates = tuple(
                line
                for line in staging_lines
                if sum(physical.car_length(car) for car in planning_cars if car["Line"] == line)
                + chunk_length
                <= physical.TRACK_SPECS[line].length_m + physical.LINE_LENGTH_TOLERANCE_M
            )
            if not candidates:
                return []
            options.append(candidates)

        assignments: list[tuple[str, ...]] = []
        assigned = [""] * len(chunks)
        order = sorted(range(len(chunks)), key=lambda index: (len(options[index]), index))

        def visit(depth: int, used: set[str]) -> None:
            if len(assignments) >= limit:
                return
            if depth == len(order):
                assignments.append(tuple(assigned))
                return
            index = order[depth]
            for line in options[index]:
                if line in used:
                    continue
                assigned[index] = line
                visit(depth + 1, {*used, line})
                assigned[index] = ""

        visit(0, set())
        return assignments

    def temporary_staging_lines(self, *, excluded_lines: set[str]) -> tuple[str, ...]:
        loads = physical.line_length_loads(self.cars)
        lines = [
            line
            for line in physical.TRACK_SPECS
            if line not in excluded_lines
            and line not in EXCLUDED_OPERATION_LINES
            and line not in OUT_OF_SCOPE_TARGET_LINES
            and not physical.is_spotting_line(line)
        ]
        lines.sort(key=lambda line: (
            0 if line in SAFE_CACHE_LINES else 1,
            0 if not physical.line_access_order(self.cars, line) else 1,
            round(loads.get(line, 0.0) / max(physical.TRACK_SPECS[line].length_m, 1.0), 6),
            line,
        ))
        return tuple(lines)

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
            tail_capacity_hold = carried[-1] in self.infeasible_nos
            target_line = tail_target if tail_target else source_line
            start = len(carried) - 1
            while start > 0 and (
                restore_line_by_no.get(carried[start - 1])
                or self.target_by_no.get(carried[start - 1], "")
            ) == tail_target and (carried[start - 1] in self.infeasible_nos) == tail_capacity_hold:
                start -= 1
            group = tuple(carried[start:])
            if not group:
                return None
            is_restore_group = all(no in restore_line_by_no for no in group)
            is_capacity_hold = (
                bool(group)
                and target_line in self.infeasible_lines
                and all(no in self.infeasible_nos for no in group)
            )
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
            if is_capacity_hold:
                put_line = self.choose_cache_line(group, source_line, planning_cars)
                if put_line:
                    note = "capacity_hold"
                    cache_puts += 1
            elif prefer_cache:
                put_line = self.choose_cache_line(group, source_line, planning_cars)
                if put_line:
                    note = "cache"
                    cache_puts += 1
            if is_capacity_hold and not put_line:
                return None
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
            before_blocked = self.blocked_active_count(self.cars)
            after_blocked = self.blocked_active_count(planning_cars)
            before_prep = self.prep_progress_for(self.cars)
            after_prep = self.prep_progress_for(planning_cars)
            if after_blocked >= before_blocked and after_prep >= before_prep:
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
        if (
            not self.target_ready(target_line, planning_cars)
            and not self.dirty_terminal_stack_allowed(
                target_line=target_line,
                move=move,
                planning_cars=planning_cars,
            )
        ):
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
        if self.route_lock_damage_after_put(
            target_line=target_line,
            move=move,
            planning_cars=planning_cars,
        ):
            return False
        return True

    def dirty_terminal_stack_allowed(
        self,
        *,
        target_line: str,
        move: tuple[str, ...],
        planning_cars: list[dict[str, Any]],
    ) -> bool:
        """Allow a recoverable stack on a dirty terminal, never on a corridor."""
        if not move or physical.is_spotting_line(target_line):
            return False
        if target_line in LINE_READY_PREREQUISITES:
            return False
        if any(self.target_by_no.get(no, "") != target_line for no in move):
            return False

        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(planning_cars, self.depot_assignment)
        } & self.active_nos
        existing_order = physical.line_access_order(planning_cars, target_line)
        outgoing = {
            no
            for no in existing_order
            if no in unsatisfied and self.target_by_no.get(no, "") != target_line
        }
        if not outgoing:
            return False
        if any(
            no in unsatisfied and self.target_by_no.get(no, "") == target_line
            for no in existing_order
        ):
            return False
        if self.line_is_pending_transit_corridor(target_line, planning_cars, ignored_nos=set(move)):
            return False

        projected = [clone_car(car) for car in planning_cars]
        positions = physical.planned_positions_for_batch(
            batch=[self.by_no()[no] for no in move if no in self.by_no()],
            target_line=target_line,
            cars=projected,
            depot_assignment=self.depot_assignment,
            batch_nos=set(move),
        )
        if len(positions) != len(move):
            return False
        physical.apply_physical_put_order(projected, target_line, list(move), positions)
        return self.line_cleanup_prefix_within_pull_limit(target_line, projected, outgoing)

    def line_cleanup_prefix_within_pull_limit(
        self,
        line: str,
        cars: list[dict[str, Any]],
        debt_nos: set[str],
    ) -> bool:
        order = physical.line_access_order(cars, line)
        indexes = [index for index, no in enumerate(order) if no in debt_nos]
        if not indexes:
            return False
        cleanup = order[: max(indexes) + 1]
        by_no = {physical.car_no(car): car for car in cars}
        return physical.pull_equivalent(
            [by_no[no] for no in cleanup if no in by_no]
        ) <= physical.PULL_LIMIT_EQUIVALENT

    def line_is_pending_transit_corridor(
        self,
        line: str,
        cars: list[dict[str, Any]],
        *,
        ignored_nos: set[str] | None = None,
    ) -> bool:
        ignored = ignored_nos or set()
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos
        by_no = {physical.car_no(car): car for car in cars}
        for no in unsatisfied - ignored:
            car = by_no.get(no)
            target = self.target_by_no.get(no, "")
            source = physical.normalize_line((car or {}).get("Line"))
            if not source or not target or source == target:
                continue
            if line in {source, target}:
                continue
            route = self.graph.route(source, target)
            if line in route[1:-1]:
                return True
        return False

    def pending_gate_lease_damage(
        self,
        occupied_lines: set[str],
        cars: list[dict[str, Any]],
        *,
        ignored_nos: set[str] | None = None,
    ) -> set[str]:
        ignored = ignored_nos or set()
        occupied = {physical.normalize_line(line) for line in occupied_lines if line}
        if not occupied:
            return set()
        pending = ({
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos) - ignored
        by_no = {physical.car_no(car): car for car in cars}
        damaged: set[str] = set()
        for no in pending:
            car = by_no.get(no)
            if car is None:
                continue
            service_lines = {
                physical.normalize_line(car.get("Line")),
                physical.normalize_line(self.target_by_no.get(no, "")),
            }
            leased_gates = {
                gate
                for service_line in service_lines
                for gate in SERVICE_GATE_LEASES.get(service_line, ())
            }
            if occupied & leased_gates:
                damaged.add(no)
        return damaged

    def route_lock_damage_after_put(
        self,
        *,
        target_line: str,
        move: tuple[str, ...],
        planning_cars: list[dict[str, Any]],
    ) -> set[str]:
        """Return pending cars whose route is newly locked by a persistent Put."""
        if not move or target_line not in physical.TRACK_SPECS:
            return set()
        ignored = set(move)
        damaged = self.pending_gate_lease_damage(
            {target_line},
            planning_cars,
            ignored_nos=ignored,
        )
        before = self.pending_route_states(planning_cars, ignored_nos=ignored)
        projected = [clone_car(car) for car in planning_cars]
        for car in projected:
            if physical.car_no(car) in ignored:
                car["Line"] = target_line
                car["Position"] = 1
        after = self.pending_route_states(projected, ignored_nos=ignored)
        for no, after_state in after.items():
            before_state = before.get(no, PendingRouteState(False, (), False, ()))
            if before_state.service_available and not after_state.service_available:
                damaged.add(no)
                continue
            if (
                not after_state.service_available
                and target_line in after_state.service_blockers
                and target_line not in before_state.service_blockers
            ):
                damaged.add(no)
                continue
        return damaged

    def pending_route_states(
        self,
        cars: list[dict[str, Any]],
        *,
        ignored_nos: set[str] | None = None,
    ) -> dict[str, PendingRouteState]:
        ignored = ignored_nos or set()
        signature = tuple(
            (physical.car_no(car), car.get("Line") or "", int(car.get("Position") or 0))
            for car in sorted(cars, key=lambda item: physical.car_no(item))
        )
        cache_key = (self.loco.line, signature, tuple(sorted(ignored)))
        cached = self._pending_route_states_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        pending = (self.current_unsatisfied_nos() & self.active_nos) - ignored
        by_no = {physical.car_no(car): car for car in cars}
        states: dict[str, PendingRouteState] = {}
        for no in pending:
            car = by_no.get(no)
            target = self.target_by_no.get(no, "")
            source = physical.normalize_line((car or {}).get("Line"))
            if not car or not source or not target or source == target:
                continue
            if source not in physical.TRACK_SPECS or target not in physical.TRACK_SPECS:
                continue
            _static, available, blockers = physical.route_blocking_lines(
                self.graph,
                cars,
                source,
                target,
                {no},
            )
            occupied = physical.occupied_lines_for_get_route(cars, {no}, source)
            access_path = self.graph.route_avoiding_occupied(
                self.loco.line,
                source,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(
                    self.loco.line,
                    cars,
                    {no},
                ),
                target_approach_lines=physical.route_approach_lines_for_get(source),
                cars=cars,
                moving_nos={no},
                train_length_m=0.0,
            )
            access_blockers: list[str] = []
            if not access_path:
                endpoints = {physical.normalize_line(self.loco.line), source}
                for line in self.graph.route(self.loco.line, source):
                    if line in occupied and line not in endpoints and line not in access_blockers:
                        access_blockers.append(line)
            states[no] = PendingRouteState(
                service_available=bool(available),
                service_blockers=tuple(blockers),
                access_available=bool(access_path),
                access_blockers=tuple(access_blockers),
            )
        self._pending_route_states_cache[cache_key] = dict(states)
        return states

    def choose_cache_line(
        self,
        move: tuple[str, ...],
        source_line: str,
        planning_cars: list[dict[str, Any]],
    ) -> str:
        active_targets = self.pending_target_lines(planning_cars)
        ranked: list[tuple[Any, ...]] = []
        for line in (*SAFE_CACHE_LINES, *ASSEMBLY_CACHE_LINES, *CORRIDOR_CACHE_LINES):
            if line == source_line:
                continue
            if line in active_targets:
                continue
            blocks_own_target = False
            for no in move:
                target = self.target_by_no.get(no, "")
                if not target or target == line or target not in physical.TRACK_SPECS:
                    continue
                route_to_target = self.graph.route(source_line, target)
                if line in route_to_target[1:-1]:
                    blocks_own_target = True
                    break
            if blocks_own_target:
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
            _static, available, _blockers = physical.route_blocking_lines(
                self.graph,
                planning_cars,
                source_line,
                line,
                set(move),
            )
            if not available:
                continue
            projected = [clone_car(car) for car in planning_cars]
            positions = physical.planned_positions_for_batch(
                batch=group_cars,
                target_line=line,
                cars=projected,
                depot_assignment=self.depot_assignment,
                batch_nos=set(move),
            )
            if len(positions) != len(move):
                continue
            physical.apply_physical_put_order(projected, line, list(move), positions)
            occupied = physical.occupied_lines_for_get_route(projected, set(move), line)
            retrieval_path = self.graph.route_avoiding_occupied(
                source_line,
                line,
                occupied,
                source_departure_lines=physical.route_departure_lines_for_source(
                    source_line,
                    projected,
                    set(),
                ),
                target_approach_lines=physical.route_approach_lines_for_get(line),
                cars=projected,
                moving_nos=set(move),
                train_length_m=0.0,
            )
            if not retrieval_path:
                continue
            route_damage = self.route_lock_damage_after_put(
                target_line=line,
                move=move,
                planning_cars=planning_cars,
            )
            if route_damage:
                continue
            route = self.graph.route(source_line, line)
            ranked.append((
                0 if line in SAFE_CACHE_LINES else 1,
                0 if not any(car["Line"] == line for car in planning_cars) else 1,
                round(load / max(spec.length_m, 1.0), 6),
                len(route) if route else 999,
                line,
            ))
        return min(ranked)[-1] if ranked else ""

    def pending_target_lines(self, cars: list[dict[str, Any]]) -> set[str]:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos
        return {
            self.target_by_no.get(no, "")
            for no in unsatisfied
            if self.target_by_no.get(no, "")
        }

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
        ranked: list[tuple[Any, ...]] = []
        for line in (*SAFE_CACHE_LINES, *ASSEMBLY_CACHE_LINES, *CORRIDOR_CACHE_LINES):
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
            route_damage = self.route_lock_damage_after_put(
                target_line=line,
                move=move,
                planning_cars=planning_cars,
            )
            if route_damage:
                continue
            route = self.graph.route(source_line, line)
            ranked.append((
                0 if line in SAFE_CACHE_LINES else 1,
                0 if not any(car["Line"] == line for car in planning_cars) else 1,
                round(load / max(physical.TRACK_SPECS[line].length_m, 1.0), 6),
                len(route) if route else 999,
                line,
            ))
        return min(ranked)[-1] if ranked else ""

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
        target_changes_after = self.refresh_active_targets()
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
            "target_changes_after": target_changes_after,
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
        clone.macro_index = self.macro_index
        clone.operation_index = self.operation_index
        clone.operations = []
        clone.trace = []
        clone.validation_cache = {}
        clone._by_no_cache = None
        clone._unsatisfied_nos_cache = None
        clone._state_signature_cache = None
        clone._prep_progress_cache = None
        clone._pending_route_states_cache = {}
        clone.target_by_no = dict(self.target_by_no)
        clone.target_reason_by_no = dict(self.target_reason_by_no)
        clone.active_nos = set(self.active_nos)
        clone.infeasible_nos = set(self.infeasible_nos)
        clone.infeasible_lines = set(self.infeasible_lines)
        clone.capacity_overflow_by_line = dict(self.capacity_overflow_by_line)
        clone.capacity_holdout_count_by_line = dict(self.capacity_holdout_count_by_line)
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
        clone.invalidate_caches()
        clone.refresh_active_targets()
        return clone

    def score_candidate(
        self,
        candidate: physical.HookCandidate,
        validation: physical.PhysicalValidation,
        progress_before: tuple[int, ...],
        progress_after: tuple[int, ...],
        prep_after: tuple[int, ...],
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
        pending = self.current_unsatisfied_nos() & self.active_nos
        by_no = self.by_no()
        resolved_source_fronts = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Get"
            and any(
                no in pending
                and (by_no.get(no) or {}).get("Line") == step.line
                and self.target_by_no.get(no, "") != step.line
                for no in step.move_car_nos
            )
        }
        target_put_runs = sum(
            1
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(
                no in pending and self.target_by_no.get(no, "") == step.line
                for no in step.move_car_nos
            )
        )
        source_rank = self.source_rank(candidate.source_line)
        reason_rank = 0 if reason.endswith("_target") else 3
        step_count = len(physical.candidate_plan_steps(candidate))
        resolved_by_tier = tuple(
            max(0, progress_before[index] - progress_after[index])
            for index in (0, 2, 4)
        )
        primary_tier = next(
            (index for index, count in enumerate(resolved_by_tier) if count > 0),
            3,
        )
        primary_resolved = resolved_by_tier[primary_tier] if primary_tier < 3 else 0
        total_resolved = sum(resolved_by_tier)
        progress_key: tuple[Any, ...] = (
            primary_tier,
            (step_count * 1000) // max(1, primary_resolved),
            (step_count * 1000) // max(1, total_resolved),
            progress_after,
            prep_after,
        )
        return (
            progress_key,
            target_put_runs,
            -len(resolved_source_fronts),
            reason_rank,
            not_ready_puts,
            cache_puts,
            -target_put_cars,
            step_count,
            source_rank,
            hot_cost,
            route_cost,
            candidate.candidate_id,
        )

    def candidate_business_target_key(
        self,
        candidate: physical.HookCandidate,
        pending_before: set[str],
    ) -> tuple[str, ...]:
        return tuple(sorted({
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(
                no in pending_before and self.target_by_no.get(no, "") == step.line
                for no in step.move_car_nos
            )
        }))

    def candidate_target_window_rank(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
        pending_before: set[str],
    ) -> tuple[int, int, int]:
        target_lines = set(self.candidate_business_target_key(candidate, pending_before))
        if not target_lines:
            step_count = len(physical.candidate_plan_steps(candidate))
            return (step_count * 1000, step_count, 0)

        debt_before = sum(
            1
            for no in pending_before
            if self.target_by_no.get(no, "") in target_lines
        )
        future_hooks = 0
        future_rounds = 0
        for target_line in sorted(target_lines):
            hooks, rounds = probe.target_window_remaining_lower_bound(target_line)
            future_hooks += hooks
            future_rounds += rounds
        session_hooks = len(physical.candidate_plan_steps(candidate))
        total_hooks = session_hooks + future_hooks
        return (
            (total_hooks * 1000) // max(1, debt_before),
            total_hooks,
            future_rounds,
        )

    def target_window_remaining_lower_bound(self, target_line: str) -> tuple[int, int]:
        """Relaxed Get/Put cost to finish one target without hiding order inversions."""
        pending = self.current_unsatisfied_nos() & self.active_nos
        pending_target = {
            no
            for no in pending
            if self.target_by_no.get(no, "") == target_line
            and (self.by_no().get(no) or {}).get("Line") != target_line
        }
        if not pending_target:
            return 0, 0

        by_no = self.by_no()
        target_existing = tuple(physical.line_access_order(self.cars, target_line))
        source_orders: dict[str, tuple[str, ...]] = {}
        for source_line in sorted({by_no[no]["Line"] for no in pending_target if no in by_no}):
            order = tuple(
                no
                for no in physical.line_access_order(self.cars, source_line)
                if no in pending_target
            )
            if order:
                source_orders[source_line] = order
        if not source_orders:
            return 0, 0

        participants = (*target_existing, *tuple(
            no
            for source_line in sorted(source_orders)
            for no in source_orders[source_line]
        ))
        planning_cars = [clone_car(car) for car in self.cars]
        if target_existing:
            physical.apply_physical_get_order(planning_cars, target_line, target_existing)
        for source_line, source_order in source_orders.items():
            physical.apply_physical_get_order(planning_cars, source_line, source_order)
        target_positions = physical.planned_positions_for_batch(
            batch=[by_no[no] for no in participants if no in by_no],
            target_line=target_line,
            cars=planning_cars,
            depot_assignment=self.depot_assignment,
            batch_nos=set(participants),
        )
        if len(target_positions) != len(participants):
            unavailable = len(source_orders) + 2
            return unavailable, 1

        runs_by_source: dict[str, int] = {}
        for source_line, source_order in source_orders.items():
            desired = [int(target_positions[no]) for no in source_order]
            runs_by_source[source_line] = 1 + sum(
                current <= previous
                for previous, current in zip(desired, desired[1:])
            )

        rounds = max(runs_by_source.values(), default=1)
        hooks = 0
        target_occupied = bool(target_existing)
        for round_index in range(rounds):
            hooks += sum(count > round_index for count in runs_by_source.values())
            if physical.is_spotting_line(target_line) and target_occupied:
                hooks += 1
            hooks += 1
            target_occupied = True
        return hooks, rounds

    def debt(self) -> dict[str, Any]:
        unsatisfied = self.current_unsatisfied_nos()
        capacity_residual = self.capacity_residual_nos(unsatisfied)
        active_unsatisfied = sorted((unsatisfied & self.active_nos) - capacity_residual)
        protected_damage = sorted(unsatisfied & self.protected_satisfied_nos)
        infeasible_unsatisfied = sorted(
            (unsatisfied & self.infeasible_nos) | capacity_residual
        )
        return {
            "complete": (
                not active_unsatisfied
                and not protected_damage
                and not self.out_of_scope_current_unsatisfied()
                and not infeasible_unsatisfied
            ),
            "actionable_complete": not active_unsatisfied and not protected_damage,
            "active_unsatisfied_count": len(active_unsatisfied),
            "active_unsatisfied_nos": active_unsatisfied,
            "infeasible_unsatisfied_count": len(infeasible_unsatisfied),
            "infeasible_unsatisfied_nos": infeasible_unsatisfied,
            "blocked_active_count": self.blocked_active_count(self.cars),
            "protected_damage_nos": protected_damage,
            "out_of_scope_unsatisfied_nos": sorted(self.out_of_scope_current_unsatisfied()),
            "excluded_line_nos": sorted(self.excluded_line_nos),
        }

    def capacity_residual_nos(self, unsatisfied: set[str]) -> set[str]:
        active_by_line: dict[str, set[str]] = {}
        preselected_by_line: dict[str, set[str]] = {}
        for no in unsatisfied & (self.active_nos | self.infeasible_nos):
            car = self.by_no().get(no)
            if not car:
                continue
            targets = {
                physical.normalize_line(line)
                for line in physical.target_lines(car)
                if physical.normalize_line(line) in physical.TRACK_SPECS
            }
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            if target in self.capacity_holdout_count_by_line:
                destination = (
                    preselected_by_line
                    if no in self.infeasible_nos
                    else active_by_line
                )
                destination.setdefault(target, set()).add(no)

        residual: set[str] = set()
        for target, nos in active_by_line.items():
            all_holdouts = nos | preselected_by_line.get(target, set())
            held_length = sum(
                physical.car_length(self.by_no()[no])
                for no in all_holdouts
                if no in self.by_no()
            )
            if (
                len(all_holdouts) <= self.capacity_holdout_count_by_line[target]
                and held_length + 1e-9 >= self.capacity_overflow_by_line[target]
            ):
                residual.update(nos)
        return residual

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

    def newly_buried_by_target_put(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
    ) -> set[str]:
        before_blocked = self.blocked_active_nos(self.cars)
        newly_blocked = probe.blocked_active_nos(probe.cars) - before_blocked
        if not newly_blocked:
            return set()
        target_put_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(self.target_by_no.get(no, "") == step.line for no in step.move_car_nos)
        }
        return {
            no
            for no in newly_blocked
            if (probe.by_no().get(no) or {}).get("Line") in target_put_lines
        }

    def target_put_burial_recoverable(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
        buried_nos: set[str],
    ) -> bool:
        put_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(self.target_by_no.get(no, "") == step.line for no in step.move_car_nos)
        }
        for line in {
            (probe.by_no().get(no) or {}).get("Line", "")
            for no in buried_nos
        }:
            if not line or line not in put_lines:
                return False
            if line in LINE_READY_PREREQUISITES:
                return False
            if physical.is_spotting_line(line):
                unsatisfied = probe.current_unsatisfied_nos() & probe.active_nos
                outbound = {
                    no
                    for no in physical.line_access_order(probe.cars, line)
                    if no in unsatisfied and probe.target_by_no.get(no, "") != line
                }
                outbound_targets = {
                    probe.target_by_no.get(no, "")
                    for no in outbound
                    if probe.target_by_no.get(no, "")
                }
                if len(outbound_targets) != 1:
                    return False
                outbound_target = next(iter(outbound_targets))
                if not probe.target_ready(outbound_target, probe.cars):
                    return False
                _static, available, _blockers = physical.route_blocking_lines(
                    probe.graph,
                    probe.cars,
                    line,
                    outbound_target,
                    outbound,
                )
                if not available:
                    return False
                if not probe.line_cleanup_prefix_within_pull_limit(line, probe.cars, outbound):
                    return False
                continue
            line_buried = {
                no
                for no in buried_nos
                if (probe.by_no().get(no) or {}).get("Line") == line
            }
            if probe.line_is_pending_transit_corridor(line, probe.cars):
                return False
            if not probe.line_cleanup_prefix_within_pull_limit(line, probe.cars, line_buried):
                return False
        return True

    def newly_access_locked_by_persistent_put(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
    ) -> set[str]:
        persistent_put_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(
                (probe.by_no().get(no) or {}).get("Line") == step.line
                for no in step.move_car_nos
            )
        }
        if not persistent_put_lines:
            return set()
        persistent_target_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(self.target_by_no.get(no, "") == step.line for no in step.move_car_nos)
        }
        before = self.pending_route_states(self.cars)
        after = probe.pending_route_states(probe.cars)
        damaged: set[str] = set()
        for no, after_state in after.items():
            before_state = before.get(no)
            if before_state is None or not before_state.access_available or after_state.access_available:
                continue
            new_blockers = set(after_state.access_blockers) - set(before_state.access_blockers)
            if new_blockers & persistent_put_lines:
                if self.target_by_no.get(no, "") in persistent_target_lines:
                    continue
                damaged.add(no)
        return damaged

    def persistent_gate_lease_damage(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
    ) -> set[str]:
        persistent_put_lines = {
            step.line
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            and any(
                (probe.by_no().get(no) or {}).get("Line") == step.line
                for no in step.move_car_nos
            )
        }
        before_by_line = {
            line: {
                physical.car_no(car)
                for car in self.cars
                if car.get("Line") == line
            }
            for line in persistent_put_lines
        }
        after_by_line = {
            line: {
                physical.car_no(car)
                for car in probe.cars
                if car.get("Line") == line
            }
            for line in persistent_put_lines
        }
        expanded_gate_lines = {
            line
            for line in persistent_put_lines
            if after_by_line[line] - before_by_line[line]
        }
        return probe.pending_gate_lease_damage(expanded_gate_lines, probe.cars)

    def inaccessible_persistent_cache_nos(
        self,
        candidate: physical.HookCandidate,
        probe: "Stage4Solver",
    ) -> set[str]:
        cached_nos = {
            no
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put"
            for no in step.move_car_nos
            if (probe.by_no().get(no) or {}).get("Line") == step.line
            and self.target_by_no.get(no, "") != step.line
            and no not in self.infeasible_nos
        }
        if not cached_nos:
            return set()
        states = probe.pending_route_states(probe.cars)
        return {
            no
            for no in cached_nos
            if no in probe.active_nos
            and no in probe.current_unsatisfied_nos()
            and (no not in states or not states[no].access_available)
        }

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
        cached = self.prep_progress_for(self.cars)
        self._prep_progress_cache = cached
        return cached

    def prep_progress_for(self, cars: list[dict[str, Any]]) -> tuple[int, ...]:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos
        by_no = {physical.car_no(car): car for car in cars}
        route_blocked = 0
        route_blocking_lines = 0
        target_contamination = 0
        target_contamination_depth = 0
        target_access_blocked = 0
        hot_blocked = 0
        target_front_process_penalty = 0
        target_access_not_front_ready = 0
        inbound_target_lines: set[str] = set()
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
            inbound_target_lines.add(target)
            if physical.is_spotting_line(target):
                existing = [
                    item
                    for item in cars
                    if item["Line"] == target and physical.car_no(item) != no
                ]
                if existing:
                    target_access_blocked += 1
                    if not self.source_front_ready_for_rebuild(no, source, cars):
                        target_front_process_penalty += self.target_process_weight(target)
                        target_access_not_front_ready += 1

        for target in inbound_target_lines:
            order = physical.line_access_order(cars, target)
            wrong_indexes = [
                index
                for index, no in enumerate(order)
                if self.target_by_no.get(no, "")
                and self.target_by_no.get(no, "") != target
            ]
            target_contamination += len(wrong_indexes)
            if wrong_indexes:
                target_contamination_depth += max(wrong_indexes) + 1

        return (
            route_blocked,
            route_blocking_lines,
            target_contamination,
            target_contamination_depth,
            target_access_blocked,
            hot_blocked,
            target_front_process_penalty,
            target_access_not_front_ready,
        )

    def source_front_ready_for_rebuild(
        self,
        no: str,
        source: str,
        cars: list[dict[str, Any]] | None = None,
    ) -> bool:
        if not source or source in EXCLUDED_OPERATION_LINES or source not in physical.TRACK_SPECS:
            return False
        ordered = physical.line_access_order(cars or self.cars, source)
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
        self._pending_route_states_cache.clear()

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
        hard_bad = [v for v in replay_bad if v.kind in RESULT_HARD_REPLAY_KINDS]
        final_cars = [normalize_car(car) for car in rv.final_cars(response, replayed)]
        response["Data"]["GeneratedEndStatus"] = [
            self.output_car(car)
            for car in sorted(final_cars, key=lambda item: (item["Line"], int(item.get("Position") or 0), physical.car_no(item)))
        ]
        combined = {
            "Data": {
                "Operations": self.combined_operations(response),
            }
        }
        combined_replayed, _combined_without_status_bad = rv.replay(self.original_request, combined)
        combined_final_cars = [
            normalize_car(car)
            for car in rv.final_cars(combined, combined_replayed)
        ]
        combined["Data"]["GeneratedEndStatus"] = [
            self.output_car(car)
            for car in sorted(
                combined_final_cars,
                key=lambda item: (
                    item["Line"],
                    int(item.get("Position") or 0),
                    physical.car_no(item),
                ),
            )
        ]
        combined_replayed, combined_bad = rv.replay(self.original_request, combined)
        combined_hard_bad = [
            v for v in combined_bad if v.kind in RESULT_HARD_REPLAY_KINDS
        ]
        final_unsatisfied = physical.unsatisfied_cars(final_cars, self.depot_assignment)
        final_unsat_nos = [physical.car_no(car) for car in final_unsatisfied]
        debt = self.debt()
        reported_infeasible_nos = set(self.infeasible_nos) | set(
            debt["infeasible_unsatisfied_nos"]
        )
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
            "stage4_strategy": "structural",
            "stage3_final_loco": self.stage3_final_loco,
            "stage4_start_loco": self.initial_loco.line,
            "operations": operation_count,
            "business_hooks": business_hooks,
            "hook_count_definition": "operation_rows_get_put_weigh",
            "active_count": len(self.active_nos),
            "active_nos": sorted(self.active_nos),
            "infeasible_count": len(reported_infeasible_nos),
            "infeasible_nos": sorted(reported_infeasible_nos),
            "infeasible_lines": sorted(self.infeasible_lines),
            "capacity_overflow_m_by_line": {
                line: round(overflow, 3)
                for line, overflow in sorted(self.capacity_overflow_by_line.items())
            },
            "capacity_minimum_holdout_by_line": dict(sorted(
                self.capacity_holdout_count_by_line.items()
            )),
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
        if debt.get("infeasible_unsatisfied_count"):
            reasons.append(f"hard_capacity_infeasible:{debt['infeasible_unsatisfied_count']}")
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
    result = run_solver(
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


def run_solver(
    *,
    case_id: str,
    request: dict[str, Any],
    depot_assignment: physical.DepotAssignment,
    stage3_request: dict[str, Any],
    stage3_response: dict[str, Any],
    stage3_combined_response: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    solver = Stage4Solver(
        case_id,
        request,
        depot_assignment,
        stage3_request,
        stage3_response,
        stage3_combined_response,
        time_budget_seconds=args.time_budget_seconds,
        max_macros=args.max_macros,
        max_candidates_per_step=args.max_candidates_per_step,
    )
    return solver.solve()


def error_summary(case_id: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": "partial",
        "stage4_strategy": "structural",
        "stage3_final_loco": "",
        "stage4_start_loco": "",
        "operations": 0,
        "business_hooks": 0,
        "hook_count_definition": "operation_rows_get_put_weigh",
        "active_count": 0,
        "active_nos": [],
        "infeasible_count": 0,
        "infeasible_nos": [],
        "infeasible_lines": [],
        "capacity_overflow_m_by_line": {},
        "capacity_minimum_holdout_by_line": {},
        "out_of_scope_count": 0,
        "out_of_scope_nos": [],
        "excluded_line_count": 0,
        "initial_unresolved_weigh_count": 0,
        "initial_unresolved_weigh_nos": [],
        "current_unresolved_weigh_count": 0,
        "current_unresolved_weigh_nos": [],
        "stage4_debt": {
            "complete": False,
            "actionable_complete": True,
            "active_unsatisfied_count": 0,
            "active_unsatisfied_nos": [],
            "infeasible_unsatisfied_count": 0,
            "infeasible_unsatisfied_nos": [],
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
