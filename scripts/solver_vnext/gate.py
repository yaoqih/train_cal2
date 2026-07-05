from __future__ import annotations

from typing import Any

from . import strategic_plan as strategic
from .domain import ContractDelta, GateDecision, ResourceDelta


class AcceptRejectGate:
    def decide(
        self,
        contract_delta: ContractDelta,
        resource_delta: ResourceDelta,
        *,
        strategic_plan: Any | None = None,
        candidate: Any | None = None,
    ) -> GateDecision:
        if resource_delta.violations:
            return GateDecision(False, "resource_violation:" + "|".join(resource_delta.violations), contract_delta, resource_delta)
        if strategic_plan is not None and candidate is not None:
            four_stage_violations = strategic.four_stage_plan_violations(
                plan=strategic_plan,
                candidate=candidate,
            )
            if four_stage_violations:
                return GateDecision(False, "four_stage_violation:" + "|".join(four_stage_violations), contract_delta, resource_delta)
            plan_violations = strategic.depot_outbound_plan_violations(
                plan=strategic_plan,
                candidate=candidate,
            )
            if plan_violations:
                return GateDecision(False, "strategic_plan_violation:" + "|".join(plan_violations), contract_delta, resource_delta)
            inbound_violations = strategic.depot_inbound_plan_violations(
                plan=strategic_plan,
                candidate=candidate,
            )
            if inbound_violations:
                return GateDecision(False, "strategic_plan_violation:" + "|".join(inbound_violations), contract_delta, resource_delta)
        if contract_delta.broken:
            return GateDecision(False, "contract_broken:" + "|".join(contract_delta.broken), contract_delta, resource_delta)
        if contract_delta.effective_gain <= 0 and contract_delta.total_reduction <= 0:
            return GateDecision(False, "nonpositive_contract_and_global_delta", contract_delta, resource_delta)
        return GateDecision(True, "accepted", contract_delta, resource_delta)

    def loop_reject(self, contract_delta: ContractDelta, resource_delta: ResourceDelta) -> GateDecision:
        return GateDecision(False, "state_signature_loop", contract_delta, resource_delta)
