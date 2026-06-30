from __future__ import annotations

from .domain import ContractDelta, GateDecision, ResourceDelta


class AcceptRejectGate:
    def decide(self, contract_delta: ContractDelta, resource_delta: ResourceDelta) -> GateDecision:
        if resource_delta.violations:
            return GateDecision(False, "resource_violation:" + "|".join(resource_delta.violations), contract_delta, resource_delta)
        if contract_delta.broken:
            return GateDecision(False, "contract_broken:" + "|".join(contract_delta.broken), contract_delta, resource_delta)
        if contract_delta.effective_gain <= 0 and contract_delta.total_reduction <= 0:
            return GateDecision(False, "nonpositive_contract_and_global_delta", contract_delta, resource_delta)
        return GateDecision(True, "accepted", contract_delta, resource_delta)

    def loop_reject(self, contract_delta: ContractDelta, resource_delta: ResourceDelta) -> GateDecision:
        return GateDecision(False, "state_signature_loop", contract_delta, resource_delta)
