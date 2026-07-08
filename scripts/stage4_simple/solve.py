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
from solver_vnext import physical


DEFAULT_STAGE3_OUT = ROOT / "artifacts" / "four_stage_balanced_early_release_v2" / "stage3"
DEFAULT_TIME_BUDGET_SECONDS = 300.0
DEFAULT_MAX_MACROS = 160
DEFAULT_MAX_CANDIDATES_PER_STEP = 96
MAX_PREFIX_OPTIONS_PER_LINE = 10
MAX_CACHE_TRIES = 5
MAX_NO_PROGRESS_MACROS = 10
MAX_STALE_BEST_MACROS = 18

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
HOT_THROATS = {"渡10", "联7", "渡9", "渡8", "渡7", "渡4"}
MOVE_MODEL_RESTRICTIONS = (
    "global_state_is_closed_no_held",
    "held_only_exists_inside_planlet_macro",
    "get_put_weigh_each_count_as_one_operation_hook",
    "depot_related_lines_are_source_only_when_target_is_front_field",
    "safe_cache_lines_are_whitelisted",
    "protected_satisfied_cars_must_remain_satisfied_after_each_macro",
    "target_ready_is_required_for_ordinary_external_put",
    "same_line_service_sweep_may_bypass_target_ready_for_self_restack",
)


@dataclass(frozen=True)
class MacroView:
    candidate: physical.HookCandidate
    validation: physical.PhysicalValidation
    score: tuple[Any, ...]
    reason: str
    progress_after: tuple[int, ...]


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
    current = {physical.normalize_line((request.get("locoNode") or {}).get("Line"))}
    for row in sorted(rv.operations(response), key=lambda item: int(item.get("Index") or 0)):
        action = str(row.get("Action") or "")
        line = physical.normalize_line(row.get("Line"))
        if action == "Get":
            current = {line}
        elif action == "Put":
            current = rv.put_loco_positions(row.get("PassbyPath") or [], line)
        elif action == "Weigh":
            current = {WEIGH_LINE}
    return sorted(line for line in current if line)[0] if any(current) else WEIGH_LINE


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
        self.started_at = time.monotonic()
        self.deadline = self.started_at + time_budget_seconds
        self.max_macros = max_macros
        self.max_candidates_per_step = max_candidates_per_step
        self.macro_index = 1
        self.operation_index = 1
        self.operations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.validation_cache: dict[tuple[Any, str], physical.PhysicalValidation] = {}
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
        self.protected_satisfied_nos = {
            physical.car_no(car)
            for car in self.cars
            if physical.car_no(car) not in self.initial_unsatisfied_nos
        }
        self.seen_signatures = {self.state_signature()}
        self.best_progress = self.main_progress()

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

    def solve(self) -> dict[str, Any]:
        no_progress = 0
        stale_best = 0
        accepted_any = False
        while self.macro_index <= self.max_macros and not self.deadline_reached():
            debt_before = self.debt()
            if debt_before["complete"]:
                break
            views = self.ranked_valid_macros(debt_before)
            if not views:
                self.trace.append({
                    "macro": self.macro_index,
                    "accepted": "",
                    "reason": "no_valid_closed_macro",
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
            else:
                no_progress = 0
            if no_progress >= MAX_NO_PROGRESS_MACROS or stale_best >= MAX_STALE_BEST_MACROS:
                self.trace.append({
                    "macro": self.macro_index,
                    "accepted": "",
                    "reason": "no_progress_guard",
                    "no_progress": no_progress,
                    "stale_best": stale_best,
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

    def ranked_valid_macros(self, debt: dict[str, Any]) -> list[MacroView]:
        views: list[MacroView] = []
        rejected: list[dict[str, Any]] = []
        progress_before = self.main_progress()
        satisfied_before = self.current_satisfied_nos()
        for candidate, reason in self.generate_macros(debt):
            if len(views) >= self.max_candidates_per_step:
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
            satisfied_damage = probe.current_unsatisfied_nos() & satisfied_before
            if satisfied_damage:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": [f"satisfied_damage:{','.join(sorted(satisfied_damage)[:8])}"],
                    })
                continue
            progress_after = probe.main_progress()
            if progress_after >= progress_before:
                if len(rejected) < 20:
                    rejected.append({
                        "candidate_id": candidate.candidate_id,
                        "reason": reason,
                        "violations": ["no_main_progress"],
                    })
                continue
            score = self.score_candidate(candidate, validation, progress_after, reason)
            views.append(MacroView(candidate, validation, score, reason, progress_after))
        views.sort(key=lambda item: item.score)
        if not views and rejected:
            self.trace.append({
                "macro": self.macro_index,
                "accepted": "",
                "reason": "candidate_rejections",
                "debt": debt,
                "rejected": rejected[:20],
            })
        return views

    def generate_macros(self, debt: dict[str, Any]) -> Iterable[tuple[physical.HookCandidate, str]]:
        emitted: set[str] = set()
        for line in self.active_source_lines(debt):
            for candidate, reason in self.target_rebuild_candidates(line, debt):
                if candidate.candidate_id in emitted:
                    continue
                emitted.add(candidate.candidate_id)
                yield candidate, reason
            for move in self.prefix_options_for_line(line):
                if not move:
                    continue
                for candidate, reason in self.service_sweep_candidates(line, move):
                    if candidate.candidate_id in emitted:
                        continue
                    emitted.add(candidate.candidate_id)
                    yield candidate, reason

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

    def prefix_options_for_line(self, line: str) -> list[tuple[str, ...]]:
        ordered = physical.line_access_order(self.cars, line)
        if not ordered:
            return []
        pending = set(self.debt()["active_unsatisfied_nos"])
        options: list[tuple[str, ...]] = []
        prefix: list[str] = []
        target_changes = 0
        last_target = ""
        for no in ordered:
            car = self.by_no().get(no)
            if not car:
                break
            trial_cars = [self.by_no()[item] for item in (*prefix, no) if item in self.by_no()]
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
            if len(prefix) >= physical.PULL_LIMIT_EQUIVALENT:
                break
        # Also allow clearing a front blocker segment before the first debt.
        first_pending_index = next((idx for idx, no in enumerate(ordered) if no in pending), -1)
        if first_pending_index > 0:
            blockers = tuple(ordered[:first_pending_index])
            if blockers and blockers not in options:
                cars = [self.by_no()[no] for no in blockers if no in self.by_no()]
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
        candidate = self.build_service_sweep(source_line, move, allow_cache=True)
        if candidate:
            yield candidate, "closed_service_sweep_cache"
        candidate = self.build_service_sweep(source_line, move, allow_cache=True, include_get_route_blockers=True)
        if candidate:
            yield candidate, "closed_service_sweep_cache_get_unblock"

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
            reason=f"stage4_closed_macro:target_rebuild:{source_line}->{target_line}",
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

    def build_service_sweep(
        self,
        source_line: str,
        move: tuple[str, ...],
        *,
        allow_cache: bool,
        include_get_route_blockers: bool = False,
    ) -> physical.HookCandidate | None:
        by_no = self.by_no()
        if any(no not in by_no for no in move):
            return None
        batch = [by_no[no] for no in move]
        planning_cars = [clone_car(car) for car in self.cars]
        blocker_groups = (
            self.get_route_blocker_groups(source_line, move)
            if include_get_route_blockers
            else []
        )
        blocker_nos = [no for _line, nos in blocker_groups for no in nos]
        if blocker_nos:
            batch = [by_no[no] for no in (*blocker_nos, *move)]
            if physical.pull_equivalent(batch) > physical.PULL_LIMIT_EQUIVALENT:
                return None
        steps: list[physical.PlanStep] = []
        carried: list[str] = []
        for blocker_line, nos in blocker_groups:
            steps.append(physical.plan_step("Get", blocker_line, nos))
            physical.apply_physical_get_order(planning_cars, blocker_line, nos)
            carried.extend(nos)

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
            tail_target = self.target_by_no.get(carried[-1], "")
            target_line = tail_target if tail_target else source_line
            start = len(carried) - 1
            while start > 0 and self.target_by_no.get(carried[start - 1], "") == tail_target:
                start -= 1
            group = tuple(carried[start:])
            if not group:
                return None
            put_line = ""
            note = "target"
            prefer_cache = (
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
            if not put_line and self.target_put_allowed(
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
                progressed.extend(no for no in group if self.target_by_no.get(no) == put_line)
            del carried[start:]
            if cache_puts > 2:
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
        active_targets = {self.target_by_no.get(no, "") for no in self.debt()["active_unsatisfied_nos"]}
        for line in (*SAFE_CACHE_LINES, *CORRIDOR_CACHE_LINES):
            if line == source_line or line in active_targets:
                continue
            if line not in physical.TRACK_SPECS:
                continue
            if line in EXCLUDED_OPERATION_LINES or line in OUT_OF_SCOPE_TARGET_LINES:
                continue
            group_cars = [self.by_no()[no] for no in move if no in self.by_no()]
            spec = physical.TRACK_SPECS[line]
            load = sum(physical.car_length(car) for car in planning_cars if car["Line"] == line and physical.car_no(car) not in set(move))
            if load + sum(physical.car_length(car) for car in group_cars) > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                continue
            return line
        return ""

    def target_ready(self, target: str, cars: list[dict[str, Any]] | None = None) -> bool:
        cars = cars or self.cars
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
        key = (self.state_signature(), candidate.candidate_id)
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
        clone.started_at = self.started_at
        clone.deadline = self.deadline
        clone.max_macros = self.max_macros
        clone.max_candidates_per_step = self.max_candidates_per_step
        clone.macro_index = self.macro_index
        clone.operation_index = self.operation_index
        clone.operations = []
        clone.trace = []
        clone.validation_cache = self.validation_cache
        clone.target_by_no = self.target_by_no
        clone.target_reason_by_no = self.target_reason_by_no
        clone.active_nos = set(self.active_nos)
        clone.out_of_scope_nos = set(self.out_of_scope_nos)
        clone.excluded_line_nos = set(self.excluded_line_nos)
        clone.initial_unsatisfied_nos = set(self.initial_unsatisfied_nos)
        clone.protected_satisfied_nos = set(self.protected_satisfied_nos)
        clone.seen_signatures = self.seen_signatures
        clone.best_progress = self.best_progress
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
            progress_after,
            reason_rank,
            not_ready_puts,
            cache_puts,
            -target_put_cars,
            len(physical.candidate_plan_steps(candidate)),
            hot_cost,
            route_cost,
            source_rank,
            candidate.candidate_id,
        )

    def debt(self) -> dict[str, Any]:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }
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
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }
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
        by_no = {physical.car_no(car): car for car in cars}
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(cars, self.depot_assignment)
        } & self.active_nos
        blocked: set[str] = set()
        for line in {car["Line"] for car in cars if car["Line"]}:
            seen_blocker = False
            for no in physical.line_access_order(cars, line):
                if no in unsatisfied:
                    if seen_blocker:
                        blocked.add(no)
                    continue
                car = by_no.get(no)
                if not car:
                    continue
                suffix_has_debt = any(item in unsatisfied for item in physical.line_access_order(cars, line)[physical.line_access_order(cars, line).index(no) + 1 :])
                if suffix_has_debt and not physical.car_is_satisfied(car, self.depot_assignment, cars):
                    seen_blocker = True
                elif suffix_has_debt and no not in unsatisfied:
                    seen_blocker = True
        return blocked

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
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }
        return sorted(unsatisfied & self.protected_satisfied_nos)

    def current_unsatisfied_nos(self) -> set[str]:
        return {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }

    def current_satisfied_nos(self) -> set[str]:
        all_nos = {physical.car_no(car) for car in self.cars}
        return all_nos - self.current_unsatisfied_nos()

    def out_of_scope_current_unsatisfied(self) -> set[str]:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        }
        return unsatisfied & self.out_of_scope_nos

    def line_has_active_debt(self, line: str) -> bool:
        unsatisfied = {
            physical.car_no(car)
            for car in physical.unsatisfied_cars(self.cars, self.depot_assignment)
        } & self.active_nos
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
        return {physical.car_no(car): car for car in self.cars}

    def state_signature(self) -> tuple[Any, ...]:
        return (
            self.loco.line,
            tuple(
                (physical.car_no(car), car["Line"], int(car.get("Position") or 0), bool(car.get("_Weighed")))
                for car in sorted(self.cars, key=lambda item: physical.car_no(item))
            ),
        )

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
        if self.trace and self.trace[-1].get("reason"):
            reasons.append(str(self.trace[-1]["reason"]))
        return reasons

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
        return error_summary(case_id, f"stage3_summary_missing:{summary_path}")
    stage3_summary = read_json(summary_path)
    if stage3_summary.get("status") != "complete" and not args.include_stage3_partial:
        return error_summary(case_id, f"stage3_not_complete:{stage3_summary.get('status')}")
    missing = [
        str(item)
        for item in (stage3_request_path, stage3_response_path, stage3_combined_path)
        if not item.exists()
    ]
    if missing:
        return error_summary(case_id, "stage3_artifact_missing:" + ",".join(missing))

    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(path)
    solver = Stage4Solver(
        case_id,
        request,
        depot_assignment,
        read_json(stage3_request_path),
        read_json(stage3_response_path),
        read_json(stage3_combined_path),
        time_budget_seconds=args.time_budget_seconds,
        max_macros=args.max_macros,
        max_candidates_per_step=args.max_candidates_per_step,
    )
    result = solver.solve()
    write_case_outputs(out_dir, case_id, result)
    return result["summary"]


def error_summary(case_id: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": "partial",
        "operations": 0,
        "business_hooks": 0,
        "hook_count_definition": "operation_rows_get_put_weigh",
        "active_count": 0,
        "blocking_reasons": [reason],
        "replay_physical_ok": False,
        "combined_replay_physical_ok": False,
        "final_unsatisfied_count": 0,
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
        "feasible_unproved": sum(1 for item in summaries if item.get("status") == "feasible_unproved"),
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
