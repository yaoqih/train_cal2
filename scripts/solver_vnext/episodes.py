from __future__ import annotations

from typing import Any, Iterable

from . import physical
from . import release
from . import serial
from .domain import CandidateEnvelope, ContractFamily, FlowContract, IntentKind, ResourceRequest
from .frontier import AccessFrontier
from .placement import planned_positions_for_batch
from .spotting import (
    build_spotting_same_line_repack_planlet,
    build_spotting_cross_line_repack_planlet,
    spotting_nonforced_prefix_would_pollute,
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
            return
        batch_nos = {physical.car_no(car) for car in batch}
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
                yield self._envelope(candidate, contract)


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

        target_lines = self._outbound_targets_by_priority(cars, subject_nos, target_for)
        if not target_lines:
            return

        for target_line in target_lines:
            candidate = self._session_candidate_for_target(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                contract=contract,
                subject_nos=subject_nos,
                target_for=target_for,
                target_line=target_line,
            )
            if candidate is None:
                continue
            yield self._envelope(candidate, contract)
            return

    def _session_candidate_for_target(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        contract: FlowContract,
        subject_nos: set[str],
        target_for: Any,
        target_line: str,
    ) -> Any | None:
        carry: list[dict[str, Any]] = []
        steps = []
        best_candidate = None
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
                candidate_carry = [*carry, *batch]
                candidate_steps = [
                    *steps,
                    physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch)),
                ]
                candidate = self._validated_session_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases or {},
                    target_line=target_line,
                    carry=candidate_carry,
                    steps=candidate_steps,
                )
                if candidate is None:
                    continue
                carry = candidate_carry
                steps = candidate_steps
                if self._session_is_structural(target_line=target_line, carry=carry, steps=steps):
                    best_candidate = candidate
        return best_candidate

    def _validated_session_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        target_line: str,
        carry: list[dict[str, Any]],
        steps: list[Any],
    ) -> Any | None:
        batch_nos = {physical.car_no(car) for car in carry}
        positions = planned_positions_for_batch(
            batch=carry,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=batch_nos,
        )
        if len(positions) != len(carry):
            return None
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
            return None
        plan_steps = (
            *steps,
            physical.plan_step("Put", target_line, tuple(physical.car_no(car) for car in carry), positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
        ):
            return None
        return physical.build_planlet_candidate(
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

    def _session_is_structural(self, *, target_line: str, carry: list[dict[str, Any]], steps: list[Any]) -> bool:
        if len(steps) < 2:
            return False
        min_batch = 3 if target_line == "存4线" else 2
        return len(carry) >= min_batch

    def _outbound_targets_by_priority(
        self,
        cars: list[dict[str, Any]],
        subject_nos: set[str],
        target_for: Any,
    ) -> list[str]:
        counts: dict[str, int] = {}
        for car in cars:
            if physical.car_no(car) not in subject_nos or car["Line"] not in self.source_order:
                continue
            target_line = target_for(car)
            if not target_line or target_line in physical.REMOTE_INTERACTION_LINES:
                continue
            counts[target_line] = counts.get(target_line, 0) + 1
        return sorted(counts, key=lambda line: (line != "存4线", -counts[line], line))


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


class TailCloseoutEpisode(DirectMoveEpisode):
    intent = IntentKind.TAIL_CLOSEOUT
    template_name = "tail_closeout_direct_accessible_prefix"

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.TAIL_CLOSEOUT


class SpottingRepackEpisode(Episode):
    intent = IntentKind.FRONT_PREP
    template_name = "spotting_repack"
    frontier = AccessFrontier()
    allowed_families = DirectMoveEpisode.allowed_families

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
                candidate_kind="vnext_spotting_repack",
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

        plan = build_spotting_cross_line_repack_planlet(
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
            candidate_kind="vnext_spotting_repack",
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
    TailBlockerPeelDigestEpisode(),
    DepotSlotSwapEpisode(),
    TailCloseoutEpisode(),
)
