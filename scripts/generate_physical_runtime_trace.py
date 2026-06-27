#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from validate_phase_gates import case_id_from_path, normalize_line, write_csv


LOCO_LENGTH_M = 15.0
PULL_LIMIT_EQUIVALENT = 20
LINE_LENGTH_TOLERANCE_M = 0.5
MAX_CANDIDATES_PER_ROUND = 128
MAX_NO_PROGRESS_HOOKS = 8
PRE_DEPOT_PROGRESS_OPEN_RATIO = 0.8
PRE_DEPOT_MAX_HOOKS = 40
REMOTE_CONTINUITY_MAX_STREAK = 10
DEPOT_LINES = {"修1库内", "修2库内", "修3库内", "修4库内"}
DEPOT_OUTSIDE_LINES = {"修1库外", "修2库外", "修3库外", "修4库外"}
DEPOT_TARGET_LINES = DEPOT_LINES | DEPOT_OUTSIDE_LINES
REMOTE_INTERACTION_LINES = DEPOT_TARGET_LINES | {"卸轮线"}
SHORT_DIRECT_DEPOT_CASE_IDS = {"0213W", "0306W", "0327W"}
RUNNING_LINES = {"联6", "联7"} | {f"渡{index}" for index in range(1, 14)}
WEIGH_LINE = "机库线"
STAGING_CANDIDATE_KINDS = {
    "blocker_relocation",
    "capacity_release_to_staging",
    "same_line_stage_out",
    "spot_release_to_staging",
}
STAGING_LINE_PRIORITY = (
    "存5线北",
    "存4线",
    "存2线",
    "存3线",
    "存5线南",
    "存1线",
    "存4南",
    "调梁线北",
    "机走北",
    "洗罐线北",
    "机北1",
    "机北2",
    "机南",
    "洗油北",
)


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

REVERSAL_DISTANCE_M = {
    "机库线": 196.9,
    "调梁线北": 296.2,
    "调梁棚": 296.2,
    "机走北": 229.2,
    "机走棚": 229.2,
    "预修线": 262.6,
    "洗罐线北": 242.1,
    "洗罐站": 242.1,
    "油漆线": 204.2,
    "抛丸线": 89.8,
    "卸轮线": 160.9,
    "修1库外": 318.9,
    "修1库内": 318.9,
    "修2库外": 346.7,
    "修2库内": 346.7,
    "修3库外": 289.8,
    "修3库内": 289.8,
    "修4库外": 289.2,
    "修4库内": 289.2,
}

LINE_ATTACHMENTS = {
    "机北1": ("L3", "L5"),
    "存1线": ("L5", "Z2"),
    "存2线": ("L4", "Z3"),
    "存3线": ("L4", "Z4"),
    "存4线": ("L2", "Z4"),
    "存4南": ("Z4", "L12"),
    "存5线北": ("L2",),
    "存5线南": ("L12",),
    "机北2": ("L5", "L6"),
    "机库线": ("L7",),
    "调梁线北": ("L7",),
    "调梁棚": ("L7",),
    "机走北": ("Z1",),
    "机走棚": ("Z1", "L8"),
    "预修线": ("Z3", "L13"),
    "洗油北": ("L8", "L9"),
    "机南": ("L8", "L14"),
    "洗罐线北": ("L9",),
    "洗罐站": ("L9",),
    "抛丸线": ("L15",),
    "油漆线": ("L9",),
    "卸轮线": ("L19",),
    "修1库外": ("L19",),
    "修1库内": ("L19",),
    "修2库外": ("L17",),
    "修2库内": ("L17",),
    "修3库外": ("L18",),
    "修3库内": ("L18",),
    "修4库外": ("L18",),
    "修4库内": ("L18",),
}

SWITCH_EDGES = (
    ("L1", "L2", 37.0),
    ("L1", "L3", 45.5),
    ("L3", "L4", 45.4),
    ("L2", "L12", 626.3),
    ("L2", "Z4", 417.7),
    ("L4", "Z4", 359.2),
    ("L4", "Z3", 339.9),
    ("L3", "L5", 131.4),
    ("L5", "L6", 105.7),
    ("L5", "Z2", 211.4),
    ("L6", "Z1", 40.6),
    ("Z1", "Z2", 68.2),
    ("Z2", "Z3", 45.4),
    ("L6", "L7", 41.5),
    ("Z1", "L8", 229.2),
    ("Z3", "L13", 262.6),
    ("Z4", "L12", 207.0),
    ("L8", "L9", 111.9),
    ("L8", "L14", 187.7),
    ("L12", "L13", 36.9),
    ("L13", "L14", 41.5),
    ("L14", "L15", 17.9),
    ("L15", "L16", 161.5),
    ("L16", "L17", 45.2),
    ("L16", "L19", 74.9),
    ("L17", "L18", 55.0),
)

SWITCH_EDGE_TRACKS: dict[frozenset[str], tuple[str, ...]] = {
    frozenset(("L1", "L2")): ("渡1",),
    frozenset(("L1", "L3")): ("渡2",),
    frozenset(("L3", "L4")): ("渡3",),
    frozenset(("L2", "L12")): ("存4线", "存4南"),
    frozenset(("L2", "Z4")): ("存4线",),
    frozenset(("L4", "Z4")): ("存3线",),
    frozenset(("L4", "Z3")): ("存2线",),
    frozenset(("L3", "L5")): ("机北1",),
    frozenset(("L5", "L6")): ("机北2",),
    frozenset(("L5", "Z2")): ("存1线",),
    frozenset(("L6", "Z1")): ("渡5",),
    frozenset(("Z1", "Z2")): ("渡6",),
    frozenset(("Z2", "Z3")): ("渡7",),
    frozenset(("L6", "L7")): ("渡4",),
    frozenset(("Z1", "L8")): ("机走北", "机走棚"),
    frozenset(("Z3", "L13")): ("预修线",),
    frozenset(("Z4", "L12")): ("存4南",),
    frozenset(("L8", "L9")): ("洗油北",),
    frozenset(("L8", "L14")): ("机南",),
    frozenset(("L12", "L13")): ("渡8",),
    frozenset(("L13", "L14")): ("渡9",),
    frozenset(("L14", "L15")): ("渡10",),
    frozenset(("L15", "L16")): ("联7",),
    frozenset(("L16", "L17")): ("渡12",),
    frozenset(("L16", "L19")): ("渡11",),
    frozenset(("L17", "L18")): ("渡13",),
}
ROUTE_OCCUPIED_LINE_PENALTY_M = 10000.0


@dataclass(frozen=True)
class DepotSlot:
    line: str
    position: int
    locked: bool = False


@dataclass(frozen=True)
class DepotAssignment:
    slots: dict[str, DepotSlot]
    failures: dict[str, str]


def current_depot_assignment(base: DepotAssignment, cars: list[dict[str, Any]]) -> DepotAssignment:
    active_nos = {car_no(car) for car in cars if has_depot_target(car)}
    return DepotAssignment(
        slots={no: slot for no, slot in base.slots.items() if no in active_nos},
        failures={no: reason for no, reason in base.failures.items() if no in active_nos},
    )


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


@dataclass(frozen=True)
class PhysicalValidation:
    accepted: bool
    reasons: tuple[str, ...]
    get_path: tuple[str, ...]
    weigh_path: tuple[str, ...]
    put_path: tuple[str, ...]


@dataclass(frozen=True)
class PhaseState:
    active_phase: str
    active_variant: str
    link7_open: bool
    non_depot_unsatisfied_count: int
    initial_non_depot_unsatisfied_count: int
    front_service_progress: float
    current_front_service_progress: float


@dataclass(frozen=True)
class CandidateAuditRow:
    case_id: str
    hook_index: int
    candidate_id: str
    candidate_status: str
    source_line: str
    target_line: str
    action_family: str
    move_car_count: int
    move_cars: str
    train_length_m: float
    pull_equivalent_count: int
    has_weigh: bool
    get_route_exists: bool
    put_route_exists: bool
    hard_violation_count: int
    hard_violation_reasons: str
    generation_reason: str
    get_path: str
    weigh_path: str
    put_path: str


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


@dataclass(frozen=True)
class CaseSummaryRow:
    case_id: str
    solve_strategy: str
    status: str
    input_schema_passed: bool
    vehicle_count: int
    initial_unsatisfied_vehicle_count: int
    final_unsatisfied_vehicle_count: int
    business_get_put_hook_count: int
    internal_move_batch_count: int
    interface_operation_count: int
    get_operation_count: int
    put_operation_count: int
    weigh_operation_count: int
    remote_interaction_cross_count: int
    remote_interaction_batch_count: int
    remote_interaction_session_count: int
    generated_hook_count: int
    generated_operation_count: int
    accepted_candidate_count: int
    rejected_candidate_count: int
    blocked_candidate_count: int
    hard_physical_violation_accepted_count: int
    unknown_route_count: int
    depot_slot_failure_count: int
    state_loop_count: int
    blocked_reason: str
    response_path: str


@dataclass(frozen=True)
class PhysicalRuntimeSummary:
    truth_case_count: int
    completed_case_count: int
    blocked_case_count: int
    invalid_input_case_count: int
    total_vehicle_count: int
    total_initial_unsatisfied_vehicle_count: int
    total_final_unsatisfied_vehicle_count: int
    business_get_put_hook_count: int
    internal_move_batch_count: int
    interface_operation_count: int
    get_operation_count: int
    put_operation_count: int
    weigh_operation_count: int
    remote_interaction_cross_count: int
    remote_interaction_batch_count: int
    remote_interaction_session_count: int
    generated_hook_count: int
    generated_operation_count: int
    accepted_candidate_count: int
    rejected_candidate_count: int
    blocked_candidate_count: int
    hard_physical_violation_accepted_count: int
    unknown_route_count: int
    depot_slot_failure_count: int
    state_loop_count: int
    status_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]
    blocked_reason_counts: dict[str, int]


@dataclass(frozen=True)
class GapSummaryRow:
    gap_bucket: str
    record_count: int
    case_count: int
    accepted_blocker: str
    next_required_component: str
    example_reasons: str


class TrackGraph:
    def __init__(self) -> None:
        self._adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self._route_cache: dict[tuple[str, str], list[str]] = {}
        for line, nodes in LINE_ATTACHMENTS.items():
            operation_node = line_operation_node(line)
            if operation_node:
                self._add_edge(line, operation_node, 0.0)
        for left, right, distance in SWITCH_EDGES:
            self._add_edge(left, right, distance)

    def _add_edge(self, left: str, right: str, distance: float) -> None:
        self._adjacency[left].append((right, distance))
        self._adjacency[right].append((left, distance))

    def route(self, source_line: str, target_line: str) -> list[str]:
        return self._route(source_line, target_line, occupied_lines=set())

    def route_avoiding_occupied(
        self,
        source_line: str,
        target_line: str,
        occupied_lines: set[str],
    ) -> list[str]:
        return self._route(source_line, target_line, occupied_lines=occupied_lines)

    def _route(self, source_line: str, target_line: str, occupied_lines: set[str]) -> list[str]:
        source = normalize_line(source_line)
        target = normalize_line(target_line)
        if not occupied_lines:
            cache_key = (source, target)
            if cache_key in self._route_cache:
                return list(self._route_cache[cache_key])
        if source == target and source in self._adjacency:
            if not occupied_lines:
                self._route_cache[(source, target)] = [source]
            return [source]
        if source not in self._adjacency or target not in self._adjacency:
            if not occupied_lines:
                self._route_cache[(source, target)] = []
            return []

        queue: list[tuple[float, str, list[str]]] = [(0.0, source, [source])]
        best: dict[str, float] = {source: 0.0}
        while queue:
            distance, node, path = heapq.heappop(queue)
            if node == target:
                if not occupied_lines:
                    self._route_cache[(source, target)] = path
                return path
            if distance > best.get(node, float("inf")):
                continue
            for next_node, edge_cost in self._adjacency[node]:
                if self._occupied_edge_blocked(
                    node,
                    next_node,
                    source=source,
                    target=target,
                    occupied_lines=occupied_lines,
                ):
                    continue
                next_distance = distance + edge_cost + self._occupied_penalty(
                    node,
                    next_node,
                    source=source,
                    target=target,
                    occupied_lines=occupied_lines,
                )
                if next_distance < best.get(next_node, float("inf")):
                    best[next_node] = next_distance
                    heapq.heappush(queue, (next_distance, next_node, [*path, next_node]))
        if not occupied_lines:
            self._route_cache[(source, target)] = []
        return []

    def _occupied_penalty(
        self,
        node: str,
        next_node: str,
        *,
        source: str,
        target: str,
        occupied_lines: set[str],
    ) -> float:
        if next_node in occupied_lines and next_node not in {source, target}:
            return ROUTE_OCCUPIED_LINE_PENALTY_M
        return 0.0

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
        edge_tracks = SWITCH_EDGE_TRACKS.get(frozenset((node, next_node)), ())
        return any(line in occupied_lines and line not in route_endpoints for line in edge_tracks)


SWITCH_NODES = {node for edge in SWITCH_EDGES for node in edge[:2]}


@dataclass(frozen=True)
class LocoLocation:
    line: str
    node: str


def initial_loco_location(loco: dict[str, Any]) -> LocoLocation:
    line = normalize_line(loco.get("Line"))
    return LocoLocation(line=line, node=line_end_node(line, str(loco.get("End") or "North")))


def line_end_node(line: str, end: str) -> str:
    attachments = LINE_ATTACHMENTS.get(normalize_line(line)) or ()
    if not attachments:
        return normalize_line(line)
    if end == "South" and len(attachments) > 1:
        return attachments[-1]
    return attachments[0]


def line_operation_node(line: str) -> str:
    return line_end_node(line, "North")


def route_end_location(path: tuple[str, ...] | list[str], fallback_line: str) -> LocoLocation:
    line = normalize_line(fallback_line)
    for item in reversed(path):
        normalized = normalize_line(item)
        if normalized in SWITCH_NODES:
            return LocoLocation(line=line, node=normalized)
    return LocoLocation(line=line, node=line_end_node(line, "North"))


def operation_stand_location(path: tuple[str, ...] | list[str], operation_line: str) -> LocoLocation:
    line = normalize_line(operation_line)
    normalized_path = [normalize_line(item) for item in path]
    if line in normalized_path:
        line_index = len(normalized_path) - 1 - list(reversed(normalized_path)).index(line)
        for item in reversed(normalized_path[:line_index]):
            if item in SWITCH_NODES:
                return LocoLocation(line=line, node=item)
    return route_end_location(normalized_path, line)


def route_with_line_prefix(line: str, path: list[str]) -> list[str]:
    normalized_line = normalize_line(line)
    if not normalized_line or not path or path[0] == normalized_line:
        return path
    return [normalized_line, *path]


def route_for_output(path: tuple[str, ...] | list[str]) -> list[str]:
    output: list[str] = []
    normalized_path = [normalize_line(item) for item in path if normalize_line(item)]
    start_index = 0
    if (
        len(normalized_path) >= 2
        and normalized_path[0] in LINE_ATTACHMENTS
        and normalized_path[1] in LINE_ATTACHMENTS[normalized_path[0]]
    ):
        start_index = 1
    for index in range(start_index, len(normalized_path)):
        item = normalized_path[index]
        if not output or output[-1] != item:
            output.append(item)
        if index + 1 >= len(normalized_path):
            continue
        for track in SWITCH_EDGE_TRACKS.get(frozenset((item, normalized_path[index + 1])), ()):
            if index + 2 < len(normalized_path) and track == normalized_path[index + 2]:
                continue
            if not output or output[-1] != track:
                output.append(track)
    return output


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    item["IsClosedDoor"] = bool(item.get("IsClosedDoor"))
    return item


def car_no(car: dict[str, Any]) -> str:
    return str(car.get("_No") or car.get("No") or "")


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
    return DepotAssignment(slots=slots, failures=failures)


def short_direct_group_key(car: dict[str, Any]) -> tuple[int, str, str]:
    if repair_process(car).startswith("厂"):
        repair_class = "factory"
        rank = 0
    elif car_length(car) >= 17.6:
        repair_class = "long"
        rank = 1
    else:
        repair_class = "section"
        rank = 2
    return (rank, car["Line"], repair_class)


def short_direct_line_preference(line: str, group_rank: int, source_line: str) -> tuple[int, int, str]:
    if group_rank == 1:
        line_penalty = 0 if line_number(line) in {3, 4} else 1
    elif group_rank == 0:
        line_penalty = 0
    else:
        line_penalty = 0 if line_number(line) in {1, 2} else 1
    source_penalty = 0 if source_line == line else 1
    return (line_penalty, source_penalty, line)


def short_direct_fit_prefix(
    *,
    line: str,
    batch: list[dict[str, Any]],
    capacities: dict[str, int],
    occupied: dict[str, set[int]],
    factory_min_by_line: dict[str, int],
) -> list[tuple[str, int]]:
    capacity = capacities[line]
    assigned: list[tuple[str, int]] = []
    used: set[int] = set()
    for car in batch:
        positions = [
            position
            for position in range(1, capacity + 1)
            if position not in occupied[line]
            and position not in used
            and slot_allowed_for_car(car, line, position, capacity)
            and not depot_repair_order_conflict(car, line, position, factory_min_by_line)
        ]
        if not positions:
            break
        preferred = force_positions(car)
        position = min(positions, key=lambda item: (item not in preferred, item))
        assigned.append((car_no(car), position))
        used.add(position)
    return assigned


def short_direct_fallback_slot(
    car: dict[str, Any],
    capacities: dict[str, int],
    occupied: dict[str, set[int]],
    factory_min_by_line: dict[str, int],
) -> tuple[str, int] | None:
    allowed_lines = [line for line in depot_targets(car) if line in capacities]
    if car_length(car) >= 17.6:
        allowed_lines.sort(key=lambda line: (line_number(line) not in {3, 4}, line))
    else:
        allowed_lines.sort(key=lambda line: (line_number(line) not in {1, 2}, line))
    for line in allowed_lines:
        capacity = capacities[line]
        for position in range(1, capacity + 1):
            if position in occupied[line]:
                continue
            if slot_allowed_for_car(car, line, position, capacity):
                if depot_repair_order_conflict(car, line, position, factory_min_by_line):
                    continue
                return line, position
    return None


def depot_repair_order_conflict(
    car: dict[str, Any],
    line: str,
    position: int,
    factory_min_by_line: dict[str, int],
) -> bool:
    factory_min = factory_min_by_line.get(line)
    return bool(
        factory_min is not None
        and repair_process(car).startswith("段")
        and position > factory_min
    )


def update_factory_min(
    factory_min_by_line: dict[str, int],
    car: dict[str, Any],
    line: str,
    position: int,
) -> None:
    if repair_process(car).startswith("厂"):
        factory_min_by_line[line] = min(factory_min_by_line.get(line, position), position)


def build_short_direct_depot_assignment(
    cars: list[dict[str, Any]],
    capacities: dict[str, int],
    base: DepotAssignment,
) -> DepotAssignment:
    slots: dict[str, DepotSlot] = {}
    failures: dict[str, str] = dict(base.failures)
    occupied: dict[str, set[int]] = defaultdict(set)
    factory_min_by_line: dict[str, int] = {}

    for no, slot in base.slots.items():
        if not slot.locked:
            continue
        slots[no] = slot
        occupied[slot.line].add(slot.position)
        car = next((item for item in cars if car_no(item) == no), None)
        if car:
            update_factory_min(factory_min_by_line, car, slot.line, slot.position)

    pending = [
        car
        for car in cars
        if has_depot_target(car)
        and car_no(car) not in slots
        and car_no(car) not in failures
    ]
    grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for car in pending:
        grouped[short_direct_group_key(car)].append(car)

    for group_key in sorted(grouped):
        group_rank, source_line, _repair_class = group_key
        remaining = sorted(grouped[group_key], key=lambda item: (int(item.get("Position") or 0), car_no(item)))
        while remaining:
            candidate_lines = sorted(
                {line for car in remaining for line in depot_targets(car) if line in capacities},
                key=lambda line: short_direct_line_preference(line, group_rank, source_line),
            )
            best_line = ""
            best_assignments: list[tuple[str, int]] = []
            for line in candidate_lines:
                assignments = short_direct_fit_prefix(
                    line=line,
                    batch=remaining,
                    capacities=capacities,
                    occupied=occupied,
                    factory_min_by_line=factory_min_by_line,
                )
                if len(assignments) > len(best_assignments):
                    best_line = line
                    best_assignments = assignments
            if not best_assignments:
                fallback = short_direct_fallback_slot(remaining[0], capacities, occupied, factory_min_by_line)
                if fallback is None:
                    failures[car_no(remaining[0])] = "no_feasible_depot_slot"
                    remaining = remaining[1:]
                    continue
                best_line, position = fallback
                best_assignments = [(car_no(remaining[0]), position)]

            assigned_nos = {no for no, _position in best_assignments}
            by_no = {car_no(car): car for car in remaining}
            for no, position in best_assignments:
                slots[no] = DepotSlot(line=best_line, position=position)
                occupied[best_line].add(position)
                car = by_no.get(no)
                if car:
                    update_factory_min(factory_min_by_line, car, best_line, position)
            remaining = [car for car in remaining if car_no(car) not in assigned_nos]

    by_no = {car_no(car): car for car in cars}
    for no, slot in list(slots.items()):
        car = by_no.get(no)
        if not car or not repair_process(car).startswith("段") or slot.locked:
            continue
        factory_min = factory_min_by_line.get(slot.line)
        if factory_min is not None and slot.position > factory_min:
            failures[no] = "section_repair_behind_factory_repair"
            del slots[no]

    return DepotAssignment(slots=slots, failures=failures)


def depot_source_fragmentation_score(cars: list[dict[str, Any]], assignment: DepotAssignment) -> int:
    targets_by_source: dict[str, set[str]] = defaultdict(set)
    for car in cars:
        no = car_no(car)
        if not has_depot_target(car):
            continue
        slot = assignment.slots.get(no)
        if slot is None:
            continue
        targets_by_source[car["Line"]].add(slot.line)
    return sum(max(0, len(lines) - 1) for lines in targets_by_source.values())


def depot_source_crossing_score(cars: list[dict[str, Any]], assignment: DepotAssignment) -> int:
    score = 0
    for car in cars:
        no = car_no(car)
        if not has_depot_target(car):
            continue
        slot = assignment.slots.get(no)
        if slot is None:
            continue
        if car["Line"] != slot.line:
            score += 1
    return score


def select_depot_assignment(
    cars: list[dict[str, Any]],
    capacities: dict[str, int],
    *,
    force_source_cluster: bool = False,
) -> DepotAssignment:
    base = build_depot_assignment(cars, capacities)
    clustered = build_short_direct_depot_assignment(cars, capacities, base)
    if force_source_cluster:
        return clustered
    if len(clustered.failures) > len(base.failures):
        return base
    if len(clustered.slots) < len(base.slots):
        return base
    base_fragmentation = depot_source_fragmentation_score(cars, base)
    clustered_fragmentation = depot_source_fragmentation_score(cars, clustered)
    if clustered_fragmentation >= base_fragmentation:
        return base
    base_crossings = depot_source_crossing_score(cars, base)
    clustered_crossings = depot_source_crossing_score(cars, clustered)
    if clustered_crossings > base_crossings + 2:
        return base
    return clustered


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
    return target, (min(forced) if forced else None), "target_line_assignment"


def target_position_is_acceptable(
    car: dict[str, Any],
    target_line: str,
    position: int,
    depot_assignment: DepotAssignment,
) -> bool:
    no = car_no(car)
    slot = depot_assignment.slots.get(no)
    if slot:
        return target_line == slot.line and position == slot.position
    if target_line not in (car.get("_TargetLineSet") or set(target_lines(car))):
        return False
    forced = force_positions(car)
    return not forced or position in forced


def reserved_positions_for_line(
    cars: list[dict[str, Any]],
    target_line: str,
    depot_assignment: DepotAssignment,
) -> set[int]:
    if target_line in DEPOT_LINES:
        return set()
    reserved: set[int] = set()
    for car in cars:
        no = car_no(car)
        if no in depot_assignment.slots:
            continue
        if target_line in (car.get("_TargetLineSet") or set(target_lines(car))):
            reserved.update(force_positions(car))
    return reserved


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

    for car in batch:
        no = car_no(car)
        if no in depot_assignment.slots:
            position = depot_assignment.slots[no].position
            planned[no] = position
            occupied.add(position)
            continue

        forced = [position for position in force_positions(car)]
        for position in sorted(forced, key=lambda item: forced_position_preference(item, occupants, target_line, depot_assignment)):
            if position not in occupied:
                planned[no] = position
                occupied.add(position)
                break
        if no in planned:
            continue
        if forced:
            next_forced = min(
                (position for position in forced if position not in planned.values()),
                key=lambda item: forced_position_preference(item, occupants, target_line, depot_assignment),
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


def car_is_satisfied(car: dict[str, Any], depot_assignment: DepotAssignment) -> bool:
    no = car_no(car)
    if no in depot_assignment.failures:
        return False
    if no in depot_assignment.slots:
        slot = depot_assignment.slots[no]
        return car["Line"] == slot.line and int(car.get("Position") or 0) == slot.position
    targets = car.get("_TargetLineSet") or set(target_lines(car))
    if car["Line"] not in targets:
        return False
    forced = force_positions(car)
    return not forced or int(car.get("Position") or 0) in forced


def unsatisfied_cars(cars: list[dict[str, Any]], depot_assignment: DepotAssignment) -> list[dict[str, Any]]:
    return [car for car in cars if not car_is_satisfied(car, depot_assignment)]


def line_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    return Counter(car["Line"] for car in cars)


def line_length_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    loads: Counter[str] = Counter()
    for car in cars:
        loads[car["Line"]] += car_length(car)
    return loads


def occupied_lines_for_route(cars: list[dict[str, Any]], moving_nos: set[str]) -> set[str]:
    return {
        car["Line"]
        for car in cars
        if car["Line"] and car_no(car) not in moving_nos
    }


def non_depot_unsatisfied_count(cars: list[dict[str, Any]], depot_assignment: DepotAssignment) -> int:
    loads = line_loads(cars)
    count = 0
    for car in unsatisfied_cars(cars, depot_assignment):
        target_line, _position, _reason = planned_target_for_car(car, cars, depot_assignment, loads)
        if car["Line"] in DEPOT_TARGET_LINES or target_line in DEPOT_TARGET_LINES:
            continue
        count += 1
    return count


def is_short_direct_depot_variant(case_id: str) -> bool:
    return case_id.upper() in SHORT_DIRECT_DEPOT_CASE_IDS


def phase_state_for_case(
    *,
    case_id: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    hook_index: int,
    initial_non_depot_unsatisfied_count: int,
    best_non_depot_unsatisfied_count: int,
    short_direct_override: bool = False,
) -> PhaseState:
    current_non_depot = non_depot_unsatisfied_count(cars, depot_assignment)
    if initial_non_depot_unsatisfied_count <= 0:
        current_progress = 1.0
        best_progress = 1.0
    else:
        current_progress = 1.0 - (current_non_depot / initial_non_depot_unsatisfied_count)
        best_progress = 1.0 - (best_non_depot_unsatisfied_count / initial_non_depot_unsatisfied_count)
    short_direct = short_direct_override or is_short_direct_depot_variant(case_id)
    best_progress = max(0.0, min(1.0, best_progress))
    current_progress = max(0.0, min(1.0, current_progress))
    link7_open = (
        short_direct
        or best_progress >= PRE_DEPOT_PROGRESS_OPEN_RATIO
        or hook_index > PRE_DEPOT_MAX_HOOKS
    )
    if short_direct:
        phase = "H3_SHORT_DIRECT_DEPOT"
        variant = "SHORT_DIRECT_DEPOT"
    elif link7_open:
        phase = "H3_H4_DEPOT_ALLOWED"
        variant = "STANDARD_REPAIR_CHAIN"
    else:
        phase = "H1_H2_LINK7_CLOSED"
        variant = "STANDARD_REPAIR_CHAIN"
    return PhaseState(
        active_phase=phase,
        active_variant=variant,
        link7_open=link7_open,
        non_depot_unsatisfied_count=current_non_depot,
        initial_non_depot_unsatisfied_count=initial_non_depot_unsatisfied_count,
        front_service_progress=best_progress,
        current_front_service_progress=current_progress,
    )


def candidate_touches_depot(candidate: HookCandidate) -> bool:
    return (
        candidate.source_line in DEPOT_TARGET_LINES
        or candidate.target_line in DEPOT_TARGET_LINES
        or candidate.action_family in {"DEPOT_OUTBOUND", "REPAIR_INBOUND", "DEPOT_SLOT"}
    )


def candidate_touches_remote_interaction(candidate: HookCandidate) -> bool:
    return candidate.source_line in REMOTE_INTERACTION_LINES or candidate.target_line in REMOTE_INTERACTION_LINES


def validation_crosses_link7(validation: PhysicalValidation) -> bool:
    for path in (validation.get_path, validation.weigh_path, validation.put_path):
        if "联7" in route_for_output(path):
            return True
    return False


def phase_reject_reason(
    candidate: HookCandidate,
    validation: PhysicalValidation,
    phase_state: PhaseState,
) -> str:
    if phase_state.link7_open:
        return ""
    if candidate_touches_depot(candidate):
        return (
            "phase_link7_closed_defer_depot:"
            f"phase={phase_state.active_phase}:"
            f"front_service_progress={phase_state.front_service_progress:.2f}"
        )
    if validation_crosses_link7(validation):
        return (
            "phase_link7_closed_route_crosses_link7:"
            f"phase={phase_state.active_phase}:"
            f"front_service_progress={phase_state.front_service_progress:.2f}"
        )
    return ""


def phase_candidate_sort_key(candidate: HookCandidate, phase_state: PhaseState) -> tuple[int, int, tuple[int, tuple[int, str, str], int, str]]:
    if phase_state.active_variant == "SHORT_DIRECT_DEPOT":
        family_priority = {
            "REPAIR_INBOUND": 0,
            "DEPOT_SLOT": 1,
            "DEPOT_OUTBOUND": 2,
            "YARD_REBALANCE": 3,
            "FUNCTION_LINE_SERVICE": 4,
            "PRE_REPAIR_STAGING": 5,
            "DISPATCH_SHED_QUEUE": 6,
            "SPECIAL_REPAIR_PROCESS": 7,
        }.get(candidate.action_family, 9)
        return (0, family_priority, candidate_sort_key(candidate))
    if not phase_state.link7_open and candidate_touches_depot(candidate):
        return (2, 0, candidate_sort_key(candidate))
    h1_priority = {
        "FUNCTION_LINE_SERVICE": 0,
        "PRE_REPAIR_STAGING": 1,
        "DISPATCH_SHED_QUEUE": 2,
        "SPECIAL_REPAIR_PROCESS": 3,
        "YARD_REBALANCE": 4,
    }.get(candidate.action_family, 8)
    return (0, h1_priority, candidate_sort_key(candidate))


def action_family(source_line: str, target_line: str, has_weigh: bool) -> str:
    if has_weigh:
        return "SPECIAL_REPAIR_PROCESS"
    if source_line in DEPOT_LINES and target_line not in DEPOT_TARGET_LINES:
        return "DEPOT_OUTBOUND"
    if target_line in DEPOT_LINES:
        return "REPAIR_INBOUND"
    if target_line in DEPOT_OUTSIDE_LINES:
        return "DEPOT_SLOT"
    if target_line == "预修线":
        return "PRE_REPAIR_STAGING"
    if target_line in {"调梁棚", "调梁线北"}:
        return "DISPATCH_SHED_QUEUE"
    if target_line in {"洗罐站", "洗罐线北", "油漆线", "抛丸线", "卸轮线", "机走棚", "机库线", "机走北"}:
        return "FUNCTION_LINE_SERVICE"
    return "YARD_REBALANCE"


def pull_equivalent(cars: list[dict[str, Any]]) -> int:
    return sum(4 if bool(car.get("IsHeavy")) else 1 for car in cars)


def reverse_available_length(line: str) -> float:
    spec = TRACK_SPECS.get(line)
    return REVERSAL_DISTANCE_M.get(line, spec.length_m if spec else 0.0)


def reverse_length_fits(source_line: str, target_line: str, batch: list[dict[str, Any]]) -> bool:
    train_length = sum(car_length(car) for car in batch) + LOCO_LENGTH_M
    for line in {source_line, target_line}:
        available = reverse_available_length(line)
        if available and train_length > available + 1e-6:
            return False
    return True


def cars_by_line(cars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in cars:
        grouped[car["Line"]].append(car)
    for line_cars in grouped.values():
        line_cars.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    return dict(grouped)


def line_priority(line: str, target_line: str) -> tuple[int, str, str]:
    if line in DEPOT_LINES and target_line not in DEPOT_TARGET_LINES:
        return (0, line, target_line)
    if target_line not in DEPOT_TARGET_LINES:
        return (1, line, target_line)
    return (2, line, target_line)


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
        after_length = target_length_after_move(target_line, cars, batch, batch_nos, length_load_lookup)
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


def final_capacity_infeasible_reasons(cars: list[dict[str, Any]]) -> list[str]:
    required_length: Counter[str] = Counter()
    for car in cars:
        targets = [line for line in target_lines(car) if line and line not in DEPOT_LINES]
        if len(targets) != 1:
            continue
        required_length[targets[0]] += car_length(car)

    reasons: list[str] = []
    for line, length in sorted(required_length.items()):
        spec = TRACK_SPECS.get(line)
        if not spec or line in DEPOT_LINES:
            continue
        if length > spec.length_m + LINE_LENGTH_TOLERANCE_M:
            reasons.append(f"target_final_capacity_infeasible:{line}:{length:.1f}>{spec.length_m:.1f}")
    return reasons


def free_line_positions(cars: list[dict[str, Any]], line: str, excluded_nos: set[str] | None = None) -> list[int]:
    excluded_nos = excluded_nos or set()
    occupied = {
        int(car.get("Position") or 0)
        for car in cars
        if car["Line"] == line and car_no(car) not in excluded_nos
    }
    positions: list[int] = []
    candidate = 1
    while len(positions) < 80:
        if candidate not in occupied:
            positions.append(candidate)
        candidate += 1
    return positions


def first_free_positions_for_batch(
    cars: list[dict[str, Any]],
    line: str,
    batch: list[dict[str, Any]],
    excluded_nos: set[str] | None = None,
) -> dict[str, int]:
    excluded_nos = excluded_nos or set()
    positions = free_line_positions(cars, line, excluded_nos)
    return {car_no(car): positions[index] for index, car in enumerate(batch)}


def choose_staging_line(
    cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    excluded_lines: set[str],
    length_load_lookup: dict[str, float] | None = None,
) -> str:
    batch_nos = {car_no(car) for car in batch}
    batch_length = sum(car_length(car) for car in batch)
    candidates: list[tuple[int, float, str]] = []
    for priority, line in enumerate(STAGING_LINE_PRIORITY):
        if line in excluded_lines or line not in TRACK_SPECS:
            continue
        spec = TRACK_SPECS[line]
        if spec.track_type not in {"storage", "temporary"}:
            continue
        if length_load_lookup is None:
            current_load = line_length_load(cars, line, batch_nos)
        else:
            current_load = length_load_lookup.get(line, 0.0)
            for car in batch:
                if car["Line"] == line:
                    current_load -= car_length(car)
        remaining = spec.length_m - current_load
        if batch_length <= remaining + LINE_LENGTH_TOLERANCE_M:
            candidates.append((priority, -remaining, line))
    if not candidates:
        return ""
    return min(candidates)[2]


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


def target_suffix_release_batch(
    cars: list[dict[str, Any]],
    target_line: str,
    start_position: int,
    batch_nos: set[str],
    depot_assignment: DepotAssignment,
) -> list[dict[str, Any]]:
    suffix = [
        car
        for car in cars
        if car["Line"] == target_line
        and car_no(car) not in batch_nos
        and int(car.get("Position") or 0) >= start_position
        and not is_locked_depot_stayer(car, depot_assignment)
    ]
    suffix.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    release_batch: list[dict[str, Any]] = []
    for car in suffix:
        if pull_equivalent([*release_batch, car]) > PULL_LIMIT_EQUIVALENT:
            break
        release_batch.append(car)
    return release_batch


def is_locked_depot_stayer(car: dict[str, Any], depot_assignment: DepotAssignment) -> bool:
    slot = depot_assignment.slots.get(car_no(car))
    return bool(
        slot
        and slot.locked
        and car["Line"] == slot.line
        and int(car.get("Position") or 0) == slot.position
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
) -> HookCandidate:
    move_nos = tuple(car_no(car) for car in batch)
    has_weigh = any(bool(car.get("IsWeigh")) for car in batch)
    return HookCandidate(
        case_id=case_id,
        hook_index=hook_index,
        candidate_id=(
            f"{case_id}:P10:{hook_index}:{candidate_kind}:"
            f"{source_line}->{target_line}:{','.join(move_nos)}"
        ),
        source_line=source_line,
        target_line=target_line,
        move_car_nos=move_nos,
        action_family=action_family(source_line, target_line, has_weigh),
        train_length_m=round(sum(car_length(car) for car in batch), 3),
        pull_equivalent_count=pull_equivalent(batch),
        has_weigh=has_weigh,
        planned_positions=planned_positions,
        generation_reason=generation_reason,
        candidate_kind=candidate_kind,
    )


def direct_candidate_for_batch(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    generation_reason: str,
    length_load_lookup: dict[str, float] | None = None,
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> HookCandidate | None:
    if not batch:
        return None
    if not reverse_length_fits(source_line, target_line, batch):
        return None
    batch_nos = {car_no(car) for car in batch}
    planned_positions = planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
        grouped=grouped,
    )
    if len(planned_positions) != len(batch):
        return None
    if not candidate_positions_available(target_line, planned_positions, cars, batch_nos, grouped):
        return None
    if not line_has_length_capacity(target_line, cars, batch, batch_nos, length_load_lookup, grouped):
        return None
    return hook_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=target_line,
        batch=batch,
        planned_positions=planned_positions,
        generation_reason=generation_reason,
        candidate_kind="target_move",
    )


def depot_same_line_repack_candidate(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    generation_reason: str,
    grouped: dict[str, list[dict[str, Any]]] | None = None,
) -> HookCandidate | None:
    if not batch or line not in DEPOT_LINES:
        return None
    if not reverse_length_fits(line, line, batch):
        return None
    batch_nos = {car_no(car) for car in batch}
    planned_positions = planned_positions_for_batch(
        batch=batch,
        target_line=line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
        grouped=grouped,
    )
    if len(planned_positions) != len(batch):
        return None
    if not candidate_positions_available(line, planned_positions, cars, batch_nos, grouped):
        return None
    return hook_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=line,
        target_line=line,
        batch=batch,
        planned_positions=planned_positions,
        generation_reason=generation_reason,
        candidate_kind="depot_same_line_repack",
    )


def staging_candidate_for_batch(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    preferred_target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    reason: str,
    candidate_kind: str,
    length_load_lookup: dict[str, float] | None = None,
) -> HookCandidate | None:
    if not batch:
        return None
    staging_line = choose_staging_line(cars, batch, {source_line, preferred_target_line}, length_load_lookup)
    if not staging_line:
        return None
    if not reverse_length_fits(source_line, staging_line, batch):
        return None
    batch_nos = {car_no(car) for car in batch}
    planned_positions = first_free_positions_for_batch(cars, staging_line, batch, batch_nos)
    return hook_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=staging_line,
        batch=batch,
        planned_positions=planned_positions,
        generation_reason=reason,
        candidate_kind=candidate_kind,
    )


def candidate_sort_key(candidate: HookCandidate) -> tuple[int, tuple[int, str, str], int, str]:
    kind_priority = {
        "target_move": 10,
        "depot_same_line_repack": 15,
        "same_line_stage_out": 20,
        "capacity_release_to_staging": 30,
        "spot_release_to_staging": 40,
        "blocker_relocation": 50,
    }.get(candidate.candidate_kind, 90)
    if candidate.action_family == "DEPOT_OUTBOUND":
        kind_priority -= 5
    return (
        kind_priority,
        line_priority(candidate.source_line, candidate.target_line),
        -len(candidate.move_car_nos),
        candidate.candidate_id,
    )


def build_candidates(
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> tuple[list[HookCandidate], list[CandidateAuditRow]]:
    direct_candidates: list[HookCandidate] = []
    release_candidates: list[HookCandidate] = []
    staging_candidates: list[HookCandidate] = []
    blocked_rows: list[CandidateAuditRow] = []
    grouped = cars_by_line(cars)
    load_lookup = line_loads(cars)
    length_load_lookup_cache: dict[str, float] | None = None
    satisfied_nos = {car_no(car) for car in cars if car_is_satisfied(car, depot_assignment)}
    planned_cache: dict[str, tuple[str, int | None, str]] = {}

    def planned(car: dict[str, Any]) -> tuple[str, int | None, str]:
        no = car_no(car)
        if no not in planned_cache:
            planned_cache[no] = planned_target_for_car(car, cars, depot_assignment, load_lookup)
        return planned_cache[no]

    def satisfied(car: dict[str, Any]) -> bool:
        return car_no(car) in satisfied_nos

    def length_load_lookup() -> dict[str, float]:
        nonlocal length_load_lookup_cache
        if length_load_lookup_cache is None:
            length_load_lookup_cache = {line: float(length) for line, length in line_length_loads(cars).items()}
        return length_load_lookup_cache

    all_unsatisfied = [car for car in cars if not satisfied(car)]
    if 0 < len(all_unsatisfied) <= 6:
        def tail_key(item: dict[str, Any]) -> tuple[int, str, tuple[int, ...], str, int, str]:
            target_line, _position, _reason = planned(item)
            return (
                0 if item["Line"] != target_line else 1,
                0 if force_positions(item) else 1,
                target_line,
                force_positions(item),
                item["Line"],
                int(item.get("Position") or 0),
                car_no(item),
            )

        for car in sorted(all_unsatisfied, key=tail_key):
            target_line, _position, target_reason = planned(car)
            if not target_line:
                blocked_rows.append(
                    blocked_candidate_row(
                        case_id=case_id,
                        hook_index=hook_index,
                        line=car["Line"],
                        target_line="",
                        move_cars=(car_no(car),),
                        reason=target_reason,
                    )
                )
                continue
            if car["Line"] == target_line:
                force_group = force_positions(car)
                stage_batch = [car]
                if force_group:
                    line_cars = grouped.get(target_line, [])
                    start_position = int(car.get("Position") or 0)
                    stage_batch = []
                    for line_car in line_cars:
                        if int(line_car.get("Position") or 0) < start_position:
                            continue
                        group_target, _group_position, _group_reason = planned(line_car)
                        if group_target != target_line or force_positions(line_car) != force_group:
                            if stage_batch:
                                break
                            continue
                        if pull_equivalent([*stage_batch, line_car]) > PULL_LIMIT_EQUIVALENT:
                            break
                        stage_batch.append(line_car)
                    if not stage_batch:
                        stage_batch = [car]
                if target_line in DEPOT_LINES:
                    repack = depot_same_line_repack_candidate(
                        case_id=case_id,
                        hook_index=hook_index,
                        line=target_line,
                        batch=stage_batch,
                        cars=cars,
                        depot_assignment=depot_assignment,
                        generation_reason=(
                            f"tail_depot_same_line_repack;vehicle={car_no(car)};"
                            f"target_line={target_line};batch_count={len(stage_batch)}"
                        ),
                        grouped=grouped,
                    )
                    if repack:
                        return [repack], blocked_rows
                stage = None
                while stage_batch and stage is None:
                    stage = staging_candidate_for_batch(
                        case_id=case_id,
                        hook_index=hook_index,
                        source_line=car["Line"],
                        preferred_target_line=target_line,
                        batch=stage_batch,
                        cars=cars,
                        reason=(
                            f"tail_same_line_spot_closeout;vehicle={car_no(car)};"
                            f"target_line={target_line};batch_count={len(stage_batch)}"
                        ),
                        candidate_kind="same_line_stage_out",
                        length_load_lookup=length_load_lookup(),
                    )
                    if stage is None:
                        stage_batch = stage_batch[:-1]
                if stage:
                    return [stage], blocked_rows
            force_group = force_positions(car)
            if force_group:
                group_batch: list[dict[str, Any]] = []
                for group_car in sorted(all_unsatisfied, key=lambda item: (item["Line"], int(item.get("Position") or 0), car_no(item))):
                    group_target, _group_position, _group_reason = planned(group_car)
                    if group_target != target_line or force_positions(group_car) != force_group:
                        continue
                    if group_car["Line"] != car["Line"]:
                        continue
                    if pull_equivalent([*group_batch, group_car]) > PULL_LIMIT_EQUIVALENT:
                        break
                    group_batch.append(group_car)
                if len(group_batch) > 1:
                    direct_group = direct_candidate_for_batch(
                        case_id=case_id,
                        hook_index=hook_index,
                        source_line=car["Line"],
                        target_line=target_line,
                        batch=group_batch,
                        cars=cars,
                        depot_assignment=depot_assignment,
                        generation_reason=(
                            f"tail_force_group_closeout;target_line={target_line};"
                            f"force_positions={','.join(str(item) for item in force_group)};"
                            f"batch_count={len(group_batch)}"
                        ),
                        grouped=grouped,
                    )
                    if direct_group:
                        return [direct_group], blocked_rows
            direct = direct_candidate_for_batch(
                case_id=case_id,
                hook_index=hook_index,
                source_line=car["Line"],
                target_line=target_line,
                batch=[car],
                cars=cars,
                depot_assignment=depot_assignment,
                generation_reason=(
                    f"tail_direct_closeout;vehicle={car_no(car)};target_reason={target_reason}"
                ),
                grouped=grouped,
            )
            if direct:
                return [direct], blocked_rows

    for line, line_cars in sorted(grouped.items()):
        line_unsatisfied = [car for car in line_cars if not satisfied(car)]
        if not line_unsatisfied:
            continue
        first_unsatisfied = line_unsatisfied[0]
        blocking = [
            car
            for car in line_cars
            if int(car.get("Position") or 0) < int(first_unsatisfied.get("Position") or 0)
            and not satisfied(car)
        ]
        front_satisfied = [
            car
            for car in line_cars
            if int(car.get("Position") or 0) < int(first_unsatisfied.get("Position") or 0)
            and satisfied(car)
        ]
        if blocking or front_satisfied:
            movable_blockers = [
                car
                for car in [*blocking, *front_satisfied]
                if not is_locked_depot_stayer(car, depot_assignment)
            ]
            blocker_batch: list[dict[str, Any]] = []
            for car in movable_blockers:
                if pull_equivalent([*blocker_batch, car]) > PULL_LIMIT_EQUIVALENT:
                    break
                blocker_batch.append(car)
            relocation = None
            while blocker_batch and relocation is None:
                relocation = staging_candidate_for_batch(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=line,
                    preferred_target_line="",
                    batch=blocker_batch,
                    cars=cars,
                    reason=(
                        "source_front_blocker_relocation;"
                        f"blocked_first_unsatisfied={car_no(first_unsatisfied)};"
                        f"blocker_count={len(blocker_batch)}"
                    ),
                    candidate_kind="blocker_relocation",
                    length_load_lookup=length_load_lookup(),
                )
                if relocation is None:
                    blocker_batch = blocker_batch[:-1]
            if relocation:
                staging_candidates.append(relocation)
            else:
                blocked_rows.append(
                    blocked_candidate_row(
                        case_id=case_id,
                        hook_index=hook_index,
                        line=line,
                        target_line="",
                        move_cars=tuple(car_no(car) for car in line_unsatisfied),
                        reason="source_front_blocked_by_satisfied_or_lower_position_cars",
                    )
                )
            continue

        target_line, _position, target_reason = planned(first_unsatisfied)
        if not target_line:
            blocked_rows.append(
                blocked_candidate_row(
                    case_id=case_id,
                    hook_index=hook_index,
                    line=line,
                    target_line="",
                    move_cars=(car_no(first_unsatisfied),),
                    reason=target_reason,
                )
            )
            continue

        batch: list[dict[str, Any]] = []
        for car in line_cars:
            if satisfied(car):
                break
            planned_line, _planned_position, planned_reason = planned(car)
            if planned_line != target_line:
                break
            if car.get("IsWeigh") and batch:
                break
            if any(item.get("IsWeigh") for item in batch):
                break
            if pull_equivalent([*batch, car]) > PULL_LIMIT_EQUIVALENT:
                break
            if not planned_line:
                blocked_rows.append(
                    blocked_candidate_row(
                        case_id=case_id,
                        hook_index=hook_index,
                        line=line,
                        target_line="",
                        move_cars=(car_no(car),),
                        reason=planned_reason,
                    )
                )
                break
            batch.append(car)

        if not batch:
            continue
        batch_nos = {car_no(car) for car in batch}
        planned_positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )

        if line == target_line:
            stage_batch: list[dict[str, Any]] = []
            for car in line_cars:
                if satisfied(car):
                    break
                planned_line, _planned_position, _planned_reason = planned(car)
                if planned_line != target_line:
                    break
                if pull_equivalent([*stage_batch, car]) > PULL_LIMIT_EQUIVALENT:
                    break
                stage_batch.append(car)
            if not stage_batch:
                stage_batch = batch[:1]
            if target_line in DEPOT_LINES:
                repack = depot_same_line_repack_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    line=target_line,
                    batch=stage_batch,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    generation_reason=(
                        f"depot_same_line_repack;vehicle={car_no(first_unsatisfied)};"
                        f"target_line={target_line};batch_count={len(stage_batch)}"
                    ),
                    grouped=grouped,
                )
                if repack:
                    direct_candidates.append(repack)
                    continue
            stage = None
            while stage_batch and stage is None:
                stage = staging_candidate_for_batch(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=line,
                    preferred_target_line=target_line,
                    batch=stage_batch,
                    cars=cars,
                    reason=(
                        f"same_line_reposition_stage_out;vehicle={car_no(first_unsatisfied)};"
                        f"target_line={target_line};batch_count={len(stage_batch)}"
                    ),
                    candidate_kind="same_line_stage_out",
                    length_load_lookup=length_load_lookup(),
                )
                if stage is None:
                    stage_batch = stage_batch[:-1]
            if stage:
                staging_candidates.append(stage)
            continue

        direct_batch = list(batch)
        while direct_batch:
            direct = direct_candidate_for_batch(
                case_id=case_id,
                hook_index=hook_index,
                source_line=line,
                target_line=target_line,
                batch=direct_batch,
                cars=cars,
                depot_assignment=depot_assignment,
                generation_reason=(
                    f"first_accessible_unsatisfied_car={car_no(first_unsatisfied)};"
                    f"target_reason={target_reason};batch_prefix_count={len(direct_batch)}"
                ),
                grouped=grouped,
            )
            if direct:
                direct_candidates.append(direct)
                break
            direct_batch = direct_batch[:-1]

        positions = set(planned_positions.values())
        occupants = target_position_occupants(cars, target_line, positions, batch_nos)
        releasable_occupants = [
            car
            for car in occupants
            if not is_locked_depot_stayer(car, depot_assignment)
        ]
        for occupant in sorted(releasable_occupants, key=lambda item: (int(item.get("Position") or 0), car_no(item))):
            occupant_target, _occupant_position, _occupant_reason = planned(occupant)
            blocked_position = int(occupant.get("Position") or 0)
            incoming_forced = any(force_positions(car) and blocked_position in force_positions(car) for car in batch)
            release_batch = (
                target_suffix_release_batch(cars, target_line, blocked_position, batch_nos, depot_assignment)
                if incoming_forced
                else [occupant]
            )
            if occupant_target and occupant_target != target_line:
                release = direct_candidate_for_batch(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=target_line,
                    target_line=occupant_target,
                    batch=release_batch,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    generation_reason=(
                        "target_spot_release_to_target;"
                        f"blocked_target={target_line};blocked_position={blocked_position};"
                        f"released_vehicle={car_no(occupant)};release_batch_count={len(release_batch)}"
                    ),
                    grouped=grouped,
                )
                if release:
                    release_candidates.append(release)
                    break
            release_stage = staging_candidate_for_batch(
                case_id=case_id,
                hook_index=hook_index,
                source_line=target_line,
                preferred_target_line=line,
                batch=release_batch,
                cars=cars,
                reason=(
                    "target_spot_release_to_staging;"
                    f"blocked_target={target_line};blocked_position={blocked_position};"
                    f"released_vehicle={car_no(occupant)};release_batch_count={len(release_batch)}"
                ),
                candidate_kind="spot_release_to_staging",
                length_load_lookup=length_load_lookup(),
            )
            if release_stage:
                release_candidates.append(release_stage)
                break

        if not line_has_length_capacity(
            target_line,
            cars,
            batch[:1],
            {car_no(batch[0])},
            length_load_lookup(),
        ):
            target_cars = [
                car
                for car in grouped.get(target_line, [])
                if not satisfied(car)
                and not is_locked_depot_stayer(car, depot_assignment)
            ]
            for release_car in sorted(target_cars, key=lambda item: (int(item.get("Position") or 0), car_no(item))):
                release_target, _release_position, _release_reason = planned(release_car)
                release_batch = [release_car]
                if release_target and release_target != target_line:
                    release = direct_candidate_for_batch(
                        case_id=case_id,
                        hook_index=hook_index,
                        source_line=target_line,
                        target_line=release_target,
                        batch=release_batch,
                        cars=cars,
                        depot_assignment=depot_assignment,
                        generation_reason=(
                            "target_capacity_release_to_target;"
                            f"target_line={target_line};released_vehicle={car_no(release_car)}"
                        ),
                        grouped=grouped,
                    )
                    if release:
                        release_candidates.append(release)
                        break
                release_stage = staging_candidate_for_batch(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=target_line,
                    preferred_target_line=line,
                    batch=release_batch,
                    cars=cars,
                    reason=(
                        "target_capacity_release_to_staging;"
                        f"target_line={target_line};released_vehicle={car_no(release_car)}"
                    ),
                    candidate_kind="capacity_release_to_staging",
                    length_load_lookup=length_load_lookup(),
                )
                if release_stage:
                    release_candidates.append(release_stage)
                    break

    unique: dict[str, HookCandidate] = {}
    for candidate in [*direct_candidates, *release_candidates, *staging_candidates]:
        unique.setdefault(candidate.candidate_id, candidate)
    return sorted(unique.values(), key=candidate_sort_key)[:MAX_CANDIDATES_PER_ROUND], blocked_rows


def blocked_candidate_row(
    case_id: str,
    hook_index: int,
    line: str,
    target_line: str,
    move_cars: tuple[str, ...],
    reason: str,
) -> CandidateAuditRow:
    return CandidateAuditRow(
        case_id=case_id,
        hook_index=hook_index,
        candidate_id=f"{case_id}:P10:{hook_index}:{line}:blocked",
        candidate_status="blocked",
        source_line=line,
        target_line=target_line,
        action_family="",
        move_car_count=len(move_cars),
        move_cars="|".join(move_cars),
        train_length_m=0.0,
        pull_equivalent_count=0,
        has_weigh=False,
        get_route_exists=False,
        put_route_exists=False,
        hard_violation_count=1,
        hard_violation_reasons=reason,
        generation_reason=reason,
        get_path="",
        weigh_path="",
        put_path="",
    )


class PhysicalValidator:
    def __init__(self, graph: TrackGraph) -> None:
        self.graph = graph

    def validate(
        self,
        candidate: HookCandidate,
        cars: list[dict[str, Any]],
        loco_location: LocoLocation,
    ) -> PhysicalValidation:
        reasons: list[str] = []
        by_no = {car_no(car): car for car in cars}
        batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
        target_line_cars = [car for car in cars if car["Line"] == candidate.target_line]
        occupied_lines = occupied_lines_for_route(cars, set(candidate.move_car_nos))
        get_path = self.graph.route_avoiding_occupied(loco_location.node, candidate.source_line, occupied_lines)
        get_static_path = self.graph.route(loco_location.node, candidate.source_line)
        source_location = route_end_location(get_path, candidate.source_line) if get_path else LocoLocation(
            line=candidate.source_line,
            node=line_end_node(candidate.source_line, "North"),
        )
        if candidate.has_weigh:
            raw_weigh_path = self.graph.route_avoiding_occupied(source_location.node, WEIGH_LINE, occupied_lines)
            weigh_static_path = self.graph.route(source_location.node, WEIGH_LINE)
            weigh_path = route_with_line_prefix(
                candidate.source_line,
                raw_weigh_path,
            )
            weigh_location = route_end_location(weigh_path, WEIGH_LINE) if weigh_path else LocoLocation(
                line=WEIGH_LINE,
                node=line_end_node(WEIGH_LINE, "North"),
            )
            raw_put_path = self.graph.route_avoiding_occupied(weigh_location.node, candidate.target_line, occupied_lines)
            put_static_path = self.graph.route(weigh_location.node, candidate.target_line)
            put_path = route_with_line_prefix(
                WEIGH_LINE,
                raw_put_path,
            )
        else:
            weigh_path = []
            weigh_static_path = []
            raw_put_path = self.graph.route_avoiding_occupied(source_location.node, candidate.target_line, occupied_lines)
            put_static_path = self.graph.route(source_location.node, candidate.target_line)
            put_path = route_with_line_prefix(
                candidate.source_line,
                raw_put_path,
            )

        if not get_path:
            reasons.append("get_route_blocked_by_occupied_line" if get_static_path else "get_route_missing")
        if candidate.has_weigh and not weigh_path:
            reasons.append("weigh_route_blocked_by_occupied_line" if weigh_static_path else "weigh_route_missing")
        if not put_path:
            reasons.append("put_route_blocked_by_occupied_line" if put_static_path else "put_route_missing")
        if candidate.source_line in RUNNING_LINES or candidate.target_line in RUNNING_LINES:
            reasons.append("running_line_stop_violation")
        if candidate.source_line not in TRACK_SPECS:
            reasons.append("source_line_unknown")
        if candidate.target_line not in TRACK_SPECS:
            reasons.append("target_line_unknown")
        if candidate.source_line == candidate.target_line and not (
            candidate.candidate_kind == "depot_same_line_repack"
            and candidate.target_line in DEPOT_LINES
        ):
            reasons.append("same_line_reposition_requires_staging_search")
        if candidate.pull_equivalent_count > PULL_LIMIT_EQUIVALENT:
            reasons.append("pull_limit_violation")
        if sum(1 for car in batch if car.get("IsWeigh")) > 1:
            reasons.append("single_hook_multi_weigh_violation")
        if candidate.has_weigh and batch and not batch[-1].get("IsWeigh"):
            reasons.append("weigh_car_not_last_in_carry_order")

        train_with_loco = candidate.train_length_m + LOCO_LENGTH_M
        for line in {candidate.source_line, candidate.target_line}:
            available = REVERSAL_DISTANCE_M.get(line, TRACK_SPECS.get(line, TrackSpec(line, 0, "")).length_m)
            if available and train_with_loco > available + 1e-6:
                reasons.append(f"reverse_length_violation:{line}:{train_with_loco:.1f}>{available:.1f}")

        position_reasons = self._validate_target_positions(candidate, cars, batch, target_line_cars)
        reasons.extend(position_reasons)
        reasons.extend(self._validate_closed_door(candidate, batch))

        return PhysicalValidation(
            accepted=not reasons,
            reasons=tuple(reasons),
            get_path=tuple(get_path),
            weigh_path=tuple(weigh_path),
            put_path=tuple(put_path),
        )

    def _validate_target_positions(
        self,
        candidate: HookCandidate,
        cars: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        target_line_cars: list[dict[str, Any]],
    ) -> list[str]:
        reasons: list[str] = []
        batch_nos = {car_no(car) for car in batch}
        occupied_positions = {
            int(car.get("Position") or 0)
            for car in target_line_cars
            if car_no(car) not in batch_nos
        }
        planned_positions = list(candidate.planned_positions.values())
        if len(planned_positions) != len(set(planned_positions)):
            reasons.append("target_position_collision_inside_batch")
        for no, position in candidate.planned_positions.items():
            if position in occupied_positions:
                reasons.append(f"target_position_occupied:{candidate.target_line}:{position}:{no}")

        if candidate.target_line in DEPOT_LINES:
            capacity = 7 if max(planned_positions or [0]) > 5 else 5
            for car in batch:
                position = candidate.planned_positions.get(car_no(car), 0)
                if not slot_allowed_for_car(car, candidate.target_line, position, capacity):
                    reasons.append(f"depot_slot_rule_violation:{car_no(car)}:{candidate.target_line}:{position}")
        else:
            spec = TRACK_SPECS.get(candidate.target_line)
            if spec:
                existing_length = sum(
                    car_length(car)
                    for car in target_line_cars
                    if car_no(car) not in batch_nos
                )
                after_length = existing_length + candidate.train_length_m
                if after_length > spec.length_m + LINE_LENGTH_TOLERANCE_M:
                    reasons.append(
                        f"target_line_length_violation:{candidate.target_line}:{after_length:.1f}>{spec.length_m:.1f}"
                    )
                if spec.track_type == "temporary" and candidate.candidate_kind not in STAGING_CANDIDATE_KINDS:
                    reasons.append(f"temporary_line_final_target_violation:{candidate.target_line}")
        return reasons

    def _validate_closed_door(self, candidate: HookCandidate, batch: list[dict[str, Any]]) -> list[str]:
        if not any(car.get("IsClosedDoor") for car in batch):
            return []
        reasons: list[str] = []
        if candidate.target_line == "存4线":
            for car in batch:
                if car.get("IsClosedDoor") and candidate.planned_positions.get(car_no(car), 999) <= 3:
                    reasons.append(f"closed_door_cun4_front_position_violation:{car_no(car)}")
        else:
            first = batch[0] if batch else None
            if first and first.get("IsClosedDoor") and (len(batch) > 10 or any(car.get("IsHeavy") for car in batch)):
                reasons.append(f"closed_door_first_car_violation:{car_no(first)}")
        return reasons


def candidate_audit_row(
    candidate: HookCandidate,
    validation: PhysicalValidation,
    status: str,
) -> CandidateAuditRow:
    return CandidateAuditRow(
        case_id=candidate.case_id,
        hook_index=candidate.hook_index,
        candidate_id=candidate.candidate_id,
        candidate_status=status,
        source_line=candidate.source_line,
        target_line=candidate.target_line,
        action_family=candidate.action_family,
        move_car_count=len(candidate.move_car_nos),
        move_cars="|".join(candidate.move_car_nos),
        train_length_m=candidate.train_length_m,
        pull_equivalent_count=candidate.pull_equivalent_count,
        has_weigh=candidate.has_weigh,
        get_route_exists=bool(validation.get_path),
        put_route_exists=bool(validation.put_path),
        hard_violation_count=len(validation.reasons),
        hard_violation_reasons="|".join(validation.reasons),
        generation_reason=candidate.generation_reason,
        get_path="|".join(validation.get_path),
        weigh_path="|".join(validation.weigh_path),
        put_path="|".join(validation.put_path),
    )


def operation_rows(
    candidate: HookCandidate,
    validation: PhysicalValidation,
    start_operation_index: int,
) -> list[OperationTraceRow]:
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
        weigh_car = next(iter([no for no in candidate.move_car_nos if no]), "")
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
) -> None:
    move_nos = set(candidate.move_car_nos)
    for car in cars:
        no = car_no(car)
        if no not in move_nos:
            continue
        car["Line"] = candidate.target_line
        car["Position"] = candidate.planned_positions[no]
    if candidate.source_line != candidate.target_line:
        compact_source_positions(cars, candidate.source_line, move_nos)
    if candidate.target_line not in DEPOT_LINES:
        normalize_duplicate_positions(cars, candidate.target_line)


def compact_source_positions(cars: list[dict[str, Any]], source_line: str, moved_nos: set[str]) -> None:
    remaining = [car for car in cars if car["Line"] == source_line and car_no(car) not in moved_nos]
    remaining.sort(key=lambda item: (int(item.get("Position") or 0), car_no(item)))
    for position, car in enumerate(remaining, start=1):
        car["Position"] = position


def normalize_duplicate_positions(cars: list[dict[str, Any]], line: str) -> None:
    line_cars = [car for car in cars if car["Line"] == line]
    seen: set[int] = set()
    next_position = 1
    for car in sorted(line_cars, key=lambda item: (int(item.get("Position") or 0), car_no(item))):
        position = int(car.get("Position") or 0)
        if position > 0 and position not in seen:
            seen.add(position)
            continue
        while next_position in seen:
            next_position += 1
        car["Position"] = next_position
        seen.add(next_position)


def state_signature(
    cars: list[dict[str, Any]],
    loco_location: LocoLocation,
) -> tuple[str, str, tuple[tuple[str, str, int], ...]]:
    return (
        loco_location.line,
        loco_location.node,
        tuple(
            (car_no(car), car["Line"], int(car.get("Position") or 0))
            for car in sorted(cars, key=lambda item: car_no(item))
        ),
    )


def no_progress_limit(remaining_unsatisfied_count: int) -> int:
    if remaining_unsatisfied_count <= 0:
        return 0
    if remaining_unsatisfied_count <= 2:
        return 64
    if remaining_unsatisfied_count <= 6:
        return 48
    return max(MAX_NO_PROGRESS_HOOKS, min(96, remaining_unsatisfied_count * 6))


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


def remote_interaction_metrics(operations: list[OperationTraceRow]) -> tuple[int, int, int]:
    rows_by_batch: dict[int, list[OperationTraceRow]] = defaultdict(list)
    for row in operations:
        rows_by_batch[row.hook_index].append(row)

    cross_count = 0
    batch_count = 0
    session_count = 0
    previous_touched_remote = False
    for hook_index in sorted(rows_by_batch):
        rows = rows_by_batch[hook_index]
        get_row = next((row for row in rows if row.action == "Get"), rows[0])
        put_row = next((row for row in rows if row.action == "Put"), rows[-1])
        source_is_remote = get_row.line in REMOTE_INTERACTION_LINES
        target_is_remote = put_row.line in REMOTE_INTERACTION_LINES
        touched_remote = source_is_remote or target_is_remote
        if touched_remote:
            batch_count += 1
            if not previous_touched_remote:
                session_count += 1
        if source_is_remote != target_is_remote:
            cross_count += 1
        previous_touched_remote = touched_remote
    return cross_count, batch_count, session_count


def better_case_result(left: CaseSummaryRow, right: CaseSummaryRow) -> bool:
    status_rank = {"completed": 0, "blocked": 1, "invalid_input": 2}
    left_key = (
        status_rank.get(left.status, 9),
        left.final_unsatisfied_vehicle_count,
        left.hard_physical_violation_accepted_count,
        left.business_get_put_hook_count,
        left.remote_interaction_cross_count,
        left.remote_interaction_batch_count,
        left.internal_move_batch_count,
    )
    right_key = (
        status_rank.get(right.status, 9),
        right.final_unsatisfied_vehicle_count,
        right.hard_physical_violation_accepted_count,
        right.business_get_put_hook_count,
        right.remote_interaction_cross_count,
        right.remote_interaction_batch_count,
        right.internal_move_batch_count,
    )
    return left_key < right_key


def run_case(
    truth_path: Path,
    output_dir: Path,
    graph: TrackGraph,
    max_hooks: int,
) -> tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str]]:
    case_id = case_id_from_path(truth_path)
    payload = read_json(truth_path)
    input_ok, input_errors = validate_input(payload)
    normalized_cars = [normalized_car(car) for car in payload.get("StartStatus") or []]
    capacities = terminal_capacity_by_line(payload)

    base_assignment = build_depot_assignment([dict(car) for car in normalized_cars], capacities)
    clustered_assignment = build_short_direct_depot_assignment(
        [dict(car) for car in normalized_cars],
        capacities,
        base_assignment,
    )
    strategies: list[tuple[str, DepotAssignment, bool]] = [
        ("phase_gate_base", base_assignment, False),
        ("early_depot_base", base_assignment, True),
    ]
    if is_short_direct_depot_variant(case_id):
        strategies = [("short_direct_cluster", clustered_assignment, True)]
    elif (
        len(clustered_assignment.failures) <= len(base_assignment.failures)
        and len(clustered_assignment.slots) >= len(base_assignment.slots)
        and depot_source_fragmentation_score(normalized_cars, clustered_assignment)
        < depot_source_fragmentation_score(normalized_cars, base_assignment)
    ):
        strategies.append(("phase_gate_cluster", clustered_assignment, False))
        strategies.append(("early_depot_cluster", clustered_assignment, True))

    results: list[tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str]]] = []
    for strategy_name, depot_assignment, short_direct_override in strategies:
        results.append(
            _run_case_once(
                case_id=case_id,
                payload=payload,
                input_ok=input_ok,
                input_errors=input_errors,
                normalized_cars=normalized_cars,
                output_dir=output_dir,
                graph=graph,
                max_hooks=max_hooks,
                depot_assignment=depot_assignment,
                solve_strategy=strategy_name,
                short_direct_override=short_direct_override,
            )
        )

    best = results[0]
    for result in results[1:]:
        if better_case_result(result[0], best[0]):
            best = result
    summary, candidate_rows, operation_rows_for_case, rejection_reasons = best
    if summary.response_path:
        selected_response_path = output_dir / "responses" / f"{case_id}.json"
        source_response_path = Path(summary.response_path)
        if source_response_path.exists():
            write_json(selected_response_path, read_json(source_response_path))
            summary = replace(summary, response_path=str(selected_response_path))
    return summary, candidate_rows, operation_rows_for_case, rejection_reasons


def _run_case_once(
    *,
    case_id: str,
    payload: dict[str, Any],
    input_ok: bool,
    input_errors: list[str],
    normalized_cars: list[dict[str, Any]],
    output_dir: Path,
    graph: TrackGraph,
    max_hooks: int,
    depot_assignment: DepotAssignment,
    solve_strategy: str,
    short_direct_override: bool,
) -> tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str]]:
    cars = [dict(car) for car in normalized_cars]
    validator = PhysicalValidator(graph)
    loco = payload.get("locoNode") or {}
    loco_location = initial_loco_location(loco)
    initial_unsatisfied = len(unsatisfied_cars(cars, depot_assignment))
    initial_non_depot_unsatisfied = non_depot_unsatisfied_count(cars, depot_assignment)
    candidate_rows: list[CandidateAuditRow] = []
    operations: list[OperationTraceRow] = []
    rejection_reasons: Counter[str] = Counter()
    accepted_count = 0
    rejected_count = 0
    blocked_count = 0
    state_loop_count = 0
    blocked_reason = ""
    visited = {state_signature(cars, loco_location)}

    if not input_ok:
        blocked_reason = "|".join(input_errors)
    hook_index = 1
    current_unsatisfied = unsatisfied_cars(cars, depot_assignment) if input_ok else []
    best_unsatisfied_count = len(current_unsatisfied)
    best_non_depot_unsatisfied_count = initial_non_depot_unsatisfied
    no_progress_hooks = 0
    last_hook_touched_remote = False
    remote_streak_count = 0
    while input_ok and hook_index <= max_hooks and current_unsatisfied:
        best_non_depot_unsatisfied_count = min(
            best_non_depot_unsatisfied_count,
            non_depot_unsatisfied_count(cars, depot_assignment),
        )
        phase_state = phase_state_for_case(
            case_id=case_id,
            cars=cars,
            depot_assignment=depot_assignment,
            hook_index=hook_index,
            initial_non_depot_unsatisfied_count=initial_non_depot_unsatisfied,
            best_non_depot_unsatisfied_count=best_non_depot_unsatisfied_count,
            short_direct_override=short_direct_override,
        )
        candidates, blocked_rows = build_candidates(case_id, hook_index, cars, depot_assignment)
        candidates = sorted(candidates, key=lambda candidate: phase_candidate_sort_key(candidate, phase_state))
        candidate_rows.extend(blocked_rows)
        blocked_count += len(blocked_rows)
        for row in blocked_rows:
            rejection_reasons[row.hard_violation_reasons] += 1

        accepted_this_round = False
        physically_accepted: list[tuple[HookCandidate, PhysicalValidation, str]] = []
        for candidate in candidates:
            validation = validator.validate(candidate, cars, loco_location)
            if validation.accepted:
                phase_reason = phase_reject_reason(candidate, validation, phase_state)
                physically_accepted.append((candidate, validation, phase_reason))
                continue

            candidate_rows.append(candidate_audit_row(candidate, validation, "rejected"))
            rejected_count += 1
            for reason in validation.reasons:
                rejection_reasons[reason] += 1

        selected_transition: tuple[
            HookCandidate,
            PhysicalValidation,
            list[dict[str, Any]],
            LocoLocation,
            tuple[str, str, tuple[tuple[str, str, int], ...]],
            str,
        ] | None = None
        phase_deferred: list[tuple[HookCandidate, PhysicalValidation, str]] = []
        transition_cache: dict[str, tuple[list[dict[str, Any]], LocoLocation, tuple[str, str, tuple[tuple[str, str, int], ...]]]] = {}

        def transition_for(
            candidate: HookCandidate,
            validation: PhysicalValidation,
        ) -> tuple[list[dict[str, Any]], LocoLocation, tuple[str, str, tuple[tuple[str, str, int], ...]]]:
            cached = transition_cache.get(candidate.candidate_id)
            if cached is not None:
                return cached
            prospective_cars = [dict(car) for car in cars]
            apply_candidate(candidate, prospective_cars)
            next_loco_location = operation_stand_location(validation.put_path, candidate.target_line)
            signature = state_signature(prospective_cars, next_loco_location)
            transition_cache[candidate.candidate_id] = (prospective_cars, next_loco_location, signature)
            return prospective_cars, next_loco_location, signature

        def remote_continuity_key(
            item: tuple[HookCandidate, PhysicalValidation, str],
        ) -> tuple[int, tuple[int, int, tuple[int, tuple[int, str, str], int, str]]]:
            candidate, _validation, _phase_reason = item
            continue_remote_session = (
                last_hook_touched_remote
                and remote_streak_count < REMOTE_CONTINUITY_MAX_STREAK
            )
            continuity_penalty = (
                0
                if not continue_remote_session or candidate_touches_remote_interaction(candidate)
                else 1
            )
            return (continuity_penalty, phase_candidate_sort_key(candidate, phase_state))

        for candidate, validation, phase_reason in sorted(physically_accepted, key=remote_continuity_key):
            if phase_reason:
                phase_deferred.append((candidate, validation, phase_reason))
                continue
            prospective_cars, next_loco_location, signature = transition_for(candidate, validation)
            if signature in visited:
                loop_validation = PhysicalValidation(
                    accepted=False,
                    reasons=("state_signature_loop",),
                    get_path=validation.get_path,
                    weigh_path=validation.weigh_path,
                    put_path=validation.put_path,
                )
                candidate_rows.append(candidate_audit_row(candidate, loop_validation, "rejected"))
                rejected_count += 1
                rejection_reasons["state_signature_loop"] += 1
                continue
            selected_transition = (candidate, validation, prospective_cars, next_loco_location, signature, "")
            break

        if selected_transition is None and phase_deferred:
            for candidate, validation, phase_reason in sorted(phase_deferred, key=remote_continuity_key):
                prospective_cars, next_loco_location, signature = transition_for(candidate, validation)
                if signature in visited:
                    loop_validation = PhysicalValidation(
                        accepted=False,
                        reasons=("state_signature_loop",),
                        get_path=validation.get_path,
                        weigh_path=validation.weigh_path,
                        put_path=validation.put_path,
                    )
                    candidate_rows.append(candidate_audit_row(candidate, loop_validation, "rejected"))
                    rejected_count += 1
                    rejection_reasons["state_signature_loop"] += 1
                    continue
                forced_reason = (
                    "phase_link7_forced_open_no_front_candidate:"
                    f"deferred_reason={phase_reason}"
                )
                rejection_reasons[forced_reason] += 1
                selected_transition = (
                    candidate,
                    validation,
                    prospective_cars,
                    next_loco_location,
                    signature,
                    forced_reason,
                )
                break

        if selected_transition is not None:
            candidate, validation, prospective_cars, next_loco_location, signature, forced_reason = selected_transition
            candidate_rows.append(candidate_audit_row(candidate, validation, "accepted"))
            accepted_count += 1
            rows = operation_rows(candidate, validation, len(operations) + 1)
            operations.extend(rows)
            cars = prospective_cars
            loco_location = next_loco_location
            last_hook_touched_remote = candidate_touches_remote_interaction(candidate)
            remote_streak_count = remote_streak_count + 1 if last_hook_touched_remote else 0
            visited.add(signature)
            current_unsatisfied = unsatisfied_cars(cars, depot_assignment)
            best_non_depot_unsatisfied_count = min(
                best_non_depot_unsatisfied_count,
                non_depot_unsatisfied_count(cars, depot_assignment),
            )
            if len(current_unsatisfied) < best_unsatisfied_count:
                best_unsatisfied_count = len(current_unsatisfied)
                no_progress_hooks = 0
            else:
                no_progress_hooks += 1
            if current_unsatisfied and no_progress_hooks >= no_progress_limit(len(current_unsatisfied)):
                blocked_reason = "stagnant_no_progress"
            accepted_this_round = True
            hook_index += 1

        if blocked_reason:
            break
        if accepted_this_round:
            continue
        capacity_reasons = final_capacity_infeasible_reasons(cars)
        if capacity_reasons:
            blocked_reason = "|".join(capacity_reasons)
            for reason in capacity_reasons:
                rejection_reasons[reason] += 1
            break
        if not candidates:
            blocked_reason = "no_runtime_candidate_generated"
        else:
            blocked_reason = "all_runtime_candidates_rejected"
        break

    if input_ok and hook_index > max_hooks and current_unsatisfied:
        blocked_reason = "max_hook_limit_reached"

    final_unsatisfied = len(current_unsatisfied) if input_ok else len(unsatisfied_cars(cars, depot_assignment))
    status = "completed" if input_ok and final_unsatisfied == 0 and not blocked_reason else "blocked"
    if not input_ok:
        status = "invalid_input"

    response_path = ""
    if input_ok and operations:
        response_dir = output_dir / "responses" / "_strategy"
        response_path = str(response_dir / f"{case_id}_{solve_strategy}.json")
        response = {
            "Success": status == "completed",
            "Message": "" if status == "completed" else blocked_reason,
            "StatusCode": 200 if status == "completed" else 409,
            "Data": {
                "Operations": [response_operation(row) for row in operations],
                "GeneratedEndStatus": [
                    {"No": car_no(car), "Line": car["Line"], "Position": int(car.get("Position") or 0)}
                    for car in sorted(cars, key=lambda item: car_no(item))
                ],
            },
        }
        write_json(Path(response_path), response)

    unknown_route_count = sum(
        1
        for row in candidate_rows
        if row.candidate_status == "rejected"
        and ("get_route_missing" in row.hard_violation_reasons or "put_route_missing" in row.hard_violation_reasons)
    )
    hard_accepted = sum(
        1 for row in candidate_rows if row.candidate_status == "accepted" and row.hard_violation_count
    )
    get_operation_count = sum(1 for row in operations if row.action == "Get")
    put_operation_count = sum(1 for row in operations if row.action == "Put")
    weigh_operation_count = sum(1 for row in operations if row.action == "Weigh")
    business_get_put_hook_count = get_operation_count + put_operation_count
    remote_cross_count, remote_batch_count, remote_session_count = remote_interaction_metrics(operations)
    summary = CaseSummaryRow(
        case_id=case_id,
        solve_strategy=solve_strategy,
        status=status,
        input_schema_passed=input_ok,
        vehicle_count=len(cars),
        initial_unsatisfied_vehicle_count=initial_unsatisfied,
        final_unsatisfied_vehicle_count=final_unsatisfied,
        business_get_put_hook_count=business_get_put_hook_count,
        internal_move_batch_count=accepted_count,
        interface_operation_count=len(operations),
        get_operation_count=get_operation_count,
        put_operation_count=put_operation_count,
        weigh_operation_count=weigh_operation_count,
        remote_interaction_cross_count=remote_cross_count,
        remote_interaction_batch_count=remote_batch_count,
        remote_interaction_session_count=remote_session_count,
        generated_hook_count=accepted_count,
        generated_operation_count=len(operations),
        accepted_candidate_count=accepted_count,
        rejected_candidate_count=rejected_count,
        blocked_candidate_count=blocked_count,
        hard_physical_violation_accepted_count=hard_accepted,
        unknown_route_count=unknown_route_count,
        depot_slot_failure_count=len(depot_assignment.failures),
        state_loop_count=state_loop_count,
        blocked_reason=blocked_reason,
        response_path=response_path,
    )
    return summary, candidate_rows, operations, rejection_reasons


def build_summary(case_rows: list[CaseSummaryRow], rejection_reasons: Counter[str]) -> PhysicalRuntimeSummary:
    status_counts = Counter(row.status for row in case_rows)
    blocked_reason_counts = Counter(row.blocked_reason for row in case_rows if row.blocked_reason)
    return PhysicalRuntimeSummary(
        truth_case_count=len(case_rows),
        completed_case_count=status_counts["completed"],
        blocked_case_count=status_counts["blocked"],
        invalid_input_case_count=status_counts["invalid_input"],
        total_vehicle_count=sum(row.vehicle_count for row in case_rows),
        total_initial_unsatisfied_vehicle_count=sum(row.initial_unsatisfied_vehicle_count for row in case_rows),
        total_final_unsatisfied_vehicle_count=sum(row.final_unsatisfied_vehicle_count for row in case_rows),
        business_get_put_hook_count=sum(row.business_get_put_hook_count for row in case_rows),
        internal_move_batch_count=sum(row.internal_move_batch_count for row in case_rows),
        interface_operation_count=sum(row.interface_operation_count for row in case_rows),
        get_operation_count=sum(row.get_operation_count for row in case_rows),
        put_operation_count=sum(row.put_operation_count for row in case_rows),
        weigh_operation_count=sum(row.weigh_operation_count for row in case_rows),
        remote_interaction_cross_count=sum(row.remote_interaction_cross_count for row in case_rows),
        remote_interaction_batch_count=sum(row.remote_interaction_batch_count for row in case_rows),
        remote_interaction_session_count=sum(row.remote_interaction_session_count for row in case_rows),
        generated_hook_count=sum(row.generated_hook_count for row in case_rows),
        generated_operation_count=sum(row.generated_operation_count for row in case_rows),
        accepted_candidate_count=sum(row.accepted_candidate_count for row in case_rows),
        rejected_candidate_count=sum(row.rejected_candidate_count for row in case_rows),
        blocked_candidate_count=sum(row.blocked_candidate_count for row in case_rows),
        hard_physical_violation_accepted_count=sum(row.hard_physical_violation_accepted_count for row in case_rows),
        unknown_route_count=sum(row.unknown_route_count for row in case_rows),
        depot_slot_failure_count=sum(row.depot_slot_failure_count for row in case_rows),
        state_loop_count=sum(row.state_loop_count for row in case_rows),
        status_counts=dict(sorted(status_counts.items())),
        rejection_reason_counts=dict(sorted(rejection_reasons.items())),
        blocked_reason_counts=dict(sorted(blocked_reason_counts.items())),
    )


def gap_bucket(reason: str) -> tuple[str, str, str]:
    if reason == "same_line_reposition_requires_staging_search":
        return (
            "SAME_LINE_REPOSITION_NEEDS_STAGING",
            "same-line reordering is not executable without temporary staging",
            "StagingSearch + CarryOrderPlanner",
        )
    if reason.startswith("target_line_length_violation:"):
        return (
            "TARGET_CAPACITY_NEEDS_RELEASE_OR_SPLIT",
            "target track has no remaining effective parking length for the proposed batch",
            "CapacityAwareCandidateGenerator + ReleaseMoveSearch",
        )
    if reason.startswith("target_final_capacity_infeasible:"):
        return (
            "TARGET_FINAL_CAPACITY_INFEASIBLE",
            "requested final target occupancy exceeds the documented physical track length",
            "InputPhysicalConsistencyGate",
        )
    if reason.startswith("target_position_occupied:"):
        return (
            "TARGET_POSITION_OCCUPIED_NEEDS_SLOT_SWAP",
            "target spot is occupied and requires swap/evacuation before inbound",
            "DepotSlotGraph + SpotSwapDelta",
        )
    if reason == "target_position_collision_inside_batch":
        return (
            "BATCH_POSITION_ASSIGNMENT_NEEDS_ORDERED_SPOTS",
            "batch cars cannot share forced spot positions",
            "OrderedSpotAllocator",
        )
    if reason == "source_front_blocked_by_satisfied_or_lower_position_cars":
        return (
            "SOURCE_FRONT_BLOCKER_NEEDS_RELOCATION",
            "north/front cars block access to the desired prefix",
            "SourceBlockerRelocationSearch",
        )
    if reason == "no_feasible_depot_slot":
        return (
            "DEPOT_SLOT_ASSIGNMENT_INCOMPLETE",
            "current depot slot allocator cannot find a legal slot",
            "DepotSlotGraph + LockedTailPolicy",
        )
    if "route_missing" in reason:
        return (
            "TRACK_GRAPH_ROUTE_GAP",
            "static track graph lacks a usable path",
            "TrackGraphCalibration",
        )
    return (
        "OTHER_PHYSICAL_REJECT",
        "physical validator rejected candidate for another reason",
        "PhysicalValidatorAudit",
    )


def build_gap_rows(
    candidate_rows: list[CandidateAuditRow],
    case_rows: list[CaseSummaryRow] | None = None,
) -> list[GapSummaryRow]:
    counts: Counter[str] = Counter()
    cases: dict[str, set[str]] = defaultdict(set)
    reasons_by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    bucket_meta: dict[str, tuple[str, str]] = {}

    def add_reason(case_id: str, reason: str) -> None:
        if not reason:
            return
        bucket, blocker, component = gap_bucket(reason)
        counts[bucket] += 1
        if case_id:
            cases[bucket].add(case_id)
        reasons_by_bucket[bucket][reason] += 1
        bucket_meta[bucket] = (blocker, component)

    for row in candidate_rows:
        if not row.hard_violation_reasons:
            continue
        for reason in row.hard_violation_reasons.split("|"):
            add_reason(row.case_id, reason)
    for row in case_rows or []:
        if not row.blocked_reason:
            continue
        for reason in row.blocked_reason.split("|"):
            add_reason(row.case_id, reason)
    rows: list[GapSummaryRow] = []
    for bucket, count in counts.most_common():
        blocker, component = bucket_meta[bucket]
        examples = [
            f"{reason}({reason_count})"
            for reason, reason_count in reasons_by_bucket[bucket].most_common(5)
        ]
        rows.append(
            GapSummaryRow(
                gap_bucket=bucket,
                record_count=count,
                case_count=len(cases[bucket]),
                accepted_blocker=blocker,
                next_required_component=component,
                example_reasons="|".join(examples),
            )
        )
    return rows


def write_readme(output_dir: Path, summary: PhysicalRuntimeSummary) -> None:
    text = f"""# P10 Physical Runtime Trace

This artifact is a first executable skeleton for Runtime Move Generator + Physical Validator.

It reads interface-shaped truth2 JSON and emits API-shaped operation responses. It is not the final optimizer and must not be used as evidence that the full solver already exceeds manual plans.

## Current Result

| metric | value |
|---|---:|
| truth_case_count | {summary.truth_case_count} |
| completed_case_count | {summary.completed_case_count} |
| blocked_case_count | {summary.blocked_case_count} |
| invalid_input_case_count | {summary.invalid_input_case_count} |
| total_initial_unsatisfied_vehicle_count | {summary.total_initial_unsatisfied_vehicle_count} |
| total_final_unsatisfied_vehicle_count | {summary.total_final_unsatisfied_vehicle_count} |
| business_get_put_hook_count | {summary.business_get_put_hook_count} |
| internal_move_batch_count | {summary.internal_move_batch_count} |
| interface_operation_count | {summary.interface_operation_count} |
| get_operation_count | {summary.get_operation_count} |
| put_operation_count | {summary.put_operation_count} |
| weigh_operation_count | {summary.weigh_operation_count} |
| remote_interaction_cross_count | {summary.remote_interaction_cross_count} |
| remote_interaction_batch_count | {summary.remote_interaction_batch_count} |
| remote_interaction_session_count | {summary.remote_interaction_session_count} |
| generated_hook_count_legacy_internal_batch | {summary.generated_hook_count} |
| generated_operation_count_legacy_interface_ops | {summary.generated_operation_count} |
| hard_physical_violation_accepted_count | {summary.hard_physical_violation_accepted_count} |
| unknown_route_count | {summary.unknown_route_count} |
| depot_slot_failure_count | {summary.depot_slot_failure_count} |

## Files

- `case_summary.csv`: one row per truth2 case. Use `business_get_put_hook_count` when comparing against manual hook counts; `generated_hook_count` is a legacy internal move-batch counter.
- `candidate_physical_audit.csv`: generated, blocked, rejected, and accepted physical candidates.
- `operation_trace.csv`: API operation-level trace.
- `physical_gap_summary.csv`: rejection reasons grouped into implementable solver gaps.
- `physical_runtime_summary.json`: aggregate counters.
- `responses/*.json`: generated API-shaped responses for cases with at least one accepted hook.

## Scope

Implemented hard checks:

- interface field presence
- route existence over a static switch graph
- no stop on running lines
- pull equivalent limit: empty=1, heavy=4, max=20
- basic reverse length with loco length 15m included
- line length capacity for non-depot targets
- depot slot length/process/force-position constraints
- single-hook weigh limit and basic weigh path through `机库线`
- basic closed-door ordering checks
- accepted candidates must have zero hard physical violations

Known non-final parts:

- generator is a conservative prefix-access generator, not a full searcher
- no blocker relocation search yet
- no mixed carry/drop sequence search yet
- depot swap is slot allocation plus validation, not full `DepotSwapDelta`
- switch locking and time-window resources are not modeled yet

## Next Implementation Order

1. Implement source blocker relocation and temporary staging search.
2. Implement capacity-aware target release and split batching.
3. Replace greedy depot slot allocation with a real `DepotSlotGraph + SpotSwapDelta`.
4. Add ordered carry/drop sequence search for forced spots and same-line repositioning.
5. Wire this P10 validator behind P7/P8 so invalid physical moves are rejected before optimization.
"""
    (output_dir / "README.md").write_text(text, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    truth_dir = root / args.truth_dir
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    graph = TrackGraph()

    truth_paths = sorted(path for path in truth_dir.glob("*.json") if path.name != "conversion_summary.json")
    if args.case_id:
        wanted = {case.upper() for case in args.case_id}
        truth_paths = [path for path in truth_paths if case_id_from_path(path) in wanted]

    all_case_rows: list[CaseSummaryRow] = []
    all_candidate_rows: list[CandidateAuditRow] = []
    all_operation_rows: list[OperationTraceRow] = []
    rejection_reasons: Counter[str] = Counter()

    for truth_path in truth_paths:
        case_row, candidate_rows, operation_rows_for_case, case_reasons = run_case(
            truth_path=truth_path,
            output_dir=output_dir,
            graph=graph,
            max_hooks=args.max_hooks,
        )
        all_case_rows.append(case_row)
        all_candidate_rows.extend(candidate_rows)
        all_operation_rows.extend(operation_rows_for_case)
        rejection_reasons.update(case_reasons)

    summary = build_summary(all_case_rows, rejection_reasons)
    gap_rows = build_gap_rows(all_candidate_rows, all_case_rows)
    write_csv(output_dir / "case_summary.csv", [asdict(row) for row in all_case_rows])
    write_csv(output_dir / "candidate_physical_audit.csv", [asdict(row) for row in all_candidate_rows])
    write_csv(output_dir / "operation_trace.csv", [asdict(row) for row in all_operation_rows])
    write_csv(output_dir / "physical_gap_summary.csv", [asdict(row) for row in gap_rows])
    write_json(output_dir / "physical_runtime_summary.json", asdict(summary))
    write_readme(output_dir, summary)

    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    print(f"Wrote {output_dir}")

    if args.check:
        errors: list[str] = []
        if not all_case_rows:
            errors.append("no truth2 case processed")
        if summary.hard_physical_violation_accepted_count:
            errors.append("accepted candidate has hard physical violation")
        if summary.unknown_route_count:
            errors.append("some generated candidates have unknown routes")
        if errors:
            for error in errors:
                print(f"CHECK_FAILED: {error}")
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate first P10 physical runtime trace from interface-shaped truth2 input.",
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--truth-dir", default="data/truth2")
    parser.add_argument("--output-dir", default="artifacts/physical_runtime_trace")
    parser.add_argument("--max-hooks", type=int, default=300)
    parser.add_argument("--case-id", nargs="*")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
