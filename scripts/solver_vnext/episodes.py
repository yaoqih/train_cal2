from __future__ import annotations

from typing import Any, Iterable

from . import legacy_adapter as legacy
from .access import build_prefix_access_lease_planlet
from .domain import CandidateEnvelope, ContractFamily, FlowContract, IntentKind, ResourceRequest
from .placement import planned_positions_for_batch
from .planlets import build_tail_digest_planlet


class Episode:
    intent: IntentKind
    template_name: str

    def applies(self, contract: FlowContract) -> bool:
        raise NotImplementedError

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        raise NotImplementedError

    def _envelope(self, candidate: Any, contract: FlowContract) -> CandidateEnvelope:
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line=candidate.source_line,
            target_line=candidate.target_line,
            move_nos=tuple(candidate.move_car_nos),
            intent=self.intent,
            same_plan_restore_nos=(),
        )
        return CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )


class DirectMoveEpisode(Episode):
    intent = IntentKind.FRONT_PREP
    template_name = "direct_accessible_prefix"
    access_template_name = "direct_prefix_access_lease"
    access_candidate_kind = "vnext_direct_prefix_access_lease"
    allowed_families = {
        ContractFamily.FUNCTION_LINE_SERVICE,
        ContractFamily.DISPATCH_SHED_QUEUE,
        ContractFamily.PRE_REPAIR_STAGING,
        ContractFamily.YARD_REBALANCE,
        ContractFamily.CUN4_PORT_STAGING,
        ContractFamily.LOCO_AREA_STAGING,
        ContractFamily.SPECIAL_REPAIR_PROCESS,
    }

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in self.allowed_families

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        by_no = {legacy.car_no(car): car for car in cars}
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        line_cars = legacy.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        contract_nos = set(contract.subject_nos)
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            no = legacy.car_no(car)
            if no not in contract_nos:
                if batch:
                    break
                continue
            if no not in by_no:
                continue
            if legacy.pull_equivalent([*batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        if not batch:
            return
        batch_nos = {legacy.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
        if len(positions) == len(batch):
            candidate = legacy.build_direct_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=f"vnext:{self.template_name};contract={contract.contract_id};batch={len(batch)}",
                candidate_kind="vnext_front_direct",
                planned_positions=positions,
            )
            if candidate:
                yield self._envelope(candidate, contract)
        access_envelope = self._prefix_access_lease_envelope(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            contract=contract,
            line_cars=line_cars,
            source_line=source_line,
            target_line=target_line,
        )
        if access_envelope:
            yield access_envelope

    def _prefix_access_lease_envelope(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        contract: FlowContract,
        line_cars: list[dict[str, Any]],
        source_line: str,
        target_line: str,
    ) -> CandidateEnvelope | None:
        contract_nos = set(contract.subject_nos)
        blocker_batch: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        saw_target = False
        for car in line_cars:
            no = legacy.car_no(car)
            if not saw_target and no not in contract_nos:
                if legacy.pull_equivalent([*blocker_batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                    return None
                blocker_batch.append(car)
                continue
            if no not in contract_nos:
                break
            saw_target = True
            if legacy.pull_equivalent([*blocker_batch, *target_batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                break
            target_batch.append(car)
        if not blocker_batch or not target_batch:
            return None

        plan = build_prefix_access_lease_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            blocker_batch=blocker_batch,
            target_batch=target_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=(
                f"vnext:{self.access_template_name};owner_contract={contract.contract_id};"
                f"blockers={','.join(legacy.car_no(car) for car in blocker_batch)};"
                f"targets={','.join(legacy.car_no(car) for car in target_batch)}"
            ),
            candidate_kind=self.access_candidate_kind,
        )
        if plan is None:
            return None
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=plan.candidate.candidate_id,
            resources=(),
            source_line=source_line,
            target_line=target_line,
            move_nos=tuple(plan.candidate.move_car_nos),
            intent=self.intent,
            same_plan_restore_nos=plan.restored_nos,
        )
        return CandidateEnvelope(
            candidate=plan.candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.access_template_name,
        )


class RemoteDepotEpisode(DirectMoveEpisode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "remote_depot_direct_accessible_prefix"
    access_template_name = "remote_depot_prefix_access_lease"
    access_candidate_kind = "vnext_remote_depot_prefix_access_lease"
    allowed_families = {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT, ContractFamily.DEPOT_OUTBOUND}

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in self.allowed_families


class RemoteSessionEpisode(Episode):
    intent = IntentKind.REMOTE_SESSION
    template_name = "remote_session_directional_digest"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.REMOTE_SESSION

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = legacy.line_loads(cars)
        subject_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
            return target_line

        def line_is_remote(line: str) -> bool:
            return line in legacy.REMOTE_INTERACTION_LINES

        def source_matches(mode: str, line: str) -> bool:
            if mode == "outbound":
                return line_is_remote(line)
            if mode == "inbound":
                return not line_is_remote(line)
            return line_is_remote(line)

        def target_matches(mode: str, line: str) -> bool:
            if not line:
                return False
            if mode == "outbound":
                return not line_is_remote(line)
            if mode == "inbound":
                return line_is_remote(line)
            return line_is_remote(line)

        for mode in ("outbound", "inbound", "internal"):
            selected_sources: list[tuple[str, list[dict[str, Any]]]] = []
            carry: list[dict[str, Any]] = []
            source_order = sorted(
                contract.source_lines,
                key=lambda line: (
                    0 if (mode != "inbound" and line_is_remote(line)) else 1,
                    line != "存4线",
                    line,
                ),
            )
            for source_line in source_order:
                if not source_matches(mode, source_line):
                    continue
                line_cars = legacy.line_cars_in_access_order(
                    cars=cars,
                    line=source_line,
                    graph=graph,
                    loco_location=loco_location,
                )
                batch: list[dict[str, Any]] = []
                for car in line_cars:
                    no = legacy.car_no(car)
                    target_line = target_for(car)
                    if no not in subject_nos or not target_matches(mode, target_line):
                        break
                    if legacy.pull_equivalent([*carry, *batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                        break
                    batch.append(car)
                    if len(batch) >= 6:
                        break
                if batch:
                    selected_sources.append((source_line, batch))
                    carry.extend(batch)
                if len(selected_sources) >= 4 or legacy.pull_equivalent(carry) >= legacy.legacy.PULL_LIMIT_EQUIVALENT:
                    break

            min_batch = 2 if mode == "internal" else 3
            if len(carry) < min_batch:
                continue

            target_groups: dict[str, list[dict[str, Any]]] = {}
            for car in carry:
                target_groups.setdefault(target_for(car), []).append(car)
            target_groups.pop("", None)
            if not target_groups:
                continue

            if mode == "outbound":
                target_order = sorted(target_groups, key=lambda line: (line != "存4线", line_is_remote(line), line))
            elif mode == "inbound":
                target_order = sorted(target_groups, key=lambda line: (line not in legacy.DEPOT_LINES, line not in legacy.DEPOT_TARGET_LINES, line))
            else:
                target_order = sorted(target_groups, key=lambda line: (line not in legacy.DEPOT_LINES, line))

            steps = [
                legacy.plan_step("Get", source_line, tuple(legacy.car_no(car) for car in batch))
                for source_line, batch in selected_sources
            ]
            planned_positions: dict[str, int] = {}
            no_to_car = {legacy.car_no(car): car for car in carry}
            no_to_target = {
                legacy.car_no(car): target_line
                for target_line, group in target_groups.items()
                for car in group
            }
            remaining = [legacy.car_no(car) for car in carry if legacy.car_no(car) in no_to_target]
            if len(remaining) != len(carry):
                continue

            # Put is constrained by the physical tail of the carried consist.
            while remaining:
                target_line = no_to_target[remaining[-1]]
                if target_line not in target_order:
                    break
                start = len(remaining) - 1
                while start > 0 and no_to_target.get(remaining[start - 1]) == target_line:
                    start -= 1
                drop = remaining[start:]
                group = [no_to_car[no] for no in drop if no in no_to_car]
                if len(group) != len(drop):
                    break
                positions = planned_positions_for_batch(
                    batch=group,
                    target_line=target_line,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    batch_nos=set(remaining),
                )
                if len(positions) != len(group):
                    break
                probe = legacy.build_direct_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=selected_sources[0][0],
                    target_line=target_line,
                    batch=group,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    reason="vnext:remote_session_position_probe",
                    candidate_kind="vnext_position_probe",
                    planned_positions=positions,
                )
                if probe is None:
                    break
                planned_positions.update(positions)
                steps.append(legacy.plan_step("Put", target_line, tuple(drop), positions))
                del remaining[start:]
            if remaining:
                continue

            candidate = legacy.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=steps[0].line,
                target_line=steps[-1].line,
                batch=carry,
                steps=tuple(steps),
                reason=(
                    f"vnext:{self.template_name};mode={mode};"
                    f"sources={len(selected_sources)};targets={len(target_groups)};batch={len(carry)}"
                ),
                candidate_kind="vnext_remote_session_digest",
            )
            yield self._envelope(candidate, contract)


class DepotOutboundSessionEpisode(Episode):
    intent = IntentKind.REMOTE_SESSION
    template_name = "depot_outbound_session"

    source_order = (
        "卸轮线",
        "修1库外",
        "修1库内",
        "修2库外",
        "修2库内",
        "修3库外",
        "修3库内",
        "修4库外",
        "修4库内",
    )

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.REMOTE_SESSION

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = legacy.line_loads(cars)
        subject_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
            return target_line

        target_line = self._primary_outbound_target(cars, subject_nos, target_for)
        if not target_line:
            return
        carry: list[dict[str, Any]] = []
        steps = []
        for source_line in self.source_order:
            if source_line not in contract.source_lines:
                continue
            line_cars = legacy.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            batch: list[dict[str, Any]] = []
            for car in line_cars:
                no = legacy.car_no(car)
                if no not in subject_nos or target_for(car) != target_line:
                    break
                if legacy.pull_equivalent([*carry, *batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                    break
                batch.append(car)
            if batch:
                carry.extend(batch)
                steps.append(legacy.plan_step("Get", source_line, tuple(legacy.car_no(car) for car in batch)))
        if len(carry) < 3 or len(steps) < 2:
            return

        batch_nos = {legacy.car_no(car) for car in carry}
        positions = planned_positions_for_batch(
            batch=carry,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
        if len(positions) != len(carry):
            return
        probe = legacy.build_direct_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=target_line,
            batch=carry,
            cars=cars,
            depot_assignment=depot_assignment,
            reason="vnext:depot_outbound_session_position_probe",
            candidate_kind="vnext_position_probe",
            planned_positions=positions,
        )
        if probe is None:
            return
        steps.append(legacy.plan_step("Put", target_line, tuple(legacy.car_no(car) for car in carry), positions))
        candidate = legacy.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=target_line,
            batch=carry,
            steps=tuple(steps),
            reason=(
                f"vnext:{self.template_name};target={target_line};"
                f"sources={len(steps) - 1};batch={len(carry)}"
            ),
            candidate_kind="vnext_depot_outbound_session",
        )
        yield self._envelope(candidate, contract)

    def _primary_outbound_target(
        self,
        cars: list[dict[str, Any]],
        subject_nos: set[str],
        target_for: Any,
    ) -> str:
        counts: dict[str, int] = {}
        for car in cars:
            if legacy.car_no(car) not in subject_nos or car["Line"] not in self.source_order:
                continue
            target_line = target_for(car)
            if not target_line or target_line in legacy.REMOTE_INTERACTION_LINES:
                continue
            counts[target_line] = counts.get(target_line, 0) + 1
        if not counts:
            return ""
        return min(counts, key=lambda line: (line != "存4线", -counts[line], line))


class DepotMultiDropEpisode(Episode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "depot_multi_drop_accessible_prefix"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT}

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        line_cars = legacy.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not line_cars:
            return
        loads = legacy.line_loads(cars)
        batch: list[dict[str, Any]] = []
        target_groups: dict[str, list[dict[str, Any]]] = {}
        for car in line_cars:
            target_line, _position, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
            if target_line not in legacy.DEPOT_TARGET_LINES:
                if batch:
                    break
                continue
            if legacy.pull_equivalent([*batch, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            target_groups.setdefault(target_line, []).append(car)
            if len(target_groups) >= 2 and len(batch) >= 2:
                # Enough structure to justify a multi-drop candidate.
                pass
        if len(batch) < 2 or len(target_groups) < 2:
            return
        steps = [legacy.plan_step("Get", source_line, tuple(legacy.car_no(car) for car in batch))]
        planned_positions: dict[str, int] = {}
        remaining = list(legacy.car_no(car) for car in batch)
        while remaining:
            tail_no = remaining[-1]
            tail_car = next(car for car in batch if legacy.car_no(car) == tail_no)
            target_line, _position, _reason = legacy.planned_target_for_car(tail_car, cars, depot_assignment, loads)
            if target_line not in target_groups:
                return
            drop: list[str] = []
            for no in reversed(remaining):
                car = next(item for item in batch if legacy.car_no(item) == no)
                car_target, _pos, _reason = legacy.planned_target_for_car(car, cars, depot_assignment, loads)
                if car_target != target_line:
                    break
                drop.append(no)
            drop = list(reversed(drop))
            group = [car for car in batch if legacy.car_no(car) in set(drop)]
            group_nos = tuple(drop)
            positions = planned_positions_for_batch(
                batch=group,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=set(group_nos),
            )
            if len(positions) != len(group):
                return
            group_candidate = legacy.build_direct_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                batch=group,
                cars=cars,
                depot_assignment=depot_assignment,
                reason="vnext:multi_drop_position_probe",
                candidate_kind="vnext_position_probe",
                planned_positions=positions,
            )
            if group_candidate is None:
                return
            planned_positions.update(positions)
            steps.append(legacy.plan_step("Put", target_line, group_nos, positions))
            remaining = remaining[: -len(drop)]
        candidate = legacy.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=steps[-1].line,
            batch=batch,
            steps=tuple(steps),
            reason=f"vnext:{self.template_name};source={source_line};targets={','.join(sorted(target_groups))};batch={len(batch)}",
            candidate_kind="vnext_depot_multi_drop",
        )
        yield self._envelope(candidate, contract)


class PrefixDigestEpisode(Episode):
    intent = IntentKind.PREFIX_DIGEST
    template_name = "owned_prefix_tail_digest_restore"
    allowed_families = {
        ContractFamily.REPAIR_INBOUND,
        ContractFamily.DEPOT_SLOT,
        ContractFamily.FUNCTION_LINE_SERVICE,
        ContractFamily.DISPATCH_SHED_QUEUE,
        ContractFamily.PRE_REPAIR_STAGING,
        ContractFamily.CUN4_PORT_STAGING,
        ContractFamily.LOCO_AREA_STAGING,
        ContractFamily.YARD_REBALANCE,
    }

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in self.allowed_families

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        line_cars = legacy.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not line_cars:
            return
        contract_nos = set(contract.subject_nos)
        loads = legacy.line_loads(cars)
        prefix: list[dict[str, Any]] = []
        seen_contract_car = False
        for car in line_cars:
            if legacy.pull_equivalent([*prefix, car]) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
            if legacy.car_no(car) in contract_nos:
                seen_contract_car = True
        if not seen_contract_car or len(prefix) < 2:
            return
        while prefix:
            tail_target, _position, _reason = legacy.planned_target_for_car(
                prefix[-1],
                cars,
                depot_assignment,
                loads,
            )
            if tail_target and tail_target != source_line:
                break
            prefix.pop()
        if len(prefix) < 2 or not any(legacy.car_no(car) in contract_nos for car in prefix):
            return
        progressed_targets = [
            legacy.planned_target_for_car(car, cars, depot_assignment, loads)[0]
            for car in prefix
        ]
        progressed_targets = [
            line
            for line in progressed_targets
            if line and line != source_line
        ]
        if any(line in legacy.REMOTE_INTERACTION_LINES for line in progressed_targets) and any(
            line not in legacy.REMOTE_INTERACTION_LINES for line in progressed_targets
        ):
            return

        plan = build_tail_digest_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            prefix=prefix,
            cars=cars,
            depot_assignment=depot_assignment,
            target_line_for_car=lambda car: legacy.planned_target_for_car(car, cars, depot_assignment, loads)[0],
            restore_remaining_to_source=True,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"prefix={','.join(legacy.car_no(car) for car in prefix)}"
            ),
            candidate_kind="vnext_owned_prefix_digest_restore",
        )
        if plan is None:
            return
        if not any(no in contract_nos for no in plan.progressed_nos):
            return
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=plan.candidate.candidate_id,
            resources=(),
            source_line=plan.candidate.source_line,
            target_line=plan.candidate.target_line,
            move_nos=tuple(plan.candidate.move_car_nos),
            intent=self.intent,
            same_plan_restore_nos=plan.restored_nos,
        )
        yield CandidateEnvelope(
            candidate=plan.candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )


class DepotRepackWithInboundTailEpisode(Episode):
    intent = IntentKind.DEPOT_REPACK
    template_name = "depot_repack_with_inbound_tail"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.REPAIR_INBOUND

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        if target_line not in legacy.DEPOT_LINES or source_line == target_line:
            return

        target_existing = legacy.line_cars_in_access_order(
            cars=cars,
            line=target_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not target_existing:
            return
        target_existing_nos = tuple(legacy.car_no(car) for car in target_existing)
        for car in target_existing:
            slot = depot_assignment.slots.get(legacy.car_no(car))
            if not slot or slot.line != target_line or slot.locked:
                return

        contract_nos = set(contract.subject_nos)
        source_access = legacy.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        prefix: list[dict[str, Any]] = []
        inbound: list[dict[str, Any]] = []
        saw_inbound = False
        for car in source_access:
            no = legacy.car_no(car)
            if not saw_inbound:
                prefix.append(car)
                if no in contract_nos:
                    saw_inbound = True
                    inbound.append(car)
                continue
            if no not in contract_nos:
                break
            prefix.append(car)
            inbound.append(car)
        if not inbound or prefix[-len(inbound):] != inbound:
            return
        blockers = prefix[: len(prefix) - len(inbound)]
        if any(car.get("IsWeigh") for car in prefix):
            return
        if legacy.pull_equivalent(prefix) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
            return
        replay_batch = [*target_existing, *inbound]
        if legacy.pull_equivalent(replay_batch) > legacy.legacy.PULL_LIMIT_EQUIVALENT:
            return

        all_move_nos = {legacy.car_no(car) for car in [*prefix, *target_existing]}
        inbound_nos = tuple(legacy.car_no(car) for car in inbound)
        blocker_nos = tuple(legacy.car_no(car) for car in blockers)
        replay_nos = (*target_existing_nos, *inbound_nos)
        grouped = legacy.cars_by_line(cars)

        for staging_line in legacy.legacy.DEPOT_STAGING_LINE_PRIORITY:
            if staging_line in {source_line, target_line}:
                continue
            if staging_line in legacy.DEPOT_TARGET_LINES or staging_line in legacy.RUNNING_LINES:
                continue
            spec = legacy.legacy.TRACK_SPECS.get(staging_line)
            if not spec or spec.track_type == "temporary":
                continue

            staging_positions = planned_positions_for_batch(
                batch=inbound,
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=all_move_nos,
            )
            if len(staging_positions) != len(inbound):
                continue
            if not legacy.legacy.candidate_positions_available(staging_line, staging_positions, cars, all_move_nos, grouped):
                continue
            if not legacy.legacy.line_has_length_capacity(staging_line, cars, inbound, all_move_nos, grouped=grouped):
                continue

            restore_positions = {}
            if blockers:
                restore_positions = {
                    legacy.car_no(car): int(car.get("Position") or index)
                    for index, car in enumerate(blockers, start=1)
                }
                if not legacy.legacy.candidate_positions_available(source_line, restore_positions, cars, all_move_nos, grouped):
                    continue
            target_positions = {
                no: position
                for position, no in enumerate(replay_nos, start=1)
            }
            steps = [
                legacy.plan_step("Get", source_line, tuple(legacy.car_no(car) for car in prefix)),
                legacy.plan_step("Put", staging_line, inbound_nos, staging_positions),
                legacy.plan_step("Get", target_line, target_existing_nos),
                legacy.plan_step("Get", staging_line, inbound_nos),
                legacy.plan_step("Put", target_line, replay_nos, target_positions),
            ]
            if blocker_nos:
                steps.insert(2, legacy.plan_step("Put", source_line, blocker_nos, restore_positions))
            candidate = legacy.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                batch=[*prefix, *target_existing],
                steps=tuple(steps),
                reason=(
                    f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                    f"staging={staging_line};blockers={','.join(blocker_nos)};"
                    f"inbound={','.join(inbound_nos)};target_existing={','.join(target_existing_nos)}"
                ),
                candidate_kind="vnext_depot_repack_with_inbound_tail",
            )
            request = ResourceRequest(
                contract_id=contract.contract_id,
                family=contract.family,
                candidate_id=candidate.candidate_id,
                resources=(),
                source_line=source_line,
                target_line=target_line,
                move_nos=tuple(candidate.move_car_nos),
                intent=self.intent,
                same_plan_restore_nos=blocker_nos,
            )
            yield CandidateEnvelope(
                candidate=candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )
            return


class TailCloseoutEpisode(DirectMoveEpisode):
    intent = IntentKind.TAIL_CLOSEOUT
    template_name = "tail_closeout_direct_accessible_prefix"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.TAIL_CLOSEOUT


EPISODES: tuple[Episode, ...] = (
    DepotOutboundSessionEpisode(),
    RemoteSessionEpisode(),
    DirectMoveEpisode(),
    PrefixDigestEpisode(),
    DepotRepackWithInboundTailEpisode(),
    DepotMultiDropEpisode(),
    RemoteDepotEpisode(),
    TailCloseoutEpisode(),
)
