from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContractFamily(str, Enum):
    REMOTE_SESSION = "REMOTE_SESSION"
    REPAIR_INBOUND = "REPAIR_INBOUND"
    DEPOT_SLOT = "DEPOT_SLOT"
    DEPOT_OUTBOUND = "DEPOT_OUTBOUND"
    CUN4_PORT_STAGING = "CUN4_PORT_STAGING"
    PRE_REPAIR_STAGING = "PRE_REPAIR_STAGING"
    DISPATCH_SHED_QUEUE = "DISPATCH_SHED_QUEUE"
    YARD_REBALANCE = "YARD_REBALANCE"
    FUNCTION_LINE_SERVICE = "FUNCTION_LINE_SERVICE"
    LOCO_AREA_STAGING = "LOCO_AREA_STAGING"
    SPECIAL_REPAIR_PROCESS = "SPECIAL_REPAIR_PROCESS"
    TAIL_CLOSEOUT = "TAIL_CLOSEOUT"
    RESIDUAL = "RESIDUAL"


class IntentKind(str, Enum):
    REMOTE_SESSION = "REMOTE_SESSION"
    FRONT_PREP = "FRONT_PREP"
    CUN4_RELEASE_GROUP = "CUN4_RELEASE_GROUP"
    CUN4_OUTBOUND_HOLD = "CUN4_OUTBOUND_HOLD"
    CUN4_RELEASE_ACCEPT = "CUN4_RELEASE_ACCEPT"
    REMOTE_DEPOT = "REMOTE_DEPOT"
    DEPOT_INBOUND_ASSEMBLY = "DEPOT_INBOUND_ASSEMBLY"
    TAIL_CLOSEOUT = "TAIL_CLOSEOUT"
    DEPOT_SLOT_SWAP = "DEPOT_SLOT_SWAP"
    BLOCKER_STAGING = "BLOCKER_STAGING"


class ResourceKind(str, Enum):
    REMOTE_SESSION = "REMOTE_SESSION"
    LOCO_POSITION = "LOCO_POSITION"
    LOCO_CARRY = "LOCO_CARRY"
    ROUTE_GET = "ROUTE_GET"
    ROUTE_PUT = "ROUTE_PUT"
    LINE_CAPACITY = "LINE_CAPACITY"
    DEPOT_SLOT = "DEPOT_SLOT"
    CUN4_NORTH_BUFFER = "CUN4_NORTH_BUFFER"
    GLOBAL_GATE = "GLOBAL_GATE"
    WEIGH_STAND = "WEIGH_STAND"
    SERIAL_LINE_GATE = "SERIAL_LINE_GATE"


@dataclass(frozen=True)
class CarRef:
    no: str
    line: str
    position: int
    target_line: str
    target_position: int | None
    target_reason: str
    contract_family: ContractFamily
    satisfied: bool
    is_remote_source: bool
    is_remote_target: bool
    is_weigh: bool
    is_closed_door: bool
    length_m: float
    force_positions: tuple[int, ...] = ()


@dataclass(frozen=True)
class FlowContract:
    contract_id: str
    family: ContractFamily
    subject_nos: tuple[str, ...]
    source_lines: tuple[str, ...]
    target_lines: tuple[str, ...]
    priority: int
    obligations: tuple[str, ...]
    protections: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ResourceRequest:
    contract_id: str
    family: ContractFamily
    candidate_id: str
    resources: tuple[ResourceKind, ...]
    source_line: str
    target_line: str
    move_nos: tuple[str, ...]
    touched_lines: tuple[str, ...] = ()
    put_lines: tuple[str, ...] = ()
    intent: IntentKind | None = None
    same_plan_source_return_nos: tuple[str, ...] = ()


class PhaseKind(str, Enum):
    H1_FRONT_SERVICE = "H1_FRONT_SERVICE"
    H2_CUN4_PORT = "H2_CUN4_PORT"
    H3_RELEASE_ACCEPT = "H3_RELEASE_ACCEPT"
    H4_REMOTE_DEPOT = "H4_REMOTE_DEPOT"
    H5_CLOSEOUT = "H5_CLOSEOUT"


@dataclass(frozen=True)
class PhaseState:
    phase: PhaseKind
    front_debt: int
    cun4_port_debt: int
    remote_debt: int
    closeout_debt: int
    reason: str
    cun4_release_ready: bool = False
    cun4_port_mode: str = ""
    cun4_release_count: int = 0
    cun4_prefix_hold_count: int = 0
    active_variant: str = ""
    front_topology_clear_for_remote: bool = True
    depot_inbound_assembly_complete: bool = True
    depot_outbound_assembly_complete: bool = True
    strategic_plan_reason: str = ""


@dataclass(frozen=True)
class ResourceDelta:
    request: ResourceRequest
    acquired: tuple[ResourceKind, ...]
    released_lines: tuple[str, ...]
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractDelta:
    contract_id: str
    family: ContractFamily
    before_unsatisfied: int
    after_unsatisfied: int
    before_contract_debt: int
    after_contract_debt: int
    fulfilled: tuple[str, ...] = ()
    reduced: tuple[str, ...] = ()
    broken: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    support_gain: int = 0

    @property
    def total_reduction(self) -> int:
        return self.before_unsatisfied - self.after_unsatisfied

    @property
    def contract_reduction(self) -> int:
        return self.before_contract_debt - self.after_contract_debt

    @property
    def effective_gain(self) -> int:
        return self.contract_reduction + self.support_gain


@dataclass(frozen=True)
class CandidateEnvelope:
    candidate: Any
    contract: FlowContract
    intent: IntentKind
    resource_request: ResourceRequest
    template_name: str


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reason: str
    contract_delta: ContractDelta | None = None
    resource_delta: ResourceDelta | None = None


@dataclass
class SerialGateLease:
    lease_id: str
    owner_contract_id: str
    blocker_line: str
    opened_hook: int
    blocker_nos: tuple[str, ...]
    debt_nos: tuple[str, ...]


@dataclass(frozen=True)
class RemoteSessionState:
    active: bool = False
    session_id: str = ""
    owner_contract_id: str = ""
    opened_hook: int = 0
    last_touched_hook: int = 0
    source_lines: tuple[str, ...] = ()
    target_lines: tuple[str, ...] = ()
    debt_nos: tuple[str, ...] = ()
    mode: str = ""


@dataclass
class SolverState:
    case_id: str
    cars: list[dict[str, Any]]
    depot_assignment: Any
    loco_location: Any
    hook_index: int = 1
    accepted_candidate_ids: set[str] = field(default_factory=set)
    visited_signatures: set[tuple[str, str, tuple[tuple[str, str, int], ...]]] = field(default_factory=set)
    remote_session: RemoteSessionState = field(default_factory=RemoteSessionState)
    depot_inbound_assembly_accepted: bool = False
    last_business_remote: bool | None = None
    serial_gate_leases: dict[str, SerialGateLease] = field(default_factory=dict)

    @property
    def remote_session_open(self) -> bool:
        return self.remote_session.active


@dataclass(frozen=True)
class StepTrace:
    case_id: str
    hook_index: int
    phase: str
    phase_reason: str
    phase_front_debt: int
    phase_cun4_port_debt: int
    phase_remote_debt: int
    phase_closeout_debt: int
    remote_session_open: bool
    remote_session_id: str
    remote_session_owner: str
    remote_session_mode: str
    candidate_id: str
    contract_id: str
    family: str
    intent: str
    template_name: str
    source_line: str
    target_line: str
    touched_lines: str
    put_lines: str
    move_nos: str
    same_plan_source_return_nos: str
    gate_accepted: bool
    selected: bool
    gate_reason: str
    physical_reasons: str
    requested_resources: str
    acquired_resources: str
    resource_violations: str
    before_unsatisfied: int
    after_unsatisfied: int
    before_contract_debt: int
    after_contract_debt: int
    contract_reduction: int
    support_gain: int
    effective_gain: int
    total_reduction: int


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str
    hook_count: int
    operation_count: int
    remote_business_transition_count: int
    initial_unsatisfied: int
    final_unsatisfied: int
    final_length_warning_count: int
    blocked_reason: str
    step_count: int
    accepted_without_phase_permission_count: int
    phase_gate_bypass_count: int
    hard_physical_violation_accepted_count: int
