#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from validate_phase_gates import (
    PHASE_ALLOWED_CANDIDATE_FAMILIES,
    case_id_from_path,
    infer_manual_phase_audit,
    normalize_line,
    parse_manual_hooks,
    phase_for_manual_step,
    try_case_id_from_path,
    write_csv,
)


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
REMOTE_CORRIDOR_LINES = {"机走棚", "预修线", "存4线"}
SHORT_DIRECT_DEPOT_CASE_IDS = {"0213W", "0306W", "0327W"}
RUNNING_LINES = {"联6", "联7"} | {f"渡{index}" for index in range(1, 14)}
WEIGH_LINE = "机库线"
STAGING_CANDIDATE_KINDS = {
    "blocker_relocation",
    "capacity_release_to_staging",
    "same_line_stage_out",
    "spot_release_to_staging",
}
REMOTE_SESSION_CONTRACT_KINDS = {
    "remote_session_contract_planlet",
    "remote_corridor_planlet",
    "depot_digest_planlet",
    "depot_slot_swap_planlet",
    "short_chain_planlet",
    "route_clear_session_planlet",
    "source_exit_session_planlet",
    "corridor_closeout_planlet",
}
OWNER_BOUND_RECOVERY_KINDS = {
    "front_capacity_release_contract_planlet",
    "front_capacity_digest_planlet",
    "remote_session_contract_planlet",
    "remote_corridor_planlet",
    "depot_digest_planlet",
    "depot_slot_swap_planlet",
    "short_chain_planlet",
    "route_clear_session_planlet",
    "source_exit_session_planlet",
    "corridor_closeout_planlet",
}
OWNERLESS_RECOVERY_KINDS = {
    "capacity_release_to_staging",
    "spot_release_to_staging",
    "blocker_relocation",
    "same_line_stage_out",
    "source_clear_restore_planlet",
    "source_exit_clear_restore_planlet",
    "route_clear_restore_planlet",
}
REMOTE_CROSS_OWNER_KINDS = REMOTE_SESSION_CONTRACT_KINDS | {
    "remote_exchange_planlet",
    "remote_session_planlet",
}
REMOTE_CROSS_OWNER_FAMILIES = {
    "REPAIR_INBOUND",
    "DEPOT_OUTBOUND",
    "DEPOT_SLOT",
    "SPECIAL_REPAIR_PROCESS",
}
FRONT_SERVICE_TARGET_LINES = {
    "洗罐站",
    "洗罐线北",
    "油漆线",
    "抛丸线",
    "调梁棚",
    "调梁线北",
    "预修线",
    "机走棚",
    "机走北",
}
ENABLE_SPOTTING_WINDOW_VALIDATION = True
SPOTTING_LINE_TOTAL_POSITIONS = {
    "调梁棚": 11,
    "洗罐站": 7,
    "油漆线": 9,
    "抛丸线": 3,
}
TAIL_CANDIDATE_PREFIXES = (
    "tail_direct_closeout",
    "tail_force_group_closeout",
    "tail_depot_same_line_repack",
    "tail_same_line_spot_closeout",
)
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
DEPOT_STAGING_LINE_PRIORITY = (
    "存4线",
    "存4南",
    "存2线",
    "存3线",
    "存5线南",
    "存1线",
    "存5线北",
    "调梁线北",
    "机走北",
    "洗罐线北",
    "机北1",
    "机北2",
    "机南",
    "洗油北",
)
MANUAL_REMOTE_PREFIXES = ("修1", "修2", "修3", "修4", "卸轮")
SHORT_CHAIN_VARIANTS = {"DIRECT_REPAIR_ENTRY", "DEPOT_DIGEST_ONLY", "MIXED_SIGNAL_REPAIR"}
STANDARD_PHASE_ORDER = ("H1", "H2", "H3", "H4", "H5")
DIAGNOSTIC_STRATEGY_MODE = "diagnostic"
STANDARD_STRATEGY_MODE = "standard"
AUDIT_REPAIR_MODE = "audit"
ALIGNED_REPAIR_MODE = "aligned"


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
class PlanStep:
    action: str
    line: str
    move_car_nos: tuple[str, ...]
    planned_positions: dict[str, int] = field(default_factory=dict)


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
class PhaseState:
    active_phase: str
    active_variant: str
    link7_open: bool
    non_depot_unsatisfied_count: int
    initial_non_depot_unsatisfied_count: int
    front_service_progress: float
    current_front_service_progress: float


@dataclass(frozen=True)
class StrategyConfig:
    staging_priority: tuple[str, ...] = STAGING_LINE_PRIORITY
    depot_aware_staging: bool = False
    enable_contract_planlets: bool = False
    prefer_contract_planlets: bool = False
    enable_remote_session_contracts: bool = False
    front_debt_first: bool = False
    enable_front_capacity_contracts: bool = False


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
    remote_business_transition_count: int
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
    remote_business_transition_count: int
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


@dataclass(frozen=True)
class ManualBaseline:
    case_id: str
    source_path: str
    observed_hook_count: int
    soft_hook_upper_bound: int
    variant: str
    first_remote_hook: int
    remote_hook_count: int
    remote_session_count: int
    remote_business_transition_count: int
    audit: Any


@dataclass(frozen=True)
class RuntimePhaseTraceRow:
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    business_hook_index_end: int
    candidate_id: str
    source_line: str
    target_line: str
    action_family: str
    candidate_kind: str
    manual_variant: str
    manual_observed_hook_count: int
    expected_h_phase: str
    runtime_phase: str
    runtime_variant: str
    phase_permission: str
    phase_permission_reason: str
    over_manual_hook_bound: bool
    link7_open: bool
    crosses_link7: bool
    touches_remote: bool
    touches_depot: bool
    front_service_progress: float
    current_front_service_progress: float
    non_depot_unsatisfied_count: int
    initial_non_depot_unsatisfied_count: int
    forced_phase_open_reason: str


@dataclass(frozen=True)
class ContractTraceRow:
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    candidate_id: str
    selected_contract: str
    structural_intent: str
    source_line: str
    target_line: str
    owner_vehicle_count: int
    owner_vehicles: str
    hard_obligations: str
    protections: str
    target_contract_reason: str
    suppressed_contracts: str
    unsatisfied_before: int
    unsatisfied_after: int
    unsatisfied_reduction: int
    non_depot_unsatisfied_before: int
    non_depot_unsatisfied_after: int
    non_depot_unsatisfied_reduction: int


@dataclass(frozen=True)
class ResourceDeltaTraceRow:
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    candidate_id: str
    requested_resources: str
    acquired_resources: str
    released_resources: str
    blocked_resources: str
    resource_status: str
    source_line: str
    target_line: str
    get_path: str
    put_path: str
    crosses_link7: bool
    touches_remote: bool
    remote_session_id: int
    remote_cross: bool
    loco_start_line: str
    loco_start_node: str
    loco_end_line: str
    loco_end_node: str


@dataclass(frozen=True)
class CandidateDominanceAuditRow:
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    selected_candidate_id: str
    generated_candidate_count: int
    physically_accepted_count: int
    selected_rank: int
    selected_unsatisfied_reduction: int
    best_unsatisfied_reduction: int
    selected_non_depot_reduction: int
    best_non_depot_reduction: int
    selected_remote_cross: bool
    selected_touches_remote: bool
    dominated_by_candidate_id: str
    dominance_reason: str
    status: str


@dataclass(frozen=True)
class CandidatePoolAuditRow:
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    runtime_phase: str
    front_service_progress: float
    selected_candidate_id: str
    selected_candidate_kind: str
    selected_action_family: str
    selected_touches_remote: bool
    selected_phase_reason: str
    generated_candidate_count: int
    physically_accepted_count: int
    phase_permitted_count: int
    phase_permitted_front_count: int
    phase_permitted_remote_count: int
    best_front_candidate_id: str
    best_front_candidate_kind: str
    best_front_delta: int
    best_front_non_depot_delta: int
    best_front_business_hooks: int
    best_phase_candidate_id: str
    best_phase_candidate_kind: str
    best_phase_delta: int
    best_phase_non_depot_delta: int
    decision_gap: str


@dataclass(frozen=True)
class DepotSessionAuditRow:
    case_id: str
    solve_strategy: str
    remote_session_id: int
    session_batch_index: int
    hook_index: int
    business_hook_index_start: int
    candidate_id: str
    source_line: str
    target_line: str
    action_family: str
    remote_event: str
    remote_cross: bool
    move_car_count: int
    move_cars: str
    manual_variant: str


@dataclass(frozen=True)
class ManualVsSolverCaseCompareRow:
    case_id: str
    solve_strategy: str
    status: str
    manual_source_path: str
    manual_variant: str
    manual_hook_count: int
    manual_soft_hook_upper_bound: int
    solver_business_hook_count: int
    hook_delta: int
    hook_ratio: float
    solver_internal_move_batch_count: int
    manual_first_remote_hook: int
    solver_first_remote_business_hook: int
    manual_first_remote_ratio: float
    solver_first_remote_ratio: float
    manual_remote_hook_count: int
    solver_remote_business_hook_count: int
    solver_remote_batch_count: int
    manual_remote_session_count: int
    solver_remote_session_count: int
    solver_remote_cross_count: int
    manual_remote_business_transition_count: int
    solver_remote_business_transition_count: int
    remote_business_transition_delta: int
    blocked_reason: str


@dataclass(frozen=True)
class ShortChainDiagnosticRow:
    case_id: str
    solve_strategy: str
    status: str
    manual_variant: str
    manual_hook_count: int
    solver_business_hook_count: int
    hook_delta: int
    hook_acceptance_limit: int
    hook_within_short_chain_limit: bool
    remote_batch_count: int
    remote_cross_count: int
    remote_session_count: int
    first_remote_business_hook: int
    post_first_remote_non_remote_batch_count: int
    unnecessary_remote_cross_count: int
    required_component: str


@dataclass(frozen=True)
class CapacityConsistencyAuditRow:
    case_id: str
    status: str
    blocked_reason: str
    line: str
    required_length_m: float
    capacity_m: float
    excess_m: float
    required_fix: str


@dataclass(frozen=True)
class StructuralRepairAcceptanceRow:
    repair_item: str
    status: str
    current_value: str
    target_value: str
    evidence: str
    next_required_component: str


@dataclass(frozen=True)
class StructureWorkAuditRow:
    structure_item: str
    structure_name: str
    scope: str
    case_id: str
    solve_strategy: str
    hook_index: int
    business_hook_index_start: int
    business_hook_index_end: int
    candidate_id: str
    status: str
    current_value: str
    target_value: str
    gap_reason: str
    evidence: str
    next_required_component: str


@dataclass
class RuntimeDiagnosticRows:
    phase_rows: list[RuntimePhaseTraceRow] = field(default_factory=list)
    contract_rows: list[ContractTraceRow] = field(default_factory=list)
    resource_rows: list[ResourceDeltaTraceRow] = field(default_factory=list)
    dominance_rows: list[CandidateDominanceAuditRow] = field(default_factory=list)
    candidate_pool_rows: list[CandidatePoolAuditRow] = field(default_factory=list)
    depot_session_rows: list[DepotSessionAuditRow] = field(default_factory=list)

    def extend(self, other: "RuntimeDiagnosticRows") -> None:
        self.phase_rows.extend(other.phase_rows)
        self.contract_rows.extend(other.contract_rows)
        self.resource_rows.extend(other.resource_rows)
        self.dominance_rows.extend(other.dominance_rows)
        self.candidate_pool_rows.extend(other.candidate_pool_rows)
        self.depot_session_rows.extend(other.depot_session_rows)


class TrackGraph:
    def __init__(self) -> None:
        self._adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self._route_cache: dict[tuple[str, str], list[str]] = {}
        self._occupied_route_cache: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
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
        source = normalize_line(source_line)
        target = normalize_line(target_line)
        cache_key = (source, target, tuple(sorted(occupied_lines)))
        cached = self._occupied_route_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        route = self._route(source, target, occupied_lines=occupied_lines)
        self._occupied_route_cache[cache_key] = list(route)
        return route

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


def candidate_plan_steps(candidate: HookCandidate) -> tuple[PlanStep, ...]:
    if candidate.plan_steps:
        return candidate.plan_steps
    return (
        PlanStep("Get", candidate.source_line, candidate.move_car_nos),
        PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions),
    )


def candidate_final_line(candidate: HookCandidate) -> str:
    for step in reversed(candidate_plan_steps(candidate)):
        if step.action == "Put":
            return step.line
    return candidate.target_line


def planlet_business_hook_count(candidate: HookCandidate) -> int:
    return sum(1 for step in candidate_plan_steps(candidate) if step.action in {"Get", "Put"})


def planlet_line_sequence(candidate: HookCandidate) -> tuple[str, ...]:
    return tuple(step.line for step in candidate_plan_steps(candidate) if step.action in {"Get", "Put"})


def planlet_touches_remote(candidate: HookCandidate) -> bool:
    return any(line in REMOTE_INTERACTION_LINES for line in planlet_line_sequence(candidate))


def planlet_remote_cross_count(candidate: HookCandidate) -> int:
    lines = planlet_line_sequence(candidate)
    return sum(
        1
        for left, right in zip(lines, lines[1:])
        if (left in REMOTE_INTERACTION_LINES) != (right in REMOTE_INTERACTION_LINES)
    )


def candidate_remote_business_transition_count(
    candidate: HookCandidate,
    previous_business_hook_remote: bool | None = None,
) -> int:
    lines = planlet_line_sequence(candidate)
    if not lines:
        return 0
    transitions = planlet_remote_cross_count(candidate)
    first_remote = lines[0] in REMOTE_INTERACTION_LINES
    if previous_business_hook_remote is not None and first_remote != previous_business_hook_remote:
        transitions += 1
    return transitions


def remote_interaction_unsatisfied_count(
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> int:
    count = 0
    for car in unsatisfied_cars(cars, depot_assignment):
        target_line, _position, _reason = planned_target_for_car(
            car,
            cars,
            depot_assignment,
            line_loads(cars),
        )
        if car["Line"] in REMOTE_INTERACTION_LINES or target_line in REMOTE_INTERACTION_LINES:
            count += 1
    return count


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


def load_manual_baselines(root: Path, manual_dir: str) -> dict[str, ManualBaseline]:
    baselines: dict[str, ManualBaseline] = {}
    base_dir = root / manual_dir
    if not base_dir.exists():
        return baselines
    for path in sorted(base_dir.glob("*人工调车作业单/*.xlsx")):
        case_id = try_case_id_from_path(path)
        if not case_id:
            continue
        hooks = parse_manual_hooks(path)
        if not hooks:
            continue
        audit = infer_manual_phase_audit(hooks)
        audit.source_path = str(path)
        first_remote = 0
        remote_count = 0
        remote_sessions = 0
        remote_transitions = 0
        previous_remote: bool | None = None
        for hook in hooks:
            touched_remote = str(hook.line_raw or hook.line or "").startswith(MANUAL_REMOTE_PREFIXES)
            if touched_remote:
                remote_count += 1
                if not first_remote:
                    first_remote = hook.step
                if previous_remote is not True:
                    remote_sessions += 1
            if previous_remote is not None and touched_remote != previous_remote:
                remote_transitions += 1
            previous_remote = touched_remote
        current = baselines.get(case_id)
        if current and current.observed_hook_count <= len(hooks):
            continue
        baselines[case_id] = ManualBaseline(
            case_id=case_id,
            source_path=str(path),
            observed_hook_count=len(hooks),
            soft_hook_upper_bound=audit.soft_hook_upper_bound,
            variant=audit.variant,
            first_remote_hook=first_remote,
            remote_hook_count=remote_count,
            remote_session_count=remote_sessions,
            remote_business_transition_count=remote_transitions,
            audit=audit,
        )
    return baselines


def baseline_for_case(manual_baselines: dict[str, ManualBaseline] | None, case_id: str) -> ManualBaseline | None:
    if not manual_baselines:
        return None
    return manual_baselines.get(case_id.upper())


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


def is_spotting_line(line: str) -> bool:
    return ENABLE_SPOTTING_WINDOW_VALIDATION and line in SPOTTING_LINE_TOTAL_POSITIONS


def spotting_window_size(line: str, forced: tuple[int, ...]) -> int:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    if not total:
        return 0
    valid_positions = [position for position in set(forced) if 1 <= position <= total]
    if not valid_positions:
        return 0
    return total - min(valid_positions) + 1


def spotting_capacity(line: str, forced: tuple[int, ...]) -> int:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    if not total:
        return 0
    return len({position for position in forced if 1 <= position <= total})


def spotting_window_bounds(line: str, forced: tuple[int, ...]) -> list[tuple[int, int]]:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    capacity = spotting_capacity(line, forced)
    valid_positions = [position for position in set(forced) if 1 <= position <= total]
    if not total or not capacity or not valid_positions:
        return []
    first_position = min(valid_positions)
    last_start = total - capacity + 1
    if last_start < first_position:
        return []
    return [(start, start + capacity - 1) for start in range(first_position, last_start + 1)]


def spotting_effective_window(line: str, forced: tuple[int, ...], south_buffer_count: int) -> set[int]:
    width = spotting_window_size(line, forced)
    if not width:
        return set()
    south_offset = max(0, south_buffer_count)
    start = south_offset + 1
    return set(range(start, width + 1))


def spotting_effective_positions(line: str, forced: tuple[int, ...], south_buffer_count: int) -> list[int]:
    total = SPOTTING_LINE_TOTAL_POSITIONS.get(line, 0)
    return [
        total - south_rank + 1
        for south_rank in sorted(spotting_effective_window(line, forced, south_buffer_count))
    ]


def line_cars_sorted(cars: list[dict[str, Any]], line: str) -> list[dict[str, Any]]:
    return sorted(
        [car for car in cars if car["Line"] == line],
        key=lambda item: (int(item.get("Position") or 0), car_no(item)),
    )


def spotting_south_buffer_count(
    cars: list[dict[str, Any]],
    target_line: str,
    forced: tuple[int, ...],
    depot_assignment: DepotAssignment,
) -> int:
    count = 0
    for car in reversed(line_cars_sorted(cars, target_line)):
        no = car_no(car)
        if no in depot_assignment.slots:
            break
        if target_line not in (car.get("_TargetLineSet") or set(target_lines(car))):
            count += 1
            continue
        if force_positions(car) == forced:
            break
        count += 1
    return count


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
    capacity = spotting_capacity(target_line, forced)
    if not forced or not capacity:
        return {}
    existing_positions = spotting_same_forced_positions(
        cars,
        target_line,
        forced,
        depot_assignment,
        excluded_nos=batch_nos,
    )
    if len(existing_positions) + len(group) > capacity:
        return {}
    windows = sorted(spotting_window_bounds(target_line, forced), key=lambda item: (item[1], item[0]), reverse=True)
    for start, end in windows:
        if not all(start <= position <= end for position in existing_positions):
            continue
        free_positions = [
            position
            for position in range(end, start - 1, -1)
            if position not in occupied and position not in used
        ]
        if len(free_positions) < len(group):
            continue
        return {car_no(car): free_positions[index] for index, car in enumerate(group)}
    return {}


def spotting_group_is_acceptable(
    cars: list[dict[str, Any]],
    target_line: str,
    forced: tuple[int, ...],
    depot_assignment: DepotAssignment,
) -> bool:
    positions = spotting_same_forced_positions(cars, target_line, forced, depot_assignment)
    capacity = spotting_capacity(target_line, forced)
    if not positions or not capacity or len(positions) > capacity:
        return False
    if any(position <= 0 for position in positions):
        return False
    for start, end in spotting_window_bounds(target_line, forced):
        if all(start <= position <= end for position in positions):
            return True
    return False


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
    buffer_count = spotting_south_buffer_count(cars, target_line, forced, depot_assignment)
    position = int(car.get("Position") or 0)
    return position in spotting_effective_positions(target_line, forced, buffer_count)


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
        return target_line == slot.line and position == slot.position
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
        if no in depot_assignment.slots:
            position = depot_assignment.slots[no].position
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
        return car["Line"] == slot.line and int(car.get("Position") or 0) == slot.position
    targets = car.get("_TargetLineSet") or set(target_lines(car))
    if car["Line"] not in targets:
        return False
    forced = force_positions(car)
    if forced and cars is not None and is_spotting_line(car["Line"]):
        return spotting_position_is_acceptable(car, cars, car["Line"], depot_assignment)
    return not forced or int(car.get("Position") or 0) in forced


def unsatisfied_cars(cars: list[dict[str, Any]], depot_assignment: DepotAssignment) -> list[dict[str, Any]]:
    return [car for car in cars if not car_is_satisfied(car, depot_assignment, cars)]


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
    if candidate.plan_steps:
        return any(
            step.line in DEPOT_TARGET_LINES for step in candidate_plan_steps(candidate)
        ) or candidate.action_family in {"DEPOT_OUTBOUND", "REPAIR_INBOUND", "DEPOT_SLOT"}
    return (
        candidate.source_line in DEPOT_TARGET_LINES
        or candidate.target_line in DEPOT_TARGET_LINES
        or candidate.action_family in {"DEPOT_OUTBOUND", "REPAIR_INBOUND", "DEPOT_SLOT"}
    )


def candidate_touches_remote_interaction(candidate: HookCandidate) -> bool:
    if candidate.plan_steps:
        return planlet_touches_remote(candidate)
    return candidate.source_line in REMOTE_INTERACTION_LINES or candidate.target_line in REMOTE_INTERACTION_LINES


def candidate_is_front_service(candidate: HookCandidate) -> bool:
    if candidate_touches_remote_interaction(candidate):
        return False
    if candidate.action_family in {"FUNCTION_LINE_SERVICE", "DISPATCH_SHED_QUEUE", "PRE_REPAIR_STAGING"}:
        return True
    lines = planlet_line_sequence(candidate)
    return any(line in FRONT_SERVICE_TARGET_LINES for line in lines)


def validation_crosses_link7(validation: PhysicalValidation) -> bool:
    for path in (validation.get_path, validation.weigh_path, validation.put_path):
        if "联7" in route_for_output(path):
            return True
    return False


def phase_reject_reason(
    candidate: HookCandidate,
    validation: PhysicalValidation,
    phase_state: PhaseState,
    manual_baseline: ManualBaseline | None = None,
    business_hook_index: int = 0,
    repair_mode: str = AUDIT_REPAIR_MODE,
) -> str:
    if repair_mode == ALIGNED_REPAIR_MODE:
        expected_phase = manual_expected_phase(manual_baseline, business_hook_index)
        manual_variant = manual_baseline.variant if manual_baseline else ""
        if (
            manual_variant == "FULL_CHAIN_REPAIR"
            and expected_phase in {"H1", "H2"}
            and candidate_touches_remote_interaction(candidate)
        ):
            return (
                "phase_manual_defer_remote_too_early:"
                f"expected_phase={expected_phase}:manual_variant={manual_variant}"
            )
        if expected_phase in {"H1", "H2"} and candidate.action_family in {"REPAIR_INBOUND", "DEPOT_OUTBOUND", "DEPOT_SLOT"}:
            if manual_variant not in SHORT_CHAIN_VARIANTS:
                return (
                    "phase_manual_defer_action_family:"
                    f"expected_phase={expected_phase}:action_family={candidate.action_family}:"
                    f"manual_variant={manual_variant}"
                )
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
    priority_lines: tuple[str, ...] = STAGING_LINE_PRIORITY,
    depot_aware: bool = False,
) -> str:
    batch_nos = {car_no(car) for car in batch}
    batch_length = sum(car_length(car) for car in batch)
    candidates: list[tuple[int, float, str]] = []
    effective_priority = DEPOT_STAGING_LINE_PRIORITY if depot_aware and any(has_depot_target(car) for car in batch) else priority_lines
    for priority, line in enumerate(effective_priority):
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


def depot_locked_tail_positions(cars: list[dict[str, Any]], line: str, depot_assignment: DepotAssignment) -> set[int]:
    locked_positions = [
        int(car.get("Position") or 0)
        for car in cars
        if car["Line"] == line and is_locked_depot_stayer(car, depot_assignment)
    ]
    if not locked_positions:
        return set()
    tail_start = max(locked_positions)
    capacity = max([slot.position for slot in depot_assignment.slots.values() if slot.line == line] or [tail_start, 5])
    return set(range(tail_start, capacity + 1))


def depot_slot_hard_reasons(
    candidate: HookCandidate,
    cars: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
) -> list[str]:
    if candidate.target_line not in DEPOT_LINES:
        return []
    reasons: list[str] = []
    batch_nos = {car_no(car) for car in batch}
    by_no = {car_no(car): car for car in cars}
    locked_occupants = {
        int(car.get("Position") or 0): car
        for car in cars
        if car["Line"] == candidate.target_line and is_locked_depot_stayer(car, depot_assignment)
    }
    locked_tail = depot_locked_tail_positions(cars, candidate.target_line, depot_assignment)
    for no, position in candidate.planned_positions.items():
        car = by_no.get(no)
        assigned_slot = depot_assignment.slots.get(no)
        if assigned_slot and (assigned_slot.line != candidate.target_line or assigned_slot.position != position):
            reasons.append(
                f"depot_assigned_slot_mismatch:{no}:{candidate.target_line}:{position}:"
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
    return reasons


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
) -> HookCandidate:
    move_nos = tuple(car_no(car) for car in batch)
    has_weigh = any(bool(car.get("IsWeigh")) for car in batch)
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


def same_line_reorder_planlet(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    seed_batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    grouped: dict[str, list[dict[str, Any]]],
    generation_reason: str,
) -> HookCandidate | None:
    if not seed_batch or line in DEPOT_LINES:
        return None
    force_spots = sorted({position for car in seed_batch for position in force_positions(car)})
    if not force_spots:
        return None
    seed_positions = [int(car.get("Position") or 0) for car in seed_batch]
    window_start = min([*seed_positions, *force_spots])
    window_end = max([*seed_positions, *force_spots])
    window = [
        car
        for car in grouped.get(line, [])
        if window_start <= int(car.get("Position") or 0) <= window_end
    ]
    if not window or pull_equivalent(window) > PULL_LIMIT_EQUIVALENT:
        return None
    if not reverse_length_fits(line, line, window):
        return None

    window_nos = {car_no(car) for car in window}
    planned_positions = planned_positions_for_batch(
        batch=window,
        target_line=line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=window_nos,
        grouped=grouped,
    )
    if len(planned_positions) != len(window):
        return None
    if not candidate_positions_available(line, planned_positions, cars, window_nos, grouped):
        return None
    if all(int(car.get("Position") or 0) == planned_positions.get(car_no(car), 0) for car in window):
        return None

    steps = (
        PlanStep("Get", line, tuple(car_no(car) for car in window)),
        PlanStep("Put", line, tuple(car_no(car) for car in window), planned_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=line,
        target_line=line,
        batch=window,
        generation_reason=(
            f"{generation_reason};window={window_start}-{window_end};"
            f"vehicle_count={len(window)}"
        ),
        candidate_kind="same_line_reorder_planlet",
        steps=steps,
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
    staging_priority: tuple[str, ...] = STAGING_LINE_PRIORITY,
    depot_aware_staging: bool = False,
) -> HookCandidate | None:
    if not batch:
        return None
    staging_line = choose_staging_line(
        cars,
        batch,
        {source_line, preferred_target_line},
        length_load_lookup,
        staging_priority,
        depot_aware_staging,
    )
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
    )


def front_capacity_release_contract_planlet(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    target_line: str,
    owner_batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
    generation_reason: str,
) -> HookCandidate | None:
    if (
        not owner_batch
        or target_line not in FRONT_SERVICE_TARGET_LINES
        or target_line in REMOTE_INTERACTION_LINES
        or source_line in REMOTE_INTERACTION_LINES
        or any(car.get("IsWeigh") for car in owner_batch)
    ):
        return None
    owner_nos = {car_no(car) for car in owner_batch}
    if not reverse_length_fits(source_line, target_line, owner_batch):
        return None

    owner_positions = planned_positions_for_batch(
        batch=owner_batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=owner_nos,
        grouped=grouped,
    )
    if len(owner_positions) != len(owner_batch):
        return None

    blocked_positions = set(owner_positions.values())
    blockers = target_position_occupants(cars, target_line, blocked_positions, owner_nos)
    has_position_blockers = bool(blockers)
    if not blockers:
        target_cars = [
            car
            for car in grouped.get(target_line, [])
            if car_no(car) not in owner_nos
            and not is_locked_depot_stayer(car, depot_assignment)
        ]
        if not target_cars:
            return None
        blockers = [
            car
            for car in target_cars
            if planned(car)[0] != target_line or not target_position_is_acceptable(
                car,
                target_line,
                int(car.get("Position") or 0),
                depot_assignment,
                cars,
            )
        ]
        if not blockers:
            blockers = [target_cars[-1]]

    release_batch: list[dict[str, Any]] = []
    for blocker in sorted(blockers, key=lambda item: (int(item.get("Position") or 0), car_no(item))):
        if blocker.get("IsWeigh") or is_locked_depot_stayer(blocker, depot_assignment):
            continue
        if pull_equivalent([*release_batch, blocker]) > PULL_LIMIT_EQUIVALENT:
            break
        release_batch.append(blocker)
    if not release_batch:
        return None
    if pull_equivalent(release_batch) + pull_equivalent(owner_batch) > PULL_LIMIT_EQUIVALENT:
        return None

    release_nos = {car_no(car) for car in release_batch}
    all_nos = owner_nos | release_nos
    if has_position_blockers and all(
        planned(car)[0] == target_line for car in release_batch
    ):
        combined_batch = [*release_batch, *owner_batch]
        combined_positions = planned_positions_for_batch(
            batch=combined_batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=grouped,
        )
        if (
            len(combined_positions) == len(combined_batch)
            and reverse_length_fits(target_line, source_line, combined_batch)
            and candidate_positions_available(target_line, combined_positions, cars, all_nos, grouped)
            and line_has_length_capacity(target_line, cars, combined_batch, all_nos, length_load_lookup, grouped)
        ):
            steps = (
                PlanStep("Get", target_line, tuple(car_no(car) for car in release_batch)),
                PlanStep("Get", source_line, tuple(car_no(car) for car in owner_batch)),
                PlanStep("Put", target_line, tuple(car_no(car) for car in combined_batch), combined_positions),
            )
            return planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=target_line,
                target_line=target_line,
                batch=combined_batch,
                generation_reason=(
                    "front_capacity_release_contract;"
                    f"owner={source_line}->{target_line};owner_count={len(owner_batch)};"
                    f"release_line={target_line};release_target={target_line};"
                    f"release_mode=same_line_recovery;release_count={len(release_batch)};"
                    f"recovery_owner=front_service;{generation_reason}"
                ),
                candidate_kind="front_capacity_release_contract_planlet",
                steps=steps,
                action_family_override=action_family(source_line, target_line, any(car.get("IsWeigh") for car in owner_batch)),
            )

    release_target = ""
    release_target_reasons: set[str] = set()
    for car in release_batch:
        car_target, _position, reason = planned(car)
        release_target_reasons.add(reason)
        if not car_target or car_target == target_line:
            release_target = ""
            break
        if release_target and release_target != car_target:
            release_target = ""
            break
        release_target = car_target

    release_positions: dict[str, int] = {}
    release_mode = "release_to_staging"
    if release_target:
        probe_positions = planned_positions_for_batch(
            batch=release_batch,
            target_line=release_target,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=release_nos,
            grouped=grouped,
        )
        if (
            len(probe_positions) == len(release_batch)
            and reverse_length_fits(target_line, release_target, release_batch)
            and candidate_positions_available(release_target, probe_positions, cars, release_nos, grouped)
            and line_has_length_capacity(release_target, cars, release_batch, release_nos, length_load_lookup, grouped)
        ):
            release_positions = probe_positions
            release_mode = "release_to_planned_target"
    if not release_positions:
        staging_line = choose_staging_line(
            cars,
            release_batch,
            {source_line, target_line},
            length_load_lookup,
            staging_priority,
            depot_aware_staging,
        )
        if not staging_line:
            return None
        release_target = staging_line
        if not reverse_length_fits(target_line, release_target, release_batch):
            return None
        release_positions = first_free_positions_for_batch(cars, release_target, release_batch, release_nos)
        if not candidate_positions_available(release_target, release_positions, cars, release_nos, grouped):
            return None
        if not line_has_length_capacity(release_target, cars, release_batch, release_nos, length_load_lookup, grouped):
            return None
    if not reverse_length_fits(source_line, target_line, owner_batch):
        return None

    projected = [dict(car) for car in cars]
    projected_by_no = {car_no(car): car for car in projected}
    for car in release_batch:
        no = car_no(car)
        if no in projected_by_no:
            projected_by_no[no]["Line"] = release_target
            projected_by_no[no]["Position"] = release_positions[no]
    projected_grouped = cars_by_line(projected)
    if not candidate_positions_available(target_line, owner_positions, projected, owner_nos, projected_grouped):
        return None
    if not line_has_length_capacity(target_line, projected, owner_batch, owner_nos, None, projected_grouped):
        return None

    steps = (
        PlanStep("Get", target_line, tuple(car_no(car) for car in release_batch)),
        PlanStep("Put", release_target, tuple(car_no(car) for car in release_batch), release_positions),
        PlanStep("Get", source_line, tuple(car_no(car) for car in owner_batch)),
        PlanStep("Put", target_line, tuple(car_no(car) for car in owner_batch), owner_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=target_line,
        target_line=target_line,
        batch=[*release_batch, *owner_batch],
        generation_reason=(
            "front_capacity_release_contract;"
            f"owner={source_line}->{target_line};owner_count={len(owner_batch)};"
            f"release_line={target_line};release_target={release_target};"
            f"release_mode={release_mode};release_count={len(release_batch)};"
            f"release_target_reasons={','.join(sorted(release_target_reasons))};"
            f"recovery_owner=front_service;"
            f"{generation_reason}"
        ),
        candidate_kind="front_capacity_release_contract_planlet",
        steps=steps,
        action_family_override=action_family(source_line, target_line, any(car.get("IsWeigh") for car in owner_batch)),
    )


def front_capacity_digest_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
) -> list[HookCandidate]:
    candidates: list[HookCandidate] = []
    for target_line in sorted(FRONT_SERVICE_TARGET_LINES - REMOTE_INTERACTION_LINES):
        inbound_sources = planned_unsatisfied_by_line(
            grouped=grouped,
            planned=planned,
            satisfied=satisfied,
            source_filter=lambda line, target_line=target_line: line != target_line and line not in REMOTE_INTERACTION_LINES,
            target_filter=lambda line, target_line=target_line: line == target_line,
            max_per_line=8,
        )
        if not inbound_sources:
            continue
        selected_inbound, inbound_carry = select_source_batches(
            inbound_sources,
            max_sources=4,
            max_per_source=8,
            remote_first=False,
        )
        if len(inbound_carry) < 2:
            continue

        inbound_nos = {car_no(car) for car in inbound_carry}
        target_cars = [
            car
            for car in grouped.get(target_line, [])
            if car_no(car) not in inbound_nos
            and not is_locked_depot_stayer(car, depot_assignment)
        ]
        release_batch: list[dict[str, Any]] = []
        for car in target_cars:
            planned_line, _planned_position, _planned_reason = planned(car)
            acceptable = (
                planned_line == target_line
                and target_position_is_acceptable(
                    car,
                    target_line,
                    int(car.get("Position") or 0),
                    depot_assignment,
                    cars,
                )
            )
            if acceptable:
                continue
            if car.get("IsWeigh"):
                continue
            if pull_equivalent([*release_batch, car]) > PULL_LIMIT_EQUIVALENT:
                break
            release_batch.append(car)

        if not release_batch:
            continue
        remaining_pull = PULL_LIMIT_EQUIVALENT - pull_equivalent(release_batch)
        if remaining_pull <= 0:
            continue
        clipped_sources: list[tuple[str, list[dict[str, Any]]]] = []
        clipped_inbound: list[dict[str, Any]] = []
        for source_line, batch in selected_inbound:
            clipped: list[dict[str, Any]] = []
            for car in batch:
                if pull_equivalent([*clipped_inbound, car]) > remaining_pull:
                    break
                clipped.append(car)
                clipped_inbound.append(car)
            if clipped:
                clipped_sources.append((source_line, clipped))
        if len(clipped_inbound) < 2:
            continue

        release_target = choose_staging_line(
            cars,
            release_batch,
            {target_line, *(line for line, _batch in clipped_sources)},
            length_load_lookup,
            staging_priority,
            depot_aware_staging,
        )
        if not release_target:
            continue
        release_nos = {car_no(car) for car in release_batch}
        all_nos = release_nos | {car_no(car) for car in clipped_inbound}
        release_positions = first_free_positions_for_batch(cars, release_target, release_batch, release_nos)
        if not candidate_positions_available(release_target, release_positions, cars, release_nos, grouped):
            continue
        if not line_has_length_capacity(release_target, cars, release_batch, release_nos, length_load_lookup, grouped):
            continue

        projected = [dict(car) for car in cars]
        projected_by_no = {car_no(car): car for car in projected}
        for car in release_batch:
            no = car_no(car)
            if no in projected_by_no:
                projected_by_no[no]["Line"] = release_target
                projected_by_no[no]["Position"] = release_positions[no]
        projected_grouped = cars_by_line(projected)
        inbound_positions = planned_positions_for_batch(
            batch=clipped_inbound,
            target_line=target_line,
            cars=projected,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=projected_grouped,
        )
        if len(inbound_positions) != len(clipped_inbound):
            continue
        if not candidate_positions_available(target_line, inbound_positions, projected, all_nos, projected_grouped):
            continue
        if not line_has_length_capacity(target_line, projected, clipped_inbound, all_nos, None, projected_grouped):
            continue

        steps: list[PlanStep] = [
            PlanStep("Get", target_line, tuple(car_no(car) for car in release_batch)),
            PlanStep("Put", release_target, tuple(car_no(car) for car in release_batch), release_positions),
        ]
        steps.extend(
            PlanStep("Get", source_line, tuple(car_no(car) for car in batch))
            for source_line, batch in clipped_sources
        )
        steps.append(PlanStep("Put", target_line, tuple(car_no(car) for car in clipped_inbound), inbound_positions))
        candidates.append(
            planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=target_line,
                target_line=target_line,
                batch=[*release_batch, *clipped_inbound],
                generation_reason=(
                    "front_capacity_digest_planlet;"
                    f"target_line={target_line};release_target={release_target};"
                    f"release_count={len(release_batch)};inbound_source_count={len(clipped_sources)};"
                    f"inbound_count={len(clipped_inbound)}"
                ),
                candidate_kind="front_capacity_digest_planlet",
                steps=tuple(steps),
                action_family_override=action_family(clipped_sources[0][0], target_line, any(car.get("IsWeigh") for car in clipped_inbound)),
            )
        )
        if len(candidates) >= 4:
            break
    return candidates


def planlet_action_family(steps: tuple[PlanStep, ...]) -> str | None:
    lines = [step.line for step in steps if step.action in {"Get", "Put"}]
    put_lines = [step.line for step in steps if step.action == "Put"]
    get_lines = [step.line for step in steps if step.action == "Get"]
    if any(line in DEPOT_LINES for line in put_lines):
        return "REPAIR_INBOUND"
    if any(line in DEPOT_TARGET_LINES for line in put_lines):
        return "DEPOT_SLOT"
    if any(line in DEPOT_TARGET_LINES for line in get_lines) and any(line not in DEPOT_TARGET_LINES for line in put_lines):
        return "DEPOT_OUTBOUND"
    if any(line in REMOTE_INTERACTION_LINES for line in lines):
        return "FUNCTION_LINE_SERVICE"
    return None


def multi_drop_planlet_for_line(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    line_cars: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> HookCandidate | None:
    carry: list[dict[str, Any]] = []
    target_groups: dict[str, list[dict[str, Any]]] = {}
    target_order: list[str] = []
    for car in line_cars:
        if satisfied(car):
            break
        target_line, _position, _reason = planned(car)
        if not target_line or target_line == line or car.get("IsWeigh"):
            break
        if pull_equivalent([*carry, car]) > PULL_LIMIT_EQUIVALENT:
            break
        carry.append(car)
        if target_line not in target_groups:
            target_groups[target_line] = []
            target_order.append(target_line)
        target_groups[target_line].append(car)

    if len(target_order) < 2 or len(carry) < 2:
        return None

    all_nos = {car_no(car) for car in carry}
    steps: list[PlanStep] = [PlanStep("Get", line, tuple(car_no(car) for car in carry))]
    for target_line in target_order:
        batch = target_groups[target_line]
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=grouped,
        )
        if len(positions) != len(batch):
            return None
        if not candidate_positions_available(target_line, positions, cars, all_nos, grouped):
            return None
        if not line_has_length_capacity(target_line, cars, batch, all_nos, length_load_lookup, grouped):
            return None
        steps.append(PlanStep("Put", target_line, tuple(car_no(car) for car in batch), positions))

    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=line,
        target_line=target_order[-1],
        batch=carry,
        generation_reason=(
            "multi_drop_line_planlet;"
            f"source_line={line};target_count={len(target_order)};vehicle_count={len(carry)}"
        ),
        candidate_kind="multi_drop_planlet",
        steps=tuple(steps),
    )


def first_front_batch_to_target(
    *,
    line_cars: list[dict[str, Any]],
    target_line: str,
    planned: Any,
    satisfied: Any,
    max_remaining_pull: int,
) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for car in line_cars:
        if satisfied(car):
            break
        planned_line, _position, _reason = planned(car)
        if planned_line != target_line or car.get("IsWeigh"):
            break
        if pull_equivalent(batch) + pull_equivalent([car]) > max_remaining_pull:
            break
        batch.append(car)
    return batch


def multi_pick_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    remote_only: bool = False,
) -> list[HookCandidate]:
    by_target: dict[str, list[tuple[str, list[dict[str, Any]]]]] = defaultdict(list)
    for line, line_cars in grouped.items():
        if remote_only and line not in REMOTE_INTERACTION_LINES:
            continue
        if not remote_only and line in REMOTE_INTERACTION_LINES:
            continue
        first = next((car for car in line_cars if not satisfied(car)), None)
        if first is None:
            continue
        target_line, _position, _reason = planned(first)
        if not target_line or target_line == line:
            continue
        if remote_only and target_line in REMOTE_INTERACTION_LINES:
            continue
        batch = first_front_batch_to_target(
            line_cars=line_cars,
            target_line=target_line,
            planned=planned,
            satisfied=satisfied,
            max_remaining_pull=PULL_LIMIT_EQUIVALENT,
        )
        if batch:
            by_target[target_line].append((line, batch))

    candidates: list[HookCandidate] = []
    for target_line, source_batches in by_target.items():
        if len(source_batches) < 2:
            continue
        carry: list[dict[str, Any]] = []
        selected: list[tuple[str, list[dict[str, Any]]]] = []
        for line, batch in sorted(source_batches, key=lambda item: (item[0] not in REMOTE_INTERACTION_LINES, item[0])):
            remaining = PULL_LIMIT_EQUIVALENT - pull_equivalent(carry)
            clipped: list[dict[str, Any]] = []
            for car in batch:
                if pull_equivalent(clipped) + pull_equivalent([car]) > remaining:
                    break
                clipped.append(car)
            if not clipped:
                continue
            selected.append((line, clipped))
            carry.extend(clipped)
            if len(selected) >= 4:
                break
        if len(selected) < 2 or len(carry) < 2:
            continue
        all_nos = {car_no(car) for car in carry}
        positions = planned_positions_for_batch(
            batch=carry,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=grouped,
        )
        if len(positions) != len(carry):
            continue
        if not candidate_positions_available(target_line, positions, cars, all_nos, grouped):
            continue
        if not line_has_length_capacity(target_line, cars, carry, all_nos, length_load_lookup, grouped):
            continue

        steps = [
            PlanStep("Get", line, tuple(car_no(car) for car in batch))
            for line, batch in selected
        ]
        steps.append(PlanStep("Put", target_line, tuple(car_no(car) for car in carry), positions))
        kind = "remote_session_planlet" if remote_only else "multi_pick_planlet"
        candidates.append(
            planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=selected[0][0],
                target_line=target_line,
                batch=carry,
                generation_reason=(
                    f"{kind};target_line={target_line};"
                    f"source_count={len(selected)};vehicle_count={len(carry)}"
                ),
                candidate_kind=kind,
                steps=tuple(steps),
            )
        )
    return candidates


def contract_carry_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    contract_name: str,
    candidate_kind: str,
    source_filter: Any,
    target_filter: Any,
    max_sources: int = 4,
    max_targets: int = 4,
) -> list[HookCandidate]:
    source_batches: list[tuple[str, list[dict[str, Any]]]] = []
    for line, line_cars in sorted(grouped.items()):
        if not source_filter(line):
            continue
        first = next((car for car in line_cars if not satisfied(car)), None)
        if first is None:
            continue
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            if satisfied(car):
                break
            target_line, _position, _reason = planned(car)
            if not target_line or target_line == line or not target_filter(target_line) or car.get("IsWeigh"):
                if batch:
                    break
                continue
            if pull_equivalent([*batch, car]) > PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            if len({planned(item)[0] for item in batch}) >= max_targets:
                break
        if batch:
            source_batches.append((line, batch))

    candidates: list[HookCandidate] = []
    if len(source_batches) < 2:
        return candidates

    source_batches.sort(key=lambda item: (item[0] not in REMOTE_INTERACTION_LINES, item[0]))
    carry: list[dict[str, Any]] = []
    selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
    target_groups: dict[str, list[dict[str, Any]]] = {}
    target_order: list[str] = []
    for line, batch in source_batches:
        selected_batch: list[dict[str, Any]] = []
        for car in batch:
            target_line, _position, _reason = planned(car)
            if target_line not in target_groups and len(target_order) >= max_targets:
                continue
            if pull_equivalent([*carry, car]) > PULL_LIMIT_EQUIVALENT:
                break
            selected_batch.append(car)
            carry.append(car)
            if target_line not in target_groups:
                target_groups[target_line] = []
                target_order.append(target_line)
            target_groups[target_line].append(car)
        if selected_batch:
            selected_sources.append((line, selected_batch))
        if len(selected_sources) >= max_sources or len(target_order) >= max_targets:
            break

    if len(selected_sources) < 2 or len(carry) < 3 or len(target_order) < 2:
        return candidates

    all_nos = {car_no(car) for car in carry}
    put_steps: list[PlanStep] = []
    accepted_target_nos: set[str] = set()
    for target_line in target_order:
        batch = target_groups[target_line]
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=grouped,
        )
        if len(positions) != len(batch):
            continue
        if not candidate_positions_available(target_line, positions, cars, all_nos, grouped):
            continue
        if not line_has_length_capacity(target_line, cars, batch, all_nos, length_load_lookup, grouped):
            continue
        target_nos = tuple(car_no(car) for car in batch)
        accepted_target_nos.update(target_nos)
        put_steps.append(PlanStep("Put", target_line, target_nos, positions))

    if len(put_steps) < 2 or len(accepted_target_nos) < 3:
        return candidates

    filtered_sources: list[tuple[str, list[dict[str, Any]]]] = []
    filtered_carry: list[dict[str, Any]] = []
    for line, batch in selected_sources:
        filtered_batch = [car for car in batch if car_no(car) in accepted_target_nos]
        if not filtered_batch:
            continue
        filtered_sources.append((line, filtered_batch))
        filtered_carry.extend(filtered_batch)
    if len(filtered_sources) < 2 or len(filtered_carry) < 3:
        return candidates

    steps: list[PlanStep] = [
        PlanStep("Get", line, tuple(car_no(car) for car in batch))
        for line, batch in filtered_sources
    ]
    steps.extend(put_steps)

    candidates.append(
        planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=filtered_sources[0][0],
            target_line=put_steps[-1].line,
            batch=filtered_carry,
            generation_reason=(
                f"{candidate_kind};contract={contract_name};"
                f"source_count={len(filtered_sources)};target_count={len(put_steps)};"
                f"vehicle_count={len(filtered_carry)}"
            ),
            candidate_kind=candidate_kind,
            steps=tuple(steps),
            action_family_override=contract_name,
        )
    )
    return candidates


def h1_carry_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    return contract_carry_planlets(
        case_id=case_id,
        hook_index=hook_index,
        cars=cars,
        depot_assignment=depot_assignment,
        planned=planned,
        satisfied=satisfied,
        length_load_lookup=length_load_lookup,
        grouped=grouped,
        contract_name="FUNCTION_LINE_SERVICE",
        candidate_kind="h1_carry_planlet",
        source_filter=lambda line: line not in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: line in FRONT_SERVICE_TARGET_LINES and line not in REMOTE_INTERACTION_LINES,
        max_sources=4,
        max_targets=4,
    )

def remote_exchange_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    return contract_carry_planlets(
        case_id=case_id,
        hook_index=hook_index,
        cars=cars,
        depot_assignment=depot_assignment,
        planned=planned,
        satisfied=satisfied,
        length_load_lookup=length_load_lookup,
        grouped=grouped,
        contract_name="REPAIR_INBOUND",
        candidate_kind="remote_exchange_planlet",
        source_filter=lambda line: line in REMOTE_INTERACTION_LINES or line not in RUNNING_LINES,
        target_filter=lambda line: line in DEPOT_TARGET_LINES or line in {"存4线", "油漆线", "卸轮线"},
        max_sources=4,
        max_targets=4,
    )


def planned_unsatisfied_by_line(
    *,
    grouped: dict[str, list[dict[str, Any]]],
    planned: Any,
    satisfied: Any,
    source_filter: Any,
    target_filter: Any,
    max_per_line: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for line, line_cars in grouped.items():
        if not source_filter(line):
            continue
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            if satisfied(car):
                continue
            target_line, _position, _reason = planned(car)
            if not target_line or target_line == line or not target_filter(target_line) or car.get("IsWeigh"):
                continue
            batch.append(car)
            if len(batch) >= max_per_line:
                break
        if batch:
            result[line] = batch
    return result


def append_put_step_if_valid(
    *,
    steps: list[PlanStep],
    target_line: str,
    batch: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    all_nos: set[str],
    grouped: dict[str, list[dict[str, Any]]],
    length_load_lookup: dict[str, float],
) -> bool:
    if not batch:
        return False
    positions = planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_nos,
        grouped=grouped,
    )
    if len(positions) != len(batch):
        return False
    if not candidate_positions_available(target_line, positions, cars, all_nos, grouped):
        return False
    if not line_has_length_capacity(target_line, cars, batch, all_nos, length_load_lookup, grouped):
        return False
    steps.append(PlanStep("Put", target_line, tuple(car_no(car) for car in batch), positions))
    return True


def build_carry_drop_steps(
    *,
    selected_sources: list[tuple[str, list[dict[str, Any]]]],
    target_groups: dict[str, list[dict[str, Any]]],
    target_order: list[str],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    grouped: dict[str, list[dict[str, Any]]],
    length_load_lookup: dict[str, float],
) -> tuple[list[PlanStep], list[dict[str, Any]]]:
    provisional_nos = {car_no(car) for _line, batch in selected_sources for car in batch}
    accepted_lines: list[str] = []
    for target_line in target_order:
        batch = target_groups.get(target_line, [])
        probe_steps: list[PlanStep] = []
        if append_put_step_if_valid(
            steps=probe_steps,
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            all_nos=provisional_nos,
            grouped=grouped,
            length_load_lookup=length_load_lookup,
        ):
            accepted_lines.append(target_line)

    accepted_nos = {
        car_no(car)
        for line in accepted_lines
        for car in target_groups.get(line, [])
    }
    if not accepted_nos:
        return [], []

    filtered_sources: list[tuple[str, list[dict[str, Any]]]] = []
    filtered_carry: list[dict[str, Any]] = []
    for line, batch in selected_sources:
        filtered = [car for car in batch if car_no(car) in accepted_nos]
        if filtered:
            filtered_sources.append((line, filtered))
            filtered_carry.extend(filtered)
    if not filtered_sources:
        return [], []

    final_nos = {car_no(car) for car in filtered_carry}
    steps: list[PlanStep] = [
        PlanStep("Get", line, tuple(car_no(car) for car in batch))
        for line, batch in filtered_sources
    ]
    for target_line in accepted_lines:
        batch = [car for car in target_groups[target_line] if car_no(car) in final_nos]
        if not append_put_step_if_valid(
            steps=steps,
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            all_nos=final_nos,
            grouped=grouped,
            length_load_lookup=length_load_lookup,
        ):
            return [], []
    return steps, filtered_carry

def select_source_batches(
    source_map: dict[str, list[dict[str, Any]]],
    *,
    max_sources: int,
    max_pull: int = PULL_LIMIT_EQUIVALENT,
    max_per_source: int = 8,
    remote_first: bool = True,
) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
    carry: list[dict[str, Any]] = []
    line_key = (
        (lambda item: (item[0] not in REMOTE_INTERACTION_LINES, item[0]))
        if remote_first
        else (lambda item: item[0])
    )
    for line, batch in sorted(source_map.items(), key=line_key):
        selected: list[dict[str, Any]] = []
        for car in batch:
            if pull_equivalent([*carry, *selected, car]) > max_pull:
                break
            selected.append(car)
            if len(selected) >= max_per_source:
                break
        if selected:
            selected_sources.append((line, selected))
            carry.extend(selected)
        if len(selected_sources) >= max_sources or pull_equivalent(carry) >= max_pull:
            break
    return selected_sources, carry


def target_groups_for_carry(
    carry: list[dict[str, Any]],
    planned: Any,
) -> dict[str, list[dict[str, Any]]]:
    target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in carry:
        target_line, _position, _reason = planned(car)
        if target_line:
            target_groups[target_line].append(car)
    return target_groups


def accepted_put_lines(
    *,
    target_groups: dict[str, list[dict[str, Any]]],
    target_order: list[str],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    all_nos: set[str],
    grouped: dict[str, list[dict[str, Any]]],
    length_load_lookup: dict[str, float],
) -> list[str]:
    accepted: list[str] = []
    for target_line in target_order:
        probe_steps: list[PlanStep] = []
        if append_put_step_if_valid(
            steps=probe_steps,
            target_line=target_line,
            batch=target_groups.get(target_line, []),
            cars=cars,
            depot_assignment=depot_assignment,
            all_nos=all_nos,
            grouped=grouped,
            length_load_lookup=length_load_lookup,
        ):
            accepted.append(target_line)
    return accepted


def filter_sources_by_nos(
    selected_sources: list[tuple[str, list[dict[str, Any]]]],
    accepted_nos: set[str],
) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    filtered_sources: list[tuple[str, list[dict[str, Any]]]] = []
    filtered_carry: list[dict[str, Any]] = []
    for line, batch in selected_sources:
        filtered = [car for car in batch if car_no(car) in accepted_nos]
        if filtered:
            filtered_sources.append((line, filtered))
            filtered_carry.extend(filtered)
    return filtered_sources, filtered_carry


def depot_digest_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    outbound_sources = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: line not in REMOTE_INTERACTION_LINES,
        max_per_line=10,
    )
    inbound_sources = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line not in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: line in REMOTE_INTERACTION_LINES,
        max_per_line=10,
    )
    internal_sources = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: line in REMOTE_INTERACTION_LINES,
        max_per_line=8,
    )
    selected_outbound, outbound_carry = select_source_batches(
        outbound_sources,
        max_sources=4,
        max_per_source=8,
        remote_first=True,
    )
    selected_inbound, inbound_carry = select_source_batches(
        inbound_sources,
        max_sources=4,
        max_per_source=8,
        remote_first=False,
    )
    selected_internal, internal_carry = select_source_batches(
        internal_sources,
        max_sources=3,
        max_per_source=6,
        remote_first=True,
    )

    candidates: list[HookCandidate] = []

    def build_digest(
        *,
        name: str,
        source_groups: list[tuple[str, list[dict[str, Any]]]],
        carry: list[dict[str, Any]],
        min_vehicle_count: int,
        action_family_name: str,
        target_order_key: Any,
    ) -> HookCandidate | None:
        if len(carry) < min_vehicle_count or not source_groups:
            return None
        target_groups = target_groups_for_carry(carry, planned)
        if not target_groups:
            return None
        target_order = sorted(target_groups, key=target_order_key)
        accepted_lines = accepted_put_lines(
            target_groups=target_groups,
            target_order=target_order,
            cars=cars,
            depot_assignment=depot_assignment,
            all_nos={car_no(car) for car in carry},
            grouped=grouped,
            length_load_lookup=length_load_lookup,
        )
        accepted_nos = {
            car_no(car)
            for line in accepted_lines
            for car in target_groups.get(line, [])
        }
        if len(accepted_nos) < min_vehicle_count:
            return None
        filtered_sources, filtered_carry = filter_sources_by_nos(source_groups, accepted_nos)
        if not filtered_sources:
            return None
        final_nos = {car_no(car) for car in filtered_carry}
        steps: list[PlanStep] = [
            PlanStep("Get", line, tuple(car_no(car) for car in batch))
            for line, batch in filtered_sources
        ]
        for target_line in accepted_lines:
            batch = [car for car in target_groups[target_line] if car_no(car) in final_nos]
            if not append_put_step_if_valid(
                steps=steps,
                target_line=target_line,
                batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                all_nos=final_nos,
                grouped=grouped,
                length_load_lookup=length_load_lookup,
            ):
                return None
        if len(steps) < 2:
            return None
        return planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=steps[-1].line,
            batch=filtered_carry,
            generation_reason=(
                f"depot_digest_contract;mode={name};"
                f"source_count={len(filtered_sources)};target_count={len(accepted_lines)};"
                f"vehicle_count={len(filtered_carry)}"
            ),
            candidate_kind="depot_digest_planlet",
            steps=tuple(steps),
            action_family_override=action_family_name,
        )

    outbound = build_digest(
        name="outbound_only",
        source_groups=selected_outbound,
        carry=outbound_carry,
        min_vehicle_count=2,
        action_family_name="DEPOT_OUTBOUND",
        target_order_key=lambda line: (line != "存4线", line in REMOTE_INTERACTION_LINES, line),
    )
    inbound = build_digest(
        name="inbound_only",
        source_groups=selected_inbound,
        carry=inbound_carry,
        min_vehicle_count=2,
        action_family_name="REPAIR_INBOUND",
        target_order_key=lambda line: (line not in DEPOT_LINES, line not in DEPOT_TARGET_LINES, line),
    )
    internal = build_digest(
        name="remote_internal",
        source_groups=selected_internal,
        carry=internal_carry,
        min_vehicle_count=2,
        action_family_name="REPAIR_INBOUND",
        target_order_key=lambda line: (line not in DEPOT_LINES, line),
    )
    candidates.extend(candidate for candidate in (outbound, inbound, internal) if candidate)

    mixed_sources: list[tuple[str, list[dict[str, Any]]]] = []
    mixed_carry: list[dict[str, Any]] = []
    for source_groups in (selected_outbound, selected_inbound, selected_internal):
        for line, batch in source_groups:
            selected: list[dict[str, Any]] = []
            for car in batch:
                if pull_equivalent([*mixed_carry, car]) > PULL_LIMIT_EQUIVALENT:
                    break
                selected.append(car)
                mixed_carry.append(car)
            if selected:
                mixed_sources.append((line, selected))
            if pull_equivalent(mixed_carry) >= PULL_LIMIT_EQUIVALENT or len(mixed_sources) >= 6:
                break
        if pull_equivalent(mixed_carry) >= PULL_LIMIT_EQUIVALENT or len(mixed_sources) >= 6:
            break
    mixed = build_digest(
        name="mixed_outbound_then_inbound",
        source_groups=mixed_sources,
        carry=mixed_carry,
        min_vehicle_count=4,
        action_family_name="REPAIR_INBOUND",
        target_order_key=lambda line: (
            line in REMOTE_INTERACTION_LINES,
            line != "存4线",
            line not in DEPOT_LINES,
            line,
        ),
    )
    if mixed:
        candidates.append(mixed)
    return candidates


def depot_slot_swap_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    candidates: list[HookCandidate] = []
    for source_line, line_cars in sorted(grouped.items()):
        if source_line in REMOTE_INTERACTION_LINES:
            continue
        first = next((car for car in line_cars if not satisfied(car)), None)
        if first is None:
            continue
        target_line, _position, _reason = planned(first)
        if target_line not in DEPOT_LINES:
            continue
        inbound_batch = first_front_batch_to_target(
            line_cars=line_cars,
            target_line=target_line,
            planned=planned,
            satisfied=satisfied,
            max_remaining_pull=PULL_LIMIT_EQUIVALENT,
        )
        if not inbound_batch:
            continue
        inbound_nos = {car_no(car) for car in inbound_batch}
        inbound_positions = planned_positions_for_batch(
            batch=inbound_batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=inbound_nos,
            grouped=grouped,
        )
        if len(inbound_positions) != len(inbound_batch):
            continue
        blockers = [
            car
            for car in target_position_occupants(cars, target_line, set(inbound_positions.values()), inbound_nos)
            if not is_locked_depot_stayer(car, depot_assignment)
            and not satisfied(car)
            and planned(car)[0]
            and planned(car)[0] != target_line
        ]
        if not blockers or pull_equivalent(blockers) > PULL_LIMIT_EQUIVALENT:
            continue
        blocker_targets = target_groups_for_carry(blockers, planned)
        all_nos = inbound_nos | {car_no(car) for car in blockers}
        blocker_order = sorted(blocker_targets, key=lambda line: (line in DEPOT_TARGET_LINES, line))
        accepted_blocker_lines = accepted_put_lines(
            target_groups=blocker_targets,
            target_order=blocker_order,
            cars=cars,
            depot_assignment=depot_assignment,
            all_nos=all_nos,
            grouped=grouped,
            length_load_lookup=length_load_lookup,
        )
        if not accepted_blocker_lines:
            continue
        blocker_nos = {
            car_no(car)
            for line in accepted_blocker_lines
            for car in blocker_targets.get(line, [])
        }
        if not blocker_nos:
            continue
        blockers = [car for car in blockers if car_no(car) in blocker_nos]
        if not candidate_positions_available(target_line, inbound_positions, cars, all_nos, grouped):
            continue
        if not line_has_length_capacity(target_line, cars, inbound_batch, all_nos, length_load_lookup, grouped):
            continue
        steps: list[PlanStep] = [
            PlanStep("Get", target_line, tuple(car_no(car) for car in blockers))
        ]
        for line in accepted_blocker_lines:
            batch = [car for car in blocker_targets[line] if car_no(car) in blocker_nos]
            if not append_put_step_if_valid(
                steps=steps,
                target_line=line,
                batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                all_nos=all_nos,
                grouped=grouped,
                length_load_lookup=length_load_lookup,
            ):
                break
        else:
            steps.extend(
                (
                    PlanStep("Get", source_line, tuple(car_no(car) for car in inbound_batch)),
                    PlanStep("Put", target_line, tuple(car_no(car) for car in inbound_batch), inbound_positions),
                )
            )
            candidates.append(
                planlet_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=target_line,
                    target_line=target_line,
                    batch=[*blockers, *inbound_batch],
                    generation_reason=(
                        "depot_slot_swap_contract;"
                        f"target_line={target_line};source_line={source_line};"
                        f"blocker_count={len(blockers)};inbound_count={len(inbound_batch)}"
                    ),
                    candidate_kind="depot_slot_swap_planlet",
                    steps=tuple(steps),
                    action_family_override="REPAIR_INBOUND",
                )
            )
            if len(candidates) >= 2:
                break
    return candidates


def remote_session_contract_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    remote_sources = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: True,
    )
    inbound_sources = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line not in REMOTE_INTERACTION_LINES,
        target_filter=lambda line: line in REMOTE_INTERACTION_LINES,
    )

    selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
    carry: list[dict[str, Any]] = []
    for source_map in (remote_sources, inbound_sources):
        for line, batch in sorted(source_map.items(), key=lambda item: (item[0] not in DEPOT_LINES, item[0])):
            selected: list[dict[str, Any]] = []
            for car in batch:
                if pull_equivalent([*carry, car]) > PULL_LIMIT_EQUIVALENT:
                    break
                selected.append(car)
                carry.append(car)
                if len(selected) >= 6:
                    break
            if selected:
                selected_sources.append((line, selected))
            if len(selected_sources) >= 4 or pull_equivalent(carry) >= PULL_LIMIT_EQUIVALENT:
                break
        if len(selected_sources) >= 4 or pull_equivalent(carry) >= PULL_LIMIT_EQUIVALENT:
            break

    if len(carry) < 5 or len(selected_sources) < 2:
        return []

    target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in carry:
        target_line, _position, _reason = planned(car)
        if target_line:
            target_groups[target_line].append(car)

    remote_targets = [line for line in target_groups if line in REMOTE_INTERACTION_LINES]
    non_remote_targets = [line for line in target_groups if line not in REMOTE_INTERACTION_LINES]
    if not remote_targets and not any(line in REMOTE_INTERACTION_LINES for line, _batch in selected_sources):
        return []

    steps, carry = build_carry_drop_steps(
        selected_sources=selected_sources,
        target_groups=target_groups,
        target_order=[*sorted(remote_targets), *sorted(non_remote_targets, key=lambda line: (line != "存4线", line))],
        cars=cars,
        depot_assignment=depot_assignment,
        grouped=grouped,
        length_load_lookup=length_load_lookup,
    )

    if len(steps) < 4:
        return []
    candidate = planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=selected_sources[0][0],
        target_line=steps[-1].line,
        batch=carry,
        generation_reason=(
            "remote_session_contract;"
            f"source_count={len(selected_sources)};target_count={len(target_groups)};"
            f"remote_target_count={len(remote_targets)};vehicle_count={len(carry)}"
        ),
        candidate_kind="remote_session_contract_planlet",
        steps=tuple(steps),
        action_family_override="REPAIR_INBOUND" if remote_targets else "DEPOT_OUTBOUND",
    )
    return [candidate]


def remote_corridor_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    candidates: list[HookCandidate] = []
    for blocker_line in ("机走棚", "存4线", "预修线"):
        line_cars = grouped.get(blocker_line, [])
        if len(line_cars) < 3 or any(car.get("IsWeigh") for car in line_cars):
            continue
        blockers = [car for car in line_cars if not is_locked_depot_stayer(car, depot_assignment)]
        if len(blockers) < 3 or pull_equivalent(blockers) > PULL_LIMIT_EQUIVALENT:
            continue

        target_sources = planned_unsatisfied_by_line(
            grouped=grouped,
            planned=planned,
            satisfied=satisfied,
            source_filter=lambda line: line != blocker_line,
            target_filter=lambda line: line in REMOTE_INTERACTION_LINES or line == "存4线",
            max_per_line=4,
        )
        target_batch: list[dict[str, Any]] = []
        selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
        for line, batch in sorted(target_sources.items(), key=lambda item: (item[0] not in REMOTE_INTERACTION_LINES, item[0])):
            selected: list[dict[str, Any]] = []
            for car in batch:
                if pull_equivalent([*blockers, *target_batch, car]) > PULL_LIMIT_EQUIVALENT:
                    break
                selected.append(car)
                target_batch.append(car)
            if selected:
                selected_sources.append((line, selected))
            if len(selected_sources) >= 3 or len(target_batch) >= 8:
                break
        if len(target_batch) < 2:
            continue

        staging_line = choose_staging_line(
            cars,
            blockers,
            {blocker_line, *(line for line, _batch in selected_sources), *(planned(car)[0] for car in target_batch)},
            length_load_lookup,
            DEPOT_STAGING_LINE_PRIORITY,
            True,
        )
        if not staging_line:
            continue
        all_carry = [*blockers, *target_batch]
        all_nos = {car_no(car) for car in all_carry}
        blocker_nos = {car_no(car) for car in blockers}
        staging_positions = first_free_positions_for_batch(cars, staging_line, blockers, blocker_nos)
        if not candidate_positions_available(staging_line, staging_positions, cars, blocker_nos, grouped):
            continue
        if not line_has_length_capacity(staging_line, cars, blockers, blocker_nos, length_load_lookup, grouped):
            continue

        target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for car in target_batch:
            target_line, _position, _reason = planned(car)
            if target_line:
                target_groups[target_line].append(car)

        steps: list[PlanStep] = [
            PlanStep("Get", blocker_line, tuple(car_no(car) for car in blockers)),
            PlanStep("Put", staging_line, tuple(car_no(car) for car in blockers), staging_positions),
        ]
        steps.extend(PlanStep("Get", line, tuple(car_no(car) for car in batch)) for line, batch in selected_sources)
        for target_line in sorted(target_groups, key=lambda line: (line not in REMOTE_INTERACTION_LINES, line)):
            if not append_put_step_if_valid(
                steps=steps,
                target_line=target_line,
                batch=target_groups[target_line],
                cars=cars,
                depot_assignment=depot_assignment,
                all_nos=all_nos,
                grouped=grouped,
                length_load_lookup=length_load_lookup,
            ):
                break
        else:
            restore_positions = {car_no(car): int(car.get("Position") or 0) for car in blockers}
            if not candidate_positions_available(blocker_line, restore_positions, cars, all_nos, grouped):
                continue
            steps.extend(
                (
                    PlanStep("Get", staging_line, tuple(car_no(car) for car in blockers)),
                    PlanStep("Put", blocker_line, tuple(car_no(car) for car in blockers), restore_positions),
                )
            )
            candidates.append(
                planlet_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=blocker_line,
                    target_line=blocker_line,
                    batch=all_carry,
                    generation_reason=(
                        "remote_corridor_contract;"
                        f"blocker_line={blocker_line};staging_line={staging_line};"
                        f"source_count={len(selected_sources)};target_count={len(target_groups)};"
                        f"blocker_count={len(blockers)};target_count_cars={len(target_batch)}"
                    ),
                    candidate_kind="remote_corridor_planlet",
                    steps=tuple(steps),
                    action_family_override="REPAIR_INBOUND",
                )
            )
    return candidates


def short_chain_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    manual_baseline: ManualBaseline | None,
) -> list[HookCandidate]:
    if not manual_baseline or manual_baseline.variant not in SHORT_CHAIN_VARIANTS:
        return []
    source_batches = planned_unsatisfied_by_line(
        grouped=grouped,
        planned=planned,
        satisfied=satisfied,
        source_filter=lambda line: line in REMOTE_INTERACTION_LINES
        or line in {"机库线", "油漆线", "洗罐站", "调梁棚", "抛丸线"},
        target_filter=lambda line: line in DEPOT_TARGET_LINES or line in {"存4线", "油漆线"},
        max_per_line=8,
    )
    carry: list[dict[str, Any]] = []
    selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
    for line, batch in sorted(source_batches.items(), key=lambda item: (item[0] not in REMOTE_INTERACTION_LINES, item[0])):
        selected: list[dict[str, Any]] = []
        for car in batch:
            if pull_equivalent([*carry, car]) > PULL_LIMIT_EQUIVALENT:
                break
            selected.append(car)
            carry.append(car)
        if selected:
            selected_sources.append((line, selected))
        if len(selected_sources) >= 4 or len(carry) >= 16:
            break
    if len(carry) < 4 or len(selected_sources) < 2:
        return []

    target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for car in carry:
        target_line, _position, _reason = planned(car)
        if target_line:
            target_groups[target_line].append(car)
    if not any(line in REMOTE_INTERACTION_LINES for line in target_groups) and not any(
        line in REMOTE_INTERACTION_LINES for line, _batch in selected_sources
    ):
        return []

    steps, carry = build_carry_drop_steps(
        selected_sources=selected_sources,
        target_groups=target_groups,
        target_order=sorted(target_groups, key=lambda line: (line not in REMOTE_INTERACTION_LINES, line != "存4线", line)),
        cars=cars,
        depot_assignment=depot_assignment,
        grouped=grouped,
        length_load_lookup=length_load_lookup,
    )
    if len(steps) < 3 or len(carry) < 3:
        return []
    return [
        planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=selected_sources[0][0],
            target_line=steps[-1].line,
            batch=carry,
            generation_reason=(
                f"short_chain_contract;variant={manual_baseline.variant};"
                f"source_count={len(selected_sources)};target_count={len(target_groups)};"
                f"vehicle_count={len(carry)}"
            ),
            candidate_kind="short_chain_planlet",
            steps=tuple(steps),
            action_family_override="REPAIR_INBOUND"
            if any(line in REMOTE_INTERACTION_LINES for line in target_groups)
            else "DEPOT_OUTBOUND",
        )
    ]


def corridor_closeout_planlets(
    *,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    planned: Any,
    satisfied: Any,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[HookCandidate]:
    candidates: list[HookCandidate] = []
    for target_line in sorted(REMOTE_CORRIDOR_LINES | FRONT_SERVICE_TARGET_LINES):
        source_batches = planned_unsatisfied_by_line(
            grouped=grouped,
            planned=planned,
            satisfied=satisfied,
            source_filter=lambda line: line not in REMOTE_INTERACTION_LINES and line != target_line,
            target_filter=lambda line, target_line=target_line: line == target_line,
            max_per_line=8,
        )
        if not source_batches:
            continue
        selected_sources, carry = select_source_batches(
            source_batches,
            max_sources=4,
            max_per_source=8,
            remote_first=False,
        )
        if len(carry) < 2:
            continue
        all_nos = {car_no(car) for car in carry}
        positions = planned_positions_for_batch(
            batch=carry,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_nos,
            grouped=grouped,
        )
        if len(positions) != len(carry):
            continue
        if not candidate_positions_available(target_line, positions, cars, all_nos, grouped):
            continue
        if not line_has_length_capacity(target_line, cars, carry, all_nos, length_load_lookup, grouped):
            continue
        steps = [
            PlanStep("Get", line, tuple(car_no(car) for car in batch))
            for line, batch in selected_sources
        ]
        steps.append(PlanStep("Put", target_line, tuple(car_no(car) for car in carry), positions))
        candidates.append(
            planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=selected_sources[0][0],
                target_line=target_line,
                batch=carry,
                generation_reason=(
                    "corridor_closeout_planlet;"
                    f"target_line={target_line};source_count={len(selected_sources)};"
                    f"vehicle_count={len(carry)}"
                ),
                candidate_kind="corridor_closeout_planlet",
                steps=tuple(steps),
                action_family_override=action_family(selected_sources[0][0], target_line, any(car.get("IsWeigh") for car in carry)),
            )
        )
        if len(candidates) >= 3:
            break
    return candidates


def source_clear_and_restore_planlet(
    *,
    case_id: str,
    hook_index: int,
    line: str,
    blocker_batch: list[dict[str, Any]],
    target_batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    planned: Any,
) -> HookCandidate | None:
    if not blocker_batch or not target_batch or not target_line or target_line == line:
        return None
    carry = [*blocker_batch, *target_batch]
    if any(car.get("IsWeigh") for car in carry):
        return None
    if pull_equivalent(carry) > PULL_LIMIT_EQUIVALENT:
        return None
    if not reverse_length_fits(line, target_line, carry):
        return None

    all_nos = {car_no(car) for car in carry}
    target_positions = planned_positions_for_batch(
        batch=target_batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=all_nos,
        grouped=grouped,
    )
    if len(target_positions) != len(target_batch):
        return None
    if not candidate_positions_available(target_line, target_positions, cars, all_nos, grouped):
        return None
    if not line_has_length_capacity(target_line, cars, target_batch, all_nos, length_load_lookup, grouped):
        return None

    restore_positions = {
        car_no(car): int(car.get("Position") or index)
        for index, car in enumerate(blocker_batch, start=1)
    }
    if not candidate_positions_available(line, restore_positions, cars, all_nos, grouped):
        return None

    blocked_first = car_no(target_batch[0])
    steps = (
        PlanStep("Get", line, tuple(car_no(car) for car in carry)),
        PlanStep("Put", target_line, tuple(car_no(car) for car in target_batch), target_positions),
        PlanStep("Put", line, tuple(car_no(car) for car in blocker_batch), restore_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=line,
        target_line=line,
        batch=carry,
        generation_reason=(
            "source_clear_and_restore_planlet;"
            f"line={line};target_line={target_line};blocked_first={blocked_first};"
            f"blocker_count={len(blocker_batch)};target_count={len(target_batch)}"
        ),
        candidate_kind="source_clear_restore_planlet",
        steps=steps,
    )


def source_exit_clear_and_restore_planlet(
    *,
    case_id: str,
    hook_index: int,
    candidate: HookCandidate,
    line_cars: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    grouped: dict[str, list[dict[str, Any]]],
) -> HookCandidate | None:
    if candidate.has_weigh or candidate.plan_steps:
        return None
    if candidate.source_line == candidate.target_line:
        return None
    by_no = {car_no(car): car for car in cars}
    target_batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
    if len(target_batch) != len(candidate.move_car_nos):
        return None
    target_nos = {car_no(car) for car in target_batch}
    if any(car["Line"] != candidate.source_line for car in target_batch):
        return None

    blockers = [car for car in line_cars if car_no(car) not in target_nos]
    if not blockers:
        return None
    if any(car.get("IsWeigh") or is_locked_depot_stayer(car, depot_assignment) for car in blockers):
        return None
    carry = [car for car in line_cars if car_no(car) in target_nos or car in blockers]
    if pull_equivalent(carry) > PULL_LIMIT_EQUIVALENT:
        return None
    if not reverse_length_fits(candidate.source_line, candidate.target_line, carry):
        return None
    if not candidate_positions_available(
        candidate.target_line,
        candidate.planned_positions,
        cars,
        {car_no(car) for car in carry},
        grouped,
    ):
        return None

    restore_positions = {car_no(car): int(car.get("Position") or 0) for car in blockers}
    all_nos = {car_no(car) for car in carry}
    if not candidate_positions_available(candidate.source_line, restore_positions, cars, all_nos, grouped):
        return None

    steps = (
        PlanStep("Get", candidate.source_line, tuple(car_no(car) for car in carry)),
        PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions),
        PlanStep("Put", candidate.source_line, tuple(car_no(car) for car in blockers), restore_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=candidate.source_line,
        target_line=candidate.source_line,
        batch=carry,
        generation_reason=(
            "source_exit_clear_and_restore_planlet;"
            f"line={candidate.source_line};target_line={candidate.target_line};"
            f"target_count={len(target_batch)};restore_count={len(blockers)}"
        ),
        candidate_kind="source_exit_clear_restore_planlet",
        steps=steps,
    )


def source_exit_session_planlet(
    *,
    case_id: str,
    hook_index: int,
    candidate: HookCandidate,
    line_cars: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    grouped: dict[str, list[dict[str, Any]]],
    length_load_lookup: dict[str, float],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
) -> HookCandidate | None:
    if candidate.has_weigh or candidate.plan_steps:
        return None
    if candidate.source_line == candidate.target_line:
        return None
    by_no = {car_no(car): car for car in cars}
    target_batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
    if len(target_batch) != len(candidate.move_car_nos):
        return None
    target_nos = {car_no(car) for car in target_batch}
    if any(car["Line"] != candidate.source_line for car in target_batch):
        return None

    blockers = [car for car in line_cars if car_no(car) not in target_nos]
    if not blockers:
        return None
    if any(car.get("IsWeigh") or is_locked_depot_stayer(car, depot_assignment) for car in blockers):
        return None
    if pull_equivalent(blockers) > PULL_LIMIT_EQUIVALENT:
        return None
    if pull_equivalent([*blockers, *target_batch]) > PULL_LIMIT_EQUIVALENT:
        return None

    staging_line = choose_staging_line(
        cars,
        blockers,
        {candidate.source_line, candidate.target_line},
        length_load_lookup,
        staging_priority,
        depot_aware_staging,
    )
    if not staging_line:
        return None
    blocker_nos = {car_no(car) for car in blockers}
    all_nos = blocker_nos | target_nos
    staging_positions = first_free_positions_for_batch(cars, staging_line, blockers, blocker_nos)
    if not candidate_positions_available(staging_line, staging_positions, cars, blocker_nos, grouped):
        return None
    if not line_has_length_capacity(staging_line, cars, blockers, blocker_nos, length_load_lookup, grouped):
        return None
    if not candidate_positions_available(candidate.target_line, candidate.planned_positions, cars, all_nos, grouped):
        return None
    if not line_has_length_capacity(candidate.target_line, cars, target_batch, all_nos, length_load_lookup, grouped):
        return None
    if not reverse_length_fits(candidate.source_line, staging_line, blockers):
        return None
    if not reverse_length_fits(candidate.source_line, candidate.target_line, target_batch):
        return None

    steps = (
        PlanStep("Get", candidate.source_line, tuple(car_no(car) for car in blockers)),
        PlanStep("Put", staging_line, tuple(car_no(car) for car in blockers), staging_positions),
        PlanStep("Get", candidate.source_line, candidate.move_car_nos),
        PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=candidate.source_line,
        target_line=candidate.target_line,
        batch=[*blockers, *target_batch],
        generation_reason=(
            "source_exit_session_planlet;"
            f"line={candidate.source_line};staging_line={staging_line};"
            f"target_line={candidate.target_line};target_count={len(target_batch)};"
            f"corridor_blocker_count={len(blockers)}"
        ),
        candidate_kind="source_exit_session_planlet",
        steps=steps,
        action_family_override=candidate.action_family,
    )


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
    available_path = graph.route_avoiding_occupied(start_node, target_line, occupied)
    if available_path:
        return static_path, available_path, ()

    blockers: list[str] = []
    route_endpoints = {normalize_line(start_node), normalize_line(target_line)}
    for left, right in zip(static_path, static_path[1:]):
        for line in (left, right, *SWITCH_EDGE_TRACKS.get(frozenset((left, right)), ())):
            if line in occupied and line not in route_endpoints and line not in blockers:
                blockers.append(line)
    return static_path, [], tuple(blockers)


def route_clear_and_restore_planlet(
    *,
    case_id: str,
    hook_index: int,
    candidate: HookCandidate,
    blocker_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
) -> HookCandidate | None:
    if candidate.has_weigh or candidate.plan_steps or blocker_line in RUNNING_LINES:
        return None
    if blocker_line in {candidate.source_line, candidate.target_line}:
        return None

    blockers = list(grouped.get(blocker_line, []))
    if not blockers:
        return None
    if any(car.get("IsWeigh") or is_locked_depot_stayer(car, depot_assignment) for car in blockers):
        return None
    by_no = {car_no(car): car for car in cars}
    target_batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
    if len(target_batch) != len(candidate.move_car_nos):
        return None
    if pull_equivalent(blockers) > PULL_LIMIT_EQUIVALENT:
        return None
    if pull_equivalent([*blockers, *target_batch]) > PULL_LIMIT_EQUIVALENT:
        return None

    staging_line = choose_staging_line(
        cars,
        blockers,
        {blocker_line, candidate.source_line, candidate.target_line},
        length_load_lookup,
        staging_priority,
        depot_aware_staging,
    )
    if not staging_line:
        return None
    blocker_nos = {car_no(car) for car in blockers}
    if not reverse_length_fits(blocker_line, staging_line, blockers):
        return None
    if not reverse_length_fits(candidate.source_line, candidate.target_line, target_batch):
        return None
    if not reverse_length_fits(staging_line, blocker_line, blockers):
        return None

    staging_positions = first_free_positions_for_batch(cars, staging_line, blockers, blocker_nos)
    if not candidate_positions_available(staging_line, staging_positions, cars, blocker_nos, grouped):
        return None
    if not line_has_length_capacity(staging_line, cars, blockers, blocker_nos, length_load_lookup, grouped):
        return None
    restore_positions = {car_no(car): int(car.get("Position") or 0) for car in blockers}
    all_nos = blocker_nos | set(candidate.move_car_nos)
    if not candidate_positions_available(blocker_line, restore_positions, cars, all_nos, grouped):
        return None

    blocker_tuple = tuple(car_no(car) for car in blockers)
    steps = (
        PlanStep("Get", blocker_line, blocker_tuple),
        PlanStep("Put", staging_line, blocker_tuple, staging_positions),
        PlanStep("Get", candidate.source_line, candidate.move_car_nos),
        PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions),
        PlanStep("Get", staging_line, blocker_tuple),
        PlanStep("Put", blocker_line, blocker_tuple, restore_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=blocker_line,
        target_line=blocker_line,
        batch=[*blockers, *target_batch],
        generation_reason=(
            "route_clear_and_restore_planlet;"
            f"blocker_line={blocker_line};staging_line={staging_line};"
            f"move={candidate.source_line}->{candidate.target_line};"
            f"blocker_count={len(blockers)};target_count={len(target_batch)}"
        ),
        candidate_kind="route_clear_restore_planlet",
        steps=steps,
    )


def route_clear_session_planlet(
    *,
    case_id: str,
    hook_index: int,
    candidate: HookCandidate,
    blocker_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
) -> HookCandidate | None:
    if candidate.has_weigh or candidate.plan_steps or blocker_line in RUNNING_LINES:
        return None
    if blocker_line in {candidate.source_line, candidate.target_line}:
        return None

    blockers = list(grouped.get(blocker_line, []))
    if not blockers:
        return None
    if any(car.get("IsWeigh") or is_locked_depot_stayer(car, depot_assignment) for car in blockers):
        return None
    by_no = {car_no(car): car for car in cars}
    target_batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
    if len(target_batch) != len(candidate.move_car_nos):
        return None
    if pull_equivalent(blockers) > PULL_LIMIT_EQUIVALENT:
        return None
    if pull_equivalent([*blockers, *target_batch]) > PULL_LIMIT_EQUIVALENT:
        return None

    staging_line = choose_staging_line(
        cars,
        blockers,
        {blocker_line, candidate.source_line, candidate.target_line},
        length_load_lookup,
        staging_priority,
        depot_aware_staging,
    )
    if not staging_line:
        return None
    blocker_nos = {car_no(car) for car in blockers}
    all_nos = blocker_nos | set(candidate.move_car_nos)
    staging_positions = first_free_positions_for_batch(cars, staging_line, blockers, blocker_nos)
    if not candidate_positions_available(staging_line, staging_positions, cars, blocker_nos, grouped):
        return None
    if not line_has_length_capacity(staging_line, cars, blockers, blocker_nos, length_load_lookup, grouped):
        return None
    if not candidate_positions_available(candidate.target_line, candidate.planned_positions, cars, all_nos, grouped):
        return None
    if not line_has_length_capacity(candidate.target_line, cars, target_batch, all_nos, length_load_lookup, grouped):
        return None
    if not reverse_length_fits(blocker_line, staging_line, blockers):
        return None
    if not reverse_length_fits(candidate.source_line, candidate.target_line, target_batch):
        return None

    blocker_tuple = tuple(car_no(car) for car in blockers)
    steps = (
        PlanStep("Get", blocker_line, blocker_tuple),
        PlanStep("Put", staging_line, blocker_tuple, staging_positions),
        PlanStep("Get", candidate.source_line, candidate.move_car_nos),
        PlanStep("Put", candidate.target_line, candidate.move_car_nos, candidate.planned_positions),
    )
    return planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=blocker_line,
        target_line=candidate.target_line,
        batch=[*blockers, *target_batch],
        generation_reason=(
            "route_clear_session_planlet;"
            f"blocker_line={blocker_line};staging_line={staging_line};"
            f"move={candidate.source_line}->{candidate.target_line};"
            f"corridor_blocker_count={len(blockers)};target_count={len(target_batch)}"
        ),
        candidate_kind="route_clear_session_planlet",
        steps=steps,
        action_family_override=candidate.action_family,
    )


def route_clear_planlets_for_candidate(
    *,
    graph: TrackGraph,
    loco_location: LocoLocation,
    candidate: HookCandidate,
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    length_load_lookup: dict[str, float],
    grouped: dict[str, list[dict[str, Any]]],
    staging_priority: tuple[str, ...],
    depot_aware_staging: bool,
) -> list[HookCandidate]:
    if candidate.plan_steps or candidate.candidate_kind != "target_move":
        return []
    moving_nos = set(candidate.move_car_nos)
    _get_static, get_path, get_blockers = route_blocking_lines(
        graph,
        cars,
        loco_location.node,
        candidate.source_line,
        moving_nos,
    )
    if get_blockers:
        session = route_clear_session_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            blocker_line=get_blockers[0],
            cars=cars,
            depot_assignment=depot_assignment,
            length_load_lookup=length_load_lookup,
            grouped=grouped,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        )
        restore = route_clear_and_restore_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            blocker_line=get_blockers[0],
            cars=cars,
            depot_assignment=depot_assignment,
            length_load_lookup=length_load_lookup,
            grouped=grouped,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        )
        return [candidate for candidate in (session, restore) if candidate]
    if not get_path:
        return []

    source_location = route_end_location(get_path, candidate.source_line)
    _put_static, _put_path, put_blockers = route_blocking_lines(
        graph,
        cars,
        source_location.node,
        candidate.target_line,
        moving_nos,
    )
    if not put_blockers:
        return []
    if put_blockers[0] == candidate.source_line:
        session = source_exit_session_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            line_cars=grouped.get(candidate.source_line, []),
            cars=cars,
            depot_assignment=depot_assignment,
            grouped=grouped,
            length_load_lookup=length_load_lookup,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        )
        restore = source_exit_clear_and_restore_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            line_cars=grouped.get(candidate.source_line, []),
            cars=cars,
            depot_assignment=depot_assignment,
            grouped=grouped,
        )
    else:
        session = route_clear_session_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            blocker_line=put_blockers[0],
            cars=cars,
            depot_assignment=depot_assignment,
            length_load_lookup=length_load_lookup,
            grouped=grouped,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        )
        restore = route_clear_and_restore_planlet(
            case_id=case_id,
            hook_index=hook_index,
            candidate=candidate,
            blocker_line=put_blockers[0],
            cars=cars,
            depot_assignment=depot_assignment,
            length_load_lookup=length_load_lookup,
            grouped=grouped,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        )
    return [candidate for candidate in (session, restore) if candidate]


def candidate_sort_key(candidate: HookCandidate) -> tuple[int, tuple[int, str, str], int, str]:
    kind_priority = {
        "short_chain_planlet": 0,
        "remote_session_contract_planlet": 1,
        "depot_digest_planlet": 1,
        "depot_slot_swap_planlet": 1,
        "route_clear_session_planlet": 2,
        "source_exit_session_planlet": 2,
        "remote_corridor_planlet": 2,
        "corridor_closeout_planlet": 3,
        "remote_session_planlet": 2,
        "remote_exchange_planlet": 3,
        "front_capacity_release_contract_planlet": 3,
        "front_capacity_digest_planlet": 2,
        "h1_carry_planlet": 4,
        "multi_drop_planlet": 5,
        "multi_pick_planlet": 8,
        "source_clear_restore_planlet": 9,
        "source_exit_clear_restore_planlet": 9,
        "same_line_reorder_planlet": 9,
        "route_clear_restore_planlet": 12,
        "target_move": 10,
        "depot_same_line_repack": 15,
        "same_line_stage_out": 20,
        "capacity_release_to_staging": 30,
        "spot_release_to_staging": 40,
        "blocker_relocation": 50,
    }.get(candidate.candidate_kind, 90)
    if candidate.action_family == "DEPOT_OUTBOUND":
        kind_priority -= 5
    if candidate.generation_reason.startswith(TAIL_CANDIDATE_PREFIXES):
        kind_priority -= 3
    return (
        kind_priority,
        line_priority(candidate.source_line, candidate.target_line),
        -planlet_business_hook_count(candidate),
        -len(candidate.move_car_nos),
        candidate.candidate_id,
    )


def build_candidates(
    case_id: str,
    hook_index: int,
    cars: list[dict[str, Any]],
    depot_assignment: DepotAssignment,
    staging_priority: tuple[str, ...] = STAGING_LINE_PRIORITY,
    depot_aware_staging: bool = False,
    graph: TrackGraph | None = None,
    loco_location: LocoLocation | None = None,
    enable_contract_planlets: bool = False,
    enable_remote_session_contracts: bool = False,
    enable_front_capacity_contracts: bool = False,
    suppress_ownerless_recovery: bool = False,
    manual_baseline: ManualBaseline | None = None,
) -> tuple[list[HookCandidate], list[CandidateAuditRow]]:
    planlet_candidates: list[HookCandidate] = []
    legacy_recovery_candidates: list[HookCandidate] = []
    direct_candidates: list[HookCandidate] = []
    release_candidates: list[HookCandidate] = []
    staging_candidates: list[HookCandidate] = []
    tail_candidates: list[HookCandidate] = []
    blocked_rows: list[CandidateAuditRow] = []
    grouped = cars_by_line(cars)
    load_lookup = line_loads(cars)
    length_load_lookup_cache: dict[str, float] | None = None
    satisfied_nos = {car_no(car) for car in cars if car_is_satisfied(car, depot_assignment, cars)}
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

    def add_route_clear_planlets(candidate: HookCandidate) -> None:
        if not graph or not loco_location:
            return
        for planlet in route_clear_planlets_for_candidate(
            graph=graph,
            loco_location=loco_location,
            candidate=candidate,
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            length_load_lookup=length_load_lookup(),
            grouped=grouped,
            staging_priority=staging_priority,
            depot_aware_staging=depot_aware_staging,
        ):
            if planlet.candidate_kind in OWNERLESS_RECOVERY_KINDS:
                legacy_recovery_candidates.append(planlet)
            else:
                planlet_candidates.append(planlet)
        return

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
                        tail_candidates.append(repack)
                reorder = same_line_reorder_planlet(
                    case_id=case_id,
                    hook_index=hook_index,
                    line=target_line,
                    seed_batch=stage_batch,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    grouped=grouped,
                    generation_reason=(
                        f"tail_same_line_reorder;vehicle={car_no(car)};"
                        f"target_line={target_line};batch_count={len(stage_batch)}"
                    ),
                )
                if reorder:
                    tail_candidates.append(reorder)
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
                        staging_priority=staging_priority,
                        depot_aware_staging=depot_aware_staging,
                    )
                    if stage is None:
                        stage_batch = stage_batch[:-1]
                if stage:
                    tail_candidates.append(stage)
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
                        tail_candidates.append(direct_group)
                        add_route_clear_planlets(direct_group)
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
                tail_candidates.append(direct)
                add_route_clear_planlets(direct)

    for line, line_cars in sorted(grouped.items()):
        line_unsatisfied = [car for car in line_cars if not satisfied(car)]
        if not line_unsatisfied:
            continue
        multi_drop = multi_drop_planlet_for_line(
            case_id=case_id,
            hook_index=hook_index,
            line=line,
            line_cars=line_cars,
            cars=cars,
            depot_assignment=depot_assignment,
            planned=planned,
            satisfied=satisfied,
            length_load_lookup=length_load_lookup(),
            grouped=grouped,
        )
        if multi_drop:
            planlet_candidates.append(multi_drop)
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
            target_line, _position, _target_reason = planned(first_unsatisfied)
            target_batch = first_front_batch_to_target(
                line_cars=[
                    car
                    for car in line_cars
                    if int(car.get("Position") or 0) >= int(first_unsatisfied.get("Position") or 0)
                ],
                target_line=target_line,
                planned=planned,
                satisfied=satisfied,
                max_remaining_pull=PULL_LIMIT_EQUIVALENT - pull_equivalent(blocker_batch),
            )
            clear_restore = source_clear_and_restore_planlet(
                case_id=case_id,
                hook_index=hook_index,
                line=line,
                blocker_batch=blocker_batch,
                target_batch=target_batch,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
                planned=planned,
            )
            if clear_restore:
                if suppress_ownerless_recovery:
                    legacy_recovery_candidates.append(clear_restore)
                else:
                    planlet_candidates.append(clear_restore)
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
                    staging_priority=staging_priority,
                    depot_aware_staging=depot_aware_staging,
                )
                if relocation is None:
                    blocker_batch = blocker_batch[:-1]
            if relocation:
                if suppress_ownerless_recovery:
                    release_candidates.append(relocation)
                else:
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
            if any(item.get("IsWeigh") for item in batch) and not car.get("IsWeigh"):
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
            if first_unsatisfied.get("IsWeigh") and not first_unsatisfied.get("_Weighed"):
                direct = direct_candidate_for_batch(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=line,
                    target_line=target_line,
                    batch=batch,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    generation_reason=(
                        f"same_line_weigh_completion;vehicle={car_no(first_unsatisfied)};"
                        f"target_line={target_line};batch_count={len(batch)}"
                    ),
                    grouped=grouped,
                )
                if direct:
                    direct_candidates.append(direct)
                    continue
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
            reorder = same_line_reorder_planlet(
                case_id=case_id,
                hook_index=hook_index,
                line=target_line,
                seed_batch=stage_batch,
                cars=cars,
                depot_assignment=depot_assignment,
                grouped=grouped,
                generation_reason=(
                    f"same_line_reorder;vehicle={car_no(first_unsatisfied)};"
                    f"target_line={target_line};batch_count={len(stage_batch)}"
                ),
            )
            if reorder:
                planlet_candidates.append(reorder)
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
                    staging_priority=staging_priority,
                    depot_aware_staging=depot_aware_staging,
                )
                if stage is None:
                    stage_batch = stage_batch[:-1]
            if stage:
                staging_candidates.append(stage)
            continue

        direct_batch = list(batch)
        direct_added = False
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
                add_route_clear_planlets(direct)
                direct_added = True
                break
            direct_batch = direct_batch[:-1]
        if not direct_added and enable_contract_planlets and enable_front_capacity_contracts:
            contract_release = front_capacity_release_contract_planlet(
                case_id=case_id,
                hook_index=hook_index,
                source_line=line,
                target_line=target_line,
                owner_batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
                staging_priority=staging_priority,
                depot_aware_staging=depot_aware_staging,
                generation_reason=(
                    f"owner_first_accessible_unsatisfied_car={car_no(first_unsatisfied)};"
                    f"target_reason={target_reason};owner_batch_count={len(batch)}"
                ),
            )
            if contract_release:
                planlet_candidates.append(contract_release)

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
                staging_priority=staging_priority,
                depot_aware_staging=depot_aware_staging,
            )
            if release_stage:
                if suppress_ownerless_recovery:
                    staging_candidates.append(release_stage)
                else:
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
                    staging_priority=staging_priority,
                    depot_aware_staging=depot_aware_staging,
                )
                if release_stage:
                    if suppress_ownerless_recovery:
                        staging_candidates.append(release_stage)
                    else:
                        release_candidates.append(release_stage)
                    break

    planlet_candidates.extend(
        multi_pick_planlets(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            planned=planned,
            satisfied=satisfied,
            length_load_lookup=length_load_lookup(),
            grouped=grouped,
            remote_only=False,
        )
    )
    planlet_candidates.extend(
        multi_pick_planlets(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            planned=planned,
            satisfied=satisfied,
            length_load_lookup=length_load_lookup(),
            grouped=grouped,
            remote_only=True,
        )
    )
    if enable_contract_planlets:
        planlet_candidates.extend(
            h1_carry_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        if enable_front_capacity_contracts:
            planlet_candidates.extend(
                front_capacity_digest_planlets(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    planned=planned,
                    satisfied=satisfied,
                    length_load_lookup=length_load_lookup(),
                    grouped=grouped,
                    staging_priority=staging_priority,
                    depot_aware_staging=depot_aware_staging,
                )
            )
        planlet_candidates.extend(
            remote_exchange_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        planlet_candidates.extend(
            corridor_closeout_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
    if enable_remote_session_contracts:
        planlet_candidates.extend(
            depot_digest_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        planlet_candidates.extend(
            depot_slot_swap_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        planlet_candidates.extend(
            remote_session_contract_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        planlet_candidates.extend(
            remote_corridor_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
            )
        )
        planlet_candidates.extend(
            short_chain_planlets(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                satisfied=satisfied,
                length_load_lookup=length_load_lookup(),
                grouped=grouped,
                manual_baseline=manual_baseline,
            )
        )

    ordered_candidates: list[HookCandidate] = [*tail_candidates, *planlet_candidates, *direct_candidates]
    if not suppress_ownerless_recovery or not ordered_candidates:
        ordered_candidates.extend(legacy_recovery_candidates)
        ordered_candidates.extend(release_candidates)
        ordered_candidates.extend(staging_candidates)
    unique: dict[str, HookCandidate] = {}
    for candidate in ordered_candidates:
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
        depot_assignment: DepotAssignment,
    ) -> PhysicalValidation:
        if candidate.plan_steps:
            return self._validate_planlet(candidate, cars, loco_location, depot_assignment)
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
            (
                candidate.candidate_kind == "depot_same_line_repack"
                and candidate.target_line in DEPOT_LINES
            )
            or (candidate.has_weigh and candidate.candidate_kind == "target_move")
        ):
            reasons.append("same_line_reposition_requires_staging_search")
        if candidate.pull_equivalent_count > PULL_LIMIT_EQUIVALENT:
            reasons.append("pull_limit_violation")
        if candidate.has_weigh and batch and not batch[-1].get("IsWeigh"):
            reasons.append("weigh_car_not_last_in_carry_order")

        train_with_loco = candidate.train_length_m + LOCO_LENGTH_M
        for line in {candidate.source_line, candidate.target_line}:
            available = REVERSAL_DISTANCE_M.get(line, TRACK_SPECS.get(line, TrackSpec(line, 0, "")).length_m)
            if available and train_with_loco > available + 1e-6:
                reasons.append(f"reverse_length_violation:{line}:{train_with_loco:.1f}>{available:.1f}")

        active_depot_assignment = current_depot_assignment(depot_assignment, cars)
        position_reasons = self._validate_target_positions(candidate, cars, batch, target_line_cars, active_depot_assignment)
        reasons.extend(position_reasons)
        reasons.extend(self._validate_closed_door(candidate, cars, batch))

        return PhysicalValidation(
            accepted=not reasons,
            reasons=tuple(reasons),
            get_path=tuple(get_path),
            weigh_path=tuple(weigh_path),
            put_path=tuple(put_path),
            operation_paths=tuple(tuple(path) for path in (get_path, weigh_path, put_path) if path),
        )

    def _validate_planlet(
        self,
        candidate: HookCandidate,
        cars: list[dict[str, Any]],
        loco_location: LocoLocation,
        depot_assignment: DepotAssignment,
    ) -> PhysicalValidation:
        reasons: list[str] = []
        working_cars = [dict(car) for car in cars]
        current_loco = loco_location
        carried: set[str] = set()
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
                occupied_lines = occupied_lines_for_route(working_cars, step_nos | carried)
                raw_path = self.graph.route_avoiding_occupied(current_loco.node, step.line, occupied_lines)
                static_path = self.graph.route(current_loco.node, step.line)
                path = tuple(route_with_line_prefix(current_loco.line, raw_path))
                if not path:
                    reasons.append("get_route_blocked_by_occupied_line" if static_path else "get_route_missing")
                    break
                if step.line in RUNNING_LINES:
                    reasons.append("running_line_stop_violation")
                    break
                if step.line not in TRACK_SPECS:
                    reasons.append("source_line_unknown")
                    break
                operation_paths.append(path)
                if not get_path:
                    get_path = path
                carried.update(step_nos)
                current_loco = operation_stand_location(path, step.line)
                continue

            if step.action == "Put":
                if not step_nos <= carried:
                    reasons.append(f"planlet_put_without_carry:step={index}")
                    break
                batch = [by_no[no] for no in step.move_car_nos if no in by_no]
                occupied_lines = occupied_lines_for_route(working_cars, carried)
                raw_path = self.graph.route_avoiding_occupied(current_loco.node, step.line, occupied_lines)
                static_path = self.graph.route(current_loco.node, step.line)
                path = tuple(route_with_line_prefix(current_loco.line, raw_path))
                if not path:
                    reasons.append("put_route_blocked_by_occupied_line" if static_path else "put_route_missing")
                    break
                if step.line in RUNNING_LINES:
                    reasons.append("running_line_stop_violation")
                    break
                if step.line not in TRACK_SPECS:
                    reasons.append("target_line_unknown")
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
                existing_target_cars = [
                    car
                    for car in working_cars
                    if car["Line"] == step.line and car_no(car) not in carried
                ]
                active_depot_assignment = current_depot_assignment(depot_assignment, working_cars)
                reasons.extend(self._validate_target_positions(step_candidate, working_cars, batch, existing_target_cars, active_depot_assignment))
                reasons.extend(self._validate_closed_door(step_candidate, working_cars, batch))
                if reasons:
                    break
                operation_paths.append(path)
                put_path = path
                for car in working_cars:
                    no = car_no(car)
                    if no in step_nos:
                        car["Line"] = step.line
                        car["Position"] = step.planned_positions.get(no, car.get("Position") or 0)
                if step.line not in DEPOT_LINES:
                    normalize_duplicate_positions(working_cars, step.line)
                carried.difference_update(step_nos)
                current_loco = operation_stand_location(path, step.line)
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

    def _validate_target_positions(
        self,
        candidate: HookCandidate,
        cars: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        target_line_cars: list[dict[str, Any]],
        depot_assignment: DepotAssignment,
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

        if is_spotting_line(candidate.target_line):
            projected = [dict(car) for car in cars]
            projected_by_no = {car_no(car): car for car in projected}
            for car in batch:
                no = car_no(car)
                if no not in projected_by_no:
                    continue
                projected_by_no[no]["Line"] = candidate.target_line
                projected_by_no[no]["Position"] = candidate.planned_positions.get(no, projected_by_no[no].get("Position") or 0)
            for car in batch:
                forced = force_positions(car)
                if not forced:
                    continue
                projected_car = projected_by_no.get(car_no(car))
                if projected_car is None:
                    continue
                projected_depot_assignment = current_depot_assignment(depot_assignment, projected)
                if not spotting_position_is_acceptable(projected_car, projected, candidate.target_line, projected_depot_assignment):
                    reasons.append(
                        f"spotting_position_window_violation:{car_no(car)}:{candidate.target_line}:"
                        f"{candidate.planned_positions.get(car_no(car), 0)}"
                    )
        elif candidate.target_line in DEPOT_LINES:
            capacity = 7 if max(planned_positions or [0]) > 5 else 5
            for car in batch:
                position = candidate.planned_positions.get(car_no(car), 0)
                if not slot_allowed_for_car(car, candidate.target_line, position, capacity):
                    reasons.append(f"depot_slot_rule_violation:{car_no(car)}:{candidate.target_line}:{position}")
            reasons.extend(depot_slot_hard_reasons(candidate, cars, batch, depot_assignment))
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

    def _validate_closed_door(
        self,
        candidate: HookCandidate,
        cars: list[dict[str, Any]],
        batch: list[dict[str, Any]],
    ) -> list[str]:
        if not any(car.get("IsClosedDoor") for car in cars):
            return []
        reasons: list[str] = []
        if candidate.target_line == "存4线":
            projected = [dict(car) for car in cars]
            projected_by_no = {car_no(car): car for car in projected}
            for car in batch:
                no = car_no(car)
                projected_car = projected_by_no.get(no)
                if projected_car is None:
                    continue
                projected_car["Line"] = candidate.target_line
                projected_car["Position"] = candidate.planned_positions.get(no, projected_car.get("Position") or 0)
            for car in projected:
                if car["Line"] == "存4线" and car.get("IsClosedDoor") and int(car.get("Position") or 0) <= 3:
                    reasons.append(f"closed_door_cun4_final_position_violation:{car_no(car)}:{int(car.get('Position') or 0)}")
            return reasons

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
    if candidate.plan_steps:
        rows: list[OperationTraceRow] = []
        paths = list(validation.operation_paths)
        for offset, step in enumerate(candidate_plan_steps(candidate)):
            path = paths[offset] if offset < len(paths) else ()
            train_cars = "|".join(step.move_car_nos) if step.action == "Get" else ""
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
        weigh_car = last_weigh_car_no(candidate.move_car_nos, None)
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
) -> None:
    if candidate.plan_steps:
        source_lines_by_step: list[tuple[str, set[str]]] = []
        for step in candidate_plan_steps(candidate):
            step_nos = set(step.move_car_nos)
            if step.action == "Get":
                source_lines_by_step.append((step.line, step_nos))
                continue
            if step.action != "Put":
                continue
            for car in cars:
                no = car_no(car)
                if no not in step_nos:
                    continue
                car["Line"] = step.line
                car["Position"] = step.planned_positions.get(no, car.get("Position") or 0)
            if step.line not in DEPOT_LINES:
                normalize_duplicate_positions(cars, step.line)
            for source_line, moved_nos in source_lines_by_step:
                if source_line != step.line:
                    compact_source_positions(cars, source_line, moved_nos)
            source_lines_by_step = [
                (source_line, moved_nos - step_nos)
                for source_line, moved_nos in source_lines_by_step
                if moved_nos - step_nos
            ]
        return
    move_nos = set(candidate.move_car_nos)
    weighed_no = last_weigh_car_no(candidate.move_car_nos, cars) if candidate.has_weigh else ""
    for car in cars:
        no = car_no(car)
        if no not in move_nos:
            continue
        car["Line"] = candidate.target_line
        car["Position"] = candidate.planned_positions[no]
        if no == weighed_no:
            car["_Weighed"] = True
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


def business_hook_count_so_far(operations: list[OperationTraceRow]) -> int:
    return sum(1 for row in operations if row.action in {"Get", "Put"})


def manual_expected_phase(manual_baseline: ManualBaseline | None, business_hook_index: int) -> str:
    if manual_baseline is None or not business_hook_index:
        return ""
    bounded_step = min(max(1, business_hook_index), manual_baseline.observed_hook_count)
    phase = phase_for_manual_step(manual_baseline.audit, bounded_step)
    if phase:
        return phase
    if business_hook_index > manual_baseline.observed_hook_count:
        return "H5"
    return ""


def phase_allowed_action(phase: str, action_family: str, manual_variant: str) -> bool:
    if not phase:
        return True
    if manual_variant in SHORT_CHAIN_VARIANTS and action_family in {"REPAIR_INBOUND", "DEPOT_OUTBOUND", "DEPOT_SLOT"}:
        return True
    if phase in {"H1", "H2"} and action_family not in {"REPAIR_INBOUND", "DEPOT_OUTBOUND", "DEPOT_SLOT"}:
        return True
    allowed = PHASE_ALLOWED_CANDIDATE_FAMILIES.get(phase, set())
    return not allowed or action_family in allowed


def phase_permission_for_candidate(
    *,
    candidate: HookCandidate,
    phase_state: PhaseState,
    phase_reason: str,
    manual_baseline: ManualBaseline | None,
    business_hook_index: int,
) -> tuple[str, str, str, bool]:
    expected_phase = manual_expected_phase(manual_baseline, business_hook_index)
    manual_variant = manual_baseline.variant if manual_baseline else ""
    over_manual_bound = bool(
        manual_baseline
        and business_hook_index > manual_baseline.soft_hook_upper_bound
    )
    if phase_reason:
        return expected_phase, "deferred_by_runtime_phase_gate", phase_reason, over_manual_bound
    if not manual_baseline:
        return expected_phase, "no_manual_baseline", "manual baseline unavailable; runtime permission only", over_manual_bound
    if over_manual_bound:
        return expected_phase, "over_manual_soft_hook_bound", "business hook index exceeds manual soft bound", over_manual_bound
    if not phase_allowed_action(expected_phase, candidate.action_family, manual_variant):
        return (
            expected_phase,
            "phase_action_mismatch",
            f"action_family={candidate.action_family} not allowed in expected_phase={expected_phase}",
            over_manual_bound,
        )
    if manual_variant == "FULL_CHAIN_REPAIR" and expected_phase in {"H1", "H2"} and candidate_touches_remote_interaction(candidate):
        return (
            expected_phase,
            "remote_too_early_against_manual_phase",
            f"manual_variant={manual_variant};expected_phase={expected_phase}",
            over_manual_bound,
        )
    return expected_phase, "allowed", "matches manual phase envelope", over_manual_bound


def selected_contract(candidate: HookCandidate) -> str:
    if candidate.candidate_kind in STAGING_CANDIDATE_KINDS:
        if candidate.candidate_kind == "blocker_relocation":
            return "BLOCKER_CLEARANCE"
        if candidate.candidate_kind.startswith("capacity"):
            return "CAPACITY_RELEASE"
        return "TEMPORARY_STAGING"
    return candidate.action_family


def structural_intent(candidate: HookCandidate) -> str:
    mapping = {
        "target_move": "MOVE_TO_PLANNED_TARGET",
        "front_capacity_release_contract_planlet": "FRONT_CAPACITY_RELEASE_CONTRACT",
        "front_capacity_digest_planlet": "FRONT_CAPACITY_DIGEST_PLANLET",
        "depot_same_line_repack": "DEPOT_SAME_LINE_REPACK",
        "same_line_stage_out": "SAME_LINE_REPOSITION_STAGE_OUT",
        "capacity_release_to_staging": "TARGET_CAPACITY_RELEASE_TO_STAGING",
        "spot_release_to_staging": "TARGET_SPOT_RELEASE_TO_STAGING",
        "blocker_relocation": "SOURCE_FRONT_BLOCKER_RELOCATION",
        "remote_session_contract_planlet": "REMOTE_SESSION_CONTRACT",
        "remote_corridor_planlet": "REMOTE_CORRIDOR_CONTRACT",
        "depot_digest_planlet": "DEPOT_DIGEST_PLANLET",
        "depot_slot_swap_planlet": "DEPOT_SLOT_SWAP_PLANLET",
        "short_chain_planlet": "SHORT_CHAIN_PLANLET",
        "route_clear_session_planlet": "ROUTE_CLEAR_SESSION_PLANLET",
        "source_exit_session_planlet": "SOURCE_EXIT_SESSION_PLANLET",
        "corridor_closeout_planlet": "CORRIDOR_CLOSEOUT_PLANLET",
    }
    return mapping.get(candidate.candidate_kind, candidate.candidate_kind.upper())


def hard_obligations_for_candidate(candidate: HookCandidate) -> str:
    obligations: list[str] = []
    if candidate.action_family in {"REPAIR_INBOUND", "DEPOT_SLOT"}:
        obligations.extend(["depot_slot_valid", "depot_route_available"])
    if candidate.action_family == "DEPOT_OUTBOUND":
        obligations.extend(["release_depot_blocker", "preserve_cun4_capacity"])
    if candidate.candidate_kind in {"front_capacity_release_contract_planlet", "front_capacity_digest_planlet"}:
        obligations.extend(["capacity_release_owner", "front_service_recovery_owner"])
    if (
        candidate_touches_remote_interaction(candidate)
        and (
            candidate.candidate_kind in REMOTE_CROSS_OWNER_KINDS
            or candidate.action_family in REMOTE_CROSS_OWNER_FAMILIES
        )
    ):
        obligations.append("remote_session_owner")
        if remote_cross_for_candidate(candidate):
            obligations.append("remote_cross_owner")
    if candidate.action_family == "SPECIAL_REPAIR_PROCESS":
        obligations.extend(["only_last_weigh_car_completes", "weigh_car_tail_order"])
    if candidate.candidate_kind in STAGING_CANDIDATE_KINDS:
        obligations.append("temporary_staging_must_be_recovered")
    if candidate.has_weigh:
        obligations.append("weigh_path_via_machine_line")
    return "|".join(obligations)


def protections_for_candidate(candidate: HookCandidate, expected_phase: str) -> str:
    protections: list[str] = []
    if expected_phase in {"H1", "H2"}:
        protections.append("protect_remote_depot_until_phase_open")
    if candidate.target_line == "存4线" or candidate.source_line == "存4线":
        protections.append("protect_cun4_north_buffer")
    if candidate.action_family in {"REPAIR_INBOUND", "DEPOT_SLOT"}:
        protections.append("protect_depot_slot_assignment")
    if candidate.candidate_kind in {"front_capacity_release_contract_planlet", "front_capacity_digest_planlet"}:
        protections.append("protect_front_service_capacity_release")
    if candidate_touches_remote_interaction(candidate):
        protections.append("protect_remote_session_continuity")
    if candidate.candidate_kind in REMOTE_SESSION_CONTRACT_KINDS:
        protections.append("protect_remote_session_contract")
    return "|".join(protections)


def suppressed_contracts_for_candidate(candidate: HookCandidate, expected_phase: str) -> str:
    suppressed: list[str] = []
    if expected_phase in {"H1", "H2"} and candidate_touches_remote_interaction(candidate):
        suppressed.append("REMOTE_DEPOT_WORK")
    if candidate.action_family in {"REPAIR_INBOUND", "DEPOT_OUTBOUND"}:
        suppressed.extend(["YARD_REBALANCE", "FUNCTION_LINE_SERVICE"])
    if candidate.candidate_kind in STAGING_CANDIDATE_KINDS:
        suppressed.append("DIRECT_TARGET_MOVE")
    return "|".join(dict.fromkeys(suppressed))


def requested_resources_for_candidate(candidate: HookCandidate, validation: PhysicalValidation) -> list[str]:
    resources = ["loco_position", "route_get", "route_put"]
    if candidate.has_weigh:
        resources.append("weighing_machine_line")
    if validation_crosses_link7(validation):
        resources.append("link7_gate")
    if candidate.source_line == "存4线" or candidate.target_line == "存4线":
        resources.append("cun4_north_buffer")
    if candidate.source_line in DEPOT_TARGET_LINES or candidate.target_line in DEPOT_TARGET_LINES:
        resources.append("depot_slot_graph")
    if candidate_touches_remote_interaction(candidate):
        resources.append("remote_session")
    if candidate.candidate_kind in REMOTE_SESSION_CONTRACT_KINDS:
        resources.append("remote_session_contract")
    if (
        remote_cross_for_candidate(candidate)
        and (
            candidate.candidate_kind in REMOTE_CROSS_OWNER_KINDS
            or candidate.action_family in REMOTE_CROSS_OWNER_FAMILIES
        )
    ):
        resources.append("remote_cross_owner")
    if candidate.candidate_kind in STAGING_CANDIDATE_KINDS:
        resources.append("temporary_staging_track")
    return list(dict.fromkeys(resources))


def line_set_for_path(path: tuple[str, ...] | list[str]) -> set[str]:
    return {item for item in route_for_output(path) if item in TRACK_SPECS or item in RUNNING_LINES}


def remote_event_for_candidate(candidate: HookCandidate) -> str:
    if candidate.plan_steps:
        lines = planlet_line_sequence(candidate)
        touched = [line for line in lines if line in REMOTE_INTERACTION_LINES]
        if not touched:
            return "pass_or_none"
        if all(line in REMOTE_INTERACTION_LINES for line in lines):
            return "remote_internal"
        if lines and lines[0] in REMOTE_INTERACTION_LINES:
            return "exit_remote"
        if lines and lines[-1] in REMOTE_INTERACTION_LINES:
            return "enter_remote"
        return "mixed_remote_session"
    source_remote = candidate.source_line in REMOTE_INTERACTION_LINES
    target_remote = candidate.target_line in REMOTE_INTERACTION_LINES
    if source_remote and target_remote:
        return "remote_internal"
    if source_remote:
        return "exit_remote"
    if target_remote:
        return "enter_remote"
    return "pass_or_none"


def remote_cross_for_candidate(candidate: HookCandidate) -> bool:
    if candidate.plan_steps:
        return planlet_remote_cross_count(candidate) > 0
    return (candidate.source_line in REMOTE_INTERACTION_LINES) != (candidate.target_line in REMOTE_INTERACTION_LINES)


def remote_cross_has_owner(candidate: HookCandidate) -> bool:
    if not remote_cross_for_candidate(candidate):
        return True
    if candidate.candidate_kind in REMOTE_CROSS_OWNER_KINDS:
        return True
    if candidate.action_family in REMOTE_CROSS_OWNER_FAMILIES:
        return True
    reason = candidate.generation_reason
    return any(token in reason for token in ("remote_session_contract", "remote_corridor_contract", "short_chain_contract"))


def contract_zero_or_negative_allowed(candidate: HookCandidate, reduction: int, non_depot_reduction: int) -> bool:
    if reduction > 0 or non_depot_reduction > 0:
        return True
    if candidate.candidate_kind in OWNER_BOUND_RECOVERY_KINDS:
        return True
    if candidate_touches_remote_interaction(candidate) and candidate.action_family in {
        "REPAIR_INBOUND",
        "DEPOT_OUTBOUND",
        "DEPOT_SLOT",
    } and remote_cross_has_owner(candidate):
        return True
    return False


def contract_trace_has_unlock_owner(row: ContractTraceRow) -> bool:
    if row.unsatisfied_reduction > 0 or row.non_depot_unsatisfied_reduction > 0:
        return True
    return row.structural_intent in {
        "FRONT_CAPACITY_RELEASE_CONTRACT",
        "REMOTE_SESSION_CONTRACT",
        "REMOTE_CORRIDOR_CONTRACT",
        "DEPOT_DIGEST_PLANLET",
        "DEPOT_SLOT_SWAP_PLANLET",
        "SHORT_CHAIN_PLANLET",
    }


def structural_reject_reason(
    *,
    candidate: HookCandidate,
    validation: PhysicalValidation,
    phase_reason: str,
    reduction: int,
    non_depot_reduction: int,
    physically_accepted: list[tuple[HookCandidate, PhysicalValidation, str]],
    transition_metrics: Any,
    repair_mode: str,
    manual_baseline: ManualBaseline | None = None,
) -> str:
    hook_count = max(1, planlet_business_hook_count(candidate))
    remote_cross_count = planlet_remote_cross_count(candidate)

    def better_non_recovery_available() -> bool:
        for other_candidate, other_validation, other_phase_reason in physically_accepted:
            if other_candidate.candidate_id == candidate.candidate_id or other_phase_reason:
                continue
            if other_candidate.candidate_kind in {
                "route_clear_restore_planlet",
                "source_exit_clear_restore_planlet",
                "route_clear_session_planlet",
                "source_exit_session_planlet",
                "remote_corridor_planlet",
            }:
                continue
            other_reduction, other_non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(
                other_candidate,
                other_validation,
            )
            if other_reduction >= reduction and other_non_depot_reduction >= non_depot_reduction:
                return True
        return False

    if candidate.candidate_kind in {
        "route_clear_restore_planlet",
        "source_exit_clear_restore_planlet",
        "remote_corridor_planlet",
    }:
        min_reduction = 2 if hook_count <= 4 else 3
        if reduction < min_reduction and non_depot_reduction <= 0 and better_non_recovery_available():
            return (
                "p7_reject_low_yield_restore_session:"
                f"kind={candidate.candidate_kind}:business_hooks={hook_count}:"
                f"unsatisfied_reduction={reduction}:non_depot_reduction={non_depot_reduction}"
            )
    if candidate.candidate_kind in {"route_clear_session_planlet", "source_exit_session_planlet"}:
        min_reduction = 2 if hook_count <= 4 else 3
        if reduction < min_reduction and non_depot_reduction <= 0 and better_non_recovery_available():
            return (
                "p7_reject_low_yield_corridor_session:"
                f"kind={candidate.candidate_kind}:business_hooks={hook_count}:"
                f"unsatisfied_reduction={reduction}:non_depot_reduction={non_depot_reduction}"
            )
    if (
        candidate.candidate_kind in REMOTE_SESSION_CONTRACT_KINDS
        and remote_cross_count > 2
        and reduction < max(3, remote_cross_count)
        and non_depot_reduction <= 0
        and better_non_recovery_available()
    ):
        return (
            "p7_reject_uncompressed_remote_session:"
            f"kind={candidate.candidate_kind}:remote_cross={remote_cross_count}:"
            f"unsatisfied_reduction={reduction}:non_depot_reduction={non_depot_reduction}"
        )
    if repair_mode != ALIGNED_REPAIR_MODE:
        return ""
    if phase_reason:
        return phase_reason
    if manual_baseline and manual_baseline.variant in SHORT_CHAIN_VARIANTS:
        return ""
    if (
        candidate.candidate_kind in OWNERLESS_RECOVERY_KINDS
        and reduction <= 0
        and non_depot_reduction <= 0
    ):
        return (
            "p4_contract_selector_reject_ownerless_nonpositive_recovery:"
            f"kind={candidate.candidate_kind}:contract={selected_contract(candidate)}:"
            f"unsatisfied_reduction={reduction}:non_depot_reduction={non_depot_reduction}"
        )
    if not contract_zero_or_negative_allowed(candidate, reduction, non_depot_reduction):
        return (
            "p4_contract_selector_reject_zero_or_negative_delta:"
            f"contract={selected_contract(candidate)}:"
            f"unsatisfied_reduction={reduction}:non_depot_reduction={non_depot_reduction}"
        )
    remote_cross = remote_cross_for_candidate(candidate)
    for other_candidate, other_validation, other_phase_reason in physically_accepted:
        if other_candidate.candidate_id == candidate.candidate_id or other_phase_reason:
            continue
        other_reduction, other_non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(
            other_candidate,
            other_validation,
        )
        if (
            remote_cross
            and not remote_cross_for_candidate(other_candidate)
            and other_reduction >= reduction
            and other_non_depot_reduction >= non_depot_reduction
        ):
            return (
                "p7_reject_same_progress_without_remote_cross:"
                f"dominated_by={other_candidate.candidate_id}"
            )
        if (
            other_reduction > reduction
            and selected_contract(other_candidate) == selected_contract(candidate)
            and other_candidate.action_family == candidate.action_family
        ):
            return (
                "p7_reject_dominated_same_contract:"
                f"dominated_by={other_candidate.candidate_id}:"
                f"selected_reduction={reduction}:best_reduction={other_reduction}"
            )
    return ""


def aligned_selection_key(
    item: tuple[HookCandidate, PhysicalValidation, str],
    *,
    transition_metrics: Any,
    phase_state: PhaseState,
    last_hook_touched_remote: bool,
    remote_streak_count: int,
    last_business_hook_remote: bool | None = None,
) -> tuple[int, int, int, int, tuple[int, int, tuple[int, tuple[int, str, str], int, str]]]:
    candidate, _validation, phase_reason = item
    reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(candidate, item[1])
    continue_remote_session = (
        last_hook_touched_remote
        and remote_streak_count < REMOTE_CONTINUITY_MAX_STREAK
    )
    continuity_penalty = (
        0
        if not continue_remote_session or candidate_touches_remote_interaction(candidate)
        else 1
    )
    phase_penalty = 1 if phase_reason else 0
    hook_count = max(1, planlet_business_hook_count(candidate))
    remote_cross_penalty = candidate_remote_business_transition_count(candidate, last_business_hook_remote)
    return (
        phase_penalty,
        round(-reduction / hook_count, 4),
        round(-non_depot_reduction / hook_count, 4),
        remote_cross_penalty + continuity_penalty,
        phase_candidate_sort_key(candidate, phase_state),
    )


def contract_planlet_selection_key(
    item: tuple[HookCandidate, PhysicalValidation, str],
    *,
    transition_metrics: Any,
    phase_state: PhaseState,
    last_hook_touched_remote: bool,
    remote_streak_count: int,
    last_business_hook_remote: bool | None = None,
) -> tuple[int, int, int, int, int, int, int, tuple[int, int, tuple[int, tuple[int, str, str], int, str]]]:
    candidate, _validation, phase_reason = item
    reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(candidate, item[1])
    hook_count = max(1, planlet_business_hook_count(candidate))
    continue_remote_session = (
        last_hook_touched_remote
        and remote_streak_count < REMOTE_CONTINUITY_MAX_STREAK
    )
    continuity_penalty = (
        0
        if not continue_remote_session or candidate_touches_remote_interaction(candidate)
        else 1
    )
    phase_penalty = 1 if phase_reason else 0
    template_priority = 0 if candidate.candidate_kind in {
        "short_chain_planlet",
        "remote_session_contract_planlet",
        "depot_digest_planlet",
        "depot_slot_swap_planlet",
        "remote_corridor_planlet",
        "remote_exchange_planlet",
        "h1_carry_planlet",
        "front_capacity_digest_planlet",
        "corridor_closeout_planlet",
        "remote_session_planlet",
        "multi_drop_planlet",
        "multi_pick_planlet",
    } else 1
    if candidate.candidate_kind in {"front_capacity_release_contract_planlet", "front_capacity_digest_planlet"}:
        template_priority = 0 if reduction > 0 or non_depot_reduction > 0 else 2
    remote_cross_penalty = candidate_remote_business_transition_count(candidate, last_business_hook_remote)
    return (
        phase_penalty,
        -reduction,
        -non_depot_reduction,
        template_priority,
        hook_count,
        remote_cross_penalty,
        continuity_penalty,
        phase_candidate_sort_key(candidate, phase_state),
    )


def build_candidate_pool_audit_row(
    *,
    case_id: str,
    solve_strategy: str,
    phase_state: PhaseState,
    selected: HookCandidate,
    selected_phase_reason: str,
    business_hook_index_start: int,
    candidates: list[HookCandidate],
    physically_accepted: list[tuple[HookCandidate, PhysicalValidation, str]],
    transition_metrics: Any,
) -> CandidatePoolAuditRow:
    phase_permitted = [item for item in physically_accepted if not item[2]]
    front_items = [item for item in phase_permitted if candidate_is_front_service(item[0])]
    remote_items = [item for item in phase_permitted if candidate_touches_remote_interaction(item[0])]

    def best_item(items: list[tuple[HookCandidate, PhysicalValidation, str]]) -> tuple[HookCandidate | None, int, int]:
        if not items:
            return None, 0, 0
        best_candidate: HookCandidate | None = None
        best_delta = 0
        best_non_depot_delta = 0
        best_key: tuple[float, float, int, str] | None = None
        for candidate, validation, _phase_reason in items:
            delta, non_depot_delta, _after_unsat, _after_non_depot = transition_metrics(candidate, validation)
            hooks = max(1, planlet_business_hook_count(candidate))
            key = (
                delta / hooks,
                non_depot_delta / hooks,
                -planlet_remote_cross_count(candidate) if candidate.plan_steps else -int(remote_cross_for_candidate(candidate)),
                candidate.candidate_id,
            )
            if best_key is None or key > best_key:
                best_candidate = candidate
                best_delta = delta
                best_non_depot_delta = non_depot_delta
                best_key = key
        return best_candidate, best_delta, best_non_depot_delta

    best_front, best_front_delta, best_front_non_depot_delta = best_item(front_items)
    best_phase, best_phase_delta, best_phase_non_depot_delta = best_item(phase_permitted)
    selected_touches_remote = candidate_touches_remote_interaction(selected)
    if selected_phase_reason:
        decision_gap = "selected_deferred_phase_candidate"
    elif selected_touches_remote and phase_state.front_service_progress < 0.95 and not front_items:
        decision_gap = "no_phase_permitted_front_candidate_before_remote"
    elif selected_touches_remote and best_front is not None and phase_state.front_service_progress < 0.95:
        decision_gap = "front_candidate_available_but_remote_selected"
    elif best_front is not None and selected.candidate_id != best_front.candidate_id and best_front_delta > 0:
        decision_gap = "front_candidate_available_not_selected"
    else:
        decision_gap = ""

    return CandidatePoolAuditRow(
        case_id=case_id,
        solve_strategy=solve_strategy,
        hook_index=selected.hook_index,
        business_hook_index_start=business_hook_index_start,
        runtime_phase=phase_state.active_phase,
        front_service_progress=round(phase_state.front_service_progress, 4),
        selected_candidate_id=selected.candidate_id,
        selected_candidate_kind=selected.candidate_kind,
        selected_action_family=selected.action_family,
        selected_touches_remote=selected_touches_remote,
        selected_phase_reason=selected_phase_reason,
        generated_candidate_count=len(candidates),
        physically_accepted_count=len(physically_accepted),
        phase_permitted_count=len(phase_permitted),
        phase_permitted_front_count=len(front_items),
        phase_permitted_remote_count=len(remote_items),
        best_front_candidate_id=best_front.candidate_id if best_front else "",
        best_front_candidate_kind=best_front.candidate_kind if best_front else "",
        best_front_delta=best_front_delta,
        best_front_non_depot_delta=best_front_non_depot_delta,
        best_front_business_hooks=planlet_business_hook_count(best_front) if best_front else 0,
        best_phase_candidate_id=best_phase.candidate_id if best_phase else "",
        best_phase_candidate_kind=best_phase.candidate_kind if best_phase else "",
        best_phase_delta=best_phase_delta,
        best_phase_non_depot_delta=best_phase_non_depot_delta,
        decision_gap=decision_gap,
    )


def better_case_result(left: CaseSummaryRow, right: CaseSummaryRow) -> bool:
    status_rank = {"completed": 0, "blocked": 1, "invalid_input": 2}
    left_primary = (
        status_rank.get(left.status, 9),
        left.final_unsatisfied_vehicle_count,
        left.hard_physical_violation_accepted_count,
    )
    right_primary = (
        status_rank.get(right.status, 9),
        right.final_unsatisfied_vehicle_count,
        right.hard_physical_violation_accepted_count,
    )
    if left_primary != right_primary:
        return left_primary < right_primary

    hook_slack = max(2, round(right.business_get_put_hook_count * 0.05))
    if left.business_get_put_hook_count > right.business_get_put_hook_count + hook_slack:
        return False
    if right.business_get_put_hook_count > left.business_get_put_hook_count + hook_slack:
        return True

    left_remote = (
        left.remote_business_transition_count,
        left.remote_interaction_cross_count,
        left.remote_interaction_batch_count,
        left.remote_interaction_session_count,
        left.business_get_put_hook_count,
        left.internal_move_batch_count,
    )
    right_remote = (
        right.remote_business_transition_count,
        right.remote_interaction_cross_count,
        right.remote_interaction_batch_count,
        right.remote_interaction_session_count,
        right.business_get_put_hook_count,
        right.internal_move_batch_count,
    )
    return left_remote < right_remote


def run_case(
    truth_path: Path,
    output_dir: Path,
    graph: TrackGraph,
    max_hooks: int,
    manual_baselines: dict[str, ManualBaseline] | None = None,
    strategy_mode: str = STANDARD_STRATEGY_MODE,
    repair_mode: str = AUDIT_REPAIR_MODE,
    allow_phase_forced_open: bool = True,
    strategy_name_filter: str = "",
) -> tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str]]:
    summary, candidate_rows, operation_rows_for_case, rejection_reasons, _diagnostics = run_case_with_diagnostics(
        truth_path=truth_path,
        output_dir=output_dir,
        graph=graph,
        max_hooks=max_hooks,
        manual_baselines=manual_baselines,
        strategy_mode=strategy_mode,
        repair_mode=repair_mode,
        allow_phase_forced_open=allow_phase_forced_open,
        strategy_name_filter=strategy_name_filter,
    )
    return summary, candidate_rows, operation_rows_for_case, rejection_reasons


def run_case_with_diagnostics(
    truth_path: Path,
    output_dir: Path,
    graph: TrackGraph,
    max_hooks: int,
    manual_baselines: dict[str, ManualBaseline] | None = None,
    strategy_mode: str = STANDARD_STRATEGY_MODE,
    repair_mode: str = AUDIT_REPAIR_MODE,
    allow_phase_forced_open: bool = True,
    strategy_name_filter: str = "",
) -> tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str], RuntimeDiagnosticRows]:
    case_id = case_id_from_path(truth_path)
    payload = read_json(truth_path)
    input_ok, input_errors = validate_input(payload)
    normalized_cars = [normalized_car(car) for car in payload.get("StartStatus") or []]
    capacities = terminal_capacity_by_line(payload)
    manual_baseline = baseline_for_case(manual_baselines, case_id)
    short_chain_manual = bool(manual_baseline and manual_baseline.variant in SHORT_CHAIN_VARIANTS)

    base_assignment = build_depot_assignment([dict(car) for car in normalized_cars], capacities)
    clustered_assignment = build_short_direct_depot_assignment(
        [dict(car) for car in normalized_cars],
        capacities,
        base_assignment,
    )
    def strategy_variants(
        prefix: str,
        depot_assignment: DepotAssignment,
        short_direct_override: bool,
    ) -> list[tuple[str, DepotAssignment, bool, StrategyConfig]]:
        return [
            (prefix, depot_assignment, short_direct_override, StrategyConfig()),
            (
                f"{prefix}_contract",
                depot_assignment,
                short_direct_override,
                StrategyConfig(
                    enable_contract_planlets=True,
                    prefer_contract_planlets=True,
                    enable_front_capacity_contracts=True,
                ),
            ),
            (
                f"{prefix}_remote_session_contract",
                depot_assignment,
                short_direct_override,
                StrategyConfig(
                    depot_aware_staging=True,
                    enable_contract_planlets=True,
                    prefer_contract_planlets=True,
                    enable_remote_session_contracts=True,
                    enable_front_capacity_contracts=True,
                ),
            ),
        ]

    cluster_feasible = (
        len(clustered_assignment.failures) <= len(base_assignment.failures)
        and len(clustered_assignment.slots) >= len(base_assignment.slots)
    )
    cluster_preferred = (
        cluster_feasible
        and depot_source_fragmentation_score(normalized_cars, clustered_assignment)
        < depot_source_fragmentation_score(normalized_cars, base_assignment)
    )
    if repair_mode == ALIGNED_REPAIR_MODE:
        aligned_config = StrategyConfig(
            depot_aware_staging=True,
            enable_contract_planlets=True,
            prefer_contract_planlets=True,
            enable_remote_session_contracts=True,
            front_debt_first=True,
            enable_front_capacity_contracts=True,
        )
        aligned_short_direct = is_short_direct_depot_variant(case_id) or short_chain_manual
        strategies = [
            ("aligned_base", base_assignment, aligned_short_direct, aligned_config),
        ]
        if cluster_feasible:
            strategies.append(("aligned_cluster", clustered_assignment, aligned_short_direct, aligned_config))
        if strategy_name_filter:
            filtered = [item for item in strategies if strategy_name_filter in item[0]]
            if filtered:
                strategies = filtered

        results: list[tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str], RuntimeDiagnosticRows]] = []
        for strategy_name, depot_assignment, short_direct_override, strategy_config in strategies:
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
                    strategy_config=strategy_config,
                    manual_baseline=manual_baseline,
                    repair_mode=repair_mode,
                    allow_phase_forced_open=allow_phase_forced_open,
                )
            )
        best = results[0]
        for result in results[1:]:
            if better_case_result(result[0], best[0]):
                best = result
        summary, candidate_rows, operation_rows_for_case, rejection_reasons, diagnostics = best
        if summary.response_path:
            selected_response_path = output_dir / "responses" / f"{case_id}.json"
            source_response_path = Path(summary.response_path)
            if source_response_path.exists():
                write_json(selected_response_path, read_json(source_response_path))
                summary = replace(summary, response_path=str(selected_response_path))
        return summary, candidate_rows, operation_rows_for_case, rejection_reasons, diagnostics

    strategies: list[tuple[str, DepotAssignment, bool, StrategyConfig]] = [
        *strategy_variants("phase_gate_base", base_assignment, False),
    ]
    if cluster_feasible and not cluster_preferred:
        strategies.append(
            (
                "phase_gate_cluster_remote_session_contract",
                clustered_assignment,
                False,
                StrategyConfig(
                    depot_aware_staging=True,
                    enable_contract_planlets=True,
                    prefer_contract_planlets=True,
                    enable_remote_session_contracts=True,
                    enable_front_capacity_contracts=True,
                ),
            )
        )
    if strategy_mode == DIAGNOSTIC_STRATEGY_MODE:
        strategies.append(("early_depot_base", base_assignment, True, StrategyConfig()))
    if is_short_direct_depot_variant(case_id) or short_chain_manual:
        strategies = strategy_variants("short_direct_cluster", clustered_assignment, True)
    elif cluster_preferred:
        strategies.extend(strategy_variants("phase_gate_cluster", clustered_assignment, False))
        if strategy_mode == DIAGNOSTIC_STRATEGY_MODE:
            strategies.append(("early_depot_cluster", clustered_assignment, True, StrategyConfig()))
    if strategy_name_filter:
        filtered = [item for item in strategies if strategy_name_filter in item[0]]
        if filtered:
            strategies = filtered

    results: list[tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str], RuntimeDiagnosticRows]] = []
    for strategy_name, depot_assignment, short_direct_override, strategy_config in strategies:
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
                strategy_config=strategy_config,
                manual_baseline=manual_baseline,
                repair_mode=repair_mode,
                allow_phase_forced_open=allow_phase_forced_open,
            )
        )

    best = results[0]
    for result in results[1:]:
        if better_case_result(result[0], best[0]):
            best = result
    summary, candidate_rows, operation_rows_for_case, rejection_reasons, diagnostics = best
    if summary.response_path:
        selected_response_path = output_dir / "responses" / f"{case_id}.json"
        source_response_path = Path(summary.response_path)
        if source_response_path.exists():
            write_json(selected_response_path, read_json(source_response_path))
            summary = replace(summary, response_path=str(selected_response_path))
    return summary, candidate_rows, operation_rows_for_case, rejection_reasons, diagnostics


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
    strategy_config: StrategyConfig = StrategyConfig(),
    manual_baseline: ManualBaseline | None = None,
    repair_mode: str = AUDIT_REPAIR_MODE,
    allow_phase_forced_open: bool = True,
) -> tuple[CaseSummaryRow, list[CandidateAuditRow], list[OperationTraceRow], Counter[str], RuntimeDiagnosticRows]:
    cars = [dict(car) for car in normalized_cars]
    validator = PhysicalValidator(graph)
    loco = payload.get("locoNode") or {}
    loco_location = initial_loco_location(loco)
    initial_unsatisfied = len(unsatisfied_cars(cars, depot_assignment))
    initial_non_depot_unsatisfied = non_depot_unsatisfied_count(cars, depot_assignment)
    candidate_rows: list[CandidateAuditRow] = []
    operations: list[OperationTraceRow] = []
    diagnostics = RuntimeDiagnosticRows()
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
    last_business_hook_remote: bool | None = None
    remote_streak_count = 0
    remote_session_id = 0
    current_remote_session_batch_index = 0
    aligned_mode = repair_mode == ALIGNED_REPAIR_MODE
    while input_ok and not blocked_reason and hook_index <= max_hooks and current_unsatisfied:
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
        effective_enable_contract_planlets = strategy_config.enable_contract_planlets or aligned_mode
        effective_enable_remote_session_contracts = strategy_config.enable_remote_session_contracts or aligned_mode
        effective_enable_front_capacity_contracts = strategy_config.enable_front_capacity_contracts or aligned_mode

        def build_round_candidates(*, suppress_ownerless_recovery: bool) -> tuple[list[HookCandidate], list[CandidateAuditRow]]:
            return build_candidates(
                case_id,
                hook_index,
                cars,
                depot_assignment,
                staging_priority=strategy_config.staging_priority,
                depot_aware_staging=strategy_config.depot_aware_staging,
                graph=graph,
                loco_location=loco_location,
                enable_contract_planlets=effective_enable_contract_planlets,
                enable_remote_session_contracts=effective_enable_remote_session_contracts,
                enable_front_capacity_contracts=effective_enable_front_capacity_contracts,
                suppress_ownerless_recovery=suppress_ownerless_recovery,
                manual_baseline=manual_baseline,
            )

        candidates, blocked_rows = build_round_candidates(suppress_ownerless_recovery=False)
        candidates = sorted(candidates, key=lambda candidate: phase_candidate_sort_key(candidate, phase_state))
        candidate_rows.extend(blocked_rows)
        blocked_count += len(blocked_rows)
        for row in blocked_rows:
            rejection_reasons[row.hard_violation_reasons] += 1

        round_business_hook_index_start = business_hook_count_so_far(operations) + 1
        remote_interaction_debt = remote_interaction_unsatisfied_count(cars, depot_assignment)
        accepted_this_round = False
        physically_accepted: list[tuple[HookCandidate, PhysicalValidation, str]] = []
        for candidate in candidates:
            validation = validator.validate(candidate, cars, loco_location, depot_assignment)
            if validation.accepted:
                phase_reason = phase_reject_reason(
                    candidate,
                    validation,
                    phase_state,
                    manual_baseline=manual_baseline,
                    business_hook_index=round_business_hook_index_start,
                    repair_mode=repair_mode,
                )
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
        prospective_cache: dict[str, list[dict[str, Any]]] = {}
        metrics_cache: dict[str, tuple[int, int, int, int]] = {}

        def prospective_cars_for(candidate: HookCandidate) -> list[dict[str, Any]]:
            cached = prospective_cache.get(candidate.candidate_id)
            if cached is not None:
                return cached
            prospective_cars = [dict(car) for car in cars]
            apply_candidate(candidate, prospective_cars)
            prospective_cache[candidate.candidate_id] = prospective_cars
            return prospective_cars

        def transition_for(
            candidate: HookCandidate,
            validation: PhysicalValidation,
        ) -> tuple[list[dict[str, Any]], LocoLocation, tuple[str, str, tuple[tuple[str, str, int], ...]]]:
            cached = transition_cache.get(candidate.candidate_id)
            if cached is not None:
                return cached
            prospective_cars = prospective_cars_for(candidate)
            next_loco_location = operation_stand_location(validation.put_path, candidate_final_line(candidate))
            signature = state_signature(prospective_cars, next_loco_location)
            transition_cache[candidate.candidate_id] = (prospective_cars, next_loco_location, signature)
            return prospective_cars, next_loco_location, signature

        def transition_metrics(
            candidate: HookCandidate,
            validation: PhysicalValidation,
        ) -> tuple[int, int, int, int]:
            cached = metrics_cache.get(candidate.candidate_id)
            if cached is not None:
                return cached
            before_unsatisfied = len(current_unsatisfied)
            before_non_depot = non_depot_unsatisfied_count(cars, depot_assignment)
            prospective_cars = prospective_cars_for(candidate)
            after_unsatisfied = len(unsatisfied_cars(prospective_cars, depot_assignment))
            after_non_depot = non_depot_unsatisfied_count(prospective_cars, depot_assignment)
            metrics_cache[candidate.candidate_id] = (
                before_unsatisfied - after_unsatisfied,
                before_non_depot - after_non_depot,
                after_unsatisfied,
                after_non_depot,
            )
            return metrics_cache[candidate.candidate_id]

        def has_followup_candidate(prospective_cars: list[dict[str, Any]], next_loco_location: LocoLocation) -> bool:
            if not unsatisfied_cars(prospective_cars, depot_assignment):
                return True
            next_candidates, _next_blocked_rows = build_candidates(
                case_id,
                hook_index + 1,
                prospective_cars,
                depot_assignment,
                staging_priority=strategy_config.staging_priority,
                depot_aware_staging=strategy_config.depot_aware_staging,
                graph=graph,
                loco_location=next_loco_location,
                enable_contract_planlets=effective_enable_contract_planlets,
                enable_remote_session_contracts=effective_enable_remote_session_contracts,
                enable_front_capacity_contracts=effective_enable_front_capacity_contracts,
                suppress_ownerless_recovery=aligned_mode,
                manual_baseline=manual_baseline,
            )
            for next_candidate in next_candidates:
                if validator.validate(next_candidate, prospective_cars, next_loco_location, depot_assignment).accepted:
                    return True
            return False

        def followup_ok(candidate: HookCandidate, validation: PhysicalValidation) -> bool:
            prospective_cars, next_loco_location, _signature = transition_for(candidate, validation)
            return has_followup_candidate(prospective_cars, next_loco_location)

        def remote_continuity_key(
            item: tuple[HookCandidate, PhysicalValidation, str],
        ) -> tuple[int, float, float, int, Any]:
            candidate, _validation, _phase_reason = item
            reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(candidate, item[1])
            hook_count = max(1, planlet_business_hook_count(candidate))
            continue_remote_session = (
                last_hook_touched_remote
                and remote_streak_count < REMOTE_CONTINUITY_MAX_STREAK
            )
            continuity_penalty = (
                0
                if not continue_remote_session or candidate_touches_remote_interaction(candidate)
                else 1
            )
            remote_cross_penalty = candidate_remote_business_transition_count(candidate, last_business_hook_remote)
            return (
                continuity_penalty,
                round(-reduction / hook_count, 4),
                round(-non_depot_reduction / hook_count, 4),
                remote_cross_penalty,
                phase_candidate_sort_key(candidate, phase_state),
            )

        def selection_key(item: tuple[HookCandidate, PhysicalValidation, str]) -> Any:
            if strategy_config.front_debt_first or aligned_mode:
                candidate, validation, phase_reason = item
                reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(candidate, validation)
                front_debt_active = (
                    phase_state.front_service_progress < 0.95
                    and not phase_reason
                    and (reduction > 0 or non_depot_reduction > 0)
                )
                front_priority = (
                    0
                    if front_debt_active and candidate_is_front_service(candidate)
                    else 1
                    if front_debt_active and candidate_touches_remote_interaction(candidate)
                    else 0
                )
                return (
                    front_priority,
                    contract_planlet_selection_key(
                        item,
                        transition_metrics=transition_metrics,
                        phase_state=phase_state,
                        last_hook_touched_remote=last_hook_touched_remote,
                        remote_streak_count=remote_streak_count,
                        last_business_hook_remote=last_business_hook_remote,
                    ),
                )
            if (
                repair_mode == ALIGNED_REPAIR_MODE
                and not (manual_baseline and manual_baseline.variant in SHORT_CHAIN_VARIANTS)
            ):
                return aligned_selection_key(
                    item,
                    transition_metrics=transition_metrics,
                    phase_state=phase_state,
                    last_hook_touched_remote=last_hook_touched_remote,
                    remote_streak_count=remote_streak_count,
                    last_business_hook_remote=last_business_hook_remote,
                )
            if strategy_config.prefer_contract_planlets:
                return contract_planlet_selection_key(
                    item,
                    transition_metrics=transition_metrics,
                    phase_state=phase_state,
                    last_hook_touched_remote=last_hook_touched_remote,
                    remote_streak_count=remote_streak_count,
                    last_business_hook_remote=last_business_hook_remote,
                )
            return remote_continuity_key(item)

        phase_selectable = [item for item in physically_accepted if not item[2]]
        remote_session_should_continue = (
            last_hook_touched_remote
            and remote_interaction_debt > 0
            and any(candidate_touches_remote_interaction(item[0]) for item in physically_accepted)
        )
        selectable_items = (
            [item for item in physically_accepted if candidate_touches_remote_interaction(item[0]) or item[2]]
            if remote_session_should_continue
            else physically_accepted
        )

        for candidate, validation, phase_reason in sorted(selectable_items, key=selection_key):
            if phase_reason:
                if repair_mode == ALIGNED_REPAIR_MODE and phase_reason.startswith("human_phase_contract"):
                    structural_validation = PhysicalValidation(
                        accepted=False,
                        reasons=(phase_reason,),
                        get_path=validation.get_path,
                        weigh_path=validation.weigh_path,
                        put_path=validation.put_path,
                    )
                    candidate_rows.append(candidate_audit_row(candidate, structural_validation, "rejected"))
                    rejected_count += 1
                    rejection_reasons[phase_reason] += 1
                else:
                    phase_deferred.append((candidate, validation, phase_reason))
                continue
            reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(candidate, validation)
            structural_reason = structural_reject_reason(
                candidate=candidate,
                validation=validation,
                phase_reason=phase_reason,
                reduction=reduction,
                non_depot_reduction=non_depot_reduction,
                physically_accepted=physically_accepted,
                transition_metrics=transition_metrics,
                repair_mode=repair_mode,
                manual_baseline=manual_baseline,
            )
            if structural_reason:
                structural_validation = PhysicalValidation(
                    accepted=False,
                    reasons=(structural_reason,),
                    get_path=validation.get_path,
                    weigh_path=validation.weigh_path,
                    put_path=validation.put_path,
                )
                candidate_rows.append(candidate_audit_row(candidate, structural_validation, "rejected"))
                rejected_count += 1
                rejection_reasons[structural_reason] += 1
                continue
            prospective_cars, next_loco_location, signature = transition_for(candidate, validation)
            if candidate.plan_steps and not followup_ok(candidate, validation):
                lookahead_validation = PhysicalValidation(
                    accepted=False,
                    reasons=("planlet_no_followup_candidate",),
                    get_path=validation.get_path,
                    weigh_path=validation.weigh_path,
                    put_path=validation.put_path,
                    operation_paths=validation.operation_paths,
                )
                candidate_rows.append(candidate_audit_row(candidate, lookahead_validation, "rejected"))
                rejected_count += 1
                rejection_reasons["planlet_no_followup_candidate"] += 1
                continue
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
            if repair_mode == ALIGNED_REPAIR_MODE and not allow_phase_forced_open:
                blocked_reason = "human_phase_contract_no_phase_permitted_candidate"
                for candidate, validation, phase_reason in phase_deferred:
                    structural_validation = PhysicalValidation(
                        accepted=False,
                        reasons=(phase_reason,),
                        get_path=validation.get_path,
                        weigh_path=validation.weigh_path,
                        put_path=validation.put_path,
                    )
                    candidate_rows.append(candidate_audit_row(candidate, structural_validation, "rejected"))
                    rejected_count += 1
                    rejection_reasons[phase_reason] += 1
            deferred_items = [] if repair_mode == ALIGNED_REPAIR_MODE and not allow_phase_forced_open else phase_deferred
            if remote_session_should_continue:
                deferred_items = [
                    item for item in deferred_items if candidate_touches_remote_interaction(item[0])
                ]
            for candidate, validation, phase_reason in sorted(deferred_items, key=selection_key):
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
            business_hook_index_start = business_hook_count_so_far(operations) + 1
            business_hook_index_end = business_hook_index_start + planlet_business_hook_count(candidate) - 1
            expected_phase, phase_permission, phase_permission_reason, over_manual_bound = phase_permission_for_candidate(
                candidate=candidate,
                phase_state=phase_state,
                phase_reason=forced_reason,
                manual_baseline=manual_baseline,
                business_hook_index=business_hook_index_start,
            )
            touches_remote = candidate_touches_remote_interaction(candidate)
            remote_cross = remote_cross_for_candidate(candidate)
            if touches_remote and not last_hook_touched_remote:
                remote_session_id += 1
                current_remote_session_batch_index = 0
            if touches_remote:
                current_remote_session_batch_index += 1
            selected_unsat_reduction, selected_non_depot_reduction, after_unsat, after_non_depot = transition_metrics(
                candidate,
                validation,
            )
            best_unsat_reduction = selected_unsat_reduction
            best_non_depot_reduction = selected_non_depot_reduction
            dominated_by = ""
            dominance_reason = ""
            selected_rank = 1
            sorted_physically_accepted = sorted(physically_accepted, key=remote_continuity_key)
            for rank, item in enumerate(sorted_physically_accepted, start=1):
                candidate_for_rank = item[0]
                if candidate_for_rank.candidate_id == candidate.candidate_id:
                    selected_rank = rank
                reduction, non_depot_reduction, _after_unsat, _after_non_depot = transition_metrics(item[0], item[1])
                best_unsat_reduction = max(best_unsat_reduction, reduction)
                best_non_depot_reduction = max(best_non_depot_reduction, non_depot_reduction)
                if item[0].candidate_id == candidate.candidate_id:
                    continue
                if item[2]:
                    continue
                if reduction > selected_unsat_reduction and not dominated_by:
                    dominated_by = item[0].candidate_id
                    dominance_reason = "better_unsatisfied_reduction_same_round"
                elif (
                    reduction == selected_unsat_reduction
                    and non_depot_reduction > selected_non_depot_reduction
                    and not dominated_by
                ):
                    dominated_by = item[0].candidate_id
                    dominance_reason = "better_non_depot_reduction_same_round"
                elif (
                    reduction == selected_unsat_reduction
                    and non_depot_reduction == selected_non_depot_reduction
                    and remote_cross
                    and not remote_cross_for_candidate(item[0])
                    and not dominated_by
                ):
                    dominated_by = item[0].candidate_id
                    dominance_reason = "same_progress_without_remote_cross"
            diagnostics.phase_rows.append(
                RuntimePhaseTraceRow(
                    case_id=case_id,
                    solve_strategy=solve_strategy,
                    hook_index=candidate.hook_index,
                    business_hook_index_start=business_hook_index_start,
                    business_hook_index_end=business_hook_index_end,
                    candidate_id=candidate.candidate_id,
                    source_line=candidate.source_line,
                    target_line=candidate.target_line,
                    action_family=candidate.action_family,
                    candidate_kind=candidate.candidate_kind,
                    manual_variant=manual_baseline.variant if manual_baseline else "",
                    manual_observed_hook_count=manual_baseline.observed_hook_count if manual_baseline else 0,
                    expected_h_phase=expected_phase,
                    runtime_phase=phase_state.active_phase,
                    runtime_variant=phase_state.active_variant,
                    phase_permission=phase_permission,
                    phase_permission_reason=phase_permission_reason,
                    over_manual_hook_bound=over_manual_bound,
                    link7_open=phase_state.link7_open,
                    crosses_link7=validation_crosses_link7(validation),
                    touches_remote=touches_remote,
                    touches_depot=candidate_touches_depot(candidate),
                    front_service_progress=round(phase_state.front_service_progress, 4),
                    current_front_service_progress=round(phase_state.current_front_service_progress, 4),
                    non_depot_unsatisfied_count=phase_state.non_depot_unsatisfied_count,
                    initial_non_depot_unsatisfied_count=phase_state.initial_non_depot_unsatisfied_count,
                    forced_phase_open_reason=forced_reason,
                )
            )
            diagnostics.contract_rows.append(
                ContractTraceRow(
                    case_id=case_id,
                    solve_strategy=solve_strategy,
                    hook_index=candidate.hook_index,
                    business_hook_index_start=business_hook_index_start,
                    candidate_id=candidate.candidate_id,
                    selected_contract=selected_contract(candidate),
                    structural_intent=structural_intent(candidate),
                    source_line=candidate.source_line,
                    target_line=candidate.target_line,
                    owner_vehicle_count=len(candidate.move_car_nos),
                    owner_vehicles="|".join(candidate.move_car_nos),
                    hard_obligations=hard_obligations_for_candidate(candidate),
                    protections=protections_for_candidate(candidate, expected_phase),
                    target_contract_reason=candidate.generation_reason,
                    suppressed_contracts=suppressed_contracts_for_candidate(candidate, expected_phase),
                    unsatisfied_before=len(current_unsatisfied),
                    unsatisfied_after=after_unsat,
                    unsatisfied_reduction=selected_unsat_reduction,
                    non_depot_unsatisfied_before=non_depot_unsatisfied_count(cars, depot_assignment),
                    non_depot_unsatisfied_after=after_non_depot,
                    non_depot_unsatisfied_reduction=selected_non_depot_reduction,
                )
            )
            requested_resources = requested_resources_for_candidate(candidate, validation)
            diagnostics.resource_rows.append(
                ResourceDeltaTraceRow(
                    case_id=case_id,
                    solve_strategy=solve_strategy,
                    hook_index=candidate.hook_index,
                    business_hook_index_start=business_hook_index_start,
                    candidate_id=candidate.candidate_id,
                    requested_resources="|".join(requested_resources),
                    acquired_resources="|".join(requested_resources),
                    released_resources=candidate.source_line if candidate.source_line != candidate.target_line else "",
                    blocked_resources="",
                    resource_status="available",
                    source_line=candidate.source_line,
                    target_line=candidate.target_line,
                    get_path="|".join(route_for_output(validation.get_path)),
                    put_path="|".join(route_for_output(validation.put_path)),
                    crosses_link7=validation_crosses_link7(validation),
                    touches_remote=touches_remote,
                    remote_session_id=remote_session_id if touches_remote else 0,
                    remote_cross=remote_cross,
                    loco_start_line=loco_location.line,
                    loco_start_node=loco_location.node,
                    loco_end_line=next_loco_location.line,
                    loco_end_node=next_loco_location.node,
                )
            )
            diagnostics.dominance_rows.append(
                CandidateDominanceAuditRow(
                    case_id=case_id,
                    solve_strategy=solve_strategy,
                    hook_index=candidate.hook_index,
                    business_hook_index_start=business_hook_index_start,
                    selected_candidate_id=candidate.candidate_id,
                    generated_candidate_count=len(candidates),
                    physically_accepted_count=len(physically_accepted),
                    selected_rank=selected_rank,
                    selected_unsatisfied_reduction=selected_unsat_reduction,
                    best_unsatisfied_reduction=best_unsat_reduction,
                    selected_non_depot_reduction=selected_non_depot_reduction,
                    best_non_depot_reduction=best_non_depot_reduction,
                    selected_remote_cross=remote_cross,
                    selected_touches_remote=touches_remote,
                    dominated_by_candidate_id=dominated_by,
                    dominance_reason=dominance_reason,
                    status="dominated" if dominated_by else "not_dominated",
                )
            )
            diagnostics.candidate_pool_rows.append(
                build_candidate_pool_audit_row(
                    case_id=case_id,
                    solve_strategy=solve_strategy,
                    phase_state=phase_state,
                    selected=candidate,
                    selected_phase_reason=forced_reason,
                    business_hook_index_start=business_hook_index_start,
                    candidates=candidates,
                    physically_accepted=physically_accepted,
                    transition_metrics=transition_metrics,
                )
            )
            if touches_remote:
                diagnostics.depot_session_rows.append(
                    DepotSessionAuditRow(
                        case_id=case_id,
                        solve_strategy=solve_strategy,
                        remote_session_id=remote_session_id,
                        session_batch_index=current_remote_session_batch_index,
                        hook_index=candidate.hook_index,
                        business_hook_index_start=business_hook_index_start,
                        candidate_id=candidate.candidate_id,
                        source_line=candidate.source_line,
                        target_line=candidate.target_line,
                        action_family=candidate.action_family,
                        remote_event=remote_event_for_candidate(candidate),
                        remote_cross=remote_cross,
                        move_car_count=len(candidate.move_car_nos),
                        move_cars="|".join(candidate.move_car_nos),
                        manual_variant=manual_baseline.variant if manual_baseline else "",
                    )
                )
            rows = operation_rows(candidate, validation, len(operations) + 1)
            operations.extend(rows)
            for row in reversed(rows):
                if row.action in {"Get", "Put"}:
                    last_business_hook_remote = row.line in REMOTE_INTERACTION_LINES
                    break
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
    remote_business_transition_count = operation_remote_business_transition_count(operations)
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
        remote_business_transition_count=remote_business_transition_count,
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
    return summary, candidate_rows, operations, rejection_reasons, diagnostics


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
        remote_business_transition_count=sum(row.remote_business_transition_count for row in case_rows),
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
    if reason.startswith("human_phase_contract"):
        return (
            "R1_HUMAN_PHASE_CONTRACT",
            "candidate violates the explicit human phase contract",
            "HumanPhaseContract",
        )
    if reason.startswith("p4_contract_selector"):
        return (
            "R2_TARGET_CONTRACT_SELECTOR",
            "candidate has no acceptable contract delta or unlock value",
            "TargetContractSelector",
        )
    if reason.startswith("p7_reject"):
        return (
            "R3_RESOURCE_DELTA_REJECT_GATE",
            "candidate is structurally dominated under the current resource context",
            "ResourceDelta + AcceptRejectGate",
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


def operations_by_case_and_batch(operations: list[OperationTraceRow]) -> dict[str, dict[int, list[OperationTraceRow]]]:
    grouped: dict[str, dict[int, list[OperationTraceRow]]] = defaultdict(lambda: defaultdict(list))
    for row in operations:
        if row.action in {"Get", "Put"}:
            grouped[row.case_id][row.hook_index].append(row)
    return grouped


def solver_remote_metrics_for_case(
    operations: list[OperationTraceRow],
    business_hook_count: int,
) -> dict[str, int | float]:
    rows_by_batch: dict[int, list[OperationTraceRow]] = defaultdict(list)
    for row in operations:
        if row.action in {"Get", "Put"}:
            rows_by_batch[row.hook_index].append(row)
    first_business_hook = 0
    remote_business_hooks = 0
    remote_batches = 0
    remote_sessions = 0
    remote_cross = 0
    previous_touched_remote = False
    post_first_remote_non_remote_batch_count = 0
    seen_remote = False
    for hook_index in sorted(rows_by_batch):
        rows = sorted(rows_by_batch[hook_index], key=lambda item: item.operation_index)
        touched_rows = [row for row in rows if row.line in REMOTE_INTERACTION_LINES]
        touched_remote = bool(touched_rows)
        if touched_remote:
            remote_batches += 1
            remote_business_hooks += len(touched_rows)
            if not first_business_hook:
                first_business_hook = min(row.operation_index for row in touched_rows)
            if not previous_touched_remote:
                remote_sessions += 1
            seen_remote = True
        elif seen_remote:
            post_first_remote_non_remote_batch_count += 1
        get_row = next((row for row in rows if row.action == "Get"), None)
        put_row = next((row for row in rows if row.action == "Put"), None)
        if get_row and put_row and ((get_row.line in REMOTE_INTERACTION_LINES) != (put_row.line in REMOTE_INTERACTION_LINES)):
            remote_cross += 1
        previous_touched_remote = touched_remote
    return {
        "first_remote_business_hook": first_business_hook,
        "first_remote_ratio": round(first_business_hook / business_hook_count, 6) if first_business_hook and business_hook_count else 0.0,
        "remote_business_hook_count": remote_business_hooks,
        "remote_batch_count": remote_batches,
        "remote_session_count": remote_sessions,
        "remote_cross_count": remote_cross,
        "post_first_remote_non_remote_batch_count": post_first_remote_non_remote_batch_count,
    }


def build_manual_vs_solver_rows(
    case_rows: list[CaseSummaryRow],
    operation_rows: list[OperationTraceRow],
    manual_baselines: dict[str, ManualBaseline],
) -> list[ManualVsSolverCaseCompareRow]:
    operations_by_case: dict[str, list[OperationTraceRow]] = defaultdict(list)
    for row in operation_rows:
        operations_by_case[row.case_id].append(row)
    rows: list[ManualVsSolverCaseCompareRow] = []
    for case_row in case_rows:
        baseline = manual_baselines.get(case_row.case_id)
        if baseline is None:
            continue
        remote_metrics = solver_remote_metrics_for_case(
            operations_by_case.get(case_row.case_id, []),
            case_row.business_get_put_hook_count,
        )
        hook_delta = case_row.business_get_put_hook_count - baseline.observed_hook_count
        rows.append(
            ManualVsSolverCaseCompareRow(
                case_id=case_row.case_id,
                solve_strategy=case_row.solve_strategy,
                status=case_row.status,
                manual_source_path=baseline.source_path,
                manual_variant=baseline.variant,
                manual_hook_count=baseline.observed_hook_count,
                manual_soft_hook_upper_bound=baseline.soft_hook_upper_bound,
                solver_business_hook_count=case_row.business_get_put_hook_count,
                hook_delta=hook_delta,
                hook_ratio=round(case_row.business_get_put_hook_count / baseline.observed_hook_count, 6)
                if baseline.observed_hook_count
                else 0.0,
                solver_internal_move_batch_count=case_row.internal_move_batch_count,
                manual_first_remote_hook=baseline.first_remote_hook,
                solver_first_remote_business_hook=int(remote_metrics["first_remote_business_hook"]),
                manual_first_remote_ratio=round(baseline.first_remote_hook / baseline.observed_hook_count, 6)
                if baseline.first_remote_hook and baseline.observed_hook_count
                else 0.0,
                solver_first_remote_ratio=float(remote_metrics["first_remote_ratio"]),
                manual_remote_hook_count=baseline.remote_hook_count,
                solver_remote_business_hook_count=int(remote_metrics["remote_business_hook_count"]),
                solver_remote_batch_count=int(remote_metrics["remote_batch_count"]),
                manual_remote_session_count=baseline.remote_session_count,
                solver_remote_session_count=int(remote_metrics["remote_session_count"]),
                solver_remote_cross_count=int(remote_metrics["remote_cross_count"]),
                manual_remote_business_transition_count=baseline.remote_business_transition_count,
                solver_remote_business_transition_count=case_row.remote_business_transition_count,
                remote_business_transition_delta=(
                    case_row.remote_business_transition_count - baseline.remote_business_transition_count
                ),
                blocked_reason=case_row.blocked_reason,
            )
        )
    return rows


def build_short_chain_rows(
    case_rows: list[CaseSummaryRow],
    operation_rows: list[OperationTraceRow],
    manual_baselines: dict[str, ManualBaseline],
) -> list[ShortChainDiagnosticRow]:
    operations_by_case: dict[str, list[OperationTraceRow]] = defaultdict(list)
    for row in operation_rows:
        operations_by_case[row.case_id].append(row)
    rows: list[ShortChainDiagnosticRow] = []
    for case_row in case_rows:
        baseline = manual_baselines.get(case_row.case_id)
        if baseline is None or baseline.variant not in SHORT_CHAIN_VARIANTS:
            continue
        remote_metrics = solver_remote_metrics_for_case(
            operations_by_case.get(case_row.case_id, []),
            case_row.business_get_put_hook_count,
        )
        limit = baseline.observed_hook_count + 1
        hook_delta = case_row.business_get_put_hook_count - baseline.observed_hook_count
        unnecessary_remote_cross = max(0, int(remote_metrics["remote_cross_count"]) - baseline.remote_hook_count)
        if baseline.variant == "MIXED_SIGNAL_REPAIR":
            component = "MIXED_SIGNAL_REPAIR conservative planlet + low-confidence phase boundary"
        elif baseline.variant == "DEPOT_DIGEST_ONLY":
            component = "DEPOT_DIGEST_ONLY depot session compression + ordered detach"
        else:
            component = "DIRECT_REPAIR_ENTRY short planlet"
        rows.append(
            ShortChainDiagnosticRow(
                case_id=case_row.case_id,
                solve_strategy=case_row.solve_strategy,
                status=case_row.status,
                manual_variant=baseline.variant,
                manual_hook_count=baseline.observed_hook_count,
                solver_business_hook_count=case_row.business_get_put_hook_count,
                hook_delta=hook_delta,
                hook_acceptance_limit=limit,
                hook_within_short_chain_limit=case_row.business_get_put_hook_count <= limit,
                remote_batch_count=int(remote_metrics["remote_batch_count"]),
                remote_cross_count=int(remote_metrics["remote_cross_count"]),
                remote_session_count=int(remote_metrics["remote_session_count"]),
                first_remote_business_hook=int(remote_metrics["first_remote_business_hook"]),
                post_first_remote_non_remote_batch_count=int(remote_metrics["post_first_remote_non_remote_batch_count"]),
                unnecessary_remote_cross_count=unnecessary_remote_cross,
                required_component=component,
            )
        )
    return rows


def build_capacity_consistency_rows(case_rows: list[CaseSummaryRow]) -> list[CapacityConsistencyAuditRow]:
    rows: list[CapacityConsistencyAuditRow] = []
    for case_row in case_rows:
        if not case_row.blocked_reason:
            continue
        for reason in case_row.blocked_reason.split("|"):
            if not reason.startswith("target_final_capacity_infeasible:"):
                continue
            parts = reason.split(":", 2)
            if len(parts) != 3:
                continue
            line = parts[1]
            lengths = parts[2].split(">", 1)
            if len(lengths) != 2:
                continue
            required = float(lengths[0])
            capacity = float(lengths[1])
            rows.append(
                CapacityConsistencyAuditRow(
                    case_id=case_row.case_id,
                    status=case_row.status,
                    blocked_reason=reason,
                    line=line,
                    required_length_m=round(required, 3),
                    capacity_m=round(capacity, 3),
                    excess_m=round(required - capacity, 3),
                    required_fix="InputPhysicalConsistencyGate",
                )
            )
    return rows


def remote_owner_candidate_ids(contract_rows: list[ContractTraceRow]) -> set[str]:
    return {
        row.candidate_id
        for row in contract_rows
        if "remote_cross_owner" in row.hard_obligations
        or row.structural_intent in {
            "SOURCE_CLEAR_RESTORE_PLANLET",
            "SOURCE_EXIT_CLEAR_RESTORE_PLANLET",
            "ROUTE_CLEAR_RESTORE_PLANLET",
            "REMOTE_SESSION_CONTRACT",
            "REMOTE_CORRIDOR_CONTRACT",
            "DEPOT_DIGEST_PLANLET",
            "DEPOT_SLOT_SWAP_PLANLET",
            "SHORT_CHAIN_PLANLET",
        }
    }


def build_structure_work_audit_rows(
    *,
    case_rows: list[CaseSummaryRow],
    phase_rows: list[RuntimePhaseTraceRow],
    contract_rows: list[ContractTraceRow],
    depot_session_rows: list[DepotSessionAuditRow],
    manual_vs_solver_rows: list[ManualVsSolverCaseCompareRow],
    short_chain_rows: list[ShortChainDiagnosticRow],
    capacity_rows: list[CapacityConsistencyAuditRow],
) -> list[StructureWorkAuditRow]:
    rows: list[StructureWorkAuditRow] = []
    owner_ids = remote_owner_candidate_ids(contract_rows)
    phase_by_candidate = {row.candidate_id: row for row in phase_rows}

    def add(
        *,
        item: int,
        name: str,
        scope: str,
        case_id: str,
        solve_strategy: str,
        hook_index: int = 0,
        business_start: int = 0,
        business_end: int = 0,
        candidate_id: str = "",
        passed: bool,
        current_value: str,
        target_value: str,
        gap_reason: str,
        evidence: str,
        next_required_component: str,
    ) -> None:
        rows.append(
            StructureWorkAuditRow(
                structure_item=f"15.13.7.{item}",
                structure_name=name,
                scope=scope,
                case_id=case_id,
                solve_strategy=solve_strategy,
                hook_index=hook_index,
                business_hook_index_start=business_start,
                business_hook_index_end=business_end,
                candidate_id=candidate_id,
                status="passed" if passed else "failed",
                current_value=current_value,
                target_value=target_value,
                gap_reason=gap_reason,
                evidence=evidence,
                next_required_component=next_required_component,
            )
        )

    for row in phase_rows:
        phase_bad = row.phase_permission in {
            "phase_action_mismatch",
            "remote_too_early_against_manual_phase",
            "over_manual_soft_hook_bound",
        }
        passed = not row.forced_phase_open_reason and not phase_bad
        add(
            item=1,
            name="HumanPhaseContractDebt",
            scope="accepted_candidate",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            hook_index=row.hook_index,
            business_start=row.business_hook_index_start,
            business_end=row.business_hook_index_end,
            candidate_id=row.candidate_id,
            passed=passed,
            current_value=(
                f"expected={row.expected_h_phase};runtime={row.runtime_phase};"
                f"permission={row.phase_permission};forced={bool(row.forced_phase_open_reason)};"
                f"front_progress={row.front_service_progress:.4f}"
            ),
            target_value="permission=allowed;forced=false;candidate declares phase debt before hard gate",
            gap_reason=row.forced_phase_open_reason or (row.phase_permission if phase_bad else ""),
            evidence="runtime_phase_trace.csv",
            next_required_component="HumanPhaseContractDebt",
        )

    for row in contract_rows:
        has_owner = contract_trace_has_unlock_owner(row)
        non_positive = row.unsatisfied_reduction <= 0 and row.non_depot_unsatisfied_reduction <= 0
        passed = not (non_positive and not has_owner)
        add(
            item=2,
            name="TargetContractSelector",
            scope="accepted_candidate",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            hook_index=row.hook_index,
            business_start=row.business_hook_index_start,
            business_end=row.business_hook_index_start,
            candidate_id=row.candidate_id,
            passed=passed,
            current_value=(
                f"contract={row.selected_contract};intent={row.structural_intent};"
                f"delta={row.unsatisfied_reduction};non_depot_delta={row.non_depot_unsatisfied_reduction};"
                f"owner={has_owner}"
            ),
            target_value="every accepted non-positive delta candidate has explicit owner/unlock/recovery contract",
            gap_reason="zero_or_negative_delta_without_owner" if not passed else "",
            evidence="contract_trace.csv",
            next_required_component="TargetContractSelector",
        )

    first_remote_by_case: dict[str, RuntimePhaseTraceRow] = {}
    for row in phase_rows:
        if row.touches_remote and row.case_id not in first_remote_by_case:
            first_remote_by_case[row.case_id] = row
    case_by_id = {row.case_id: row for row in case_rows}
    for case_id, case_row in case_by_id.items():
        first_remote = first_remote_by_case.get(case_id)
        baseline_like_short = any(row.case_id == case_id and row.manual_variant in SHORT_CHAIN_VARIANTS for row in short_chain_rows)
        if first_remote is None:
            progress = 1.0 if case_row.status == "completed" else 0.0
            business_start = 0
            candidate_id = ""
            hook_index = 0
        else:
            progress = first_remote.front_service_progress
            business_start = first_remote.business_hook_index_start
            candidate_id = first_remote.candidate_id
            hook_index = first_remote.hook_index
        passed = baseline_like_short or progress >= 0.95
        add(
            item=3,
            name="FrontServiceCarryChain",
            scope="case",
            case_id=case_id,
            solve_strategy=case_row.solve_strategy,
            hook_index=hook_index,
            business_start=business_start,
            business_end=business_start,
            candidate_id=candidate_id,
            passed=passed,
            current_value=f"front_service_progress_at_first_remote={progress:.4f};short_chain={baseline_like_short}",
            target_value="front service debt completion >=0.95 before first remote session, except short direct depot variants",
            gap_reason="" if passed else "remote_entered_before_front_service_debt_closed",
            evidence="runtime_phase_trace.csv + manual phase variant",
            next_required_component="FrontServiceCarryChain",
        )

    for row in depot_session_rows:
        has_owner = row.candidate_id in owner_ids
        requires_owner = row.remote_cross or row.remote_event in {"enter_remote", "exit_remote", "mixed_remote_session"}
        passed = not requires_owner or has_owner
        add(
            item=4,
            name="RemoteSessionDebtSet",
            scope="remote_session_batch",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            hook_index=row.hook_index,
            business_start=row.business_hook_index_start,
            business_end=row.business_hook_index_start,
            candidate_id=row.candidate_id,
            passed=passed,
            current_value=(
                f"session={row.remote_session_id};batch={row.session_batch_index};"
                f"event={row.remote_event};remote_cross={row.remote_cross};owner={has_owner}"
            ),
            target_value="every remote enter/exit/cross has declared remote session owner and debt set",
            gap_reason="" if passed else "remote_session_without_owner",
            evidence="depot_session_audit.csv + contract_trace.csv",
            next_required_component="RemoteSessionDebtSet",
        )

    for row in manual_vs_solver_rows:
        if row.status != "completed":
            continue
        delta = row.solver_remote_business_hook_count - row.manual_remote_hook_count
        transition_delta = row.remote_business_transition_delta
        passed = delta <= 1 and transition_delta <= 1
        add(
            item=4,
            name="DepotDigest/OrderedDetach Session",
            scope="case_remote_compression",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            passed=passed,
            current_value=(
                f"manual_remote_hook={row.manual_remote_hook_count};"
                f"solver_remote_hook={row.solver_remote_business_hook_count};delta={delta};"
                f"manual_transition={row.manual_remote_business_transition_count};"
                f"solver_transition={row.solver_remote_business_transition_count};"
                f"transition_delta={transition_delta};remote_cross={row.solver_remote_cross_count}"
            ),
            target_value="remote_hook_delta<=1 and remote_business_transition_delta<=1",
            gap_reason="" if passed else "remote_session_not_compressed_or_too_fragmented",
            evidence="manual_vs_solver_case_compare.csv",
            next_required_component="DepotDigest/Swap + OrderedDetachSearch",
        )
    for row in short_chain_rows:
        passed = row.status == "completed" and row.hook_within_short_chain_limit
        add(
            item=5,
            name="DepotDigest/Swap + OrderedDetachSearch",
            scope="short_chain_case",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            passed=passed,
            current_value=(
                f"variant={row.manual_variant};manual={row.manual_hook_count};"
                f"solver={row.solver_business_hook_count};limit={row.hook_acceptance_limit};"
                f"remote_batch={row.remote_batch_count};remote_cross={row.remote_cross_count}"
            ),
            target_value="short chain completed and solver hooks <= manual+1",
            gap_reason="" if passed else "short_chain_ordered_detach_missing",
            evidence="short_chain_diagnostic.csv",
            next_required_component=row.required_component,
        )

    for row in contract_rows:
        if row.structural_intent not in {
            "FRONT_CAPACITY_RELEASE_CONTRACT",
            "SAME_LINE_REPOSITION_STAGE_OUT",
        }:
            continue
        has_owner = contract_trace_has_unlock_owner(row)
        passed = has_owner and (row.unsatisfied_reduction > 0 or row.non_depot_unsatisfied_reduction > 0)
        add(
            item=1,
            name="RecoveryAdmissionGate",
            scope="accepted_candidate",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            hook_index=row.hook_index,
            business_start=row.business_hook_index_start,
            business_end=phase_by_candidate.get(row.candidate_id).business_hook_index_end
            if phase_by_candidate.get(row.candidate_id)
            else row.business_hook_index_start,
            candidate_id=row.candidate_id,
            passed=passed,
            current_value=(
                f"intent={row.structural_intent};delta={row.unsatisfied_reduction};"
                f"non_depot_delta={row.non_depot_unsatisfied_reduction};owner={has_owner}"
            ),
            target_value="recovery action is owner-bound and produces immediate contract progress",
            gap_reason="" if passed else "recovery_without_positive_owner_progress",
            evidence="contract_trace.csv",
            next_required_component="RecoveryAdmissionGate",
        )
    for row in case_rows:
        if row.blocked_reason != "all_runtime_candidates_rejected":
            continue
        add(
            item=6,
            name="CapacityInfeasiblePreGate",
            scope="blocked_case",
            case_id=row.case_id,
            solve_strategy=row.solve_strategy,
            passed=False,
            current_value=f"blocked_reason={row.blocked_reason};final_unsatisfied={row.final_unsatisfied_vehicle_count}",
            target_value="all_runtime_candidates_rejected=0 or blocked case has proven input infeasibility",
            gap_reason="blocker_or_capacity_feasibility_search_missing",
            evidence="case_summary.csv + physical_gap_summary.csv",
            next_required_component="BlockerCapacityFeasibility",
        )
    for row in capacity_rows:
        add(
            item=6,
            name="CapacityInfeasiblePreGate",
            scope="capacity_infeasible_case",
            case_id=row.case_id,
            solve_strategy="",
            passed=True,
            current_value=f"{row.line}:{row.required_length_m}>{row.capacity_m}",
            target_value="input capacity feasible before runtime search",
            gap_reason=row.blocked_reason,
            evidence="capacity_consistency_audit.csv",
            next_required_component=row.required_fix,
        )
    return rows


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def build_structural_repair_acceptance_rows(
    *,
    summary: PhysicalRuntimeSummary,
    case_rows: list[CaseSummaryRow],
    phase_rows: list[RuntimePhaseTraceRow],
    contract_rows: list[ContractTraceRow],
    dominance_rows: list[CandidateDominanceAuditRow],
    depot_session_rows: list[DepotSessionAuditRow],
    manual_vs_solver_rows: list[ManualVsSolverCaseCompareRow],
    short_chain_rows: list[ShortChainDiagnosticRow],
    capacity_rows: list[CapacityConsistencyAuditRow],
    repair_mode: str,
) -> list[StructuralRepairAcceptanceRow]:
    phase_mismatch = sum(1 for row in phase_rows if row.phase_permission == "phase_action_mismatch")
    remote_too_early = sum(1 for row in phase_rows if row.phase_permission == "remote_too_early_against_manual_phase")
    forced_open = sum(1 for row in phase_rows if row.forced_phase_open_reason)
    human_phase_rejects = sum(
        count
        for reason, count in summary.rejection_reason_counts.items()
        if reason.startswith("human_phase_contract") or reason.startswith("phase_manual_defer")
    )
    zero_or_negative = sum(
        1
        for row in contract_rows
        if row.unsatisfied_reduction <= 0
        and row.non_depot_unsatisfied_reduction <= 0
        and not contract_trace_has_unlock_owner(row)
    )
    p4_rejects = sum(
        count
        for reason, count in summary.rejection_reason_counts.items()
        if reason.startswith("p4_contract_selector")
    )
    dominated = sum(1 for row in dominance_rows if row.status == "dominated")
    dominance_rate = dominated / len(dominance_rows) if dominance_rows else 0.0
    p7_rejects = sum(
        count
        for reason, count in summary.rejection_reason_counts.items()
        if reason.startswith("p7_reject")
    )
    completed_compares = [row for row in manual_vs_solver_rows if row.status == "completed"]
    remote_cross_values = [float(row.solver_remote_cross_count) for row in completed_compares]
    remote_cross_p50 = percentile(remote_cross_values, 0.5)
    remote_hook_values = [float(row.solver_remote_business_hook_count) for row in completed_compares]
    remote_hook_p50 = percentile(remote_hook_values, 0.5)
    remote_hook_delta_values = [
        float(row.solver_remote_business_hook_count - row.manual_remote_hook_count)
        for row in completed_compares
    ]
    remote_hook_delta_p50 = percentile(remote_hook_delta_values, 0.5)
    remote_batch_over_manual_values = [
        float(row.solver_remote_batch_count - row.manual_remote_hook_count)
        for row in completed_compares
    ]
    remote_batch_over_manual_p75 = percentile(remote_batch_over_manual_values, 0.75)
    remote_transition_delta_values = [
        float(row.remote_business_transition_delta)
        for row in completed_compares
    ]
    remote_transition_delta_p50 = percentile(remote_transition_delta_values, 0.5)
    remote_transition_delta_p90 = percentile(remote_transition_delta_values, 0.9)
    remote_owner_ids = remote_owner_candidate_ids(contract_rows)
    remote_cross_without_owner = sum(
        1
        for row in depot_session_rows
        if row.remote_cross and row.candidate_id not in remote_owner_ids
    )
    short_chain_failed = sum(1 for row in short_chain_rows if row.status != "completed" or not row.hook_within_short_chain_limit)
    all_runtime_rejected = summary.blocked_reason_counts.get("all_runtime_candidates_rejected", 0)

    def row(
        repair_item: str,
        passed: bool,
        current_value: str,
        target_value: str,
        evidence: str,
        next_required_component: str,
    ) -> StructuralRepairAcceptanceRow:
        return StructuralRepairAcceptanceRow(
            repair_item=repair_item,
            status="passed" if passed else "failed",
            current_value=current_value,
            target_value=target_value,
            evidence=evidence,
            next_required_component=next_required_component,
        )

    return [
        row(
            "R1_HumanPhaseContract",
            repair_mode == ALIGNED_REPAIR_MODE and phase_mismatch == 0 and remote_too_early == 0 and forced_open == 0,
            f"phase_mismatch={phase_mismatch};remote_too_early={remote_too_early};forced_open={forced_open};phase_rejects={human_phase_rejects}",
            "phase_mismatch=0;remote_too_early=0;forced_open=0",
            "runtime_phase_trace.csv + rejection_reason_counts",
            "HumanPhaseContract",
        ),
        row(
            "R2_TargetContractSelector",
            zero_or_negative == 0,
            f"zero_or_negative_delta={zero_or_negative};p4_rejects={p4_rejects}",
            "zero_or_negative_delta_without_owner=0",
            "contract_trace.csv",
            "TargetContractSelector",
        ),
        row(
            "R3_ResourceDeltaRejectGate",
            dominance_rate < 0.05,
            f"dominated={dominated};dominance_rate={dominance_rate:.4f};p7_rejects={p7_rejects}",
            "dominance_rate<0.05",
            "candidate_dominance_audit.csv + resource_delta_trace.csv",
            "ResourceDelta + AcceptRejectGate",
        ),
        row(
            "R4_RemoteSessionPlanlet",
            remote_cross_p50 <= 3
            and remote_hook_delta_p50 <= 1
            and remote_transition_delta_p50 <= 1
            and remote_transition_delta_p90 <= 2
            and remote_batch_over_manual_p75 <= 1
            and remote_cross_without_owner == 0,
            (
                f"remote_cross_p50={remote_cross_p50:.2f};remote_hook_p50={remote_hook_p50:.2f};"
                f"remote_hook_delta_p50={remote_hook_delta_p50:.2f};"
                f"remote_transition_delta_p50={remote_transition_delta_p50:.2f};"
                f"remote_transition_delta_p90={remote_transition_delta_p90:.2f};"
                f"remote_batch_over_manual_p75={remote_batch_over_manual_p75:.2f};"
                f"remote_cross_without_owner={remote_cross_without_owner}"
            ),
            "remote_cross_p50<=3;remote_hook_delta_p50<=1;remote_transition_delta_p50<=1;remote_transition_delta_p90<=2;remote_batch_over_manual_p75<=1;remote_cross_without_owner=0",
            "manual_vs_solver_case_compare.csv + depot_session_audit.csv",
            "RemoteSessionContract + DepotDigest/Swap Planlet",
        ),
        row(
            "R5_ShortChainPlanlet",
            short_chain_failed == 0,
            f"short_chain_failed={short_chain_failed};short_chain_cases={len(short_chain_rows)}",
            "all short chains completed and <= manual+1",
            "short_chain_diagnostic.csv",
            "ShortChainPlanlet",
        ),
        row(
            "R6_BlockerCapacityFeasibility",
            summary.completed_case_count + len(capacity_rows) == summary.truth_case_count and all_runtime_rejected == 0,
            f"completed={summary.completed_case_count}/{summary.truth_case_count};capacity_infeasible={len(capacity_rows)};all_runtime_candidates_rejected={all_runtime_rejected}",
            "completed+capacity_infeasible=truth_case_count;all_runtime_candidates_rejected=0",
            "case_summary.csv + capacity_consistency_audit.csv + physical_gap_summary.csv",
            "Blocker/Capacity Feasibility",
        ),
    ]


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
| remote_business_transition_count | {summary.remote_business_transition_count} |
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
- `manual_vs_solver_case_compare.csv`: case-level hook, phase-variant, and remote-area comparison against manual plans.
- `runtime_phase_trace.csv`: accepted-candidate phase permission diagnostics.
- `contract_trace.csv`: accepted-candidate contract and structural intent diagnostics.
- `resource_delta_trace.csv`: accepted-candidate resource request/release diagnostics.
- `candidate_dominance_audit.csv`: same-round dominance audit for accepted candidates.
- `candidate_pool_audit.csv`: per-round phase-permitted front/remote candidate pool audit.
- `depot_session_audit.csv`: remote depot/unwheel session audit.
- `short_chain_diagnostic.csv`: short-chain specific acceptance diagnostics.
- `capacity_consistency_audit.csv`: final target length/capacity consistency failures.
- `structure_work_audit.csv`: detailed 15.13.7 six-structure audit rows.
- `structural_repair_acceptance.csv`: R1-R6 structural repair acceptance summary.
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
- a hook may carry multiple weigh cars; only the last weigh car is marked complete, with the path through `机库线`
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
    manual_baselines = load_manual_baselines(root, args.manual_dir)

    truth_paths = sorted(path for path in truth_dir.glob("*.json") if path.name != "conversion_summary.json")
    if args.case_id:
        wanted = {case.upper() for case in args.case_id}
        truth_paths = [path for path in truth_paths if case_id_from_path(path) in wanted]

    all_case_rows: list[CaseSummaryRow] = []
    all_candidate_rows: list[CandidateAuditRow] = []
    all_operation_rows: list[OperationTraceRow] = []
    all_diagnostics = RuntimeDiagnosticRows()
    rejection_reasons: Counter[str] = Counter()

    for truth_path in truth_paths:
        case_row, candidate_rows, operation_rows_for_case, case_reasons, diagnostics = run_case_with_diagnostics(
            truth_path=truth_path,
            output_dir=output_dir,
            graph=graph,
            max_hooks=args.max_hooks,
            manual_baselines=manual_baselines,
            strategy_mode=args.strategy_mode,
            repair_mode=args.repair_mode,
            allow_phase_forced_open=args.allow_phase_forced_open,
            strategy_name_filter=args.strategy_name_filter,
        )
        all_case_rows.append(case_row)
        all_candidate_rows.extend(candidate_rows)
        all_operation_rows.extend(operation_rows_for_case)
        all_diagnostics.extend(diagnostics)
        rejection_reasons.update(case_reasons)

    summary = build_summary(all_case_rows, rejection_reasons)
    gap_rows = build_gap_rows(all_candidate_rows, all_case_rows)
    manual_vs_solver_rows = build_manual_vs_solver_rows(all_case_rows, all_operation_rows, manual_baselines)
    short_chain_rows = build_short_chain_rows(all_case_rows, all_operation_rows, manual_baselines)
    capacity_rows = build_capacity_consistency_rows(all_case_rows)
    structure_work_rows = build_structure_work_audit_rows(
        case_rows=all_case_rows,
        phase_rows=all_diagnostics.phase_rows,
        contract_rows=all_diagnostics.contract_rows,
        depot_session_rows=all_diagnostics.depot_session_rows,
        manual_vs_solver_rows=manual_vs_solver_rows,
        short_chain_rows=short_chain_rows,
        capacity_rows=capacity_rows,
    )
    structural_repair_rows = build_structural_repair_acceptance_rows(
        summary=summary,
        case_rows=all_case_rows,
        phase_rows=all_diagnostics.phase_rows,
        contract_rows=all_diagnostics.contract_rows,
        dominance_rows=all_diagnostics.dominance_rows,
        depot_session_rows=all_diagnostics.depot_session_rows,
        manual_vs_solver_rows=manual_vs_solver_rows,
        short_chain_rows=short_chain_rows,
        capacity_rows=capacity_rows,
        repair_mode=args.repair_mode,
    )
    write_csv(output_dir / "case_summary.csv", [asdict(row) for row in all_case_rows])
    write_csv(output_dir / "candidate_physical_audit.csv", [asdict(row) for row in all_candidate_rows])
    write_csv(output_dir / "operation_trace.csv", [asdict(row) for row in all_operation_rows])
    write_csv(output_dir / "physical_gap_summary.csv", [asdict(row) for row in gap_rows])
    write_csv(output_dir / "manual_vs_solver_case_compare.csv", [asdict(row) for row in manual_vs_solver_rows])
    write_csv(output_dir / "runtime_phase_trace.csv", [asdict(row) for row in all_diagnostics.phase_rows])
    write_csv(output_dir / "contract_trace.csv", [asdict(row) for row in all_diagnostics.contract_rows])
    write_csv(output_dir / "resource_delta_trace.csv", [asdict(row) for row in all_diagnostics.resource_rows])
    write_csv(output_dir / "candidate_dominance_audit.csv", [asdict(row) for row in all_diagnostics.dominance_rows])
    write_csv(output_dir / "candidate_pool_audit.csv", [asdict(row) for row in all_diagnostics.candidate_pool_rows])
    write_csv(output_dir / "depot_session_audit.csv", [asdict(row) for row in all_diagnostics.depot_session_rows])
    write_csv(output_dir / "short_chain_diagnostic.csv", [asdict(row) for row in short_chain_rows])
    write_csv(output_dir / "capacity_consistency_audit.csv", [asdict(row) for row in capacity_rows])
    write_csv(output_dir / "structure_work_audit.csv", [asdict(row) for row in structure_work_rows])
    write_csv(output_dir / "structural_repair_acceptance.csv", [asdict(row) for row in structural_repair_rows])
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
    parser.add_argument("--manual-dir", default="data/人工调车数据")
    parser.add_argument("--output-dir", default="artifacts/physical_runtime_trace")
    parser.add_argument("--max-hooks", type=int, default=300)
    parser.add_argument(
        "--strategy-mode",
        choices=(STANDARD_STRATEGY_MODE, DIAGNOSTIC_STRATEGY_MODE),
        default=STANDARD_STRATEGY_MODE,
        help="standard excludes early_depot strategies from selected runtime; diagnostic includes them for comparison.",
    )
    parser.add_argument(
        "--repair-mode",
        choices=(AUDIT_REPAIR_MODE, ALIGNED_REPAIR_MODE),
        default=AUDIT_REPAIR_MODE,
        help="audit records R1-R6 diagnostics only; aligned applies structural phase/contract/reject gates.",
    )
    parser.add_argument(
        "--allow-phase-forced-open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="allow deferred phase candidates to run when no phase-permitted candidate exists; still counted as R1 failure.",
    )
    parser.add_argument(
        "--strategy-name-filter",
        default="",
        help="diagnostic-only substring filter for strategy names; default runs the normal strategy set.",
    )
    parser.add_argument("--case-id", nargs="*")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
