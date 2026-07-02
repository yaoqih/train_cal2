#!/usr/bin/env python3
from __future__ import annotations

import csv
import heapq
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .domain import ContractFamily


LOCO_LENGTH_M = 15.0
PULL_LIMIT_EQUIVALENT = 20
LINE_LENGTH_TOLERANCE_M = 0.5
DEPOT_LINES = {"修1库内", "修2库内", "修3库内", "修4库内"}
DEPOT_OUTSIDE_LINES = {"修1库外", "修2库外", "修3库外", "修4库外"}
DEPOT_TARGET_LINES = DEPOT_LINES | DEPOT_OUTSIDE_LINES
DEPOT_INNER_BLOCKERS = {
    f"修{index}库内": f"修{index}库外"
    for index in range(1, 5)
}
# Internal line keys follow the existing runtime model. LINE_FULL_NAMES records
# the full table names used by operations for diagnostics and new rules.
LINE_FULL_NAMES = {
    "机北1": "机走北1线",
    "存1线": "存1线",
    "存2线": "存2线",
    "存3线": "存3线",
    "存4线": "存4线",
    "存4南": "存4线南",
    "存5线北": "存5线北",
    "存5线南": "存5线南",
    "机北2": "机走北2线",
    "机库线": "机库线",
    "调梁线北": "调梁线北",
    "调梁棚": "调梁棚",
    "机走北": "机走北",
    "机走棚": "机走棚",
    "预修线": "预修线",
    "洗油北": "洗罐油漆北",
    "机南": "机走线南",
    "洗罐线北": "洗罐线北",
    "洗罐站": "洗罐站",
    "抛丸线": "抛丸线",
    "油漆线": "油漆线",
    "联7": "联7线",
    "卸轮线": "卸轮线",
    "修1库外": "修1库外",
    "修1库内": "修1库内",
    "修2库外": "修2库外",
    "修2库内": "修2库内",
    "修3库外": "修3库外",
    "修3库内": "修3库内",
    "修4库外": "修4库外",
    "修4库内": "修4库内",
}
# Strategy-layer serial gate map.  It describes which occupied lines tend to
# block downstream work and is used by serial/resource policies; hard physical
# reachability is governed by TrackGraph + route occupancy + reversal rules.
SERIAL_LINE_BLOCKERS: dict[str, tuple[str, ...]] = {
    **{inner_line: (outer_line,) for inner_line, outer_line in DEPOT_INNER_BLOCKERS.items()},
    "机南": ("机走棚",),
    "机走棚": ("机走北",),
    "洗油北": ("机走棚",),
    "洗罐线北": ("洗油北",),
    "油漆线": ("洗油北",),
    "洗罐站": ("洗罐线北",),
    "调梁棚": ("调梁线北",),
    "存4南": ("存4线", "存3线"),
    "存5线南": ("存5线北",),
    "存1线": ("机北1",),
    "机北2": ("机北1",),
}
REMOTE_INTERACTION_LINES = DEPOT_TARGET_LINES | {"卸轮线"}
REMOTE_PROFILE_FRONT_ONLY = "FRONT_ONLY"
REMOTE_PROFILE_REMOTE_ONLY = "REMOTE_ONLY"
REMOTE_PROFILE_FRONT_TO_REMOTE = "FRONT_TO_REMOTE"
REMOTE_PROFILE_REMOTE_TO_FRONT = "REMOTE_TO_FRONT"
REMOTE_PROFILE_MIXED = "MIXED_REMOTE_FRONT"
REMOTE_PROFILE_NONE = "NONE"
RUNNING_LINES = {"联6", "联7"} | {f"渡{index}" for index in range(1, 14)}
WEIGH_LINE = "机库线"
STAGING_CANDIDATE_KINDS = {
    "blocker_relocation",
    "capacity_release_to_staging",
    "vnext_remote_prefix_lease_open",
    "same_line_stage_out",
    "spot_release_to_staging",
}
ENABLE_SPOTTING_WINDOW_VALIDATION = True
SPOTTING_LINE_TOTAL_POSITIONS = {
    "调梁棚": 11,
    "洗罐站": 7,
    "油漆线": 9,
    "抛丸线": 3,
}
_normalize_line_cache: dict[Any, str] = {}
_access_order_cache: dict[tuple[int, int, str, str, str, frozenset[str], frozenset[str]], list[dict[str, Any]]] = {}
_unsatisfied_cache: dict[tuple[int, int], tuple[dict[str, Any], ...]] = {}
_line_loads_cache: dict[int, Counter[str]] = {}


def clear_access_order_cache() -> None:
    _access_order_cache.clear()
    _unsatisfied_cache.clear()
    _line_loads_cache.clear()


def case_id_from_path(path: Path) -> str:
    match = re.search(r"(\d{4}[ZWzw])", path.name)
    if not match:
        raise ValueError(f"Cannot infer case id from {path}")
    return match.group(1).upper()


def normalize_line(value: Any) -> str:
    try:
        cached = _normalize_line_cache.get(value)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    text = str(value or "").strip()
    if not text:
        return ""
    aliases = {
        "机走北1线": "机北1",
        "机走北2线": "机北2",
        "机走北": "机走北",
        "机北3": "机走北",
        "机走线南": "机南",
        "机南": "机南",
        "洗罐油漆北": "洗油北",
        "洗油北": "洗油北",
        "洗罐线北": "洗罐线北",
        "洗北": "洗罐线北",
        "洗罐站": "洗罐站",
        "洗南": "洗罐站",
        "调梁线北": "调梁线北",
        "调北": "调梁线北",
        "调梁棚": "调梁棚",
        "调棚": "调梁棚",
        "存4线": "存4线",
        "存4北": "存4线",
        "存4线南": "存4南",
        "存4南": "存4南",
        "存5北": "存5线北",
        "存5南": "存5线南",
        "存5线北": "存5线北",
        "存5线南": "存5线南",
        "联7线": "联7",
        "机库": "机库线",
        "库": "机库线",
        "注意库": "机库线",
        "修1": "修1库内",
        "修1外": "修1库外",
        "修2": "修2库内",
        "修2外": "修2库外",
        "修3": "修3库内",
        "修3外": "修3库外",
        "修4": "修4库内",
        "修4外": "修4库外",
        "预修": "预修线",
        "联6线": "联6",
        "存1": "存1线",
        "存2": "存2线",
        "注意存2": "存2线",
        "存2叉": "存2线",
        "存3": "存3线",
        "存4": "存4线",
        "注意存4": "存4线",
        "存5": "存5线",
        "注意存5": "存5线",
        "调": "调梁棚",
        "机": "机库线",
        "注意机": "机库线",
        "机走": "机走棚",
        "机棚": "机走棚",
        "洗": "洗罐站",
        "油": "油漆线",
        "抛": "抛丸线",
        "轮": "卸轮线",
    }
    if text in aliases:
        normalized = aliases[text]
    else:
        normalized = text.replace("線", "线")
    try:
        _normalize_line_cache[value] = normalized
    except TypeError:
        pass
    return normalized


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@dataclass(frozen=True)
class TrackSpec:
    line: str
    length_m: float
    track_type: str


TRACK_SPECS: dict[str, TrackSpec] = {
    "机北1": TrackSpec("机北1", 81.4, "temporary"),
    "存1线": TrackSpec("存1线", 113.0, "storage"),
    "存2线": TrackSpec("存2线", 239.2, "storage"),
    "存3线": TrackSpec("存3线", 258.5, "storage"),
    "存4线": TrackSpec("存4线", 317.8, "storage"),
    "存4南": TrackSpec("存4南", 154.5, "temporary"),
    "存5线北": TrackSpec("存5线北", 367.0, "storage"),
    "存5线南": TrackSpec("存5线南", 156.0, "storage"),
    "机北2": TrackSpec("机北2", 55.7, "temporary"),
    "机库线": TrackSpec("机库线", 71.6, "special"),
    "调梁线北": TrackSpec("调梁线北", 70.1, "storage"),
    "调梁棚": TrackSpec("调梁棚", 174.3, "operation"),
    "机走北": TrackSpec("机走北", 69.1, "storage"),
    "机走棚": TrackSpec("机走棚", 111.1, "operation"),
    "预修线": TrackSpec("预修线", 208.5, "operation"),
    "洗油北": TrackSpec("洗油北", 62.9, "temporary"),
    "机南": TrackSpec("机南", 90.1, "temporary"),
    "洗罐线北": TrackSpec("洗罐线北", 100.0, "storage"),
    "洗罐站": TrackSpec("洗罐站", 88.7, "operation"),
    "抛丸线": TrackSpec("抛丸线", 42.3, "operation"),
    "油漆线": TrackSpec("油漆线", 109.0, "operation"),
    "卸轮线": TrackSpec("卸轮线", 47.3, "operation"),
    "修1库外": TrackSpec("修1库外", 49.3, "storage"),
    "修1库内": TrackSpec("修1库内", 151.7, "operation"),
    "修2库外": TrackSpec("修2库外", 49.3, "storage"),
    "修2库内": TrackSpec("修2库内", 151.7, "operation"),
    "修3库外": TrackSpec("修3库外", 49.3, "storage"),
    "修3库内": TrackSpec("修3库内", 151.7, "operation"),
    "修4库外": TrackSpec("修4库外", 49.3, "storage"),
    "修4库内": TrackSpec("修4库内", 151.7, "operation"),
}

LINE_GRAPH_EDGES: tuple[tuple[str, str], ...] = (
    ("修4库内", "修4库外"),
    ("修3库内", "修3库外"),
    ("修2库内", "修2库外"),
    ("修1库内", "修1库外"),
    ("修4库外", "渡13"),
    ("修3库外", "渡13"),
    ("修2库外", "渡12"),
    ("修1库外", "渡11"),
    ("卸轮线", "渡11"),
    ("渡13", "渡12"),
    ("渡12", "联7"),
    ("渡11", "联7"),
    ("抛丸线", "渡10"),
    ("联7", "渡10"),
    ("渡10", "渡9"),
    ("渡10", "机南"),
    ("渡9", "渡8"),
    ("渡8", "存4南"),
    ("渡8", "存5线南"),
    ("渡9", "预修线"),
    ("洗罐站", "洗罐线北"),
    ("洗罐线北", "洗油北"),
    ("油漆线", "洗油北"),
    ("机南", "机走棚"),
    ("洗油北", "机走棚"),
    ("调梁棚", "调梁线北"),
    ("机库线", "渡4"),
    ("预修线", "存2线"),
    ("预修线", "渡7"),
    ("调梁线北", "渡4"),
    ("存5线南", "存5线北"),
    ("存4南", "存4线"),
    ("存4南", "存3线"),
    ("渡7", "存1线"),
    ("渡7", "渡6"),
    ("机走北", "渡5"),
    ("机走棚", "机走北"),
    ("渡6", "渡5"),
    ("渡4", "机北2"),
    ("存5线北", "渡1"),
    ("存4线", "渡1"),
    ("存3线", "渡3"),
    ("存2线", "渡3"),
    ("存1线", "机北1"),
    ("机北2", "机北1"),
    ("渡5", "机北2"),
    ("机北1", "渡2"),
    ("渡3", "渡2"),
    ("渡1", "联6"),
    ("渡2", "联6"),
)

OCCUPIED_LINE_APPROACH_LINES: dict[str, tuple[str, ...]] = {
    "修4库外": ("渡13",),
    "修3库外": ("渡13",),
    "修2库外": ("渡12",),
    "修1库外": ("渡11",),
    "卸轮线": ("渡11",),
    "抛丸线": ("渡10",),
    "存5线南": ("存5线北",),
    "存5线北": ("渡1",),
    "存4南": ("存4线", "存3线"),
    "存4线": ("渡1",),
    "存3线": ("渡3",),
    "存2线": ("渡3",),
    "存1线": ("机北1",),
    "机北1": ("渡2",),
    "机北2": ("机北1",),
    "机走北": ("渡5",),
    "调梁线北": ("渡4",),
    "机库线": ("渡4",),
    "机走棚": ("机走北",),
    "预修线": ("渡7", "存2线"),
    "机南": ("机走棚",),
    "洗油北": ("机走棚",),
    "洗罐线北": ("洗油北",),
}

REVERSAL_RULES_IGNORE_BLOCKER_LENGTH = (
    (("调梁线北", "渡4", "机库线"), (("机北2", 41.5), ("机北1", 97.2))),
    (("渡5", "机北2", "渡4"), (("机北2", 0.0), ("机北1", 55.7))),
    (("机北2", "机北1", "存1线"), (("机北1", 0.0),)),
    (("渡6", "渡5", "机走北"), (("机北2", 40.6), ("机北1", 96.3))),
)

REVERSAL_RULES_WITH_BLOCKER_LENGTH = (
    (("存2线", "预修线", "渡7"), (("预修线", 208.5),)),
    (("存1线", "渡7", "渡6"), (("预修线", 253.9),)),
    (("存4线", "存4南", "存3线"), (("存4南", 154.5),)),
)


@dataclass(frozen=True)
class DepotSlot:
    line: str
    position: int
    locked: bool = False


@dataclass(frozen=True)
class DepotAssignment:
    slots: dict[str, DepotSlot]
    failures: dict[str, str]
    capacities: dict[str, int] = field(default_factory=dict)


def current_depot_assignment(base: DepotAssignment, cars: list[dict[str, Any]]) -> DepotAssignment:
    active_nos = {car_no(car) for car in cars if has_depot_target(car)}
    return DepotAssignment(
        slots={no: slot for no, slot in base.slots.items() if no in active_nos},
        failures={no: reason for no, reason in base.failures.items() if no in active_nos},
        capacities=dict(base.capacities),
    )


@dataclass(frozen=True)
class PlanStep:
    action: str
    line: str
    move_car_nos: tuple[str, ...]
    planned_positions: dict[str, int] = field(default_factory=dict)


def plan_step(
    action: str,
    line: str,
    move_nos: tuple[str, ...],
    planned_positions: dict[str, int] | None = None,
) -> PlanStep:
    return PlanStep(action, line, move_nos, planned_positions or {})


@dataclass(frozen=True)
class HookCandidate:
    case_id: str
    hook_index: int
    candidate_id: str
    source_line: str
    target_line: str
    move_car_nos: tuple[str, ...]
    action_family: str
    train_length_m: float
    pull_equivalent_count: int
    has_weigh: bool
    planned_positions: dict[str, int]
    generation_reason: str
    candidate_kind: str = "target_move"
    plan_steps: tuple[PlanStep, ...] = ()


@dataclass(frozen=True)
class PhysicalValidation:
    accepted: bool
    reasons: tuple[str, ...]
    get_path: tuple[str, ...]
    weigh_path: tuple[str, ...]
    put_path: tuple[str, ...]
    operation_paths: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class PhysicalAccessContext:
    graph: Any | None = None
    loco_location: LocoLocation | None = None










@dataclass(frozen=True)
class OperationTraceRow:
    case_id: str
    hook_index: int
    operation_index: int
    candidate_id: str
    line: str
    action: str
    move_cars: str
    train_cars: str
    passby_path: str





















class TrackGraph:
    def __init__(self) -> None:
        self._adjacency: dict[str, list[str]] = defaultdict(list)
        self._route_cache: dict[tuple[str, str], list[str]] = {}
        self._occupied_route_cache: dict[tuple[str, str, tuple[str, ...], tuple[str, ...]], list[str]] = {}
        for left, right in LINE_GRAPH_EDGES:
            self._add_edge(left, right)

    def _add_edge(self, left: str, right: str) -> None:
        left = normalize_line(left)
        right = normalize_line(right)
        if right not in self._adjacency[left]:
            self._adjacency[left].append(right)
        if left not in self._adjacency[right]:
            self._adjacency[right].append(left)

    def route(self, source_line: str, target_line: str) -> list[str]:
        return self._route(source_line, target_line, occupied_lines=set())

    def route_avoiding_occupied(
        self,
        source_line: str,
        target_line: str,
        occupied_lines: set[str],
        target_approach_lines: set[str] | None = None,
    ) -> list[str]:
        source = normalize_line(source_line)
        target = normalize_line(target_line)
        effective_occupied = {normalize_line(line) for line in occupied_lines if normalize_line(line)}
        if source == target and source in self._adjacency:
            return [source]
        approach_lines = frozenset(normalize_line(line) for line in (target_approach_lines or set()) if normalize_line(line))
        cache_key = (source, target, tuple(sorted(effective_occupied)), tuple(sorted(approach_lines)))
        cached = self._occupied_route_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        route = self._route(source, target, occupied_lines=effective_occupied, target_approach_lines=set(approach_lines))
        self._occupied_route_cache[cache_key] = list(route)
        return route

    def _route(
        self,
        source_line: str,
        target_line: str,
        occupied_lines: set[str],
        target_approach_lines: set[str] | None = None,
    ) -> list[str]:
        source = normalize_line(source_line)
        target = normalize_line(target_line)
        target_approach_lines = target_approach_lines or set()
        if not occupied_lines and not target_approach_lines:
            cache_key = (source, target)
            if cache_key in self._route_cache:
                return list(self._route_cache[cache_key])
        if source == target and source in self._adjacency:
            if not occupied_lines and not target_approach_lines:
                self._route_cache[(source, target)] = [source]
            return [source]
        if source not in self._adjacency or target not in self._adjacency:
            if not occupied_lines and not target_approach_lines:
                self._route_cache[(source, target)] = []
            return []

        queue: list[tuple[int, int, str, list[str]]] = [(0, 0, source, [source])]
        best: dict[str, int] = {source: 0}
        sequence = 1
        while queue:
            distance, _sequence, node, path = heapq.heappop(queue)
            if node == target:
                if not occupied_lines and not target_approach_lines:
                    self._route_cache[(source, target)] = path
                return path
            if distance > best.get(node, 10**9):
                continue
            for next_node in self._adjacency[node]:
                if self._occupied_edge_blocked(
                    node,
                    next_node,
                    source=source,
                    target=target,
                    occupied_lines=occupied_lines,
                ):
                    continue
                if next_node == target and target_approach_lines and node not in target_approach_lines:
                    continue
                next_distance = distance + 1
                if next_distance < best.get(next_node, 10**9):
                    best[next_node] = next_distance
                    heapq.heappush(queue, (next_distance, sequence, next_node, [*path, next_node]))
                    sequence += 1
        if not occupied_lines and not target_approach_lines:
            self._route_cache[(source, target)] = []
        return []

    def _occupied_edge_blocked(
        self,
        node: str,
        next_node: str,
        *,
        source: str,
        target: str,
        occupied_lines: set[str],
    ) -> bool:
        route_endpoints = {source, target}
        if next_node in occupied_lines and next_node not in route_endpoints:
            return True
        if node in occupied_lines and node not in route_endpoints:
            return True
        return False


@dataclass(frozen=True)
class LocoLocation:
    line: str


def initial_loco_location(loco: dict[str, Any]) -> LocoLocation:
    line = normalize_line(loco.get("Line"))
    return LocoLocation(line=line)


def operation_approach_lines(line: str) -> set[str]:
    normalized = normalize_line(line)
    return {normalize_line(node) for node in OCCUPIED_LINE_APPROACH_LINES.get(normalized, ())}


def line_has_stationary_cars(
    line: str,
    cars: list[dict[str, Any]],
    moving_nos: set[str],
) -> bool:
    normalized = normalize_line(line)
    return any(car["Line"] == normalized and car_no(car) not in moving_nos for car in cars)


def route_approach_lines_for_get(line: str) -> set[str]:
    return operation_approach_lines(line)


def route_approach_lines_for_put(
    line: str,
    cars: list[dict[str, Any]],
    moving_nos: set[str],
) -> set[str]:
    if not line_has_stationary_cars(line, cars, moving_nos):
        return set()
    return operation_approach_lines(line)


def route_end_location(path: tuple[str, ...] | list[str], fallback_line: str) -> LocoLocation:
    line = normalize_line(fallback_line)
    if path:
        line = normalize_line(path[-1])
    return LocoLocation(line=line)


def operation_stand_location(path: tuple[str, ...] | list[str], operation_line: str) -> LocoLocation:
    del path
    line = normalize_line(operation_line)
    return LocoLocation(line=line)


def route_with_line_prefix(line: str, path: list[str]) -> list[str]:
    normalized_line = normalize_line(line)
    if not normalized_line or not path or path[0] == normalized_line:
        return path
    return [normalized_line, *path]


def route_for_output(path: tuple[str, ...] | list[str]) -> list[str]:
    output: list[str] = []
    for item in [normalize_line(item) for item in path if normalize_line(item)]:
        if not output or output[-1] != item:
            output.append(item)
    return output


def line_full_name(line: str) -> str:
    normalized = normalize_line(line)
    return LINE_FULL_NAMES.get(normalized, normalized)


def candidate_plan_steps(candidate: HookCandidate) -> tuple[PlanStep, ...]:
    if candidate.plan_steps:
        return candidate.plan_steps
    steps = [PlanStep("Get", candidate.source_line, candidate.move_car_nos)]
    if candidate.has_weigh:
        steps.append(PlanStep("Weigh", WEIGH_LINE, (candidate.move_car_nos[-1],)))
    steps.append(PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions))
    return tuple(steps)


def candidate_final_line(candidate: HookCandidate) -> str:
    for step in reversed(candidate_plan_steps(candidate)):
        if step.action == "Put":
            return step.line
    return candidate.target_line



def planlet_line_sequence(candidate: HookCandidate) -> tuple[str, ...]:
    return tuple(step.line for step in candidate_plan_steps(candidate) if step.action in {"Get", "Put"})









def candidate_remote_profile(candidate: HookCandidate) -> str:
    return remote_profile_for_lines(planlet_line_sequence(candidate))


def remote_profile_for_lines(lines: tuple[str, ...]) -> str:
    if not lines:
        return REMOTE_PROFILE_NONE
    remote_flags = [line in REMOTE_INTERACTION_LINES for line in lines]
    if not any(remote_flags):
        return REMOTE_PROFILE_FRONT_ONLY
    if all(remote_flags):
        return REMOTE_PROFILE_REMOTE_ONLY
    transition_count = sum(1 for left, right in zip(remote_flags, remote_flags[1:]) if left != right)
    if transition_count > 1:
        return REMOTE_PROFILE_MIXED
    return REMOTE_PROFILE_FRONT_TO_REMOTE if remote_flags[-1] else REMOTE_PROFILE_REMOTE_TO_FRONT





















def operation_remote_business_transition_count(operations: list[OperationTraceRow]) -> int:
    business_rows = sorted(
        (row for row in operations if row.action in {"Get", "Put"}),
        key=lambda row: (row.hook_index, row.operation_index),
    )
    count = 0
    previous_remote: bool | None = None
    for row in business_rows:
        current_remote = row.line in REMOTE_INTERACTION_LINES
        if previous_remote is not None and current_remote != previous_remote:
            count += 1
        previous_remote = current_remote
    return count




def planlet_candidate_id(
    *,
    case_id: str,
    hook_index: int,
    candidate_kind: str,
    steps: tuple[PlanStep, ...],
) -> str:
    step_key = ";".join(
        f"{step.action}:{step.line}:{','.join(step.move_car_nos)}"
        for step in steps
        if step.action in {"Get", "Put"}
    )
    return f"{case_id}:P10:{hook_index}:{candidate_kind}:{step_key}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_case(path: Path) -> tuple[str, dict[str, Any], list[dict[str, Any]], DepotAssignment, LocoLocation]:
    clear_access_order_cache()
    payload = read_json(path)
    input_ok, errors = validate_input(payload)
    if not input_ok:
        raise ValueError("|".join(errors))
    case_id = case_id_from_path(path)
    cars = [normalized_car(car) for car in payload.get("StartStatus") or []]
    capacities = terminal_capacity_by_line(payload)
    depot_assignment = build_depot_assignment([dict(car) for car in cars], capacities)
    loco = initial_loco_location(payload.get("locoNode") or {})
    return case_id, payload, cars, depot_assignment, loco




def target_lines(car: dict[str, Any]) -> list[str]:
    return car.get("TargetLines") or []


def normalized_car(car: dict[str, Any]) -> dict[str, Any]:
    item = dict(car)
    item["_No"] = str(item.get("No") or "")
    item["Line"] = normalize_line(item.get("Line"))
    item["TargetLines"] = [normalize_line(line) for line in item.get("TargetLines") or []]
    item["_TargetLineSet"] = set(item["TargetLines"])
    item["_ForcePositions"] = tuple(int(value) for value in item.get("ForceTargetPosition") or [] if int(value) > 0)
    item["Position"] = int(item.get("Position") or 0)
    item["Length"] = float(item.get("Length") or 14.3)
    item["IsHeavy"] = bool(item.get("IsHeavy"))
    item["IsWeigh"] = bool(item.get("IsWeigh"))
    item["_Weighed"] = bool(item.get("_Weighed"))
    item["IsClosedDoor"] = bool(item.get("IsClosedDoor"))
    return item


def car_no(car: dict[str, Any]) -> str:
    try:
        return car["_No"]
    except KeyError:
        return str(car.get("No") or "")


def car_length(car: dict[str, Any]) -> float:
    return float(car.get("Length") or 14.3)


def force_positions(car: dict[str, Any]) -> tuple[int, ...]:
    cached = car.get("_ForcePositions")
    if cached is not None:
        return tuple(cached)
    return tuple(int(item) for item in car.get("ForceTargetPosition") or [] if int(item) > 0)


def depot_targets(car: dict[str, Any]) -> tuple[str, ...]:
    return tuple(line for line in target_lines(car) if line in DEPOT_LINES)


def has_depot_target(car: dict[str, Any]) -> bool:
    return bool(depot_targets(car))


def repair_process(car: dict[str, Any]) -> str:
    return str(car.get("RepairProcess") or "")


def terminal_capacity_by_line(payload: dict[str, Any]) -> dict[str, int]:
    capacities: dict[str, int] = {}
    for item in payload.get("TerminalLines") or []:
        line = normalize_line(item.get("Line"))
        if line in DEPOT_LINES:
            capacities[line] = 7 if bool(item.get("IsInspectionMode")) else 5
    for line in DEPOT_LINES:
        capacities.setdefault(line, 5)
    return capacities


def depot_line_capacity(
    depot_assignment: DepotAssignment,
    line: str,
    *,
    fallback_position: int = 0,
) -> int:
    """Return the configured depot capacity, falling back only for legacy empty assignments."""
    configured = getattr(depot_assignment, "capacities", {}).get(line)
    if configured:
        return int(configured)
    assigned_max = max(
        [int(slot.position) for slot in depot_assignment.slots.values() if slot.line == line]
        or [0]
    )
    return max(assigned_max, int(fallback_position or 0), 5)


def line_number(line: str) -> int:
    for index in range(1, 5):
        if line == f"修{index}库内":
            return index
    return 0


def slot_allowed_for_car(car: dict[str, Any], line: str, position: int, capacity: int) -> bool:
    if position < 1 or position > capacity:
        return False
    forced = force_positions(car)
    if forced and position not in forced:
        return False
    if car_length(car) >= 17.6 and line_number(line) not in {3, 4}:
        return False
    if repair_process(car).startswith("厂") and position not in {4, 5}:
        return False
    return True


def depot_actual_position_allowed(car: dict[str, Any], line: str, position: int, capacity: int) -> bool:
    return slot_allowed_for_car(car, line, position, capacity)


def depot_section_repair_position_allowed(
    car: dict[str, Any],
    line: str,
    position: int,
    cars: list[dict[str, Any]],
) -> bool:
    if line not in DEPOT_LINES or not repair_process(car).startswith("段"):
        return True
    factory_positions = [
        int(item.get("Position") or 0)
        for item in cars
        if item["Line"] == line and repair_process(item).startswith("厂")
    ]
    if not factory_positions:
        return True
    return position <= min(factory_positions)


def build_depot_assignment(cars: list[dict[str, Any]], capacities: dict[str, int]) -> DepotAssignment:
    slots: dict[str, DepotSlot] = {}
    failures: dict[str, str] = {}
    occupied: dict[str, set[int]] = defaultdict(set)

    depot_target_cars = [car for car in cars if has_depot_target(car)]
    for car in sorted(depot_target_cars, key=lambda item: (item["Line"], item["Position"], car_no(item))):
        line = car["Line"]
        position = int(car.get("Position") or 0)
        if line not in depot_targets(car) or line not in DEPOT_LINES:
            continue
        if not slot_allowed_for_car(car, line, position, capacities[line]):
            continue
        no = car_no(car)
        slots[no] = DepotSlot(line=line, position=position, locked=True)
        occupied[line].add(position)

    remaining = [car for car in depot_target_cars if car_no(car) not in slots]

    car_lookup = {car_no(car): car for car in remaining}
    candidates_by_no: dict[str, list[tuple[str, int]]] = {}
    for car in remaining:
        no = car_no(car)
        allowed_lines = [line for line in depot_targets(car) if line in capacities]
        if car_length(car) < 17.6:
            allowed_lines.sort(key=lambda line: (line_number(line) not in {1, 2}, line_number(line), line))
        else:
            allowed_lines.sort(key=lambda line: (line_number(line) not in {3, 4}, line_number(line), line))
        candidate_slots: list[tuple[str, int]] = []
        for line in allowed_lines:
            for position in range(1, capacities[line] + 1):
                if position in occupied[line]:
                    continue
                if slot_allowed_for_car(car, line, position, capacities[line]):
                    candidate_slots.append((line, position))
        candidates_by_no[no] = candidate_slots

    matched_by_slot: dict[tuple[str, int], str] = {}

    def slot_preference(car: dict[str, Any], slot: tuple[str, int]) -> tuple[int, int, int, str]:
        line, position = slot
        preferred_long_line = line_number(line) in {3, 4}
        preferred_short_line = line_number(line) in {1, 2}
        if car_length(car) >= 17.6:
            line_penalty = 0 if preferred_long_line else 1
        else:
            line_penalty = 0 if preferred_short_line else 1
        factory_penalty = 0 if not repair_process(car).startswith("厂") or position in {4, 5} else 1
        return (line_penalty, factory_penalty, position, line)

    def assign(no: str, seen: set[tuple[str, int]]) -> bool:
        car = car_lookup[no]
        for slot in sorted(candidates_by_no.get(no, []), key=lambda item: slot_preference(car, item)):
            if slot in seen:
                continue
            seen.add(slot)
            other_no = matched_by_slot.get(slot)
            if other_no is None or assign(other_no, seen):
                matched_by_slot[slot] = no
                return True
        return False

    remaining_nos = sorted(
        (car_no(car) for car in remaining),
        key=lambda no: (
            len(candidates_by_no.get(no, [])),
            not repair_process(car_lookup[no]).startswith("厂"),
            car_length(car_lookup[no]) < 17.6,
            car_lookup[no]["Line"],
            int(car_lookup[no].get("Position") or 0),
            no,
        ),
    )
    for no in remaining_nos:
        if not candidates_by_no.get(no):
            failures[no] = "no_feasible_depot_slot"
            continue
        if not assign(no, set()):
            failures[no] = "no_feasible_depot_slot"

    for (line, position), no in matched_by_slot.items():
        if no not in failures:
            slots[no] = DepotSlot(line=line, position=position)

    factory_positions: dict[str, list[int]] = defaultdict(list)
    for no, slot in slots.items():
        car = next((item for item in depot_target_cars if car_no(item) == no), None)
        if car and repair_process(car).startswith("厂"):
            factory_positions[slot.line].append(slot.position)

    for no, slot in list(slots.items()):
        car = next((item for item in depot_target_cars if car_no(item) == no), None)
        if not car or not repair_process(car).startswith("段"):
            continue
        if slot.locked:
            continue
        factory_min = min(factory_positions[slot.line]) if factory_positions[slot.line] else None
        if factory_min is not None and slot.position > factory_min:
            failures[no] = "section_repair_behind_factory_repair"
    return DepotAssignment(slots=slots, failures=failures, capacities=dict(capacities))












def planned_target_for_car(
    car: dict[str, Any],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    loads: Counter[str] | None = None,
) -> tuple[str, int | None, str]:
    no = car_no(car)
    if no in depot_assignment.failures:
        return "", None, depot_assignment.failures[no]
    if no in depot_assignment.slots:
        slot = depot_assignment.slots[no]
        return slot.line, slot.position, "depot_slot_assignment"

    targets = [line for line in target_lines(car) if line]
    if not targets:
        return "", None, "target_missing"

    loads = loads or line_loads(cars)
    target = min(targets, key=lambda line: (loads[line], line))
    forced = force_positions(car)
    if forced and is_spotting_line(target):
        return target, None, "target_line_assignment"
    return target, (min(forced) if forced else None), "target_line_assignment"


def is_spotting_line(line: str) -> bool:
    return ENABLE_SPOTTING_WINDOW_VALIDATION and line in SPOTTING_LINE_TOTAL_POSITIONS



def spotting_mask_positions(line: str, forced: tuple[int, ...]) -> tuple[int, ...]:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    if not total:
        return ()
    return tuple(sorted({position for position in forced if 1 <= position <= total}))


def spotting_mask_is_contiguous(line: str, forced: tuple[int, ...]) -> bool:
    positions = spotting_mask_positions(line, forced)
    if not positions:
        return False
    return positions == tuple(range(positions[0], positions[-1] + 1))


def spotting_capacity(line: str, forced: tuple[int, ...]) -> int:
    return len(spotting_mask_positions(line, forced))


def spotting_window_bounds(line: str, forced: tuple[int, ...]) -> list[tuple[int, int]]:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    mask_positions = spotting_mask_positions(line, forced)
    if not total or not mask_positions or not spotting_mask_is_contiguous(line, forced):
        return []
    first_position = mask_positions[0]
    return [
        (first_position, end_position)
        for end_position in range(first_position, total + 1)
    ]


def spotting_physical_position_for_mask(line: str, mask_position: int) -> int:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    if not total or not 1 <= mask_position <= total:
        return 0
    return mask_position



def spotting_physical_window_sets(line: str, forced: tuple[int, ...]) -> list[set[int]]:
    windows: list[set[int]] = []
    for start, end in spotting_window_bounds(line, forced):
        physical_positions = {
            spotting_physical_position_for_mask(line, position)
            for position in range(start, end + 1)
        }
        if 0 not in physical_positions:
            windows.append(physical_positions)
    return windows




def spotting_same_forced_positions(
    cars: list[dict[str, Any]],
    target_line: str,
    forced: tuple[int, ...],
    depot_assignment: DepotAssignment,
    excluded_nos: set[str] | None = None,
) -> list[int]:
    excluded_nos = excluded_nos or set()
    positions: list[int] = []
    for car in cars:
        no = car_no(car)
        if no in excluded_nos or no in depot_assignment.failures:
            continue
        if car["Line"] != target_line:
            continue
        if target_line not in (car.get("_TargetLineSet") or set(target_lines(car))):
            continue
        if force_positions(car) == forced:
            positions.append(int(car.get("Position") or 0))
    return positions


def spotting_allowed_positions(
    cars: list[dict[str, Any]],
    target_line: str,
    forced: tuple[int, ...],
    depot_assignment: DepotAssignment,
) -> set[int]:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(target_line, 0)
    mask_positions = spotting_mask_positions(target_line, forced)
    if not total or not mask_positions or not spotting_mask_is_contiguous(target_line, forced):
        return set()
    same_members = [
        car
        for car in cars
        if car_no(car) not in depot_assignment.failures
        and car["Line"] == target_line
        and target_line in (car.get("_TargetLineSet") or set(target_lines(car)))
        and force_positions(car) == forced
    ]
    if not same_members:
        return set()
    southmost_same_position = max(int(car.get("Position") or 0) for car in same_members)
    southern_nonforced_suffix = sum(
        1
        for car in cars
        if car["Line"] == target_line
        and int(car.get("Position") or 0) > southmost_same_position
        and not (
            target_line in (car.get("_TargetLineSet") or set(target_lines(car)))
            and force_positions(car) == forced
        )
    )
    max_position = total - southern_nonforced_suffix
    min_position = mask_positions[0]
    if max_position < min_position:
        return set()
    return set(range(min_position, max_position + 1))


def spotting_group_positions_for_batch(
    *,
    group: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    batch_nos: set[str],
    occupied: set[int],
    used: set[int],
) -> dict[str, int]:
    if not group:
        return {}
    forced = force_positions(group[0])
    if not forced:
        return {}
    existing_positions = spotting_same_forced_positions(
        cars,
        target_line,
        forced,
        depot_assignment,
        excluded_nos=batch_nos,
    )
    capacity = spotting_capacity(target_line, forced)
    if not capacity:
        return {}
    existing_set = set(existing_positions)
    if len(existing_set) + len(group) > capacity:
        return {}
    projected = [dict(car) for car in cars]
    projected_nos = {car_no(car) for car in projected}
    for car in group:
        if car_no(car) in projected_nos:
            continue
        item = dict(car)
        item["Line"] = target_line
        item["Position"] = 0
        projected.append(item)
    allowed = spotting_allowed_positions(projected, target_line, forced, depot_assignment)
    if not existing_set <= allowed:
        return {}
    free_positions = [
        position
        for position in sorted(allowed)
        if position not in existing_set and position not in used
    ]
    if len(free_positions) < len(group):
        return {}
    return {car_no(car): free_positions[index] for index, car in enumerate(group)}


def spotting_group_is_acceptable(
    cars: list[dict[str, Any]],
    target_line: str,
    forced: tuple[int, ...],
    depot_assignment: DepotAssignment,
) -> bool:
    positions = spotting_same_forced_positions(cars, target_line, forced, depot_assignment)
    if not positions:
        return False
    if any(position <= 0 for position in positions):
        return False
    window_sets = spotting_physical_window_sets(target_line, forced)
    if not window_sets:
        return False
    capacity = spotting_capacity(target_line, forced)
    if not capacity or len(positions) > capacity:
        return False
    occupied = set(positions)
    return occupied <= spotting_allowed_positions(cars, target_line, forced, depot_assignment)


def spotting_position_is_acceptable(
    car: dict[str, Any],
    cars: list[dict[str, Any]],
    target_line: str,
    depot_assignment: DepotAssignment,
) -> bool:
    forced = force_positions(car)
    if not is_spotting_line(target_line) or not forced:
        return False
    if not spotting_group_is_acceptable(cars, target_line, forced, depot_assignment):
        return False
    return car["Line"] == target_line


def target_position_is_acceptable(
    car: dict[str, Any],
    target_line: str,
    position: int,
    depot_assignment: DepotAssignment,
    cars: list[dict[str, Any]] | None = None,
) -> bool:
    no = car_no(car)
    slot = depot_assignment.slots.get(no)
    if slot:
        if target_line != slot.line:
            if slot.locked or target_line not in depot_targets(car):
                return False
        if slot.locked:
            return position == slot.position
        capacity = depot_line_capacity(depot_assignment, target_line, fallback_position=position)
        return depot_actual_position_allowed(car, target_line, position, capacity)
    if target_line not in (car.get("_TargetLineSet") or set(target_lines(car))):
        return False
    forced = force_positions(car)
    if forced and is_spotting_line(target_line):
        if cars is None:
            return False
        projected = [dict(item) for item in cars]
        projected_by_no = {car_no(item): item for item in projected}
        projected_car = projected_by_no.get(no)
        if projected_car is None:
            projected_car = dict(car)
            projected.append(projected_car)
        projected_car["Line"] = target_line
        projected_car["Position"] = position
        return spotting_position_is_acceptable(projected_car, projected, target_line, depot_assignment)
    return not forced or position in forced


def reserved_positions_for_line(
    cars: list[dict[str, Any]],
    target_line: str,
    depot_assignment: DepotAssignment,
) -> set[int]:
    if target_line in DEPOT_LINES or is_spotting_line(target_line):
        return set()
    reserved: set[int] = set()
    for car in cars:
        no = car_no(car)
        if no in depot_assignment.slots:
            continue
        if target_line in (car.get("_TargetLineSet") or set(target_lines(car))):
            reserved.update(force_positions(car))
    return reserved


def first_free_south_positions_for_batch(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    batch_nos: set[str],
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    target_cars = grouped.get(target_line, []) if grouped is not None else cars
    occupied = {
        int(item.get("Position") or 0)
        for item in target_cars
        if item["Line"] == target_line and car_no(item) not in batch_nos
    }
    forced_batch = [car for car in batch if force_positions(car)]
    other_batch = [car for car in batch if not force_positions(car)]
    planned: dict[str, int] = {}
    used: set[int] = set()

    forced_groups: dict[tuple[int, ...], list[dict[str, Any]]] = defaultdict(list)
    for car in forced_batch:
        forced_groups[force_positions(car)].append(car)
    for group_forced, group in forced_groups.items():
        group_planned = spotting_group_positions_for_batch(
            group=group,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
            occupied=occupied,
            used=used,
        )
        if len(group_planned) != len(group):
            return {}
        planned.update(group_planned)
        used.update(group_planned.values())
    if len(planned) != len(forced_batch):
        return {}

    next_position = 1
    for car in other_batch:
        no = car_no(car)
        while next_position in occupied or next_position in used:
            next_position += 1
        planned[no] = next_position
        used.add(next_position)
    return planned


def occupied_cars_by_position(
    cars: list[dict[str, Any]],
    target_line: str,
    batch_nos: set[str],
) -> dict[int, dict[str, Any]]:
    return {
        int(car.get("Position") or 0): car
        for car in cars
        if car["Line"] == target_line and car_no(car) not in batch_nos
    }


def forced_position_preference(
    position: int,
    occupants: dict[int, dict[str, Any]],
    target_line: str,
    depot_assignment: DepotAssignment,
    cars: list[dict[str, Any]],
) -> tuple[int, int]:
    occupant = occupants.get(position)
    if occupant is None:
        return (0, position)
    occupant_forced = bool(force_positions(occupant))
    occupant_accepts_position = target_position_is_acceptable(
        occupant,
        target_line,
        position,
        depot_assignment,
        cars,
    )
    if occupant_forced and occupant_accepts_position:
        return (3, position)
    if occupant_accepts_position:
        return (1, position)
    return (2, position)


def planned_positions_for_batch(
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    batch_nos: set[str],
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    target_cars = grouped.get(target_line, []) if grouped is not None else cars
    occupied = {
        int(item.get("Position") or 0)
        for item in target_cars
        if item["Line"] == target_line and car_no(item) not in batch_nos
    }
    occupants = occupied_cars_by_position(cars, target_line, batch_nos)
    reserved_positions = reserved_positions_for_line(cars, target_line, depot_assignment)
    planned: dict[str, int] = {}
    next_position = max(occupied or {0}) + 1

    if is_spotting_line(target_line) and any(force_positions(car) for car in batch):
        spotting_planned = first_free_south_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
            grouped=grouped,
        )
        return spotting_planned

    for car in batch:
        no = car_no(car)
        slot = depot_assignment.slots.get(no)
        if slot and slot.line == target_line:
            position = slot.position
            if position in occupied:
                return {}
            planned[no] = position
            occupied.add(position)
            continue

        forced = [position for position in force_positions(car)]
        for position in sorted(
            forced,
            key=lambda item: forced_position_preference(item, occupants, target_line, depot_assignment, cars),
        ):
            if position not in occupied:
                planned[no] = position
                occupied.add(position)
                break
        if no in planned:
            continue
        if forced:
            next_forced = min(
                (position for position in forced if position not in planned.values()),
                key=lambda item: forced_position_preference(item, occupants, target_line, depot_assignment, cars),
                default=min(forced),
            )
            planned[no] = next_forced
            occupied.add(planned[no])
            continue
        if target_line not in DEPOT_LINES:
            for position in range(1, 120):
                if position not in occupied and position not in reserved_positions:
                    planned[no] = position
                    occupied.add(position)
                    break
            if no in planned:
                continue
        while next_position in occupied:
            next_position += 1
        planned[no] = next_position
        occupied.add(next_position)
    return planned


def car_is_satisfied(
    car: dict[str, Any],
    depot_assignment: DepotAssignment,
    cars: list[dict[str, Any]] | None = None,
) -> bool:
    no = car_no(car)
    if no in depot_assignment.failures:
        return False
    if car.get("IsWeigh") and not car.get("_Weighed"):
        return False
    if no in depot_assignment.slots:
        slot = depot_assignment.slots[no]
        position = int(car.get("Position") or 0)
        if slot.locked and car["Line"] != slot.line:
            return False
        if slot.locked:
            return position == slot.position
        if car["Line"] not in depot_targets(car):
            return False
        capacity = depot_line_capacity(depot_assignment, car["Line"], fallback_position=position)
        return depot_actual_position_allowed(car, car["Line"], position, capacity) and (
            cars is None or depot_section_repair_position_allowed(car, car["Line"], position, cars)
        )
    targets = car.get("_TargetLineSet") or set(target_lines(car))
    if car["Line"] not in targets:
        return False
    forced = force_positions(car)
    if forced and cars is not None and is_spotting_line(car["Line"]):
        return spotting_position_is_acceptable(car, cars, car["Line"], depot_assignment)
    return not forced or int(car.get("Position") or 0) in forced


def unsatisfied_cars(cars: list[dict[str, Any]], depot_assignment: DepotAssignment) -> list[dict[str, Any]]:
    key = (id(cars), id(depot_assignment))
    cached = _unsatisfied_cache.get(key)
    if cached is not None:
        return list(cached)
    unsatisfied = tuple(car for car in cars if not car_is_satisfied(car, depot_assignment, cars))
    _unsatisfied_cache[key] = unsatisfied
    return list(unsatisfied)


def line_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    key = id(cars)
    cached = _line_loads_cache.get(key)
    if cached is None:
        cached = Counter(car["Line"] for car in cars)
        _line_loads_cache[key] = cached
    return cached.copy()


def line_length_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    loads: Counter[str] = Counter()
    for car in cars:
        loads[car["Line"]] += car_length(car)
    return loads


def final_line_length_warnings(cars: list[dict[str, Any]]) -> tuple[str, ...]:
    loads = line_length_loads(cars)
    warnings: list[str] = []
    for line, load in sorted(loads.items()):
        spec = TRACK_SPECS.get(line)
        if not spec:
            continue
        if load > spec.length_m + LINE_LENGTH_TOLERANCE_M:
            warnings.append(f"{line}:{load:.1f}>{spec.length_m:.1f}")
    return tuple(warnings)


def occupied_lines_for_route(cars: list[dict[str, Any]], moving_nos: set[str]) -> set[str]:
    return {
        car["Line"]
        for car in cars
        if car["Line"] and car_no(car) not in moving_nos
    }



def occupied_lines_for_get_route(cars: list[dict[str, Any]], moving_nos: set[str], source_line: str) -> set[str]:
    occupied = occupied_lines_for_route(cars, moving_nos)
    return occupied


def line_access_order(
    cars: list[dict[str, Any]],
    line: str,
    excluded_nos: set[str] | None = None,
) -> list[str]:
    excluded_nos = excluded_nos or set()
    line_cars = [
        car
        for car in cars
        if car["Line"] == line and car_no(car) not in excluded_nos
    ]
    line_cars.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    return [car_no(car) for car in line_cars]


def physical_positions_after_put(
    cars: list[dict[str, Any]],
    line: str,
    put_order: list[str],
) -> dict[str, int]:
    put_order = [no for no in put_order if no]
    put_nos = set(put_order)
    existing_access_order = [
        no for no in line_access_order(cars, line, put_nos)
        if no not in put_nos
    ]
    final_access_order = [*put_order, *existing_access_order]
    return {no: position for position, no in enumerate(final_access_order, start=1)}


def apply_physical_put_order(
    cars: list[dict[str, Any]],
    line: str,
    put_order: list[str],
) -> None:
    positions = physical_positions_after_put(
        cars,
        line,
        put_order,
    )
    for car in cars:
        no = car_no(car)
        if no not in positions:
            continue
        car["Line"] = line
        car["Position"] = positions[no]


def apply_physical_get_order(
    cars: list[dict[str, Any]],
    line: str,
    get_order: list[str] | tuple[str, ...],
) -> None:
    get_nos = {no for no in get_order if no}
    for car in cars:
        if car_no(car) not in get_nos:
            continue
        car["Line"] = ""
        car["Position"] = 0
    compact_source_positions(cars, line, get_nos)


def projected_after_physical_put(
    cars: list[dict[str, Any]],
    line: str,
    put_order: list[str],
) -> list[dict[str, Any]]:
    projected = [dict(car) for car in cars]
    apply_physical_put_order(
        projected,
        line,
        put_order,
    )
    return projected


def inaccessible_get_reason(
    *,
    cars: list[dict[str, Any]],
    line: str,
    move_nos: tuple[str, ...],
    carried_nos: set[str],
    step_index: int,
) -> str:
    if not move_nos:
        return ""
    access_order = line_access_order(cars, line, carried_nos)
    reachable = access_order[: len(move_nos)]
    if reachable == list(move_nos):
        return ""
    return (
        f"line_end_get_order_violation:step={step_index}:line={line}:"
        f"reachable={','.join(reachable)}:"
        f"move={','.join(move_nos)}"
    )


def carried_order_after_get(
    *,
    cars: list[dict[str, Any]],
    line: str,
    move_nos: set[str],
    carried_nos: set[str],
) -> list[str]:
    return [
        no
        for no in line_access_order(cars, line, carried_nos)
        if no in move_nos
    ]


def inaccessible_put_reason(carried_order: list[str], move_nos: tuple[str, ...], step_index: int) -> str:
    if not move_nos:
        return ""
    suffix = carried_order[-len(move_nos):]
    if suffix == list(move_nos):
        return ""
    return (
        f"train_tail_put_order_violation:step={step_index}:"
        f"tail={','.join(suffix)}:move={','.join(move_nos)}:"
        f"train={','.join(carried_order)}"
    )


def line_cars_in_access_order(
    *,
    cars: list[dict[str, Any]],
    line: str,
    access_context: PhysicalAccessContext | None = None,
    graph: Any | None = None,
    loco_location: LocoLocation | None = None,
    moving_nos: set[str] | None = None,
    carried_nos: set[str] | None = None,
    current_loco: LocoLocation | None = None,
) -> list[dict[str, Any]]:
    line = normalize_line(line)
    moving_key = frozenset(moving_nos or set())
    carried_key = frozenset(carried_nos or set())
    key = (
        id(cars),
        line,
        moving_key,
        carried_key,
    )
    cached = _access_order_cache.get(key)
    if cached is not None:
        return list(cached)
    by_no = {
        car_no(car): car
        for car in cars
        if car["Line"] == line and car_no(car) not in set(carried_nos or set())
    }
    ordered = [by_no[no] for no in line_access_order(cars, line, carried_nos) if no in by_no]
    _access_order_cache[key] = list(ordered)
    return ordered











def classify_action_family(source_line: str, target_line: str, is_weigh: bool) -> ContractFamily:
    if is_weigh:
        return ContractFamily.SPECIAL_REPAIR_PROCESS
    if source_line in REMOTE_INTERACTION_LINES and target_line not in DEPOT_TARGET_LINES:
        return ContractFamily.DEPOT_OUTBOUND
    if target_line == "存4线":
        return ContractFamily.CUN4_PORT_STAGING
    if target_line in DEPOT_LINES:
        return ContractFamily.REPAIR_INBOUND
    if target_line in DEPOT_OUTSIDE_LINES:
        return ContractFamily.DEPOT_SLOT
    if target_line == "预修线":
        return ContractFamily.PRE_REPAIR_STAGING
    if target_line in {"调梁棚", "调梁线北"}:
        return ContractFamily.DISPATCH_SHED_QUEUE
    if target_line in {"洗罐站", "洗罐线北", "油漆线", "抛丸线", "卸轮线"}:
        return ContractFamily.FUNCTION_LINE_SERVICE
    if target_line in {"机走棚", "机库线", "机走北"}:
        return ContractFamily.LOCO_AREA_STAGING
    if target_line:
        return ContractFamily.YARD_REBALANCE
    return ContractFamily.RESIDUAL


def action_family(source_line: str, target_line: str, has_weigh: bool) -> str:
    return classify_action_family(source_line, target_line, has_weigh).value


def pull_equivalent(cars: list[dict[str, Any]]) -> int:
    return sum(4 if bool(car.get("IsHeavy")) else 1 for car in cars)


def cars_by_line(cars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in cars:
        grouped[car["Line"]].append(car)
    for line_cars in grouped.values():
        line_cars.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    return dict(grouped)



def target_length_after_move(
    target_line: str,
    cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    batch_nos: set[str],
    length_load_lookup: dict[str, float] | None = None,
) -> float:
    if length_load_lookup is None:
        existing_length = sum(
            car_length(car)
            for car in cars
            if car["Line"] == target_line and car_no(car) not in batch_nos
        )
    else:
        existing_length = length_load_lookup.get(target_line, 0.0)
        for car in batch:
            if car["Line"] == target_line:
                existing_length -= car_length(car)
    return existing_length + sum(car_length(car) for car in batch)


def line_has_length_capacity(
    target_line: str,
    cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    batch_nos: set[str],
    length_load_lookup: dict[str, float] | None = None,
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> bool:
    if target_line in DEPOT_LINES:
        return True
    spec = TRACK_SPECS.get(target_line)
    if not spec:
        return False
    if length_load_lookup is None and grouped is not None:
        target_cars = grouped.get(target_line, [])
        existing_length = sum(car_length(car) for car in target_cars if car_no(car) not in batch_nos)
        after_length = existing_length + sum(car_length(car) for car in batch)
    else:
        if length_load_lookup is None:
            after_length = target_length_after_move(target_line, cars, batch, batch_nos, length_load_lookup)
        else:
            existing_length = length_load_lookup.get(target_line, 0.0)
            existing_length -= sum(
                car_length(car)
                for car in cars
                if car["Line"] == target_line and car_no(car) in batch_nos
            )
            after_length = existing_length + sum(car_length(car) for car in batch)
    return after_length <= spec.length_m + LINE_LENGTH_TOLERANCE_M


def candidate_positions_available(
    target_line: str,
    planned_positions: dict[str, int],
    cars: list[dict[str, Any]],
    batch_nos: set[str],
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> bool:
    if not planned_positions:
        return False
    target_cars = grouped.get(target_line, []) if grouped is not None else cars
    occupied_positions = {
        int(car.get("Position") or 0)
        for car in target_cars
        if car["Line"] == target_line and car_no(car) not in batch_nos
    }
    positions = list(planned_positions.values())
    return len(positions) == len(set(positions)) and not any(position in occupied_positions for position in positions)


def line_length_load(cars: list[dict[str, Any]], line: str, excluded_nos: set[str] | None = None) -> float:
    excluded_nos = excluded_nos or set()
    return sum(car_length(car) for car in cars if car["Line"] == line and car_no(car) not in excluded_nos)


def train_length_for_nos(cars: list[dict[str, Any]], nos: set[str]) -> float:
    return sum(car_length(car) for car in cars if car_no(car) in nos)


def normalized_route_token(value: str) -> str:
    return normalize_line(value)


def route_contains_triplet(path: tuple[str, ...] | list[str], triplet: tuple[str, str, str]) -> bool:
    route = [normalized_route_token(str(item)) for item in path]
    target = [normalized_route_token(item) for item in triplet]
    reversed_target = list(reversed(target))
    for index in range(0, max(0, len(route) - 2)):
        window = route[index : index + 3]
        if window == target or window == reversed_target:
            return True
    return False


def pre_repair_reversal_reasons(
    path: tuple[str, ...] | list[str],
    cars: list[dict[str, Any]],
    moving_nos: set[str],
    train_length_m: float,
) -> list[str]:
    if not path:
        return []
    required_base = train_length_m + LOCO_LENGTH_M
    reasons: list[str] = []

    for triplet, blocker_limits in REVERSAL_RULES_IGNORE_BLOCKER_LENGTH:
        if not route_contains_triplet(path, triplet):
            continue
        for blocker_line, limit_m in blocker_limits:
            blocker = normalize_line(blocker_line)
            if not any(car["Line"] == blocker and car_no(car) not in moving_nos for car in cars):
                continue
            if required_base > limit_m + LINE_LENGTH_TOLERANCE_M:
                reasons.append(
                    "route_reversal_length_violation:"
                    f"{'/'.join(triplet)}:{blocker}:{required_base:.1f}>{limit_m:.1f}"
                )

    for triplet, blocker_limits in REVERSAL_RULES_WITH_BLOCKER_LENGTH:
        if not route_contains_triplet(path, triplet):
            continue
        for blocker_line, limit_m in blocker_limits:
            blocker = normalize_line(blocker_line)
            blocker_length = line_length_load(cars, blocker, moving_nos)
            if blocker_length <= 0:
                continue
            required = required_base + blocker_length
            if required > limit_m + LINE_LENGTH_TOLERANCE_M:
                reasons.append(
                    "route_reversal_with_blocker_length_violation:"
                    f"{'/'.join(triplet)}:{blocker}:{required:.1f}>{limit_m:.1f}"
                )
    return reasons


def single_hook_weigh_reasons(
    candidate: HookCandidate,
    batch: list[dict[str, Any]],
) -> list[str]:
    if not candidate.has_weigh:
        return []
    tail = pending_tail_weigh_car(batch)
    if tail is None:
        pending = pending_single_hook_weigh_cars(batch)
        pending_nos = ",".join(car_no(car) for car in pending) or "none"
        return [f"weigh_requires_pending_tail_car:pending={pending_nos}"]
    return []


def pending_single_hook_weigh_cars(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [car for car in batch if car.get("IsWeigh") and not car.get("_Weighed")]


def pending_tail_weigh_car(batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not batch:
        return None
    tail = batch[-1]
    if tail.get("IsWeigh") and not tail.get("_Weighed"):
        return tail
    return None


def single_hook_weigh_car_no(move_car_nos: tuple[str, ...], cars: list[dict[str, Any]] | None) -> str:
    if not cars:
        return move_car_nos[-1] if move_car_nos else ""
    by_no = {car_no(car): car for car in cars}
    batch = [by_no[no] for no in move_car_nos if no in by_no]
    tail = pending_tail_weigh_car(batch)
    return car_no(tail) if tail else ""







def target_position_occupants(
    cars: list[dict[str, Any]],
    target_line: str,
    positions: set[int],
    batch_nos: set[str],
) -> list[dict[str, Any]]:
    return [
        car
        for car in cars
        if car["Line"] == target_line
        and car_no(car) not in batch_nos
        and int(car.get("Position") or 0) in positions
    ]



def is_locked_depot_stayer(car: dict[str, Any], depot_assignment: DepotAssignment) -> bool:
    slot = depot_assignment.slots.get(car_no(car))
    return bool(
        slot
        and slot.locked
        and car["Line"] == slot.line
        and int(car.get("Position") or 0) == slot.position
    )


def depot_locked_tail_positions(cars: list[dict[str, Any]], line: str, depot_assignment: DepotAssignment) -> set[int]:
    locked_positions = [
        int(car.get("Position") or 0)
        for car in cars
        if car["Line"] == line and is_locked_depot_stayer(car, depot_assignment)
    ]
    if not locked_positions:
        return set()
    tail_start = max(locked_positions)
    capacity = depot_line_capacity(depot_assignment, line, fallback_position=tail_start)
    return set(range(tail_start, capacity + 1))


class DepotSlotGraph:
    """Single hard-rule boundary for depot slots.

    The graph owns slot legality, locked stayer protection, section/factory
    ordering, and accepted-candidate depot resource checks.  Candidate
    generation may ask for slots, but only this boundary decides whether a
    depot placement is business-legal.
    """

    def __init__(self, depot_assignment: DepotAssignment) -> None:
        self.depot_assignment = depot_assignment

    def candidate_put_reasons(
        self,
        candidate: HookCandidate,
        projected_cars: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        actual_positions: dict[str, int],
    ) -> list[str]:
        if candidate.target_line not in DEPOT_LINES:
            return []
        reasons: list[str] = []
        batch_nos = {car_no(car) for car in batch}
        locked_occupants = {
            int(car.get("Position") or 0): car
            for car in projected_cars
            if car["Line"] == candidate.target_line and is_locked_depot_stayer(car, self.depot_assignment)
        }
        locked_tail = depot_locked_tail_positions(projected_cars, candidate.target_line, self.depot_assignment)
        for no, position in actual_positions.items():
            assigned_slot = self.depot_assignment.slots.get(no)
            if assigned_slot and assigned_slot.line != candidate.target_line:
                reasons.append(
                    f"depot_assigned_line_mismatch:{no}:{candidate.target_line}:{position}:"
                    f"expected={assigned_slot.line}"
                )
            if assigned_slot and assigned_slot.locked and assigned_slot.position != position:
                reasons.append(
                    f"depot_locked_slot_mismatch:{no}:{candidate.target_line}:{position}:"
                    f"expected={assigned_slot.line}:{assigned_slot.position}"
                )
            occupant = locked_occupants.get(position)
            if occupant and car_no(occupant) not in batch_nos:
                reasons.append(
                    f"depot_locked_slot_occupied:{candidate.target_line}:{position}:"
                    f"locked={car_no(occupant)}:incoming={no}"
                )
            if position in locked_tail and not (assigned_slot and assigned_slot.locked and assigned_slot.position == position):
                reasons.append(f"depot_locked_tail_position_violation:{candidate.target_line}:{position}:{no}")

        factory_positions = [
            int(car.get("Position") or 0)
            for car in projected_cars
            if car["Line"] == candidate.target_line and repair_process(car).startswith("厂")
        ]
        if factory_positions:
            factory_min = min(factory_positions)
            for car in projected_cars:
                if car["Line"] != candidate.target_line or not repair_process(car).startswith("段"):
                    continue
                if is_locked_depot_stayer(car, self.depot_assignment):
                    continue
                position = int(car.get("Position") or 0)
                if position > factory_min:
                    reasons.append(
                        f"depot_section_after_factory_violation:{candidate.target_line}:"
                        f"{car_no(car)}:{position}>factory_min={factory_min}"
                    )
        return reasons

    def resource_violations(
        self,
        request: Any,
        *,
        candidate: HookCandidate,
        validation: PhysicalValidation,
        cars: list[dict[str, Any]],
    ) -> list[str]:
        steps = candidate_plan_steps(candidate)
        touches_depot = any(
            step.action in {"Get", "Put"} and step.line in DEPOT_TARGET_LINES
            for step in steps
        )
        if not touches_depot:
            return []

        prospective = [dict(car) for car in cars]
        apply_candidate(candidate, prospective, validation)
        after_by_no = {car_no(car): car for car in prospective}
        violations: list[str] = []
        for no in request.move_nos:
            after = after_by_no.get(no)
            if not after or after["Line"] not in DEPOT_TARGET_LINES:
                continue
            target_line, target_position, _reason = planned_target_for_car(
                after,
                prospective,
                self.depot_assignment,
            )
            if target_line not in DEPOT_TARGET_LINES:
                violations.append(f"depot_slot_non_depot_vehicle:{no}:{after['Line']}")
                continue
            if not car_is_satisfied(after, self.depot_assignment, prospective):
                actual = f"{after['Line']}#{int(after.get('Position') or 0)}"
                expected = f"{target_line}#{target_position or ''}"
                violations.append(f"depot_slot_unsatisfied_put:{no}:{actual}->{expected}")

        before_collisions = self.locked_slot_collisions(cars)
        after_collisions = self.locked_slot_collisions(prospective)
        for collision in sorted(after_collisions - before_collisions):
            violations.append(f"depot_locked_slot_collision:{collision}")
        return violations

    def locked_slot_collisions(self, cars: list[dict[str, Any]]) -> set[str]:
        collisions: set[str] = set()
        for owner_no, slot in self.depot_assignment.slots.items():
            if not getattr(slot, "locked", False):
                continue
            occupants = [
                car_no(car)
                for car in cars
                if car["Line"] == slot.line and int(car.get("Position") or 0) == int(slot.position)
            ]
            for occupant_no in occupants:
                if occupant_no != owner_no:
                    collisions.add(f"{slot.line}#{slot.position}:owner={owner_no}:occupant={occupant_no}")
        return collisions


def depot_slot_hard_reasons(
    candidate: HookCandidate,
    projected_cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    actual_positions: dict[str, int],
) -> list[str]:
    return DepotSlotGraph(depot_assignment).candidate_put_reasons(
        candidate,
        projected_cars,
        batch,
        actual_positions,
    )


def depot_resource_violations(
    request: Any,
    *,
    candidate: HookCandidate,
    validation: PhysicalValidation,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> list[str]:
    return DepotSlotGraph(depot_assignment).resource_violations(
        request,
        candidate=candidate,
        validation=validation,
        cars=cars,
    )


def hook_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    planned_positions: dict[str, int],
    generation_reason: str,
    candidate_kind: str,
    plan_steps: tuple[PlanStep, ...] = (),
    action_family_override: str | None = None,
    has_weigh_override: bool | None = None,
) -> HookCandidate:
    move_nos = tuple(car_no(car) for car in batch)
    has_weigh = (
        bool(has_weigh_override)
        if has_weigh_override is not None
        else pending_tail_weigh_car(batch) is not None
    )
    candidate_id = (
        planlet_candidate_id(
            case_id=case_id,
            hook_index=hook_index,
            candidate_kind=candidate_kind,
            steps=plan_steps,
        )
        if plan_steps
        else f"{case_id}:P10:{hook_index}:{candidate_kind}:{source_line}->{target_line}:{','.join(move_nos)}"
    )
    return HookCandidate(
        case_id=case_id,
        hook_index=hook_index,
        candidate_id=candidate_id,
        source_line=source_line,
        target_line=target_line,
        move_car_nos=move_nos,
        action_family=action_family_override or action_family(source_line, target_line, has_weigh),
        train_length_m=round(sum(car_length(car) for car in batch), 3),
        pull_equivalent_count=pull_equivalent(batch),
        has_weigh=has_weigh,
        planned_positions=planned_positions,
        generation_reason=generation_reason,
        candidate_kind=candidate_kind,
        plan_steps=plan_steps,
    )


def build_direct_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    reason: str,
    candidate_kind: str = "vnext_target_move",
    planned_positions: dict[str, int] | None = None,
) -> HookCandidate | None:
    del cars, depot_assignment
    if planned_positions is None:
        return None
    candidate = hook_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        batch=batch,
        planned_positions=planned_positions,
        generation_reason=reason,
        candidate_kind=candidate_kind,
    )
    return replace(
        candidate,
        candidate_kind=candidate_kind,
        candidate_id=planlet_candidate_id(
            case_id=case_id,
            hook_index=hook_index,
            candidate_kind=candidate_kind,
            steps=candidate_plan_steps(candidate),
        ),
    )






def planlet_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    generation_reason: str,
    candidate_kind: str,
    steps: tuple[PlanStep, ...],
    action_family_override: str | None = None,
) -> HookCandidate:
    by_no = {car_no(car): car for car in batch}
    pending_weigh = {
        no
        for no, car in by_no.items()
        if car.get("IsWeigh") and not car.get("_Weighed")
    }
    normalized_steps: list[PlanStep] = []
    carried: set[str] = set()
    carried_order: list[str] = []
    weighed: set[str] = set()
    for step in steps:
        step_nos = set(step.move_car_nos)
        if step.action == "Get":
            for no in step.move_car_nos:
                if no not in carried:
                    carried_order.append(no)
                carried.add(no)
            normalized_steps.append(step)
            continue
        if step.action == "Weigh":
            tail_no = carried_order[-1] if carried_order else ""
            if tail_no and tail_no in step_nos and tail_no in pending_weigh and tail_no not in weighed:
                normalized_steps.append(PlanStep("Weigh", WEIGH_LINE, (tail_no,)))
                weighed.add(tail_no)
            continue
        if step.action == "Put":
            tail_no = carried_order[-1] if carried_order else ""
            if tail_no in pending_weigh and tail_no not in weighed:
                normalized_steps.append(PlanStep("Weigh", WEIGH_LINE, (tail_no,)))
                weighed.add(tail_no)
            normalized_steps.append(step)
            carried.difference_update(step_nos)
            carried_order = [no for no in carried_order if no not in step_nos]
            continue
        normalized_steps.append(step)
    steps = tuple(normalized_steps)
    planned_positions: dict[str, int] = {}
    for step in steps:
        planned_positions.update(step.planned_positions)
    return hook_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        batch=batch,
        planned_positions=planned_positions,
        generation_reason=generation_reason,
        candidate_kind=candidate_kind,
        plan_steps=steps,
        action_family_override=action_family_override or planlet_action_family(steps),
        has_weigh_override=any(step.action == "Weigh" for step in steps),
    )


def build_planlet_candidate(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    steps: tuple[PlanStep, ...],
    reason: str,
    candidate_kind: str,
) -> HookCandidate:
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        batch=batch,
        generation_reason=reason,
        candidate_kind=candidate_kind,
        steps=steps,
    )


def planlet_action_family(steps: tuple[PlanStep, ...]) -> str | None:
    lines = [step.line for step in steps if step.action in {"Get", "Put"}]
    put_lines = [step.line for step in steps if step.action == "Put"]
    get_lines = [step.line for step in steps if step.action == "Get"]
    if any(line in DEPOT_TARGET_LINES for line in get_lines) and any(line not in DEPOT_TARGET_LINES for line in put_lines):
        return ContractFamily.DEPOT_OUTBOUND.value
    for put_line in put_lines:
        family = classify_action_family(get_lines[0] if get_lines else "", put_line, False)
        if family in {
            ContractFamily.CUN4_PORT_STAGING,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
        }:
            return family.value
    if any(line in REMOTE_INTERACTION_LINES for line in lines):
        return ContractFamily.FUNCTION_LINE_SERVICE.value
    return None



































def route_blocking_lines(
    graph: TrackGraph,
    cars: list[dict[str, Any]],
    start_node: str,
    target_line: str,
    moving_nos: set[str],
) -> tuple[list[str], list[str], tuple[str, ...]]:
    occupied = occupied_lines_for_route(cars, moving_nos)
    static_path = graph.route(start_node, target_line)
    if not static_path:
        return [], [], ()
    available_path = graph.route_avoiding_occupied(
        start_node,
        target_line,
        occupied,
        target_approach_lines=route_approach_lines_for_put(target_line, cars, moving_nos),
    )
    if available_path:
        return static_path, available_path, ()

    blockers: list[str] = []
    route_endpoints = {normalize_line(start_node), normalize_line(target_line)}
    for line in static_path:
        if line in occupied and line not in route_endpoints and line not in blockers:
            blockers.append(line)
    return static_path, [], tuple(blockers)










def closed_door_non_cun4_reasons(
    target_line: str,
    train_consist: list[dict[str, Any]],
) -> list[str]:
    if target_line == "存4线" or not train_consist:
        return []
    first = train_consist[0]
    train_count = len(train_consist)
    has_heavy = any(car.get("IsHeavy") for car in train_consist)
    if first.get("IsClosedDoor") and (train_count > 10 or has_heavy):
        return [
            "closed_door_full_consist_first_car_violation:"
            f"{car_no(first)}:target={target_line}:train_count={train_count}:has_heavy={has_heavy}"
        ]
    return []


def closed_door_cun4_position_reasons(cars: list[dict[str, Any]], *, moved_nos: set[str] | None = None) -> list[str]:
    reasons: list[str] = []
    moved_nos = moved_nos or set()
    for car in cars:
        if car["Line"] != "存4线" or not car.get("IsClosedDoor"):
            continue
        position = int(car.get("Position") or 0)
        if position <= 3 and (not moved_nos or car_no(car) in moved_nos):
            reasons.append(f"closed_door_cun4_put_position_violation:{car_no(car)}:{position}")
    return reasons


def closed_door_put_reasons(
    *,
    target_line: str,
    projected_cars: list[dict[str, Any]],
    moved_nos: set[str],
    train_consist: list[dict[str, Any]],
) -> list[str]:
    if target_line == "存4线":
        return closed_door_cun4_position_reasons(projected_cars, moved_nos=moved_nos)
    return closed_door_non_cun4_reasons(target_line, train_consist)


def closed_door_replay_violation_reasons(
    operations: list[OperationTraceRow],
    cars: list[dict[str, Any]],
) -> list[str]:
    if not any(car.get("IsClosedDoor") for car in cars):
        return []

    by_no = {car_no(car): car for car in cars}
    carried_order: list[str] = []
    reasons: list[str] = []
    for row in sorted(operations, key=lambda item: (item.hook_index, item.operation_index)):
        move_nos = [no for no in row.move_cars.split("|") if no]
        if row.action == "Get":
            for no in move_nos:
                if no not in carried_order:
                    carried_order.append(no)
            continue
        if row.action != "Put":
            continue
        train_consist = [by_no[no] for no in carried_order if no in by_no]
        if row.line != "存4线":
            reasons.extend(closed_door_non_cun4_reasons(row.line, train_consist))
        move_set = set(move_nos)
        carried_order = [no for no in carried_order if no not in move_set]

    reasons.extend(closed_door_cun4_position_reasons(cars))
    return reasons


def validate_candidate(
    graph: TrackGraph,
    candidate: HookCandidate,
    cars: list[dict[str, Any]],
    loco_location: LocoLocation,
    depot_assignment: DepotAssignment,
) -> PhysicalValidation:
    if candidate.plan_steps:
        return validate_planlet(graph, candidate, cars, loco_location, depot_assignment)
    reasons: list[str] = []
    by_no = {car_no(car): car for car in cars}
    batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
    occupied_lines = occupied_lines_for_get_route(cars, set(candidate.move_car_nos), candidate.source_line)
    move_nos = set(candidate.move_car_nos)
    get_path = graph.route_avoiding_occupied(
        loco_location.line,
        candidate.source_line,
        occupied_lines,
        target_approach_lines=route_approach_lines_for_get(candidate.source_line),
    )
    get_static_path = graph.route(loco_location.line, candidate.source_line)
    source_location = route_end_location(get_path, candidate.source_line) if get_path else LocoLocation(
        line=candidate.source_line,
    )
    occupied_after_get = occupied_lines_for_route(cars, set(candidate.move_car_nos))
    if candidate.has_weigh:
        raw_weigh_path = graph.route_avoiding_occupied(
            source_location.line,
            WEIGH_LINE,
            occupied_after_get,
            target_approach_lines=route_approach_lines_for_put(WEIGH_LINE, cars, move_nos),
        )
        weigh_static_path = graph.route(source_location.line, WEIGH_LINE)
        weigh_path = route_with_line_prefix(
            candidate.source_line,
            raw_weigh_path,
        )
        weigh_location = route_end_location(weigh_path, WEIGH_LINE) if weigh_path else LocoLocation(
            line=WEIGH_LINE,
        )
        raw_put_path = graph.route_avoiding_occupied(
            weigh_location.line,
            candidate.target_line,
            occupied_after_get,
            target_approach_lines=route_approach_lines_for_put(candidate.target_line, cars, move_nos),
        )
        put_static_path = graph.route(weigh_location.line, candidate.target_line)
        put_path = route_with_line_prefix(
            WEIGH_LINE,
            raw_put_path,
        )
    else:
        weigh_path = []
        weigh_static_path = []
        raw_put_path = graph.route_avoiding_occupied(
            source_location.line,
            candidate.target_line,
            occupied_after_get,
            target_approach_lines=route_approach_lines_for_put(candidate.target_line, cars, move_nos),
        )
        put_static_path = graph.route(source_location.line, candidate.target_line)
        put_path = route_with_line_prefix(
            candidate.source_line,
            raw_put_path,
        )

    if not get_path:
        reasons.append("get_route_blocked_by_occupied_line" if get_static_path else "get_route_missing")
    else:
        get_order_reason = inaccessible_get_reason(
            cars=cars,
            line=candidate.source_line,
            move_nos=candidate.move_car_nos,
            carried_nos=set(),
            step_index=1,
        )
        if get_order_reason:
            reasons.append(get_order_reason)
    if candidate.has_weigh and not weigh_path:
        reasons.append("weigh_route_blocked_by_occupied_line" if weigh_static_path else "weigh_route_missing")
    elif candidate.has_weigh:
        reasons.extend(pre_repair_reversal_reasons(
            weigh_path,
            cars,
            set(candidate.move_car_nos),
            candidate.train_length_m,
        ))
    if not put_path:
        reasons.append("put_route_blocked_by_occupied_line" if put_static_path else "put_route_missing")
    else:
        reasons.extend(pre_repair_reversal_reasons(
            put_path,
            cars,
            set(candidate.move_car_nos),
            candidate.train_length_m,
        ))
    if candidate.source_line in RUNNING_LINES or candidate.target_line in RUNNING_LINES:
        reasons.append("running_line_stop_violation")
    if candidate.source_line not in TRACK_SPECS:
        reasons.append("source_line_unknown")
    if candidate.target_line not in TRACK_SPECS:
        reasons.append("target_line_unknown")
    if candidate.source_line == candidate.target_line and not (
        (
            candidate.candidate_kind == "depot_same_line_repack"
            and candidate.target_line in DEPOT_LINES
        )
        or (candidate.has_weigh and candidate.candidate_kind == "target_move")
    ):
        reasons.append("same_line_reposition_requires_staging_search")
    if candidate.pull_equivalent_count > PULL_LIMIT_EQUIVALENT:
        reasons.append("pull_limit_violation")
    reasons.extend(single_hook_weigh_reasons(candidate, batch))

    active_depot_assignment = current_depot_assignment(depot_assignment, cars)
    put_order = carried_order_after_get(
        cars=cars,
        line=candidate.source_line,
        move_nos=set(candidate.move_car_nos),
        carried_nos=set(),
    )
    if not put_order:
        put_order = list(candidate.move_car_nos)
    projected_after_put = projected_after_physical_put(
        cars,
        candidate.target_line,
        put_order,
    )
    position_reasons = validate_target_positions(
        candidate,
        projected_after_put,
        batch,
        active_depot_assignment,
    )
    reasons.extend(position_reasons)
    reasons.extend(validate_closed_door(candidate, projected_after_put, batch, batch))

    return PhysicalValidation(
        accepted=not reasons,
        reasons=tuple(reasons),
        get_path=tuple(get_path),
        weigh_path=tuple(weigh_path),
        put_path=tuple(put_path),
        operation_paths=tuple(tuple(path) for path in (get_path, weigh_path, put_path) if path),
    )

def validate_planlet(
    graph: TrackGraph,
    candidate: HookCandidate,
    cars: list[dict[str, Any]],
    loco_location: LocoLocation,
    depot_assignment: DepotAssignment,
) -> PhysicalValidation:
    reasons: list[str] = []
    working_cars = [dict(car) for car in cars]
    current_loco = loco_location
    carried: set[str] = set()
    carried_order: list[str] = []
    operation_paths: list[tuple[str, ...]] = []
    get_path: tuple[str, ...] = ()
    put_path: tuple[str, ...] = ()

    for index, step in enumerate(candidate_plan_steps(candidate), start=1):
        step_nos = set(step.move_car_nos)
        by_no = {car_no(car): car for car in working_cars}
        step_cars = [by_no[no] for no in step.move_car_nos if no in by_no]
        if len(step_cars) != len(step.move_car_nos):
            reasons.append(f"planlet_missing_car:step={index}")
            break
        if step.action == "Get":
            source_lines = {car["Line"] for car in step_cars}
            if source_lines != {step.line}:
                reasons.append(f"planlet_get_line_mismatch:step={index}:{step.line}")
                break
            if carried & step_nos:
                reasons.append(f"planlet_duplicate_carry:step={index}")
                break
            if pull_equivalent([by_no[no] for no in sorted(carried | step_nos) if no in by_no]) > PULL_LIMIT_EQUIVALENT:
                reasons.append("pull_limit_violation")
                break
            occupied_lines = occupied_lines_for_get_route(working_cars, step_nos | carried, step.line)
            raw_path = graph.route_avoiding_occupied(
                current_loco.line,
                step.line,
                occupied_lines,
                target_approach_lines=route_approach_lines_for_get(step.line),
            )
            static_path = graph.route(current_loco.line, step.line)
            path = tuple(route_with_line_prefix(current_loco.line, raw_path))
            if not path:
                reasons.append("get_route_blocked_by_occupied_line" if static_path else "get_route_missing")
                reasons.extend(pre_repair_reversal_reasons(
                    route_with_line_prefix(current_loco.line, static_path),
                    working_cars,
                    step_nos | carried,
                    train_length_for_nos(working_cars, step_nos | carried),
                ))
                break
            source_location = operation_stand_location(path, step.line)
            order_reason = inaccessible_get_reason(
                cars=working_cars,
                line=step.line,
                move_nos=step.move_car_nos,
                carried_nos=carried,
                step_index=index,
            )
            if order_reason:
                reasons.append(order_reason)
                break
            if step.line in RUNNING_LINES:
                reasons.append("running_line_stop_violation")
                break
            if step.line not in TRACK_SPECS:
                reasons.append("source_line_unknown")
                break
            reasons.extend(pre_repair_reversal_reasons(
                path,
                working_cars,
                step_nos | carried,
                train_length_for_nos(working_cars, step_nos | carried),
            ))
            if reasons:
                break
            operation_paths.append(path)
            if not get_path:
                get_path = path
            carried.update(step_nos)
            for no in carried_order_after_get(
                cars=working_cars,
                line=step.line,
                move_nos=step_nos,
                carried_nos=carried - step_nos,
            ):
                if no not in carried_order:
                    carried_order.append(no)
            apply_physical_get_order(working_cars, step.line, step.move_car_nos)
            current_loco = source_location
            continue

        if step.action == "Put":
            if not step_nos <= carried:
                reasons.append(f"planlet_put_without_carry:step={index}")
                break
            order_reason = inaccessible_put_reason(carried_order, step.move_car_nos, index)
            if order_reason:
                reasons.append(order_reason)
                break
            batch = [by_no[no] for no in step.move_car_nos if no in by_no]
            occupied_lines = occupied_lines_for_route(working_cars, carried)
            raw_path = graph.route_avoiding_occupied(
                current_loco.line,
                step.line,
                occupied_lines,
                target_approach_lines=route_approach_lines_for_put(step.line, working_cars, carried),
            )
            static_path = graph.route(current_loco.line, step.line)
            path = tuple(route_with_line_prefix(current_loco.line, raw_path))
            if not path:
                reasons.append("put_route_blocked_by_occupied_line" if static_path else "put_route_missing")
                reasons.extend(pre_repair_reversal_reasons(
                    route_with_line_prefix(current_loco.line, static_path),
                    working_cars,
                    carried,
                    train_length_for_nos(working_cars, carried),
                ))
                break
            if step.line in RUNNING_LINES:
                reasons.append("running_line_stop_violation")
                break
            if step.line not in TRACK_SPECS:
                reasons.append("target_line_unknown")
                break
            reasons.extend(pre_repair_reversal_reasons(
                path,
                working_cars,
                carried,
                train_length_for_nos(working_cars, carried),
            ))
            if reasons:
                break
            step_candidate = replace(
                candidate,
                source_line=current_loco.line,
                target_line=step.line,
                move_car_nos=step.move_car_nos,
                planned_positions=step.planned_positions,
                train_length_m=round(sum(car_length(car) for car in batch), 3),
                pull_equivalent_count=pull_equivalent(batch),
                has_weigh=any(bool(car.get("IsWeigh")) for car in batch),
                plan_steps=(),
            )
            active_depot_assignment = current_depot_assignment(depot_assignment, working_cars)
            put_order = carried_order[-len(step_nos):] if step_nos else []
            projected_after_put = projected_after_physical_put(
                working_cars,
                step.line,
                put_order,
            )
            reasons.extend(validate_target_positions(step_candidate, projected_after_put, batch, active_depot_assignment))
            train_consist = [by_no[no] for no in carried_order if no in by_no]
            reasons.extend(validate_closed_door(step_candidate, projected_after_put, batch, train_consist))
            if reasons:
                break
            operation_paths.append(path)
            put_path = path
            apply_physical_put_order(
                working_cars,
                step.line,
                put_order,
            )
            carried.difference_update(step_nos)
            carried_order = [no for no in carried_order if no not in step_nos]
            current_loco = operation_stand_location(path, step.line)
            continue

        if step.action == "Weigh":
            if step.line != WEIGH_LINE:
                reasons.append(f"planlet_weigh_line_invalid:step={index}:{step.line}")
                break
            if step_nos and not step_nos <= carried:
                reasons.append(f"planlet_weigh_without_carry:step={index}")
                break
            if len(step.move_car_nos) != 1:
                reasons.append(f"planlet_weigh_requires_single_tail_car:step={index}")
                break
            weigh_no = step.move_car_nos[0] if step.move_car_nos else ""
            if not carried_order or carried_order[-1] != weigh_no:
                reasons.append(f"planlet_weigh_car_not_tail_in_carry_order:step={index}:{weigh_no}")
                break
            if not by_no.get(weigh_no, {}).get("IsWeigh"):
                reasons.append(f"planlet_weigh_car_not_marked_weigh:step={index}:{weigh_no}")
                break
            if by_no.get(weigh_no, {}).get("_Weighed"):
                reasons.append(f"planlet_weigh_car_already_complete:step={index}:{weigh_no}")
                break
            occupied_lines = occupied_lines_for_route(working_cars, carried)
            raw_path = graph.route_avoiding_occupied(
                current_loco.line,
                WEIGH_LINE,
                occupied_lines,
                target_approach_lines=route_approach_lines_for_put(WEIGH_LINE, working_cars, carried),
            )
            static_path = graph.route(current_loco.line, WEIGH_LINE)
            path = tuple(route_with_line_prefix(current_loco.line, raw_path))
            if not path:
                reasons.append("weigh_route_blocked_by_occupied_line" if static_path else "weigh_route_missing")
                break
            train_length = train_length_for_nos(working_cars, carried)
            reasons.extend(pre_repair_reversal_reasons(path, working_cars, carried, train_length))
            if reasons:
                break
            operation_paths.append(path)
            current_loco = operation_stand_location(path, WEIGH_LINE)
            continue

        reasons.append(f"planlet_unknown_action:step={index}:{step.action}")
        break

    if not reasons and carried:
        reasons.append("planlet_dirty_carry_after_last_step")

    return PhysicalValidation(
        accepted=not reasons,
        reasons=tuple(reasons),
        get_path=get_path,
        weigh_path=(),
        put_path=put_path,
        operation_paths=tuple(operation_paths),
    )

def validate_target_positions(
    candidate: HookCandidate,
    projected_cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> list[str]:
    reasons: list[str] = []
    batch_nos = {car_no(car) for car in batch}
    projected_by_no = {car_no(car): car for car in projected_cars}
    actual_positions = {
        no: int(projected_by_no[no].get("Position") or 0)
        for no in batch_nos
        if no in projected_by_no and projected_by_no[no]["Line"] == candidate.target_line
    }
    if len(actual_positions) != len(batch_nos):
        reasons.append(f"target_physical_put_missing:{candidate.target_line}")
        return reasons
    target_positions = list(actual_positions.values())
    if len(target_positions) != len(set(target_positions)):
        reasons.append("target_position_collision_inside_batch")
    occupied_positions = {
        int(car.get("Position") or 0)
        for car in projected_cars
        if car["Line"] == candidate.target_line and car_no(car) not in batch_nos
    }
    for no, position in actual_positions.items():
        if position in occupied_positions:
            reasons.append(f"target_position_occupied:{candidate.target_line}:{position}:{no}")

    if is_spotting_line(candidate.target_line):
        forced_groups = {
            force_positions(car)
            for car in projected_cars
            if car["Line"] == candidate.target_line
            and force_positions(car)
            and candidate.target_line in (car.get("_TargetLineSet") or set(target_lines(car)))
        }
        for forced in sorted(forced_groups):
            if spotting_group_is_acceptable(projected_cars, candidate.target_line, forced, depot_assignment):
                continue
            members = [
                car_no(car)
                for car in sorted(
                    projected_cars,
                    key=lambda item: (int(item.get("Position") or 0), car_no(item)),
                )
                if car["Line"] == candidate.target_line
                and force_positions(car) == forced
                and candidate.target_line in (car.get("_TargetLineSet") or set(target_lines(car)))
            ]
            reasons.append(
                "spotting_group_window_violation:"
                f"{candidate.target_line}:{','.join(str(position) for position in forced)}:"
                f"{','.join(members)}"
            )
        return reasons
    elif candidate.target_line in DEPOT_LINES:
        capacity = depot_line_capacity(
            depot_assignment,
            candidate.target_line,
            fallback_position=max(target_positions or [0]),
        )
        for car in batch:
            position = actual_positions.get(car_no(car), 0)
            if not depot_actual_position_allowed(car, candidate.target_line, position, capacity):
                reasons.append(f"depot_slot_rule_violation:{car_no(car)}:{candidate.target_line}:{position}")
        reasons.extend(depot_slot_hard_reasons(candidate, projected_cars, batch, depot_assignment, actual_positions))
    else:
        spec = TRACK_SPECS.get(candidate.target_line)
        if spec:
            existing_length = sum(
                car_length(car)
                for car in projected_cars
                if car["Line"] == candidate.target_line
                if car_no(car) not in batch_nos
            )
            after_length = existing_length + candidate.train_length_m
            if after_length > spec.length_m + LINE_LENGTH_TOLERANCE_M:
                reason = f"target_line_length_violation:{candidate.target_line}:{after_length:.1f}>{spec.length_m:.1f}"
                if not _is_final_target_put(candidate.target_line, batch, projected_cars, depot_assignment):
                    reasons.append(reason)
            if spec.track_type == "temporary" and candidate.candidate_kind not in STAGING_CANDIDATE_KINDS:
                reasons.append(f"temporary_line_final_target_violation:{candidate.target_line}")
    return reasons


def _is_final_target_put(
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> bool:
    loads = line_loads(cars)
    for car in batch:
        planned_line, _position, _reason = planned_target_for_car(car, cars, depot_assignment, loads)
        if planned_line != target_line:
            return False
    return bool(batch)

def validate_closed_door(
    candidate: HookCandidate,
    cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    train_consist: list[dict[str, Any]] | None = None,
) -> list[str]:
    if not any(car.get("IsClosedDoor") for car in cars):
        return []
    return closed_door_put_reasons(
        target_line=candidate.target_line,
        projected_cars=cars,
        moved_nos=set(candidate.move_car_nos),
        train_consist=train_consist or batch,
    )



def operation_rows(
    candidate: HookCandidate,
    validation: PhysicalValidation,
    start_operation_index: int,
) -> list[OperationTraceRow]:
    if candidate.plan_steps:
        rows: list[OperationTraceRow] = []
        paths = list(validation.operation_paths)
        carried_order: list[str] = []
        for offset, step in enumerate(candidate_plan_steps(candidate)):
            path = paths[offset] if offset < len(paths) else ()
            if step.action == "Get":
                for no in step.move_car_nos:
                    if no not in carried_order:
                        carried_order.append(no)
                train_cars = "|".join(carried_order)
            elif step.action == "Put":
                move_set = set(step.move_car_nos)
                carried_order = [no for no in carried_order if no not in move_set]
                train_cars = "|".join(carried_order)
            else:
                train_cars = "|".join(carried_order)
            rows.append(
                OperationTraceRow(
                    case_id=candidate.case_id,
                    hook_index=candidate.hook_index,
                    operation_index=start_operation_index + offset,
                    candidate_id=candidate.candidate_id,
                    line=step.line,
                    action=step.action,
                    move_cars="|".join(step.move_car_nos),
                    train_cars=train_cars,
                    passby_path="|".join(route_for_output(path)),
                )
            )
        return rows
    rows = [
        OperationTraceRow(
            case_id=candidate.case_id,
            hook_index=candidate.hook_index,
            operation_index=start_operation_index,
            candidate_id=candidate.candidate_id,
            line=candidate.source_line,
            action="Get",
            move_cars="|".join(candidate.move_car_nos),
            train_cars="|".join(candidate.move_car_nos),
            passby_path="|".join(route_for_output(validation.get_path)),
        )
    ]
    operation_index = start_operation_index + 1
    if candidate.has_weigh:
        weigh_car = single_hook_weigh_car_no(candidate.move_car_nos, None)
        rows.append(
            OperationTraceRow(
                case_id=candidate.case_id,
                hook_index=candidate.hook_index,
                operation_index=operation_index,
                candidate_id=candidate.candidate_id,
                line=WEIGH_LINE,
                action="Weigh",
                move_cars=weigh_car,
                train_cars="|".join(candidate.move_car_nos),
                passby_path="|".join(route_for_output(validation.weigh_path)),
            )
        )
        operation_index += 1
    rows.append(
        OperationTraceRow(
            case_id=candidate.case_id,
            hook_index=candidate.hook_index,
            operation_index=operation_index,
            candidate_id=candidate.candidate_id,
            line=candidate.target_line,
        action="Put",
        move_cars="|".join(candidate.move_car_nos),
        train_cars="",
        passby_path="|".join(route_for_output(validation.put_path)),
    )
    )
    return rows


def last_weigh_car_no(move_car_nos: tuple[str, ...], cars: list[dict[str, Any]] | None) -> str:
    if not cars:
        return move_car_nos[-1] if move_car_nos else ""
    by_no = {car_no(car): car for car in cars}
    for no in reversed(move_car_nos):
        if by_no.get(no, {}).get("IsWeigh"):
            return no
    return move_car_nos[-1] if move_car_nos else ""


def response_operation(row: OperationTraceRow) -> dict[str, Any]:
    return {
        "Index": row.operation_index,
        "Line": row.line,
        "Action": row.action,
        "MoveCars": row.move_cars.split("|") if row.move_cars else [],
        "TrainCars": row.train_cars.split("|") if row.train_cars else [],
        "PassbyPath": row.passby_path.split("|") if row.passby_path else [],
    }


def apply_candidate(
    candidate: HookCandidate,
    cars: list[dict[str, Any]],
    validation: PhysicalValidation | None = None,
) -> None:
    if candidate.plan_steps:
        carried_order: list[str] = []
        paths = list(validation.operation_paths) if validation else []
        for step in candidate_plan_steps(candidate):
            step_nos = set(step.move_car_nos)
            if step.action == "Get":
                if paths:
                    paths.pop(0)
                for no in carried_order_after_get(
                    cars=cars,
                    line=step.line,
                    move_nos=step_nos,
                    carried_nos=set(carried_order),
                ):
                    if no not in carried_order:
                        carried_order.append(no)
                apply_physical_get_order(cars, step.line, step.move_car_nos)
                continue
            if step.action == "Weigh":
                if paths:
                    paths.pop(0)
                for car in cars:
                    if car_no(car) in step_nos:
                        car["_Weighed"] = True
                continue
            if step.action != "Put":
                continue
            if paths:
                paths.pop(0)
            put_order = carried_order[-len(step_nos):] if step_nos else []
            apply_physical_put_order(
                cars,
                step.line,
                put_order,
            )
            carried_order = [no for no in carried_order if no not in step_nos]
        return
    move_nos = set(candidate.move_car_nos)
    weighed_no = single_hook_weigh_car_no(candidate.move_car_nos, cars) if candidate.has_weigh else ""
    put_order = carried_order_after_get(
        cars=cars,
        line=candidate.source_line,
        move_nos=move_nos,
        carried_nos=set(),
    )
    if not put_order:
        put_order = list(candidate.move_car_nos)
    for car in cars:
        no = car_no(car)
        if no == weighed_no:
            car["_Weighed"] = True
    apply_physical_put_order(
        cars,
        candidate.target_line,
        put_order,
    )
    if candidate.source_line != candidate.target_line:
        compact_source_positions(cars, candidate.source_line, move_nos)


def compact_source_positions(cars: list[dict[str, Any]], source_line: str, moved_nos: set[str]) -> None:
    remaining = [car for car in cars if car["Line"] == source_line and car_no(car) not in moved_nos]
    remaining.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    for position, car in enumerate(remaining, start=1):
        car["Position"] = position



def state_signature(
    cars: list[dict[str, Any]],
    loco_location: LocoLocation,
) -> tuple[str, tuple[tuple[str, str, int], ...]]:
    return (
        loco_location.line,
        tuple(
            (car_no(car), car["Line"], int(car.get("Position") or 0))
            for car in sorted(cars, key=lambda item: car_no(item))
        ),
    )


def next_loco_location(candidate: HookCandidate, validation: PhysicalValidation) -> LocoLocation:
    final_line = candidate_final_line(candidate)
    return operation_stand_location(validation.put_path, final_line)



def validate_input(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload.get("StartStatus"), list):
        errors.append("StartStatus_missing_or_not_list")
    if not isinstance(payload.get("TerminalLines"), list):
        errors.append("TerminalLines_missing_or_not_list")
    loco = payload.get("locoNode")
    if not isinstance(loco, dict):
        errors.append("locoNode_missing_or_not_object")
    else:
        if not normalize_line(loco.get("Line")):
            errors.append("locoNode.Line_missing")
        if str(loco.get("End") or "") not in {"North", "South"}:
            errors.append("locoNode.End_invalid")
    for index, car in enumerate(payload.get("StartStatus") or [], start=1):
        for key in ("Line", "Position", "RepairProcess", "Type", "No", "Length", "TargetLines"):
            if key not in car:
                errors.append(f"StartStatus[{index}].{key}_missing")
    return not errors, errors
