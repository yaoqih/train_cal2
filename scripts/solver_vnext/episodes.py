from __future__ import annotations

from typing import Any, Iterable

from . import physical
from . import release
from . import serial
from .access import build_prefix_access_lease_planlet
from .domain import CandidateEnvelope, ContractFamily, FlowContract, IntentKind, ResourceRequest
from .frontier import AccessFrontier
from .placement import planned_positions_for_batch
from .planlets import build_tail_digest_planlet
from .spotting import (
    build_spotting_same_line_repack_planlet,
    build_spotting_target_repack_planlet,
    spotting_nonforced_prefix_would_pollute,
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
        serial_gate_leases: dict[str, Any] | None = None,
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
            same_plan_source_return_nos=(),
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
    frontier = AccessFrontier()
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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        by_no = {physical.car_no(car): car for car in cars}
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        contract_nos = set(contract.subject_nos)
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            no = physical.car_no(car)
            if no not in contract_nos:
                break
            if no not in by_no:
                continue
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            if contract.family == ContractFamily.SPECIAL_REPAIR_PROCESS:
                break
        if not batch:
            access_envelope = self._prefix_access_lease_envelope(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                contract=contract,
                line_cars=line_cars,
                source_line=source_line,
                target_line=target_line,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            )
            if access_envelope:
                yield access_envelope
            return
        batch_nos = {physical.car_no(car) for car in batch}
        direct_generated = False
        spotting_prefix_pollution = spotting_nonforced_prefix_would_pollute(
            contract=contract,
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
        )
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
        if not spotting_prefix_pollution and len(positions) == len(batch) and self.frontier.direct_move_is_reachable(
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            planned_positions=positions,
        ):
            candidate = physical.build_direct_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=f"vnext:{self.template_name};contract={contract.contract_id};batch={len(batch)}",
                candidate_kind="target_move" if contract.family == ContractFamily.SPECIAL_REPAIR_PROCESS else "vnext_front_direct",
                planned_positions=positions,
            )
            if candidate:
                direct_generated = True
                yield self._envelope(candidate, contract)
        if (
            not direct_generated
            and physical.is_spotting_line(target_line)
            and any(physical.force_positions(car) for car in batch)
        ):
            spotting_repack = build_spotting_target_repack_planlet(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                source_batch=batch,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=(
                    f"vnext:spotting_target_repack;owner_contract={contract.contract_id};"
                    f"targets={','.join(physical.car_no(car) for car in batch)}"
                ),
                candidate_kind="vnext_spotting_target_repack",
                frontier=self.frontier,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            )
            if spotting_repack:
                request = ResourceRequest(
                    contract_id=contract.contract_id,
                    family=contract.family,
                    candidate_id=spotting_repack.candidate.candidate_id,
                    resources=(),
                    source_line=spotting_repack.candidate.source_line,
                    target_line=target_line,
                    move_nos=tuple(spotting_repack.candidate.move_car_nos),
                    intent=self.intent,
                    same_plan_source_return_nos=(),
                )
                yield CandidateEnvelope(
                    candidate=spotting_repack.candidate,
                    contract=contract,
                    intent=self.intent,
                    resource_request=request,
                    template_name="spotting_target_repack",
                )
        access_envelope = self._prefix_access_lease_envelope(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            contract=contract,
            line_cars=line_cars,
            source_line=source_line,
            target_line=target_line,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
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
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any] | None = None,
    ) -> CandidateEnvelope | None:
        contract_nos = set(contract.subject_nos)
        blocker_batch: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        saw_target = False
        for car in line_cars:
            no = physical.car_no(car)
            if not saw_target and no not in contract_nos:
                if physical.pull_equivalent([*blocker_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    return None
                blocker_batch.append(car)
                continue
            if no not in contract_nos:
                break
            saw_target = True
            if physical.pull_equivalent([*blocker_batch, *target_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            target_batch.append(car)
        if not blocker_batch or not target_batch:
            return None
        if spotting_nonforced_prefix_would_pollute(
            contract=contract,
            target_line=target_line,
            batch=target_batch,
            cars=cars,
            depot_assignment=depot_assignment,
        ):
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
                f"blockers={','.join(physical.car_no(car) for car in blocker_batch)};"
                f"targets={','.join(physical.car_no(car) for car in target_batch)}"
            ),
            candidate_kind=self.access_candidate_kind,
            frontier=self.frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
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
            same_plan_source_return_nos=plan.source_return_nos,
        )
        return CandidateEnvelope(
            candidate=plan.candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.access_template_name,
        )

    def _source_access_batch(
        self,
        line_cars: list[dict[str, Any]],
        contract: FlowContract,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        contract_nos = set(contract.subject_nos)
        blocker_batch: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        saw_target = False
        for car in line_cars:
            no = physical.car_no(car)
            if not saw_target and no not in contract_nos:
                if physical.pull_equivalent([*blocker_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    return [], []
                blocker_batch.append(car)
                continue
            if no not in contract_nos:
                break
            saw_target = True
            if physical.pull_equivalent([*blocker_batch, *target_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            target_batch.append(car)
        return blocker_batch, target_batch


class RemoteDepotEpisode(DirectMoveEpisode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "remote_depot_direct_accessible_prefix"
    access_template_name = "remote_depot_prefix_access_lease"
    access_candidate_kind = "vnext_remote_depot_prefix_access_lease"
    allowed_families = {ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT, ContractFamily.DEPOT_OUTBOUND}

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in self.allowed_families

    def _prefix_access_lease_envelope(self, **_: Any) -> CandidateEnvelope | None:
        return None


class RemoteSessionPrefixDigestEpisode(Episode):
    intent = IntentKind.REMOTE_SESSION
    template_name = "remote_session_prefix_batch_digest_restore"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)
        for source_line in contract.source_lines:
            candidate = self._build_source_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                subject_nos=subject_nos,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                contract=contract,
                loads=loads,
            )
            if candidate is None:
                continue
            yield candidate

    def _build_source_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        source_line: str,
        subject_nos: set[str],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        contract: FlowContract,
        loads: Any,
    ) -> CandidateEnvelope | None:
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        prefix: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        for car in line_cars:
            no = physical.car_no(car)
            target_line = self._target_for(car, cars, depot_assignment, loads)
            if target_batch and (no not in subject_nos or not self._target_allowed(source_line, target_line)):
                break
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            if no in subject_nos and self._target_allowed(source_line, target_line):
                prefix.append(car)
                target_batch.append(car)
                continue
            if target_batch:
                break
            prefix.append(car)
            blockers.append(car)
        if not blockers or len(target_batch) < 2:
            return None
        if prefix[-len(target_batch):] != target_batch:
            return None
        if any(car.get("IsWeigh") for car in prefix):
            return None

        blocker_nos = tuple(physical.car_no(car) for car in blockers)
        target_nos = tuple(physical.car_no(car) for car in target_batch)
        steps: list[Any] = [physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in prefix))]
        remaining = list(target_nos)
        no_to_car = {physical.car_no(car): car for car in target_batch}
        all_move_nos = {physical.car_no(car) for car in prefix}
        put_lines: list[str] = []
        while remaining:
            target_line = self._target_for(no_to_car[remaining[-1]], cars, depot_assignment, loads)
            start = len(remaining) - 1
            while start > 0 and self._target_for(no_to_car[remaining[start - 1]], cars, depot_assignment, loads) == target_line:
                start -= 1
            drop = tuple(remaining[start:])
            group = [no_to_car[no] for no in drop]
            positions = planned_positions_for_batch(
                batch=group,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=all_move_nos,
            )
            if len(positions) != len(group):
                return None
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            put_lines.append(target_line)
            del remaining[start:]

        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(blockers, start=1)
        }
        steps.append(physical.plan_step("Put", source_line, blocker_nos, restore_positions))
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            graph=graph,
            loco_location=loco_location,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases,
        ):
            return None
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=source_line,
            batch=prefix,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"source={source_line};blockers={','.join(blocker_nos)};"
                f"targets={','.join(target_nos)};put_lines={','.join(dict.fromkeys(put_lines))}"
            ),
            candidate_kind="vnext_remote_session_prefix_batch_digest",
        )
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line=source_line,
            target_line=source_line,
            move_nos=tuple(candidate.move_car_nos),
            intent=self.intent,
            same_plan_source_return_nos=blocker_nos,
        )
        return CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )

    def _target_for(
        self,
        car: dict[str, Any],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
    ) -> str:
        return physical.planned_target_for_car(car, cars, depot_assignment, loads)[0]

    def _target_allowed(self, source_line: str, target_line: str) -> bool:
        if not target_line or target_line == source_line:
            return False
        if target_line in physical.DEPOT_TARGET_LINES:
            return True
        return source_line in physical.REMOTE_INTERACTION_LINES and target_line not in physical.REMOTE_INTERACTION_LINES


class RemotePrefixMiddleDigestEpisode(Episode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "remote_prefix_middle_digest_restore"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {ContractFamily.REMOTE_SESSION, ContractFamily.REPAIR_INBOUND, ContractFamily.DEPOT_SLOT}

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        loads = physical.line_loads(cars)
        contract_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            return target_line

        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        prefix: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        for car in line_cars:
            no = physical.car_no(car)
            if target_batch and (no not in contract_nos or target_for(car) not in physical.DEPOT_TARGET_LINES):
                break
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                return
            car_target = target_for(car)
            if no in contract_nos and car_target in physical.DEPOT_TARGET_LINES:
                prefix.append(car)
                target_batch.append(car)
                continue
            if target_batch:
                break
            prefix.append(car)
            blockers.append(car)
        if not target_batch or prefix[-len(target_batch):] != target_batch:
            return
        if not blockers:
            return
        if any(car.get("IsWeigh") for car in prefix):
            return

        target_nos = tuple(physical.car_no(car) for car in target_batch)
        blocker_nos = tuple(physical.car_no(car) for car in blockers)
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(blockers, start=1)
        }
        steps = [physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in prefix))]
        remaining = [physical.car_no(car) for car in target_batch]
        no_to_car = {physical.car_no(car): car for car in target_batch}
        while remaining:
            target_line = target_for(no_to_car[remaining[-1]])
            start = len(remaining) - 1
            while start > 0 and target_for(no_to_car[remaining[start - 1]]) == target_line:
                start -= 1
            drop = tuple(remaining[start:])
            group = [no_to_car[no] for no in drop]
            positions = planned_positions_for_batch(
                batch=group,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=set(physical.car_no(car) for car in prefix),
            )
            if len(positions) != len(group):
                return
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        steps.append(physical.plan_step("Put", source_line, blocker_nos, restore_positions))
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            graph=graph,
            loco_location=loco_location,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=source_line,
            batch=prefix,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"blockers={','.join(blocker_nos)};targets={','.join(target_nos)}"
            ),
            candidate_kind="vnext_remote_prefix_middle_digest_restore",
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
            same_plan_source_return_nos=blocker_nos,
        )
        yield CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )


class RemoteSessionEpisode(Episode):
    intent = IntentKind.REMOTE_SESSION
    template_name = "remote_session_directional_digest"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            return target_line

        def line_is_remote(line: str) -> bool:
            return line in physical.REMOTE_INTERACTION_LINES

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
                line_cars = physical.line_cars_in_access_order(
                    cars=cars,
                    line=source_line,
                    graph=graph,
                    loco_location=loco_location,
                )
                batch: list[dict[str, Any]] = []
                for car in line_cars:
                    no = physical.car_no(car)
                    target_line = target_for(car)
                    if no not in subject_nos or not target_matches(mode, target_line):
                        break
                    if physical.pull_equivalent([*carry, *batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                        break
                    batch.append(car)
                    if len(batch) >= 6:
                        break
                if batch:
                    selected_sources.append((source_line, batch))
                    carry.extend(batch)
                if len(selected_sources) >= 4 or physical.pull_equivalent(carry) >= physical.PULL_LIMIT_EQUIVALENT:
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
                target_order = sorted(target_groups, key=lambda line: (line not in physical.DEPOT_LINES, line not in physical.DEPOT_TARGET_LINES, line))
            else:
                target_order = sorted(target_groups, key=lambda line: (line not in physical.DEPOT_LINES, line))

            steps = [
                physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch))
                for source_line, batch in selected_sources
            ]
            planned_positions: dict[str, int] = {}
            no_to_car = {physical.car_no(car): car for car in carry}
            no_to_target = {
                physical.car_no(car): target_line
                for target_line, group in target_groups.items()
                for car in group
            }
            remaining = [physical.car_no(car) for car in carry if physical.car_no(car) in no_to_target]
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
                probe = physical.build_direct_candidate(
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
                steps.append(physical.plan_step("Put", target_line, tuple(drop), positions))
                del remaining[start:]
            if remaining:
                continue

            plan_steps = tuple(steps)
            if not self.frontier.plan_steps_are_reachable(
                steps=plan_steps,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            ):
                continue
            candidate = physical.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=steps[0].line,
                target_line=steps[-1].line,
                batch=carry,
                steps=plan_steps,
                reason=(
                    f"vnext:{self.template_name};mode={mode};"
                    f"sources={len(selected_sources)};targets={len(target_groups)};batch={len(carry)}"
                ),
                candidate_kind="vnext_remote_session_digest",
            )
            yield self._envelope(candidate, contract)


class DepotOutboundSessionEpisode(Episode):
    intent = IntentKind.CUN4_OUTBOUND_HOLD
    template_name = "depot_outbound_session"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            return target_line

        target_line = self._primary_outbound_target(cars, subject_nos, target_for)
        if not target_line:
            return
        carry: list[dict[str, Any]] = []
        steps = []
        for source_line in self.source_order:
            if source_line not in contract.source_lines:
                continue
            line_cars = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            batch: list[dict[str, Any]] = []
            for car in line_cars:
                no = physical.car_no(car)
                if no not in subject_nos or target_for(car) != target_line:
                    break
                if car.get("IsWeigh"):
                    break
                if physical.pull_equivalent([*carry, *batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                batch.append(car)
            if batch:
                carry.extend(batch)
                steps.append(physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch)))
        if len(carry) < 3 or len(steps) < 2:
            return

        batch_nos = {physical.car_no(car) for car in carry}
        positions = planned_positions_for_batch(
            batch=carry,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
        if len(positions) != len(carry):
            return
        probe = physical.build_direct_candidate(
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
        steps.append(physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in carry), positions))
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=target_line,
            batch=carry,
            steps=plan_steps,
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
            if physical.car_no(car) not in subject_nos or car["Line"] not in self.source_order:
                continue
            target_line = target_for(car)
            if not target_line or target_line in physical.REMOTE_INTERACTION_LINES:
                continue
            counts[target_line] = counts.get(target_line, 0) + 1
        if not counts:
            return ""
        return min(counts, key=lambda line: (line != "存4线", -counts[line], line))


class Cun4ReleaseGroupAssemblyEpisode(Episode):
    intent = IntentKind.CUN4_RELEASE_GROUP
    template_name = "cun4_release_group_assembly"
    frontier = AccessFrontier()

    source_lines = (
        "预修线",
        "调梁棚",
        "抛丸线",
        "油漆线",
        "洗罐站",
        "洗罐线北",
        "存2线",
        "存3线",
        "存5线北",
        "存5线南",
        "存1线",
    )

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        current_group = release.cun4_release_group(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
        )
        if not release.standard_cun4_chain_applicable(cars, depot_assignment):
            return
        if current_group.count >= release.STANDARD_RELEASE_TARGET_COUNT:
            return
        source_line = contract.source_lines[0]
        if source_line not in self.source_lines:
            return
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        contract_nos = set(contract.subject_nos)
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in contract_nos or target_line not in physical.DEPOT_TARGET_LINES:
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            if len(batch) >= 10:
                break
        if len(batch) < 2:
            return
        moving_nos = {physical.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line="存4线",
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(batch):
            return
        candidate = physical.build_direct_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line="存4线",
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"existing_release_group={current_group.count};batch={len(batch)}"
            ),
            candidate_kind="vnext_cun4_release_group_assembly",
            planned_positions=positions,
        )
        if candidate is None:
            return
        if not self.frontier.direct_move_is_reachable(
            source_line=source_line,
            target_line="存4线",
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            planned_positions=positions,
        ):
            return
        yield self._envelope(candidate, contract)


class Cun4ReleaseAcceptEpisode(Episode):
    intent = IntentKind.CUN4_RELEASE_ACCEPT
    template_name = "cun4_release_accept_digest"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        port_state = release.cun4_port_state(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
        )
        if not port_state.release_ready:
            return
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
        group_nos = set(port_state.release_nos)
        contract_nos = set(contract.subject_nos)
        if not group_nos & contract_nos:
            return
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        target_by_no: dict[str, str] = {}
        for car in line_cars:
            no = physical.car_no(car)
            if no not in group_nos and not batch:
                if no in port_state.prefix_hold_nos:
                    batch.append(car)
                    target_by_no[no] = "存4线"
                    continue
                return
            if no not in group_nos:
                if no in port_state.prefix_hold_nos:
                    batch.append(car)
                    target_by_no[no] = "存4线"
                    continue
                break
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line not in physical.DEPOT_TARGET_LINES:
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            target_by_no[no] = target_line
        if not any(physical.car_no(car) in group_nos for car in batch):
            return
        steps = [physical.plan_step("Get", "存4线", tuple(physical.car_no(car) for car in batch))]
        remaining = [physical.car_no(car) for car in batch]
        planned_positions: dict[str, int] = {}
        no_to_car = {physical.car_no(car): car for car in batch}
        while remaining:
            target_line = target_by_no.get(remaining[-1], "")
            if target_line == "存4线":
                drop = tuple(no for no in remaining if target_by_no.get(no) == "存4线")
                if tuple(remaining[: len(drop)]) != drop or any(target_by_no.get(no) != "存4线" for no in drop):
                    return
                restore_positions = {
                    no: int(no_to_car[no].get("Position") or index)
                    for index, no in enumerate(drop, start=1)
                }
                steps.append(physical.plan_step("Put", "存4线", drop, restore_positions))
                del remaining[: len(drop)]
                continue
            if target_line not in physical.DEPOT_TARGET_LINES:
                return
            start = len(remaining) - 1
            while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                start -= 1
            drop = tuple(remaining[start:])
            group_batch = [no_to_car[no] for no in drop]
            positions = planned_positions_for_batch(
                batch=group_batch,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=set(remaining),
            )
            if len(positions) != len(group_batch):
                return
            planned_positions.update(positions)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line="存4线",
            target_line=steps[-1].line,
            batch=batch,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"release_group={port_state.release_count};batch={len(batch)}"
            ),
            candidate_kind="vnext_cun4_release_accept_digest",
        )
        yield self._envelope(candidate, contract)


class DepotInboundGatherSessionEpisode(Episode):
    intent = IntentKind.REMOTE_SESSION
    template_name = "depot_inbound_prefix_multidrop_session"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)

        def target_for(car: dict[str, Any]) -> str:
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            return target_line

        for source_line in contract.source_lines:
            if source_line in physical.REMOTE_INTERACTION_LINES:
                continue
            line_cars = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            carry: list[dict[str, Any]] = []
            target_by_no: dict[str, str] = {}
            for car in line_cars:
                no = physical.car_no(car)
                target_line = target_for(car)
                if no not in subject_nos or target_line not in physical.DEPOT_TARGET_LINES:
                    break
                if car.get("IsWeigh"):
                    break
                if physical.pull_equivalent([*carry, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                carry.append(car)
                target_by_no[no] = target_line
                if len(carry) >= 12:
                    break
            if len(carry) < 4 or len(set(target_by_no.values())) < 2:
                continue
            steps = [physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in carry))]
            remaining = [physical.car_no(car) for car in carry]
            moving_nos = {physical.car_no(car) for car in carry}
            planned_positions: dict[str, int] = {}
            no_to_car = {physical.car_no(car): car for car in carry}
            while remaining:
                target_line = target_by_no.get(remaining[-1], "")
                if target_line not in physical.DEPOT_TARGET_LINES:
                    break
                start = len(remaining) - 1
                while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                    start -= 1
                drop = tuple(remaining[start:])
                group = [no_to_car[no] for no in drop]
                positions = planned_positions_for_batch(
                    batch=group,
                    target_line=target_line,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    batch_nos=moving_nos,
                )
                if len(positions) != len(group):
                    break
                planned_positions.update(positions)
                steps.append(physical.plan_step("Put", target_line, drop, positions))
                del remaining[start:]
            if remaining:
                continue
            plan_steps = tuple(steps)
            if not self.frontier.plan_steps_are_reachable(
                steps=plan_steps,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            ):
                continue
            candidate = physical.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=steps[0].line,
                target_line=target_line,
                batch=carry,
                steps=plan_steps,
                reason=(
                    f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                    f"source={source_line};targets={','.join(sorted(set(target_by_no.values())))};"
                    f"batch={len(carry)}"
                ),
                candidate_kind="vnext_depot_inbound_gather_session",
            )
            yield self._envelope(candidate, contract)


class DepotMultiDropEpisode(Episode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "depot_multi_drop_accessible_prefix"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not line_cars:
            return
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        target_groups: dict[str, list[dict[str, Any]]] = {}
        for car in line_cars:
            target_line, _position, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
            if target_line not in physical.DEPOT_TARGET_LINES:
                if batch:
                    break
                continue
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            target_groups.setdefault(target_line, []).append(car)
            if len(target_groups) >= 2 and len(batch) >= 2:
                # Enough structure to justify a multi-drop candidate.
                pass
        if len(batch) < 2 or len(target_groups) < 2:
            return
        steps = [physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch))]
        planned_positions: dict[str, int] = {}
        remaining = list(physical.car_no(car) for car in batch)
        while remaining:
            tail_no = remaining[-1]
            tail_car = next(car for car in batch if physical.car_no(car) == tail_no)
            target_line, _position, _reason = physical.planned_target_for_car(tail_car, cars, depot_assignment, loads)
            if target_line not in target_groups:
                return
            drop: list[str] = []
            for no in reversed(remaining):
                car = next(item for item in batch if physical.car_no(item) == no)
                car_target, _pos, _reason = physical.planned_target_for_car(car, cars, depot_assignment, loads)
                if car_target != target_line:
                    break
                drop.append(no)
            drop = list(reversed(drop))
            group = [car for car in batch if physical.car_no(car) in set(drop)]
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
            group_candidate = physical.build_direct_candidate(
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
            steps.append(physical.plan_step("Put", target_line, group_nos, positions))
            remaining = remaining[: -len(drop)]
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=steps[-1].line,
            batch=batch,
            steps=plan_steps,
            reason=f"vnext:{self.template_name};source={source_line};targets={','.join(sorted(target_groups))};batch={len(batch)}",
            candidate_kind="vnext_depot_multi_drop",
        )
        yield self._envelope(candidate, contract)


class DepotSlotFillEpisode(Episode):
    intent = IntentKind.DEPOT_SLOT_SWAP
    template_name = "depot_locked_tail_slot_fill"
    frontier = AccessFrontier()

    staging_lines = (
        "存2线",
        "存1线",
        "存3线",
        "存5线北",
        "存5线南",
        "预修线",
        "调梁棚",
    )

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        by_no = {physical.car_no(car): car for car in cars}
        contract_nos = set(contract.subject_nos)
        for target_line in sorted(physical.DEPOT_LINES):
            required_nos = self._required_front_slot_nos(
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
            )
            required_nos = self._source_access_ordered_required_nos(
                required_nos=required_nos,
                cars=cars,
                graph=graph,
                loco_location=loco_location,
            )
            if not required_nos or not (set(required_nos) & contract_nos):
                continue
            if any(no not in by_no or by_no[no]["Line"] == target_line for no in required_nos):
                continue
            candidate = self._build_fill_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                target_line=target_line,
                required_nos=required_nos,
                by_no=by_no,
                owner_contract=contract,
            )
            if candidate is None:
                continue
            if not self.frontier.plan_steps_are_reachable(
                steps=physical.candidate_plan_steps(candidate),
                cars=cars,
                graph=graph,
                loco_location=loco_location,
                depot_assignment=depot_assignment,
                serial_gate_leases=serial_gate_leases or {},
            ):
                continue
            request = ResourceRequest(
                contract_id=contract.contract_id,
                family=contract.family,
                candidate_id=candidate.candidate_id,
                resources=(),
                source_line=candidate.source_line,
                target_line=target_line,
                move_nos=tuple(candidate.move_car_nos),
                intent=self.intent,
                same_plan_source_return_nos=(),
            )
            yield CandidateEnvelope(
                candidate=candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )
            return

    def _required_front_slot_nos(
        self,
        *,
        target_line: str,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, ...]:
        locked_positions = [
            int(car.get("Position") or 0)
            for car in cars
            if car["Line"] == target_line and physical.is_locked_depot_stayer(car, depot_assignment)
        ]
        if not locked_positions:
            return ()
        first_locked = min(locked_positions)
        if first_locked <= 1:
            return ()
        prefix_occupants = [
            car
            for car in cars
            if car["Line"] == target_line and int(car.get("Position") or 0) < first_locked
        ]
        if prefix_occupants:
            return ()
        slot_items = [
            (int(slot.position), no)
            for no, slot in depot_assignment.slots.items()
            if slot.line == target_line and int(slot.position) < first_locked
        ]
        if len(slot_items) != first_locked - 1:
            return ()
        return tuple(no for _position, no in sorted(slot_items))

    def _source_access_ordered_required_nos(
        self,
        *,
        required_nos: tuple[str, ...],
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
    ) -> tuple[str, ...]:
        if len(required_nos) < 2:
            return required_nos
        by_no = {physical.car_no(car): car for car in cars}
        result = list(required_nos)
        source_lines = sorted({by_no[no]["Line"] for no in required_nos if no in by_no})
        for source_line in source_lines:
            indexes = [index for index, no in enumerate(result) if by_no.get(no, {}).get("Line") == source_line]
            if len(indexes) < 2:
                continue
            access_order = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            rank = {physical.car_no(car): index for index, car in enumerate(access_order)}
            ordered = sorted((result[index] for index in indexes), key=lambda no: rank.get(no, 10_000))
            for index, no in zip(indexes, ordered):
                result[index] = no
        return tuple(result)

    def _build_fill_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        target_line: str,
        required_nos: tuple[str, ...],
        by_no: dict[str, dict[str, Any]],
        owner_contract: FlowContract,
    ) -> Any | None:
        steps: list[Any] = []
        all_move_nos: set[str] = set()
        staged: list[tuple[str, str, tuple[str, ...], dict[str, int]]] = []
        grouped = physical.cars_by_line(cars)

        for no in required_nos:
            car = by_no[no]
            source_line = car["Line"]
            access_order = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            access_nos = [physical.car_no(item) for item in access_order]
            if no not in access_nos:
                return None
            blockers = [by_no[item] for item in access_nos[: access_nos.index(no)] if item not in all_move_nos]
            if blockers:
                if any(physical.car_no(item) in required_nos for item in blockers):
                    return None
                if any(item.get("IsWeigh") or physical.is_locked_depot_stayer(item, depot_assignment) for item in blockers):
                    return None
                if physical.pull_equivalent(blockers) > physical.PULL_LIMIT_EQUIVALENT:
                    return None
                staging_line = self._choose_staging_line(
                    blockers=blockers,
                    source_line=source_line,
                    target_line=target_line,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    moving_nos=all_move_nos | {physical.car_no(item) for item in blockers},
                    grouped=grouped,
                )
                if not staging_line:
                    return None
                blocker_nos = tuple(physical.car_no(item) for item in blockers)
                staging_positions = planned_positions_for_batch(
                    batch=blockers,
                    target_line=staging_line,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    batch_nos=set(blocker_nos),
                )
                if len(staging_positions) != len(blockers):
                    return None
                steps.append(physical.plan_step("Get", source_line, blocker_nos))
                steps.append(physical.plan_step("Put", staging_line, blocker_nos, staging_positions))
                restore_positions = {
                    physical.car_no(item): int(item.get("Position") or index)
                    for index, item in enumerate(blockers, start=1)
                }
                staged.append((staging_line, source_line, blocker_nos, restore_positions))
                all_move_nos.update(blocker_nos)
            steps.append(physical.plan_step("Get", source_line, (no,)))
            all_move_nos.add(no)

        if physical.pull_equivalent([by_no[no] for no in required_nos]) > physical.PULL_LIMIT_EQUIVALENT:
            return None
        if len(staged) > 1 or len(steps) > 4:
            return None
        target_positions = {no: position for position, no in enumerate(required_nos, start=1)}
        if len(target_positions) != len(required_nos):
            return None
        steps.append(physical.plan_step("Put", target_line, required_nos, target_positions))

        for staging_line, source_line, blocker_nos, restore_positions in reversed(staged):
            steps.append(physical.plan_step("Get", staging_line, blocker_nos))
            steps.append(physical.plan_step("Put", source_line, blocker_nos, restore_positions))

        batch = [by_no[no] for no in tuple(dict.fromkeys((*all_move_nos, *required_nos))) if no in by_no]
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=target_line,
            batch=batch,
            steps=tuple(steps),
            reason=(
                f"vnext:{self.template_name};owner_contract={owner_contract.contract_id};"
                f"target_line={target_line};required={','.join(required_nos)};"
                f"staged_groups={len(staged)}"
            ),
            candidate_kind="vnext_depot_locked_tail_slot_fill",
        )

    def _choose_staging_line(
        self,
        *,
        blockers: list[dict[str, Any]],
        source_line: str,
        target_line: str,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        moving_nos: set[str],
        grouped: dict[str, list[dict[str, Any]]],
    ) -> str:
        for staging_line in self.staging_lines:
            if staging_line in {source_line, target_line}:
                continue
            if staging_line in physical.RUNNING_LINES or staging_line in physical.DEPOT_TARGET_LINES:
                continue
            positions = planned_positions_for_batch(
                batch=blockers,
                target_line=staging_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=moving_nos,
            )
            if len(positions) != len(blockers):
                continue
            if not physical.candidate_positions_available(staging_line, positions, cars, moving_nos, grouped):
                continue
            if not physical.line_has_length_capacity(staging_line, cars, blockers, moving_nos, grouped=grouped):
                continue
            return staging_line
        return ""


class DepotSlotSwapEpisode(Episode):
    intent = IntentKind.DEPOT_SLOT_SWAP
    template_name = "depot_slot_swap"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        loads = physical.line_loads(cars)

        def planned(car: dict[str, Any]) -> tuple[str, int | None, str]:
            return physical.planned_target_for_car(car, cars, depot_assignment, loads)

        def satisfied(car: dict[str, Any]) -> bool:
            return physical.car_is_satisfied(car, depot_assignment, cars)

        contract_nos = set(contract.subject_nos)
        grouped = physical.cars_by_line(cars)
        length_load_lookup = {
            line: float(length)
            for line, length in physical.line_length_loads(cars).items()
        }
        for candidate in self._build_swap_candidates(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            planned=planned,
            satisfied=satisfied,
            length_load_lookup=length_load_lookup,
            grouped=grouped,
        ):
            candidate = self._lifo_safe_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                candidate=candidate,
            )
            if not (set(candidate.move_car_nos) & contract_nos):
                continue
            if not self.frontier.plan_steps_are_reachable(
                steps=physical.candidate_plan_steps(candidate),
                cars=cars,
                graph=graph,
                loco_location=loco_location,
                depot_assignment=depot_assignment,
                serial_gate_leases=serial_gate_leases or {},
            ):
                continue
            request = ResourceRequest(
                contract_id=contract.contract_id,
                family=contract.family,
                candidate_id=candidate.candidate_id,
                resources=(),
                source_line=candidate.source_line,
                target_line=candidate.target_line,
                move_nos=tuple(candidate.move_car_nos),
                intent=self.intent,
                same_plan_source_return_nos=(),
            )
            yield CandidateEnvelope(
                candidate=candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )

    def _build_swap_candidates(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        planned: Any,
        satisfied: Any,
        length_load_lookup: dict[str, float],
        grouped: dict[str, list[dict[str, Any]]],
    ) -> list[Any]:
        candidates: list[Any] = []
        for source_line, line_cars in sorted(grouped.items()):
            source_remote = source_line in physical.REMOTE_INTERACTION_LINES
            first = next((car for car in line_cars if not satisfied(car)), None)
            if first is None:
                continue
            target_line, _position, _reason = planned(first)
            if target_line not in physical.DEPOT_LINES or source_line == target_line:
                continue
            inbound_batch = self._first_front_batch_to_target(
                line_cars=line_cars,
                target_line=target_line,
                planned=planned,
                satisfied=satisfied,
                max_remaining_pull=physical.PULL_LIMIT_EQUIVALENT,
            )
            if not inbound_batch:
                continue
            inbound_nos = {physical.car_no(car) for car in inbound_batch}
            inbound_positions = planned_positions_for_batch(
                batch=inbound_batch,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=inbound_nos,
            )
            if len(inbound_positions) != len(inbound_batch):
                continue
            blockers: list[dict[str, Any]] = []
            for car in physical.target_position_occupants(cars, target_line, set(inbound_positions.values()), inbound_nos):
                blocker_target, _blocker_position, _blocker_reason = planned(car)
                if (
                    physical.is_locked_depot_stayer(car, depot_assignment)
                    or satisfied(car)
                    or not blocker_target
                    or blocker_target == target_line
                ):
                    continue
                if source_remote and blocker_target not in physical.REMOTE_INTERACTION_LINES:
                    continue
                blockers.append(car)
            if not blockers or physical.pull_equivalent(blockers) > physical.PULL_LIMIT_EQUIVALENT:
                continue
            blocker_targets = self._target_groups_for_carry(blockers, planned)
            all_nos = inbound_nos | {physical.car_no(car) for car in blockers}
            blocker_order = sorted(blocker_targets, key=lambda line: (line in physical.DEPOT_TARGET_LINES, line))
            accepted_blocker_lines = self._accepted_put_lines(
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
                physical.car_no(car)
                for line in accepted_blocker_lines
                for car in blocker_targets.get(line, [])
            }
            if not blocker_nos:
                continue
            blockers = [car for car in blockers if physical.car_no(car) in blocker_nos]
            if not physical.candidate_positions_available(target_line, inbound_positions, cars, all_nos, grouped):
                continue
            if not physical.line_has_length_capacity(target_line, cars, inbound_batch, all_nos, length_load_lookup, grouped):
                continue
            if source_line in physical.REMOTE_INTERACTION_LINES:
                steps = [
                    physical.plan_step("Get", target_line, tuple(physical.car_no(car) for car in blockers)),
                    physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in inbound_batch)),
                    physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in inbound_batch), inbound_positions),
                ]
            else:
                steps = [
                    physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in inbound_batch)),
                    physical.plan_step("Get", target_line, tuple(physical.car_no(car) for car in blockers)),
                    physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in inbound_batch), inbound_positions),
                ]
            for line in accepted_blocker_lines:
                batch = [car for car in blocker_targets[line] if physical.car_no(car) in blocker_nos]
                if not self._append_put_step_if_valid(
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
                candidate = physical.build_planlet_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=target_line,
                    target_line=steps[-1].line,
                    batch=[*blockers, *inbound_batch],
                    steps=tuple(steps),
                    reason=(
                        f"vnext:{self.template_name};target_line={target_line};"
                        f"source_line={source_line};blocker_count={len(blockers)};"
                        f"inbound_count={len(inbound_batch)}"
                    ),
                    candidate_kind="vnext_depot_slot_swap",
                )
                if source_remote and physical.candidate_remote_profile(candidate) != physical.REMOTE_PROFILE_REMOTE_ONLY:
                    continue
                candidates.append(candidate)
                if len(candidates) >= 2:
                    break
        return candidates

    def _first_front_batch_to_target(
        self,
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
                if batch:
                    break
                continue
            planned_line, _position, _reason = planned(car)
            if planned_line != target_line or car.get("IsWeigh"):
                break
            if physical.pull_equivalent(batch) + physical.pull_equivalent([car]) > max_remaining_pull:
                break
            batch.append(car)
        return batch

    def _target_groups_for_carry(self, carry: list[dict[str, Any]], planned: Any) -> dict[str, list[dict[str, Any]]]:
        target_groups: dict[str, list[dict[str, Any]]] = {}
        for car in carry:
            target_line, _position, _reason = planned(car)
            if target_line:
                target_groups.setdefault(target_line, []).append(car)
        return target_groups

    def _append_put_step_if_valid(
        self,
        *,
        steps: list[Any],
        target_line: str,
        batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
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
        )
        if len(positions) != len(batch):
            return False
        if not physical.candidate_positions_available(target_line, positions, cars, all_nos, grouped):
            return False
        if not physical.line_has_length_capacity(target_line, cars, batch, all_nos, length_load_lookup, grouped):
            return False
        steps.append(physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in batch), positions))
        return True

    def _accepted_put_lines(
        self,
        *,
        target_groups: dict[str, list[dict[str, Any]]],
        target_order: list[str],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        all_nos: set[str],
        grouped: dict[str, list[dict[str, Any]]],
        length_load_lookup: dict[str, float],
    ) -> list[str]:
        accepted: list[str] = []
        for target_line in target_order:
            probe_steps: list[Any] = []
            if self._append_put_step_if_valid(
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

    def _lifo_safe_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        candidate: Any,
    ) -> Any:
        steps = list(physical.candidate_plan_steps(candidate))
        if (
            len(steps) >= 4
            and steps[0].action == "Get"
            and steps[1].action == "Get"
            and steps[2].action == "Put"
            and tuple(steps[2].move_car_nos) == tuple(steps[0].move_car_nos)
        ):
            steps = [steps[1], steps[0], *steps[2:]]
            by_no = {physical.car_no(car): car for car in cars}
            batch = [by_no[no] for no in candidate.move_car_nos if no in by_no]
            return physical.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=steps[0].line,
                target_line=steps[-1].line,
                batch=batch,
                steps=tuple(steps),
                reason=(
                    f"vnext:{self.template_name};lifo_safe_reorder;"
                    f"original={candidate.candidate_id}"
                ),
                candidate_kind="vnext_depot_slot_swap",
            )
        return candidate


class PrefixDigestEpisode(Episode):
    intent = IntentKind.PREFIX_DIGEST
    template_name = "owned_prefix_tail_digest_restore"
    frontier = AccessFrontier()
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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        if source_line == "存4线":
            return
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not line_cars:
            return
        contract_nos = set(contract.subject_nos)
        loads = physical.line_loads(cars)
        prefix: list[dict[str, Any]] = []
        seen_contract_car = False
        for car in line_cars:
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
            if physical.car_no(car) in contract_nos:
                seen_contract_car = True
        if not seen_contract_car or len(prefix) < 2:
            return
        while prefix:
            tail_target, _position, _reason = physical.planned_target_for_car(
                prefix[-1],
                cars,
                depot_assignment,
                loads,
            )
            if tail_target and tail_target != source_line:
                break
            prefix.pop()
        if len(prefix) < 2 or not any(physical.car_no(car) in contract_nos for car in prefix):
            return
        plan = build_tail_digest_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            prefix=prefix,
            cars=cars,
            depot_assignment=depot_assignment,
            target_line_for_car=lambda car: physical.planned_target_for_car(car, cars, depot_assignment, loads)[0],
            restore_remaining_to_source=True,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"prefix={','.join(physical.car_no(car) for car in prefix)}"
            ),
            candidate_kind="vnext_owned_prefix_digest_restore",
            frontier=self.frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        )
        if plan is None:
            return
        actual_put_lines = tuple(line for line in plan.put_lines if line != source_line)
        if any(line in physical.REMOTE_INTERACTION_LINES for line in actual_put_lines) and any(
            line not in physical.REMOTE_INTERACTION_LINES for line in actual_put_lines
        ):
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
            same_plan_source_return_nos=plan.source_return_nos,
        )
        yield CandidateEnvelope(
            candidate=plan.candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )


class TailBlockerPeelDigestEpisode(Episode):
    intent = IntentKind.BLOCKER_STAGING
    template_name = "tail_blocker_peel_digest"
    frontier = AccessFrontier()
    staging_lines = (
        "存2线",
        "存1线",
        "存3线",
        "存5线北",
        "存5线南",
        "预修线",
        "调梁棚",
    )
    protected_serial_storage_lines = {
        "存4线",
        "存3线",
        "修1库外",
        "修2库外",
        "修3库外",
        "修4库外",
    }
    allowed_families = PrefixDigestEpisode.allowed_families

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not line_cars:
            return
        loads = physical.line_loads(cars)
        contract_nos = set(contract.subject_nos)
        prefix: list[dict[str, Any]] = []
        seen_contract_car = False
        for car in line_cars:
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            prefix.append(car)
            if physical.car_no(car) in contract_nos:
                seen_contract_car = True
        if not seen_contract_car or len(prefix) < 3:
            return

        tail_target = self._target_for(prefix[-1], cars, depot_assignment, loads)
        if not tail_target or tail_target == source_line:
            return

        tail_group = self._tail_same_target_group(prefix, tail_target, cars, depot_assignment, loads)
        if not tail_group or len(tail_group) == len(prefix):
            return
        if any(car.get("IsWeigh") for car in tail_group):
            return

        all_move_nos = {physical.car_no(car) for car in prefix}
        tail_nos = tuple(physical.car_no(car) for car in tail_group)
        remaining = [physical.car_no(car) for car in prefix if physical.car_no(car) not in set(tail_nos)]
        if not self._tail_group_requires_peel(
            source_line=source_line,
            tail_target=tail_target,
            tail_group=tail_group,
            all_move_nos=all_move_nos,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        if not any(
            self._target_for(car, cars, depot_assignment, loads)
            and self._target_for(car, cars, depot_assignment, loads) != source_line
            for car in prefix[: len(prefix) - len(tail_group)]
        ):
            return
        excluded = {source_line, tail_target}
        if tail_target in serial.serial_blocker_lines():
            excluded.update(serial.downstream_lines(tail_target))
        staging_candidates = self.frontier.reachable_staging_lines(
            source_line=source_line,
            batch=tail_group,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            candidate_lines=self.staging_lines,
            excluded_lines=excluded,
        )
        for staging_line in staging_candidates:
            plan = self._build_plan(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                staging_line=staging_line,
                tail_target=tail_target,
                tail_group=tail_group,
                tail_nos=tail_nos,
                remaining=remaining,
                prefix=prefix,
                all_move_nos=all_move_nos,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                loads=loads,
            )
            if plan is None:
                continue
            candidate, progressed_nos, source_return_nos = plan
            if not set(progressed_nos) & contract_nos:
                continue
            request = ResourceRequest(
                contract_id=contract.contract_id,
                family=contract.family,
                candidate_id=candidate.candidate_id,
                resources=(),
                source_line=source_line,
                target_line=candidate.target_line,
                move_nos=tuple(candidate.move_car_nos),
                intent=self.intent,
                same_plan_source_return_nos=source_return_nos,
            )
            yield CandidateEnvelope(
                candidate=candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )
            return

    def _build_plan(
        self,
        *,
        case_id: str,
        hook_index: int,
        source_line: str,
        staging_line: str,
        tail_target: str,
        tail_group: list[dict[str, Any]],
        tail_nos: tuple[str, ...],
        remaining: list[str],
        prefix: list[dict[str, Any]],
        all_move_nos: set[str],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        loads: Any,
    ) -> tuple[Any, tuple[str, ...], tuple[str, ...]] | None:
        working_cars = [dict(car) for car in cars]
        steps = [
            physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in prefix)),
        ]
        progressed: list[str] = []
        by_no = {physical.car_no(car): car for car in prefix}
        first_staging_line = staging_line
        while remaining:
            tail_no = remaining[-1]
            tail_car = by_no[tail_no]
            target_line = self._target_for(tail_car, cars, depot_assignment, loads)
            if not target_line or target_line == source_line:
                break
            drop: list[str] = []
            for no in reversed(remaining):
                car = by_no[no]
                if self._target_for(car, cars, depot_assignment, loads) != target_line:
                    break
                drop.append(no)
            drop = list(reversed(drop))
            group = [by_no[no] for no in drop]
            moving_now = set(remaining)
            final_put = self._project_put(
                target_line=target_line,
                group=group,
                moving_nos=moving_now,
                working_cars=working_cars,
                depot_assignment=depot_assignment,
            )
            if final_put is not None and not self._serial_storage_is_unsafe(
                target_line=target_line,
                working_cars=working_cars,
                depot_assignment=depot_assignment,
                moving_nos=set(drop),
            ):
                positions, projected = final_put
                probe = physical.build_direct_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=source_line,
                    target_line=target_line,
                    batch=group,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    reason="vnext:tail_blocker_peel_position_probe",
                    candidate_kind="vnext_position_probe",
                    planned_positions=positions,
                )
                if probe is None:
                    break
                steps.append(physical.plan_step("Put", target_line, tuple(drop), positions))
                working_cars = projected
                progressed.extend(drop)
                remaining = remaining[: -len(drop)]
                continue
            if progressed:
                break
            staged = self._stage_tail_group(
                preferred_staging_line=first_staging_line,
                source_line=source_line,
                blocked_target_line=target_line,
                group=group,
                group_nos=tuple(drop),
                moving_nos=moving_now,
                working_cars=working_cars,
                depot_assignment=depot_assignment,
            )
            if staged is None:
                return None
            stage_line, stage_positions, projected = staged
            steps.append(physical.plan_step("Put", stage_line, tuple(drop), stage_positions))
            working_cars = projected
            remaining = remaining[: -len(drop)]

        if not progressed:
            return None
        source_return_nos: tuple[str, ...] = ()
        if remaining:
            source_return_nos = tuple(remaining)
            restore_positions = {
                no: int(by_no[no].get("Position") or index)
                for index, no in enumerate(source_return_nos, start=1)
            }
            steps.append(physical.plan_step("Put", source_line, source_return_nos, restore_positions))

        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        ):
            return None
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=steps[-1].line,
            batch=prefix,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};staging={staging_line};"
                f"peeled={','.join(tail_nos)};tail_target={tail_target};"
                f"progressed={','.join(progressed)}"
            ),
            candidate_kind="vnext_tail_blocker_peel_digest",
        )
        return candidate, tuple(progressed), source_return_nos

    def _project_put(
        self,
        *,
        target_line: str,
        group: list[dict[str, Any]],
        moving_nos: set[str],
        working_cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[dict[str, int], list[dict[str, Any]]] | None:
        positions = planned_positions_for_batch(
            batch=group,
            target_line=target_line,
            cars=working_cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(group):
            return None
        projected = [dict(car) for car in working_cars]
        group_nos = {physical.car_no(car) for car in group}
        for car in projected:
            no = physical.car_no(car)
            if no in group_nos:
                car["Line"] = target_line
                car["Position"] = positions[no]
        active_assignment = physical.current_depot_assignment(depot_assignment, working_cars)
        if self.frontier.target_put_violation_reasons(
            target_line=target_line,
            batch=group,
            projected_cars=projected,
            depot_assignment=active_assignment,
        ):
            return None
        return positions, projected

    def _stage_tail_group(
        self,
        *,
        preferred_staging_line: str,
        source_line: str,
        blocked_target_line: str,
        group: list[dict[str, Any]],
        group_nos: tuple[str, ...],
        moving_nos: set[str],
        working_cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[str, dict[str, int], list[dict[str, Any]]] | None:
        if any(car.get("IsWeigh") for car in group):
            return None
        excluded = {source_line, blocked_target_line}
        if blocked_target_line in serial.serial_blocker_lines():
            excluded.update(serial.downstream_lines(blocked_target_line))
        candidate_lines = tuple(dict.fromkeys((preferred_staging_line, *self.staging_lines)))
        for staging_line in candidate_lines:
            if staging_line in excluded:
                continue
            if staging_line in physical.RUNNING_LINES or staging_line in physical.DEPOT_TARGET_LINES:
                continue
            staged = self._project_staging_put(
                target_line=staging_line,
                group=group,
                moving_nos=moving_nos,
                working_cars=working_cars,
                depot_assignment=depot_assignment,
            )
            if staged is None:
                continue
            positions, projected = staged
            if self._serial_storage_is_unsafe(
                target_line=staging_line,
                working_cars=working_cars,
                depot_assignment=depot_assignment,
                moving_nos=set(group_nos),
            ):
                continue
            return staging_line, positions, projected
        return None

    def _project_staging_put(
        self,
        *,
        target_line: str,
        group: list[dict[str, Any]],
        moving_nos: set[str],
        working_cars: list[dict[str, Any]],
        depot_assignment: Any,
    ) -> tuple[dict[str, int], list[dict[str, Any]]] | None:
        positions = planned_positions_for_batch(
            batch=group,
            target_line=target_line,
            cars=working_cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(group):
            return None
        projected = [dict(car) for car in working_cars]
        group_nos = {physical.car_no(car) for car in group}
        for car in projected:
            no = physical.car_no(car)
            if no in group_nos:
                car["Line"] = target_line
                car["Position"] = positions[no]
        return positions, projected

    def _serial_storage_is_unsafe(
        self,
        *,
        target_line: str,
        working_cars: list[dict[str, Any]],
        depot_assignment: Any,
        moving_nos: set[str],
    ) -> bool:
        return (
            target_line in serial.serial_blocker_lines()
            and target_line not in self.protected_serial_storage_lines
            and bool(
                serial.downstream_debt_nos(
                    blocker_line=target_line,
                    cars=working_cars,
                    depot_assignment=depot_assignment,
                    moving_nos=moving_nos,
                )
            )
        )

    def _tail_group_requires_peel(
        self,
        *,
        source_line: str,
        tail_target: str,
        tail_group: list[dict[str, Any]],
        all_move_nos: set[str],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
    ) -> bool:
        working_cars = [dict(car) for car in cars]
        if self._serial_storage_is_unsafe(
            target_line=tail_target,
            working_cars=working_cars,
            depot_assignment=depot_assignment,
            moving_nos={physical.car_no(car) for car in tail_group},
        ):
            return True
        projected_put = self._project_put(
            target_line=tail_target,
            group=tail_group,
            moving_nos=all_move_nos,
            working_cars=working_cars,
            depot_assignment=depot_assignment,
        )
        return projected_put is None or not self.frontier.direct_move_is_reachable(
            source_line=source_line,
            target_line=tail_target,
            batch=tail_group,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            planned_positions=projected_put[0],
        )

    def _target_for(
        self,
        car: dict[str, Any],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
    ) -> str:
        return physical.planned_target_for_car(car, cars, depot_assignment, loads)[0]

    def _tail_same_target_group(
        self,
        prefix: list[dict[str, Any]],
        target_line: str,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
    ) -> list[dict[str, Any]]:
        group: list[dict[str, Any]] = []
        for car in reversed(prefix):
            if self._target_for(car, cars, depot_assignment, loads) != target_line:
                break
            group.append(car)
        return list(reversed(group))


class MiddlePrefixDigestEpisode(Episode):
    intent = IntentKind.PREFIX_DIGEST
    template_name = "middle_prefix_digest_restore"
    frontier = AccessFrontier()
    allowed_families = {
        ContractFamily.FUNCTION_LINE_SERVICE,
        ContractFamily.DISPATCH_SHED_QUEUE,
        ContractFamily.PRE_REPAIR_STAGING,
        ContractFamily.YARD_REBALANCE,
        ContractFamily.LOCO_AREA_STAGING,
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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        if not target_line or target_line == source_line:
            return
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if len(line_cars) < 3:
            return

        loads = physical.line_loads(cars)
        contract_nos = set(contract.subject_nos)
        prefix: list[dict[str, Any]] = []
        target_start = -1
        target_end = -1
        for car in line_cars:
            if physical.pull_equivalent([*prefix, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            no = physical.car_no(car)
            car_target = physical.planned_target_for_car(car, cars, depot_assignment, loads)[0]
            if target_start >= 0 and (no not in contract_nos or car_target != target_line):
                break
            prefix.append(car)
            if no in contract_nos and car_target == target_line:
                if target_start < 0:
                    target_start = len(prefix) - 1
                target_end = len(prefix)
        if target_start <= 0 or target_end <= target_start:
            return
        if target_end >= len(line_cars):
            return

        blocker_batch = prefix[:target_start]
        target_batch = prefix[target_start:target_end]
        if len(target_batch) < 2:
            return
        if any(car.get("IsWeigh") for car in [*blocker_batch, *target_batch]):
            return
        if target_line in serial.serial_blocker_lines() and serial.downstream_debt_nos(
            blocker_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos={physical.car_no(car) for car in target_batch},
        ):
            return

        tail_batch = self._next_same_target_group(
            line_cars=line_cars,
            start=target_end,
            cars=cars,
            depot_assignment=depot_assignment,
            loads=loads,
            source_line=source_line,
        )
        multi_candidate = self._build_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            blocker_batch=blocker_batch,
            target_batch=target_batch,
            tail_batch=tail_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            contract=contract,
            loads=loads,
        )
        if multi_candidate is not None:
            yield multi_candidate
            return

        yield from self._restore_middle_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            blocker_batch=blocker_batch,
            target_batch=target_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            contract=contract,
        )

    def _restore_middle_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        source_line: str,
        target_line: str,
        blocker_batch: list[dict[str, Any]],
        target_batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        carry = [*blocker_batch, *target_batch]
        carry_nos = {physical.car_no(car) for car in carry}
        target_positions = planned_positions_for_batch(
            batch=target_batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=carry_nos,
        )
        if len(target_positions) != len(target_batch):
            return
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(blocker_batch, start=1)
        }
        steps = (
            physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in carry)),
            physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in target_batch), target_positions),
            physical.plan_step("Put", source_line, tuple(physical.car_no(car) for car in blocker_batch), restore_positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=source_line,
            batch=carry,
            steps=steps,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"blockers={','.join(physical.car_no(car) for car in blocker_batch)};"
                f"targets={','.join(physical.car_no(car) for car in target_batch)}"
            ),
            candidate_kind="vnext_middle_prefix_digest_restore",
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
            same_plan_source_return_nos=tuple(physical.car_no(car) for car in blocker_batch),
        )
        yield CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )

    def _build_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        source_line: str,
        target_line: str,
        blocker_batch: list[dict[str, Any]],
        target_batch: list[dict[str, Any]],
        tail_batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        contract: FlowContract,
        loads: Any,
    ) -> CandidateEnvelope | None:
        if not tail_batch:
            return None
        if any(car.get("IsWeigh") for car in tail_batch):
            return None
        tail_target = self._target_for(tail_batch[0], cars, depot_assignment, loads)
        if not tail_target or tail_target == source_line or tail_target in physical.REMOTE_INTERACTION_LINES:
            return None
        if target_line in physical.REMOTE_INTERACTION_LINES:
            return None
        if tail_target in serial.serial_blocker_lines() and serial.downstream_debt_nos(
            blocker_line=tail_target,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos={physical.car_no(car) for car in tail_batch},
        ):
            return None

        carry = [*blocker_batch, *target_batch, *tail_batch]
        if physical.pull_equivalent(carry) > physical.PULL_LIMIT_EQUIVALENT:
            return None
        carry_nos = {physical.car_no(car) for car in carry}
        tail_positions = planned_positions_for_batch(
            batch=tail_batch,
            target_line=tail_target,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=carry_nos,
        )
        if len(tail_positions) != len(tail_batch):
            return None
        target_positions = planned_positions_for_batch(
            batch=target_batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=carry_nos,
        )
        if len(target_positions) != len(target_batch):
            return None

        steps: list[Any] = [
            physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in carry)),
            physical.plan_step("Put", tail_target, tuple(physical.car_no(car) for car in tail_batch), tail_positions),
            physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in target_batch), target_positions),
        ]
        blocker_final_line = self._same_final_target(blocker_batch, cars, depot_assignment, loads)
        source_return_nos: tuple[str, ...] = ()
        progressed_nos = {physical.car_no(car) for car in [*target_batch, *tail_batch]}
        if blocker_final_line and blocker_final_line != source_line and self._blocker_final_put_is_safe(
            blocker_line=blocker_final_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos=carry_nos | progressed_nos,
        ):
            blocker_positions = planned_positions_for_batch(
                batch=blocker_batch,
                target_line=blocker_final_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos={physical.car_no(car) for car in blocker_batch},
            )
            if len(blocker_positions) != len(blocker_batch):
                return None
            steps.append(
                physical.plan_step(
                    "Put",
                    blocker_final_line,
                    tuple(physical.car_no(car) for car in blocker_batch),
                    blocker_positions,
                )
            )
        else:
            source_return_nos = tuple(physical.car_no(car) for car in blocker_batch)
            restore_positions = {
                physical.car_no(car): int(car.get("Position") or index)
                for index, car in enumerate(blocker_batch, start=1)
            }
            steps.append(physical.plan_step("Put", source_line, source_return_nos, restore_positions))

        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        ):
            return None
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=steps[-1].line,
            batch=carry,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"tail={tail_target}:{','.join(physical.car_no(car) for car in tail_batch)};"
                f"targets={','.join(physical.car_no(car) for car in target_batch)};"
                f"blockers={','.join(physical.car_no(car) for car in blocker_batch)}"
            ),
            candidate_kind="vnext_middle_prefix_digest_restore",
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
            same_plan_source_return_nos=source_return_nos,
        )
        return CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )

    def _next_same_target_group(
        self,
        *,
        line_cars: list[dict[str, Any]],
        start: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
        source_line: str,
    ) -> list[dict[str, Any]]:
        if start >= len(line_cars):
            return []
        target_line = self._target_for(line_cars[start], cars, depot_assignment, loads)
        if not target_line or target_line == source_line:
            return []
        group: list[dict[str, Any]] = []
        for car in line_cars[start:]:
            if self._target_for(car, cars, depot_assignment, loads) != target_line:
                break
            group.append(car)
        return group

    def _target_for(
        self,
        car: dict[str, Any],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
    ) -> str:
        return physical.planned_target_for_car(car, cars, depot_assignment, loads)[0]

    def _same_final_target(
        self,
        batch: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
    ) -> str:
        targets = {self._target_for(car, cars, depot_assignment, loads) for car in batch}
        return next(iter(targets)) if len(targets) == 1 else ""

    def _blocker_final_put_is_safe(
        self,
        *,
        blocker_line: str,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        moving_nos: set[str],
    ) -> bool:
        if blocker_line not in serial.serial_blocker_lines():
            return True
        return not serial.downstream_debt_nos(
            blocker_line=blocker_line,
            cars=cars,
            depot_assignment=depot_assignment,
            moving_nos=moving_nos,
        )


class DepotRepackWithInboundTailEpisode(Episode):
    intent = IntentKind.DEPOT_REPACK
    template_name = "depot_repack_with_inbound_tail"
    frontier = AccessFrontier()

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
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        if target_line not in physical.DEPOT_LINES or source_line == target_line:
            return

        target_existing = physical.line_cars_in_access_order(
            cars=cars,
            line=target_line,
            graph=graph,
            loco_location=loco_location,
        )
        if not target_existing:
            return
        target_existing_nos = tuple(physical.car_no(car) for car in target_existing)
        for car in target_existing:
            slot = depot_assignment.slots.get(physical.car_no(car))
            if not slot or slot.line != target_line or slot.locked:
                return

        contract_nos = set(contract.subject_nos)
        source_access = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        prefix: list[dict[str, Any]] = []
        inbound: list[dict[str, Any]] = []
        saw_inbound = False
        for car in source_access:
            no = physical.car_no(car)
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
        if physical.pull_equivalent(prefix) > physical.PULL_LIMIT_EQUIVALENT:
            return
        replay_batch = [*target_existing, *inbound]
        if physical.pull_equivalent(replay_batch) > physical.PULL_LIMIT_EQUIVALENT:
            return

        all_move_nos = {physical.car_no(car) for car in [*prefix, *target_existing]}
        inbound_nos = tuple(physical.car_no(car) for car in inbound)
        blocker_nos = tuple(physical.car_no(car) for car in blockers)
        replay_nos = (*target_existing_nos, *inbound_nos)
        grouped = physical.cars_by_line(cars)

        for staging_line in DEPOT_STAGING_LINE_PRIORITY:
            if staging_line in {source_line, target_line}:
                continue
            if staging_line in physical.DEPOT_TARGET_LINES or staging_line in physical.RUNNING_LINES:
                continue
            spec = physical.TRACK_SPECS.get(staging_line)
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
            if not physical.candidate_positions_available(staging_line, staging_positions, cars, all_move_nos, grouped):
                continue
            if not physical.line_has_length_capacity(staging_line, cars, inbound, all_move_nos, grouped=grouped):
                continue

            restore_positions = {}
            if blockers:
                restore_positions = {
                    physical.car_no(car): int(car.get("Position") or index)
                    for index, car in enumerate(blockers, start=1)
                }
                if not physical.candidate_positions_available(source_line, restore_positions, cars, all_move_nos, grouped):
                    continue
            target_positions = {
                no: position
                for position, no in enumerate(replay_nos, start=1)
            }
            steps = [
                physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in prefix)),
                physical.plan_step("Put", staging_line, inbound_nos, staging_positions),
                physical.plan_step("Get", target_line, target_existing_nos),
                physical.plan_step("Get", staging_line, inbound_nos),
                physical.plan_step("Put", target_line, replay_nos, target_positions),
            ]
            if blocker_nos:
                steps.insert(2, physical.plan_step("Put", source_line, blocker_nos, restore_positions))
            plan_steps = tuple(steps)
            if not self.frontier.plan_steps_are_reachable(
                steps=plan_steps,
                cars=cars,
                graph=graph,
                loco_location=loco_location,
                depot_assignment=depot_assignment,
                serial_gate_leases=serial_gate_leases or {},
            ):
                continue
            candidate = physical.build_planlet_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=source_line,
                target_line=target_line,
                batch=[*prefix, *target_existing],
                steps=plan_steps,
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
                same_plan_source_return_nos=blocker_nos,
            )
            yield CandidateEnvelope(
                candidate=candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )
            return


class SerialGateClearEpisode(Episode):
    intent = IntentKind.SERIAL_GATE_CLEAR
    template_name = "serial_gate_clear_support"

    staging_lines = (
        "存2线",
        "存1线",
        "存3线",
        "存5线北",
        "存5线南",
        "预修线",
        "调梁棚",
    )
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {
            ContractFamily.FUNCTION_LINE_SERVICE,
            ContractFamily.DISPATCH_SHED_QUEUE,
            ContractFamily.PRE_REPAIR_STAGING,
            ContractFamily.YARD_REBALANCE,
            ContractFamily.LOCO_AREA_STAGING,
            ContractFamily.REPAIR_INBOUND,
            ContractFamily.DEPOT_SLOT,
            ContractFamily.CUN4_PORT_STAGING,
        }

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        contract_nos = set(contract.subject_nos)
        all_by_line = physical.cars_by_line(cars)
        for blocker_line in sorted(serial.serial_blocker_lines()):
            downstream_debt = serial.downstream_debt_nos(
                blocker_line=blocker_line,
                cars=cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
            downstream_debt_nos = set(downstream_debt)
            if not downstream_debt_nos or not (downstream_debt_nos & contract_nos):
                continue
            blockers = physical.line_cars_in_access_order(
                cars=cars,
                line=blocker_line,
                graph=graph,
                loco_location=loco_location,
            )
            if not blockers:
                continue
            if any(car.get("IsWeigh") for car in blockers):
                continue
            if physical.pull_equivalent(blockers) > physical.PULL_LIMIT_EQUIVALENT:
                continue
            moving_nos = {physical.car_no(car) for car in blockers}
            staging_lines = self.frontier.reachable_staging_lines(
                source_line=blocker_line,
                batch=blockers,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                candidate_lines=self.staging_lines,
                excluded_lines=serial.downstream_lines(blocker_line),
            )
            for staging_line in staging_lines:
                positions = planned_positions_for_batch(
                    batch=blockers,
                    target_line=staging_line,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    batch_nos=moving_nos,
                )
                if len(positions) != len(blockers):
                    continue
                if not physical.candidate_positions_available(
                    staging_line,
                    positions,
                    cars,
                    moving_nos,
                    all_by_line,
                ):
                    continue
                candidate = physical.build_direct_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    source_line=blocker_line,
                    target_line=staging_line,
                    batch=blockers,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    reason=(
                        f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                        f"blocker={blocker_line};downstream={','.join(sorted(downstream_debt_nos & contract_nos))}"
                    ),
                    candidate_kind="vnext_serial_gate_clear",
                    planned_positions=positions,
                )
                if candidate:
                    yield self._envelope(candidate, contract)
                    return


class TailCloseoutEpisode(DirectMoveEpisode):
    intent = IntentKind.TAIL_CLOSEOUT
    template_name = "tail_closeout_direct_accessible_prefix"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.TAIL_CLOSEOUT


class SpottingRepackEpisode(Episode):
    intent = IntentKind.FRONT_PREP
    template_name = "spotting_repack"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {ContractFamily.DISPATCH_SHED_QUEUE, ContractFamily.FUNCTION_LINE_SERVICE}

    def generate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any] | None = None,
        contract: FlowContract,
    ) -> Iterable[CandidateEnvelope]:
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        if not physical.is_spotting_line(target_line):
            return

        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        if source_line == target_line:
            plan = build_spotting_same_line_repack_planlet(
                case_id=case_id,
                hook_index=hook_index,
                line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                reason=f"vnext:{self.template_name};owner_contract={contract.contract_id}",
                candidate_kind="vnext_spotting_target_repack",
                frontier=self.frontier,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
            )
            if plan is None:
                return
            request = ResourceRequest(
                contract_id=contract.contract_id,
                family=contract.family,
                candidate_id=plan.candidate.candidate_id,
                resources=(),
                source_line=source_line,
                target_line=target_line,
                move_nos=tuple(plan.candidate.move_car_nos),
                intent=self.intent,
                same_plan_source_return_nos=(),
            )
            yield CandidateEnvelope(
                candidate=plan.candidate,
                contract=contract,
                intent=self.intent,
                resource_request=request,
                template_name=self.template_name,
            )
            return

        blocker_batch, target_batch = self._source_access_batch(line_cars, contract)
        if not target_batch:
            return

        plan = build_spotting_target_repack_planlet(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            source_batch=target_batch,
            source_blocker_batch=blocker_batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"blockers={','.join(physical.car_no(car) for car in blocker_batch)};"
                f"targets={','.join(physical.car_no(car) for car in target_batch)}"
            ),
            candidate_kind="vnext_spotting_target_repack",
            frontier=self.frontier,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
        )
        if plan is None:
            return
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=plan.candidate.candidate_id,
            resources=(),
            source_line=source_line,
            target_line=target_line,
            move_nos=tuple(plan.candidate.move_car_nos),
            intent=self.intent,
            same_plan_source_return_nos=plan.source_return_nos,
        )
        yield CandidateEnvelope(
            candidate=plan.candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )

    def _source_access_batch(
        self,
        line_cars: list[dict[str, Any]],
        contract: FlowContract,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        contract_nos = set(contract.subject_nos)
        blocker_batch: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        saw_target = False
        for car in line_cars:
            no = physical.car_no(car)
            if not saw_target and no not in contract_nos:
                if physical.pull_equivalent([*blocker_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    return [], []
                blocker_batch.append(car)
                continue
            if no not in contract_nos:
                break
            saw_target = True
            if physical.pull_equivalent([*blocker_batch, *target_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            target_batch.append(car)
        return blocker_batch, target_batch


EPISODES: tuple[Episode, ...] = (
    DepotOutboundSessionEpisode(),
    Cun4ReleaseAcceptEpisode(),
    Cun4ReleaseGroupAssemblyEpisode(),
    DepotInboundGatherSessionEpisode(),
    RemoteSessionPrefixDigestEpisode(),
    RemoteSessionEpisode(),
    DirectMoveEpisode(),
    SpottingRepackEpisode(),
    SerialGateClearEpisode(),
    TailBlockerPeelDigestEpisode(),
    MiddlePrefixDigestEpisode(),
    PrefixDigestEpisode(),
    DepotRepackWithInboundTailEpisode(),
    RemotePrefixMiddleDigestEpisode(),
    DepotMultiDropEpisode(),
    DepotSlotFillEpisode(),
    DepotSlotSwapEpisode(),
    RemoteDepotEpisode(),
    TailCloseoutEpisode(),
)
