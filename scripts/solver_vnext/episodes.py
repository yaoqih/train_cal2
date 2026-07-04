from __future__ import annotations

from typing import Any, Iterable

from . import depot_outbound_plan
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
        strategic_plan: Any | None = None,
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
        strategic_plan: Any | None = None,
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is not None and not strategic_plan.depot_inbound.assembly_complete:
            return
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
        return contract.family in {ContractFamily.REMOTE_SESSION, ContractFamily.DEPOT_OUTBOUND}

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        if not strategic_plan.depot_inbound.assembly_complete:
            return
        if strategic_plan.cun4_release.release_nos or strategic_plan.cun4_release.dirty_nos:
            return
        temporary_line_by_no = strategic_plan.depot_outbound.temporary_line_by_no
        if not temporary_line_by_no:
            return
        subject_nos = self._depot_outbound_subject_nos(
            contract=contract,
            strategic_plan=strategic_plan,
        )
        if not subject_nos:
            return

        def target_for(car: dict[str, Any]) -> str:
            no = physical.car_no(car)
            return temporary_line_by_no.get(no, "")

        target_lines = self._outbound_targets_by_plan(strategic_plan, subject_nos)
        if not target_lines:
            return
        if target_lines == ["存4线"]:
            candidate = self._complete_cun4_session_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                strategic_plan=strategic_plan,
                subject_nos=subject_nos,
            )
            if candidate is not None:
                yield self._envelope(candidate, contract)
                return
            candidate = self._repacked_complete_cun4_session_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                strategic_plan=strategic_plan,
                subject_nos=subject_nos,
            )
            if candidate is not None:
                yield self._envelope(candidate, contract)
            return
        plan_candidate = self._plan_session_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            contract=contract,
            subject_nos=subject_nos,
            temporary_line_by_no=temporary_line_by_no,
            target_lines=target_lines,
        )
        if plan_candidate is not None:
            yield self._envelope(plan_candidate, contract)

        for target_line in target_lines:
            plan_target_nos = {
                no for no, line in temporary_line_by_no.items()
                if line == target_line and no in subject_nos
            }
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
                plan_target_nos=plan_target_nos,
            )
            if candidate is None:
                continue
            yield self._envelope(candidate, contract)
            return

    def _complete_cun4_session_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        strategic_plan: Any,
        subject_nos: set[str],
    ) -> Any | None:
        outbound_plan = strategic_plan.depot_outbound
        plan_order = tuple(outbound_plan.pull_order_nos)
        if not plan_order or not set(plan_order) <= subject_nos:
            return None
        collected = self._collect_outbound_get_steps_in_plan_order(
            cars=cars,
            plan_order=plan_order,
        )
        if collected is None:
            return None
        steps, carry = collected
        moving_nos = set(plan_order)
        positions = planned_positions_for_batch(
            batch=carry,
            target_line="存4线",
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(carry):
            return None
        plan_steps = (
            *steps,
            physical.plan_step("Put", "存4线", plan_order, positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_outbound_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line="存4线",
            batch=carry,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};target=存4线;"
                f"mode=complete_plan;batch={len(carry)}"
            ),
            candidate_kind="vnext_depot_outbound_session",
        )

    def _depot_outbound_subject_nos(self, *, contract: FlowContract, strategic_plan: Any) -> set[str]:
        if contract.family == ContractFamily.DEPOT_OUTBOUND:
            return set(strategic_plan.depot_outbound.cun4_nos)
        return set(contract.subject_nos)

    def _depot_outbound_exchange_subject_nos(self, *, contract: FlowContract, strategic_plan: Any) -> set[str]:
        if contract.family == ContractFamily.DEPOT_OUTBOUND:
            return set(strategic_plan.depot_outbound.cun4_nos) | set(strategic_plan.cun4_release.release_nos)
        return set(contract.subject_nos)

    def _collect_outbound_get_steps_in_plan_order(
        self,
        *,
        cars: list[dict[str, Any]],
        plan_order: tuple[str, ...],
        initial_order: tuple[str, ...] = (),
        initial_steps: tuple[Any, ...] = (),
        stop_after_nos: set[str] | None = None,
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]] | None:
        by_no = {physical.car_no(car): car for car in cars}
        if tuple(plan_order[: len(initial_order)]) != tuple(initial_order):
            return None
        selected_order: list[str] = list(initial_order)
        selected_nos: set[str] = set(initial_order)
        steps: list[Any] = list(initial_steps)
        index = len(selected_order)
        required_nos = stop_after_nos or set(plan_order)
        while index < len(plan_order):
            if required_nos <= selected_nos:
                break
            no = plan_order[index]
            if no in selected_nos:
                index += 1
                continue
            car = by_no.get(no)
            if car is None or car["Line"] not in self.source_order:
                return None
            source_line = car["Line"]
            batch: list[dict[str, Any]] = []
            while index < len(plan_order):
                next_no = plan_order[index]
                if next_no in selected_nos:
                    index += 1
                    continue
                next_car = by_no.get(next_no)
                if next_car is None or next_car["Line"] != source_line:
                    break
                access_order = physical.line_access_order(cars, source_line, selected_nos)
                if not access_order or access_order[0] != next_no:
                    return None
                carry = [by_no[item] for item in selected_order]
                if physical.pull_equivalent([*carry, next_car]) > physical.PULL_LIMIT_EQUIVALENT:
                    return None
                batch.append(next_car)
                selected_order.append(next_no)
                selected_nos.add(next_no)
                index += 1
            if not batch:
                return None
            steps.append(physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch)))
        if not required_nos <= selected_nos:
            return None
        return tuple(steps), [by_no[no] for no in selected_order]

    def _repacked_complete_cun4_session_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        strategic_plan: Any,
        subject_nos: set[str],
    ) -> Any | None:
        outbound_plan = strategic_plan.depot_outbound
        plan_order = tuple(outbound_plan.pull_order_nos)
        if not plan_order or not set(plan_order) <= subject_nos:
            return None
        by_no = {physical.car_no(car): car for car in cars}
        outbound_batch = [by_no[no] for no in plan_order if no in by_no]
        if len(outbound_batch) != len(plan_order):
            return None
        if physical.pull_equivalent(outbound_batch) > physical.PULL_LIMIT_EQUIVALENT:
            return None
        segments = self._desired_source_segments(cars=cars, desired_order=plan_order)
        if len(segments) < 2:
            return None
        moving_nos = set(plan_order)
        staging_lines = self._assign_source_repack_staging_lines(
            cars=cars,
            segments=segments,
            moving_nos=moving_nos,
        )
        if len(staging_lines) != len(segments):
            return None
        outbound_positions = planned_positions_for_batch(
            batch=outbound_batch,
            target_line="存4线",
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(outbound_positions) != len(outbound_batch):
            return None

        working_cars = [dict(car) for car in cars]
        steps: list[Any] = []
        staged: set[int] = set()
        while len(staged) < len(segments):
            progressed = False
            for index, (source_line, nos) in enumerate(segments):
                if index in staged:
                    continue
                if not self._source_repack_segment_accessible(
                    cars=working_cars,
                    source_line=source_line,
                    nos=nos,
                ):
                    continue
                batch = [
                    car
                    for no in nos
                    for car in working_cars
                    if physical.car_no(car) == no
                ]
                if len(batch) != len(nos):
                    return None
                target_line = staging_lines[index]
                positions = planned_positions_for_batch(
                    batch=batch,
                    target_line=target_line,
                    cars=working_cars,
                    depot_assignment=depot_assignment,
                    batch_nos=set(nos),
                )
                if len(positions) != len(batch):
                    return None
                steps.append(physical.plan_step("Get", source_line, nos))
                physical.apply_physical_get_order(working_cars, source_line, nos)
                steps.append(physical.plan_step("Put", target_line, nos, positions))
                physical.apply_physical_put_order(working_cars, target_line, list(nos), positions)
                staged.add(index)
                progressed = True
            if not progressed:
                return None
        for index, (_source_line, nos) in enumerate(segments):
            staging_line = staging_lines[index]
            if not self._source_repack_segment_accessible(
                cars=working_cars,
                source_line=staging_line,
                nos=nos,
            ):
                return None
            steps.append(physical.plan_step("Get", staging_line, nos))
            physical.apply_physical_get_order(working_cars, staging_line, nos)
        steps.append(physical.plan_step("Put", "存4线", plan_order, outbound_positions))
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            graph=graph,
            loco_location=loco_location,
            depot_assignment=depot_assignment,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_cun4_source_repack_exchange",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=plan_steps[0].line,
            target_line="存4线",
            batch=outbound_batch,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};mode=source_repack_to_cun4;"
                f"segments={len(segments)};batch={len(outbound_batch)}"
            ),
            candidate_kind="vnext_depot_cun4_source_repack_exchange",
        )

    def _desired_source_segments(
        self,
        *,
        cars: list[dict[str, Any]],
        desired_order: tuple[str, ...],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        by_no = {physical.car_no(car): car for car in cars}
        segments: list[tuple[str, list[str]]] = []
        for no in desired_order:
            car = by_no.get(no)
            if car is None or car["Line"] not in self.source_order:
                return ()
            source_line = car["Line"]
            if segments and segments[-1][0] == source_line:
                segments[-1][1].append(no)
                continue
            segments.append((source_line, [no]))
        return tuple((line, tuple(nos)) for line, nos in segments)

    def _assign_source_repack_staging_lines(
        self,
        *,
        cars: list[dict[str, Any]],
        segments: tuple[tuple[str, tuple[str, ...]], ...],
        moving_nos: set[str],
    ) -> dict[int, str]:
        by_no = {physical.car_no(car): car for car in cars}
        available = list(DEPOT_OUTBOUND_REPACK_STAGING_LINES)
        assigned: dict[int, str] = {}
        for index in sorted(
            range(len(segments)),
            key=lambda item: (
                -sum(physical.car_length(by_no[no]) for no in segments[item][1]),
                item,
            ),
        ):
            _source_line, nos = segments[index]
            required_length = sum(physical.car_length(by_no[no]) for no in nos)
            for line in list(available):
                used = physical.line_length_load(cars, line, excluded_nos=moving_nos)
                if used + required_length <= physical.TRACK_SPECS[line].length_m + physical.LINE_LENGTH_TOLERANCE_M:
                    assigned[index] = line
                    available.remove(line)
                    break
        return assigned

    def _source_repack_segment_accessible(
        self,
        *,
        cars: list[dict[str, Any]],
        source_line: str,
        nos: tuple[str, ...],
    ) -> bool:
        return physical.line_access_order(cars, source_line, set())[: len(nos)] == list(nos)

    def _plan_session_candidate(
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
        temporary_line_by_no: dict[str, str],
        target_lines: list[str],
    ) -> Any | None:
        if len(target_lines) < 2:
            return None
        plan_nos = set(temporary_line_by_no) & subject_nos
        carry: list[dict[str, Any]] = []
        steps: list[Any] = []
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
                if no not in plan_nos:
                    break
                if physical.pull_equivalent([*carry, *batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                batch.append(car)
            if not batch:
                continue
            carry.extend(batch)
            steps.append(physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in batch)))
            if physical.pull_equivalent(carry) >= physical.PULL_LIMIT_EQUIVALENT:
                break
        if len({temporary_line_by_no[physical.car_no(car)] for car in carry}) < 2:
            return None
        return self._validated_plan_session_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            carry=carry,
            steps=steps,
            temporary_line_by_no=temporary_line_by_no,
        )

    def _validated_plan_session_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        carry: list[dict[str, Any]],
        steps: list[Any],
        temporary_line_by_no: dict[str, str],
    ) -> Any | None:
        if not carry or not steps:
            return None
        moving_nos = {physical.car_no(car) for car in carry}
        no_to_car = {physical.car_no(car): car for car in carry}
        remaining = [physical.car_no(car) for car in carry]
        put_lines: list[str] = []
        while remaining:
            tail_no = remaining[-1]
            target_line = temporary_line_by_no[tail_no]
            if target_line in put_lines:
                return None
            start = len(remaining) - 1
            while start > 0 and temporary_line_by_no.get(remaining[start - 1]) == target_line:
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
                return None
            probe = physical.build_direct_candidate(
                case_id=case_id,
                hook_index=hook_index,
                source_line=steps[0].line,
                target_line=target_line,
                batch=group,
                cars=cars,
                depot_assignment=depot_assignment,
                reason="vnext:depot_outbound_plan_session_position_probe",
                candidate_kind="vnext_position_probe",
                planned_positions=positions,
            )
            if probe is None:
                return None
            put_lines.append(target_line)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        if len(put_lines) < 2:
            return None
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_outbound_plan_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=steps[-1].line,
            batch=carry,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};plan_session=multi_target;"
                f"put_lines={','.join(put_lines)};batch={len(carry)}"
            ),
            candidate_kind="vnext_depot_outbound_plan_session",
        )

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
        plan_target_nos: set[str],
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
                carry_nos = {physical.car_no(car) for car in carry}
                if plan_target_nos and plan_target_nos <= carry_nos:
                    best_candidate = candidate
                elif self._session_is_structural(target_line=target_line, carry=carry, steps=steps):
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
            candidate_kind="vnext_depot_outbound_session",
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

    def _outbound_targets_by_plan(self, strategic_plan: Any, subject_nos: set[str]) -> list[str]:
        lines: list[str] = []
        for group in strategic_plan.depot_outbound.groups:
            if not group.vehicle_nos:
                continue
            if set(group.vehicle_nos) & subject_nos:
                lines.append(group.line)
        return list(dict.fromkeys(lines))


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
        strategic_plan: Any | None = None,
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
        planned_lines = strategic_plan.depot_inbound.temporary_line_by_no if strategic_plan is not None else {}
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
            if planned_lines and planned_lines.get(no) != "存4线":
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


class Cun4OutboundAssemblyReleaseEpisode(Episode):
    intent = IntentKind.CUN4_OUTBOUND_HOLD
    template_name = "cun4_outbound_assembly_release"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        if contract.source_lines != ("存4线",):
            return False
        target_line = contract.target_lines[0] if contract.target_lines else ""
        return bool(target_line and target_line not in physical.DEPOT_TARGET_LINES and target_line != "存4线")

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
        target_line = contract.target_lines[0]
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
        contract_nos = set(contract.subject_nos)
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        for car in line_cars:
            no = physical.car_no(car)
            planned_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in contract_nos or planned_line != target_line:
                break
            if planned_line in physical.DEPOT_TARGET_LINES or planned_line == "存4线":
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        if not batch:
            return
        moving_nos = {physical.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(batch):
            return
        candidate = physical.build_direct_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line="存4线",
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"target={target_line};batch={len(batch)}"
            ),
            candidate_kind="vnext_cun4_outbound_assembly_release",
            planned_positions=positions,
        )
        if candidate is None:
            return
        if not self.frontier.direct_move_is_reachable(
            source_line="存4线",
            target_line=target_line,
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


class Cun4UnwheelReleaseEpisode(Episode):
    intent = IntentKind.CUN4_RELEASE_ACCEPT
    template_name = "cun4_unwheel_release"
    frontier = AccessFrontier()
    parking_lines = ("修4库外", "修3库外", "修2库外", "修1库外")

    def applies(self, contract: FlowContract) -> bool:
        if contract.family == ContractFamily.REMOTE_SESSION:
            return True
        return contract.source_lines == ("存4线",) and contract.target_lines == ("卸轮线",)

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is not None and not strategic_plan.depot_inbound.assembly_complete:
            return
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)
        unwheel_batch = self._cun4_unwheel_prefix(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            loads=loads,
            subject_nos=subject_nos,
        )
        if not unwheel_batch:
            return
        blocker_batch = self._unwheel_outbound_prefix(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            loads=loads,
            subject_nos=subject_nos,
        )
        if blocker_batch and contract.family != ContractFamily.REMOTE_SESSION:
            return
        candidate = self._candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            unwheel_batch=unwheel_batch,
            blocker_batch=blocker_batch,
        )
        if candidate is not None:
            yield self._envelope(candidate, contract)

    def _cun4_unwheel_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        loads: Any,
        subject_nos: set[str],
    ) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in subject_nos or target_line != "卸轮线":
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _unwheel_outbound_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        loads: Any,
        subject_nos: set[str],
    ) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line="卸轮线",
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if target_line == "卸轮线" or physical.car_is_satisfied(car, depot_assignment, cars):
                break
            if no not in subject_nos:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        unwheel_batch: list[dict[str, Any]],
        blocker_batch: list[dict[str, Any]],
    ) -> Any | None:
        if blocker_batch:
            for parking_line in self.parking_lines:
                candidate = self._candidate_with_parking(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases,
                    unwheel_batch=unwheel_batch,
                    blocker_batch=blocker_batch,
                    parking_line=parking_line,
                )
                if candidate is not None:
                    return candidate
            return None
        return self._candidate_with_parking(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            unwheel_batch=unwheel_batch,
            blocker_batch=[],
            parking_line="",
        )

    def _candidate_with_parking(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        unwheel_batch: list[dict[str, Any]],
        blocker_batch: list[dict[str, Any]],
        parking_line: str,
    ) -> Any | None:
        planning_cars = [dict(car) for car in cars]
        steps: list[Any] = []
        carry: list[dict[str, Any]] = []
        blocker_nos = tuple(physical.car_no(car) for car in blocker_batch)
        unwheel_nos = tuple(physical.car_no(car) for car in unwheel_batch)
        if blocker_batch:
            physical.apply_physical_get_order(planning_cars, "卸轮线", blocker_nos)
            blocker_positions = planned_positions_for_batch(
                batch=blocker_batch,
                target_line=parking_line,
                cars=planning_cars,
                depot_assignment=depot_assignment,
                batch_nos=set(blocker_nos),
            )
            if len(blocker_positions) != len(blocker_batch):
                return None
            steps.append(physical.plan_step("Get", "卸轮线", blocker_nos))
            steps.append(physical.plan_step("Put", parking_line, blocker_nos, blocker_positions))
            physical.apply_physical_put_order(planning_cars, parking_line, list(blocker_nos), blocker_positions)
            carry.extend(blocker_batch)

        physical.apply_physical_get_order(planning_cars, "存4线", unwheel_nos)
        unwheel_positions = planned_positions_for_batch(
            batch=unwheel_batch,
            target_line="卸轮线",
            cars=planning_cars,
            depot_assignment=depot_assignment,
            batch_nos=set(unwheel_nos),
        )
        if len(unwheel_positions) != len(unwheel_batch):
            return None
        steps.append(physical.plan_step("Get", "存4线", unwheel_nos))
        steps.append(physical.plan_step("Put", "卸轮线", unwheel_nos, unwheel_positions))
        carry.extend(unwheel_batch)
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_cun4_unwheel_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=plan_steps[0].line,
            target_line="卸轮线",
            batch=carry,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};"
                f"unwheel={len(unwheel_batch)};blockers={len(blocker_batch)};"
                f"parking={parking_line}"
            ),
            candidate_kind="vnext_cun4_unwheel_release",
        )


DEPOT_OUTBOUND_REPACK_STAGING_LINES = (
    "存5线北",
    "存3线",
    "存2线",
    "存1线",
    "调梁线北",
    "机北1",
    "机北2",
    "机库线",
    "洗罐线北",
)


class DepotCun4SourceRepackExchangeEpisode(DepotOutboundSessionEpisode):
    intent = IntentKind.CUN4_OUTBOUND_HOLD
    template_name = "depot_cun4_source_repack_exchange"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {ContractFamily.REMOTE_SESSION, ContractFamily.DEPOT_OUTBOUND}

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None or not strategic_plan.depot_inbound.assembly_complete:
            return
        if not strategic_plan.cun4_release.release_nos:
            return
        outbound_plan = strategic_plan.depot_outbound
        desired_order = tuple(outbound_plan.pull_order_nos)
        if not desired_order or not outbound_plan.cun4_prefix_unsafe_nos:
            return
        subject_nos = self._depot_outbound_exchange_subject_nos(
            contract=contract,
            strategic_plan=strategic_plan,
        )
        if not set(desired_order) <= subject_nos:
            return

        port_state = release.cun4_port_state(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
        )
        if port_state.dirty_nos:
            return
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
        release_order = tuple(port_state.release_nos)
        inbound_nos = tuple(physical.car_no(car) for car in line_cars[: len(release_order)])
        if not inbound_nos or inbound_nos != release_order:
            return

        loads = physical.line_loads(cars)
        release_nos = set(port_state.release_nos)
        inbound_batch: list[dict[str, Any]] = []
        target_by_no: dict[str, str] = {}
        for car in line_cars[: len(inbound_nos)]:
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in release_nos or no not in subject_nos:
                return
            if target_line not in physical.DEPOT_TARGET_LINES:
                return
            if car.get("IsWeigh"):
                return
            inbound_batch.append(car)
            target_by_no[no] = target_line

        by_no = {physical.car_no(car): car for car in cars}
        outbound_batch: list[dict[str, Any]] = []
        for no in desired_order:
            car = by_no.get(no)
            if car is None or car["Line"] not in self.source_order:
                return
            outbound_batch.append(car)
        if physical.pull_equivalent(outbound_batch) > physical.PULL_LIMIT_EQUIVALENT:
            return

        all_moving_nos = set(desired_order) | set(inbound_nos)
        steps = self._source_repack_exchange_steps(
            cars=cars,
            depot_assignment=depot_assignment,
            outbound_batch=outbound_batch,
            desired_order=desired_order,
            inbound_batch=inbound_batch,
            inbound_nos=inbound_nos,
            target_by_no=target_by_no,
            all_moving_nos=all_moving_nos,
        )
        if not steps:
            return
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            candidate_kind="vnext_depot_cun4_source_repack_exchange",
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line="存4线",
            batch=[*outbound_batch, *inbound_batch],
            steps=steps,
            reason=(
                f"vnext:{self.template_name};mode=source_repack_port_exchange;"
                f"inbound={len(inbound_nos)};outbound={len(desired_order)}"
            ),
            candidate_kind="vnext_depot_cun4_source_repack_exchange",
        )
        yield self._envelope(candidate, contract)

    def _source_repack_exchange_steps(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        outbound_batch: list[dict[str, Any]],
        desired_order: tuple[str, ...],
        inbound_batch: list[dict[str, Any]],
        inbound_nos: tuple[str, ...],
        target_by_no: dict[str, str],
        all_moving_nos: set[str],
    ) -> tuple[Any, ...]:
        segments = self._desired_source_segments(cars=cars, desired_order=desired_order)
        if not segments:
            return ()
        staging_lines = self._assign_repack_staging_lines(
            cars=cars,
            segments=segments,
            all_moving_nos=all_moving_nos,
            inbound_nos=inbound_nos,
            target_by_no=target_by_no,
        )
        if len(staging_lines) != len(segments):
            return ()
        inbound_positions = self._planned_inbound_positions(
            cars=cars,
            depot_assignment=depot_assignment,
            inbound_batch=inbound_batch,
            inbound_nos=inbound_nos,
            target_by_no=target_by_no,
            all_moving_nos=all_moving_nos,
        )
        if len(inbound_positions) != len(inbound_batch):
            return ()
        outbound_positions = planned_positions_for_batch(
            batch=outbound_batch,
            target_line="存4线",
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_moving_nos,
        )
        if len(outbound_positions) != len(outbound_batch):
            return ()

        working_cars = [dict(car) for car in cars]
        steps: list[Any] = []
        staged_segments: set[int] = set()

        def stage_segment(index: int) -> bool:
            if index in staged_segments:
                return True
            source_line, nos = segments[index]
            by_no = {physical.car_no(car): car for car in working_cars}
            access_order = physical.line_access_order(working_cars, source_line, set())
            if access_order[: len(nos)] != list(nos):
                return False
            batch = [by_no[no] for no in nos]
            staging_line = staging_lines[index]
            positions = planned_positions_for_batch(
                batch=batch,
                target_line=staging_line,
                cars=working_cars,
                depot_assignment=depot_assignment,
                batch_nos=set(nos),
            )
            if len(positions) != len(batch):
                return False
            steps.append(physical.plan_step("Get", source_line, nos))
            physical.apply_physical_get_order(working_cars, source_line, nos)
            steps.append(physical.plan_step("Put", staging_line, nos, positions))
            physical.apply_physical_put_order(working_cars, staging_line, list(nos), positions)
            staged_segments.add(index)
            return True

        steps.append(physical.plan_step("Get", "存4线", inbound_nos))
        physical.apply_physical_get_order(working_cars, "存4线", inbound_nos)
        remaining = list(inbound_nos)
        while remaining:
            target_line = target_by_no.get(remaining[-1], "")
            if target_line not in physical.DEPOT_TARGET_LINES:
                return ()
            start = len(remaining) - 1
            while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                start -= 1
            drop = tuple(remaining[start:])
            positions = {
                no: inbound_positions[no]
                for no in drop
                if no in inbound_positions
            }
            if len(positions) != len(drop):
                return ()
            blocking_segments = self._segments_blocking_target_positions(
                cars=working_cars,
                segments=segments,
                staged_segments=staged_segments,
                target_line=target_line,
                positions=set(positions.values()),
                all_moving_nos=all_moving_nos,
            )
            if blocking_segments is None:
                return ()
            for index in blocking_segments:
                if not stage_segment(index):
                    return ()
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            physical.apply_physical_put_order(working_cars, target_line, list(drop), positions)
            del remaining[start:]

        for index in range(len(segments)):
            if not stage_segment(index):
                return ()
        for index, (_source_line, nos) in enumerate(segments):
            steps.append(physical.plan_step("Get", staging_lines[index], nos))
            physical.apply_physical_get_order(working_cars, staging_lines[index], nos)
        steps.append(physical.plan_step("Put", "存4线", desired_order, outbound_positions))
        return tuple(steps)

    def _assign_repack_staging_lines(
        self,
        *,
        cars: list[dict[str, Any]],
        segments: tuple[tuple[str, tuple[str, ...]], ...],
        all_moving_nos: set[str],
        inbound_nos: tuple[str, ...],
        target_by_no: dict[str, str],
    ) -> dict[int, str]:
        by_no = {physical.car_no(car): car for car in cars}
        available = list(DEPOT_OUTBOUND_REPACK_STAGING_LINES)
        assigned: dict[int, str] = {}
        required_length_by_index = {
            index: sum(physical.car_length(by_no[no]) for no in nos)
            for index, (_source_line, nos) in enumerate(segments)
        }
        stage_order = sorted(
            range(len(segments)),
            key=lambda index: (
                self._segment_stage_rank(segments[index][0], inbound_nos, target_by_no),
                -required_length_by_index[index],
                index,
            ),
        )
        for index in stage_order:
            _source_line, nos = segments[index]
            required_length = required_length_by_index[index]
            for line in list(available):
                spec = physical.TRACK_SPECS[line]
                used = physical.line_length_load(cars, line, excluded_nos=all_moving_nos)
                if used + required_length <= spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                    assigned[index] = line
                    available.remove(line)
                    break
        if len(segments) >= 2 and (len(segments) - 1) in assigned:
            penultimate = len(segments) - 2
            tail = len(segments) - 1
            if self._segment_stage_rank(segments[penultimate][0], inbound_nos, target_by_no) > self._segment_stage_rank(
                segments[tail][0],
                inbound_nos,
                target_by_no,
            ):
                shared_line = assigned[tail]
                combined_length = sum(
                    physical.car_length(by_no[no])
                    for _source_line, nos in (segments[penultimate], segments[tail])
                    for no in nos
                )
                used = physical.line_length_load(cars, shared_line, excluded_nos=all_moving_nos)
                spec = physical.TRACK_SPECS[shared_line]
                if used + combined_length > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                    for line in list(available):
                        spec = physical.TRACK_SPECS[line]
                        used = physical.line_length_load(cars, line, excluded_nos=all_moving_nos)
                        if used + combined_length <= spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                            shared_line = line
                            assigned[tail] = line
                            available.remove(line)
                            break
                    used = physical.line_length_load(cars, shared_line, excluded_nos=all_moving_nos)
                    spec = physical.TRACK_SPECS[shared_line]
                if used + combined_length <= spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
                    assigned[penultimate] = shared_line
        return assigned

    def _segment_stage_rank(
        self,
        source_line: str,
        inbound_nos: tuple[str, ...],
        target_by_no: dict[str, str],
    ) -> int:
        remaining = list(inbound_nos)
        rank = 0
        while remaining:
            target_line = target_by_no.get(remaining[-1], "")
            start = len(remaining) - 1
            while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                start -= 1
            if target_line == source_line:
                return rank
            rank += 1
            del remaining[start:]
        return rank + len(inbound_nos)

    def _desired_source_segments(
        self,
        *,
        cars: list[dict[str, Any]],
        desired_order: tuple[str, ...],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        by_no = {physical.car_no(car): car for car in cars}
        segments: list[tuple[str, list[str]]] = []
        for no in desired_order:
            car = by_no.get(no)
            if car is None or car["Line"] not in self.source_order:
                return ()
            source_line = car["Line"]
            if segments and segments[-1][0] == source_line:
                segments[-1][1].append(no)
                continue
            segments.append((source_line, [no]))
        return tuple((line, tuple(nos)) for line, nos in segments)

    def _planned_inbound_positions(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        inbound_batch: list[dict[str, Any]],
        inbound_nos: tuple[str, ...],
        target_by_no: dict[str, str],
        all_moving_nos: set[str],
    ) -> dict[str, int]:
        no_to_car = {physical.car_no(car): car for car in inbound_batch}
        planned: dict[str, int] = {}
        for target_line in dict.fromkeys(target_by_no[no] for no in inbound_nos):
            if target_line not in physical.DEPOT_TARGET_LINES:
                return {}
            group = [no_to_car[no] for no in inbound_nos if target_by_no[no] == target_line]
            positions = planned_positions_for_batch(
                batch=group,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                batch_nos=all_moving_nos,
            )
            if len(positions) != len(group):
                return {}
            planned.update(positions)
        return planned

    def _segments_blocking_target_positions(
        self,
        *,
        cars: list[dict[str, Any]],
        segments: tuple[tuple[str, tuple[str, ...]], ...],
        staged_segments: set[int],
        target_line: str,
        positions: set[int],
        all_moving_nos: set[str],
    ) -> tuple[int, ...] | None:
        if not positions or target_line not in physical.DEPOT_TARGET_LINES:
            return ()
        segment_by_no = {
            no: index
            for index, (_source_line, nos) in enumerate(segments)
            if index not in staged_segments
            for no in nos
        }
        limit = max(positions)
        blocker_segments: list[int] = []
        for car in physical.line_cars_in_access_order(cars=cars, line=target_line):
            no = physical.car_no(car)
            segment_index = segment_by_no.get(no)
            if segment_index is not None:
                if segment_index not in blocker_segments:
                    blocker_segments.append(segment_index)
                continue
            if int(car.get("Position") or 0) <= limit and no not in all_moving_nos:
                return None
        return tuple(blocker_segments)


class DepotCun4InboundOutboundExchangeEpisode(DepotOutboundSessionEpisode):
    intent = IntentKind.CUN4_OUTBOUND_HOLD
    template_name = "depot_cun4_inbound_outbound_exchange"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        return contract.family in {ContractFamily.REMOTE_SESSION, ContractFamily.DEPOT_OUTBOUND}

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None or not strategic_plan.depot_inbound.assembly_complete:
            return
        if not strategic_plan.cun4_release.release_nos:
            return
        outbound_plan = strategic_plan.depot_outbound
        plan_order = tuple(outbound_plan.pull_order_nos)
        subject_nos = self._depot_outbound_exchange_subject_nos(
            contract=contract,
            strategic_plan=strategic_plan,
        )
        if not plan_order or not set(plan_order) <= subject_nos:
            return
        port_state = release.cun4_port_state(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
        )
        if port_state.dirty_nos:
            return
        loads = physical.line_loads(cars)
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        )
        release_order = tuple(port_state.release_nos)
        inbound_nos = tuple(physical.car_no(car) for car in line_cars[: len(release_order)])
        if not inbound_nos or inbound_nos != release_order:
            return
        release_nos = set(port_state.release_nos)
        target_by_no: dict[str, str] = {}
        inbound_batch: list[dict[str, Any]] = []
        for car in line_cars[: len(inbound_nos)]:
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in release_nos or no not in subject_nos:
                return
            if target_line not in physical.DEPOT_TARGET_LINES:
                return
            if car.get("IsWeigh"):
                return
            inbound_batch.append(car)
            target_by_no[no] = target_line

        all_moving_nos = set(plan_order) | set(inbound_nos)
        inbound_put_steps, blocker_nos = self._inbound_release_steps_and_blockers(
            cars=cars,
            depot_assignment=depot_assignment,
            inbound_batch=inbound_batch,
            inbound_nos=inbound_nos,
            target_by_no=target_by_no,
            all_moving_nos=all_moving_nos,
            plan_nos=set(plan_order),
        )
        if not inbound_put_steps:
            return

        if blocker_nos:
            prefix_collected = self._collect_outbound_get_steps_in_plan_order(
                cars=cars,
                plan_order=plan_order,
                stop_after_nos=set(blocker_nos),
            )
            if prefix_collected is None:
                return
            prefix_get_steps, prefix_batch = prefix_collected
            prefix_order = tuple(physical.car_no(car) for car in prefix_batch)
            if physical.pull_equivalent([*prefix_batch, *inbound_batch]) > physical.PULL_LIMIT_EQUIVALENT:
                return
            release_steps = (
                *prefix_get_steps,
                physical.plan_step("Get", "存4线", inbound_nos),
                *inbound_put_steps,
            )
        else:
            prefix_order = ()
            release_steps = (
                physical.plan_step("Get", "存4线", inbound_nos),
                *inbound_put_steps,
            )
        collected = self._collect_outbound_get_steps_in_plan_order(
            cars=cars,
            plan_order=plan_order,
            initial_order=prefix_order,
            initial_steps=release_steps,
        )
        if collected is None:
            return
        steps_before_final_put, outbound_batch = collected
        if not set(blocker_nos) <= {physical.car_no(car) for car in outbound_batch}:
            return

        outbound_positions = planned_positions_for_batch(
            batch=outbound_batch,
            target_line="存4线",
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=all_moving_nos,
        )
        if len(outbound_positions) != len(outbound_batch):
            return
        steps = (
            *steps_before_final_put,
            physical.plan_step("Put", "存4线", plan_order, outbound_positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            candidate_kind="vnext_depot_cun4_inbound_outbound_exchange",
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line="存4线",
            batch=[*outbound_batch, *inbound_batch],
            steps=steps,
            reason=(
                f"vnext:{self.template_name};mode=complete_port_exchange;"
                f"inbound={len(inbound_nos)};outbound={len(plan_order)};"
            f"blockers={','.join(blocker_nos)}"
            ),
            candidate_kind="vnext_depot_cun4_inbound_outbound_exchange",
        )
        yield self._envelope(candidate, contract)

    def _inbound_release_steps_and_blockers(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        inbound_batch: list[dict[str, Any]],
        inbound_nos: tuple[str, ...],
        target_by_no: dict[str, str],
        all_moving_nos: set[str],
        plan_nos: set[str],
    ) -> tuple[tuple[Any, ...], tuple[str, ...]]:
        no_to_car = {physical.car_no(car): car for car in inbound_batch}
        remaining = list(inbound_nos)
        steps: list[Any] = []
        blocker_nos: list[str] = []
        while remaining:
            target_line = target_by_no.get(remaining[-1], "")
            if target_line not in physical.DEPOT_TARGET_LINES:
                return (), ()
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
                batch_nos=all_moving_nos,
            )
            if len(positions) != len(group):
                return (), ()
            occupants = physical.target_position_occupants(
                cars,
                target_line,
                set(positions.values()),
                set(inbound_nos),
            )
            for occupant in occupants:
                occupant_no = physical.car_no(occupant)
                if occupant_no not in plan_nos:
                    return (), ()
                if occupant_no not in blocker_nos:
                    blocker_nos.append(occupant_no)
            for blocker_no in self._target_line_outbound_blockers(
                cars=cars,
                target_line=target_line,
                plan_nos=plan_nos,
            ):
                if blocker_no not in blocker_nos:
                    blocker_nos.append(blocker_no)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        return tuple(steps), tuple(blocker_nos)

    def _target_line_outbound_blockers(
        self,
        *,
        cars: list[dict[str, Any]],
        target_line: str,
        plan_nos: set[str],
    ) -> tuple[str, ...]:
        if target_line not in physical.DEPOT_TARGET_LINES:
            return ()
        blocker_lines = [target_line]
        outer_line = physical.DEPOT_INNER_BLOCKERS.get(target_line)
        if outer_line:
            blocker_lines.append(outer_line)
        blockers: list[str] = []
        for line in blocker_lines:
            for car in physical.line_cars_in_access_order(cars=cars, line=line):
                no = physical.car_no(car)
                if no in plan_nos and no not in blockers:
                    blockers.append(no)
        return tuple(blockers)


class DepotOutboundOverflowReleaseEpisode(Episode):
    intent = IntentKind.CUN4_OUTBOUND_HOLD
    template_name = "depot_outbound_overflow_release"
    frontier = AccessFrontier()

    def applies(self, contract: FlowContract) -> bool:
        if not contract.source_lines or not contract.target_lines:
            return False
        source_line = contract.source_lines[0]
        target_line = contract.target_lines[0]
        return (
            source_line in depot_outbound_plan.OVERFLOW_ASSEMBLY_LINES
            and target_line
            and target_line != source_line
            and target_line not in physical.DEPOT_TARGET_LINES
        )

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
            planned_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in contract_nos or planned_line != target_line:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        if not batch:
            return
        moving_nos = {physical.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
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
            target_line=target_line,
            batch=batch,
            cars=cars,
            depot_assignment=depot_assignment,
            reason=(
                f"vnext:{self.template_name};owner_contract={contract.contract_id};"
                f"target={target_line};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_outbound_overflow_release",
            planned_positions=positions,
        )
        if candidate is None:
            return
        if not self.frontier.direct_move_is_reachable(
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
            return
        yield self._envelope(candidate, contract)


DEPOT_INBOUND_SOURCE_PRIORITY = (
    "洗罐线北",
    "洗罐站",
    "油漆线",
    "抛丸线",
    "卸轮线",
    "存5线北",
    "存5线南",
    "存4南",
    "存3线",
    "存2线",
    "存1线",
    "调梁棚",
    "调梁线北",
    "机库线",
    "预修线",
)
DEPOT_INBOUND_ROUTE_BLOCKERS_BY_SOURCE = {
    "调梁棚": ("调梁线北",),
    "洗罐站": ("洗罐线北",),
    "洗罐线北": ("洗油北",),
    "油漆线": ("洗油北",),
    "抛丸线": ("机南",),
    "存5线南": ("机南", "存5线北"),
    "存5线北": ("机走棚",),
}
DEPOT_INBOUND_ROUTE_BLOCKER_PRIORITY = (
    "调梁线北",
    "洗罐线北",
    "洗油北",
    "机走棚",
    "机南",
    "存5线北",
)


def _depot_inbound_source_lines(plan_lines: tuple[str, ...], contract_lines: tuple[str, ...]) -> tuple[str, ...]:
    contract_set = set(contract_lines)
    lines = [line for line in DEPOT_INBOUND_SOURCE_PRIORITY if line in contract_set and line in plan_lines]
    lines.extend(line for line in plan_lines if line in contract_set and line not in lines)
    return tuple(lines)


def _depot_inbound_source_rank(line: str) -> int:
    try:
        return DEPOT_INBOUND_SOURCE_PRIORITY.index(line)
    except ValueError:
        return len(DEPOT_INBOUND_SOURCE_PRIORITY)


def _depot_inbound_route_blocker_lines(source_lines: tuple[str, ...]) -> tuple[str, ...]:
    lines: list[str] = []
    for source_line in source_lines:
        lines.extend(DEPOT_INBOUND_ROUTE_BLOCKERS_BY_SOURCE.get(source_line, ()))
        lines.extend(physical.SERIAL_LINE_BLOCKERS.get(source_line, ()))
    priority = {line: index for index, line in enumerate(DEPOT_INBOUND_ROUTE_BLOCKER_PRIORITY)}
    return tuple(
        sorted(
            dict.fromkeys(lines),
            key=lambda line: (priority.get(line, len(priority)), line),
        )
    )


def _depot_inbound_candidate_score(
    *,
    candidate: Any,
    pending_nos: set[str],
) -> tuple[int, int, int, int, int, int, str]:
    put_steps = [
        step
        for step in physical.candidate_plan_steps(candidate)
        if step.action == "Put"
    ]
    grouped_count = sum(
        1
        for step in put_steps
        for no in step.move_car_nos
        if no in pending_nos and step.line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
    )
    non_grouped_count = len(set(candidate.move_car_nos) - pending_nos)
    return (
        -grouped_count,
        _depot_inbound_assembly_stage_rank(put_steps),
        len(put_steps),
        non_grouped_count,
        len(candidate.move_car_nos),
        _depot_inbound_source_rank(candidate.source_line),
        candidate.candidate_id,
    )


def _depot_inbound_assembly_stage_rank(put_steps: list[Any]) -> int:
    priority = {
        "存4线": 0,
        "机南": 1,
        "洗油北": 1,
        "机走棚": 2,
        "机走北": 3,
    }
    ranks = [
        priority.get(step.line, 9)
        for step in put_steps
        if step.line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
    ]
    return max(ranks) if ranks else 9


def _depot_inbound_lifo_safe_prefix(
    batch: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    planned_lines: dict[str, str],
) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    previous_rank: int | None = None
    previous_target = ""
    for car in batch:
        target_line = planned_lines.get(physical.car_no(car), "")
        rank = _depot_inbound_put_rank(target_line)
        if previous_rank is not None and target_line != previous_target and rank > previous_rank:
            break
        safe.append(car)
        previous_rank = rank
        previous_target = target_line
    return safe


def _depot_inbound_put_rank(line: str) -> int:
    priority = {
        "机南": 0,
        "洗油北": 0,
        "机走棚": 1,
        "机走北": 2,
        "存4线": 2,
    }
    return priority.get(line, 9)


def _depot_inbound_stepwise_put_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    source_line: str,
    batch: tuple[dict[str, Any], ...],
    target_by_no: dict[str, str],
) -> tuple[tuple[Any, ...], tuple[str, ...]] | None:
    remaining = [physical.car_no(car) for car in batch]
    if not remaining:
        return None
    if any(no not in target_by_no or not target_by_no[no] for no in remaining):
        return None
    no_to_car = {physical.car_no(car): car for car in batch}
    planning_cars = [dict(car) for car in cars]
    physical.apply_physical_get_order(planning_cars, source_line, tuple(remaining))
    steps: list[Any] = [physical.plan_step("Get", source_line, tuple(remaining))]
    put_lines: list[str] = []
    while remaining:
        release_drop = _depot_inbound_next_release_drop(
            remaining=remaining,
            target_by_no=target_by_no,
            no_to_car=no_to_car,
            cars=planning_cars,
            depot_assignment=depot_assignment,
        )
        if release_drop is None:
            return None
        start, target_line, drop, positions = release_drop
        put_lines.append(target_line)
        steps.append(physical.plan_step("Put", target_line, drop, positions))
        physical.apply_physical_put_order(planning_cars, target_line, list(drop), positions)
        del remaining[start:]
    return tuple(steps), tuple(put_lines)


def _depot_inbound_multisource_stepwise_put_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    source_batches: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
    target_by_no: dict[str, str],
) -> tuple[tuple[Any, ...], tuple[str, ...], tuple[dict[str, Any], ...]] | None:
    if len(source_batches) < 2:
        return None
    planning_cars = [dict(car) for car in cars]
    steps: list[Any] = []
    remaining: list[str] = []
    carry: list[dict[str, Any]] = []
    no_to_car: dict[str, dict[str, Any]] = {}
    for source_line, batch in source_batches:
        nos = tuple(physical.car_no(car) for car in batch)
        if not nos:
            return None
        if any(no not in target_by_no or not target_by_no[no] for no in nos):
            return None
        physical.apply_physical_get_order(planning_cars, source_line, nos)
        steps.append(physical.plan_step("Get", source_line, nos))
        remaining.extend(nos)
        carry.extend(batch)
        no_to_car.update({physical.car_no(car): car for car in batch})

    put_lines: list[str] = []
    deferred: list[tuple[str, tuple[str, ...], str]] = []
    while remaining:
        release_drop = _depot_inbound_next_release_drop(
            remaining=remaining,
            target_by_no=target_by_no,
            no_to_car=no_to_car,
            cars=planning_cars,
            depot_assignment=depot_assignment,
        )
        if release_drop is None:
            return None
        start, target_line, drop, positions = release_drop
        group = [no_to_car[no] for no in drop]
        blocking_inner = _depot_inner_blocked_by_outer_target(target_line)
        if blocking_inner and any(target_by_no.get(no) == blocking_inner for no in remaining[:start]):
            staging_line = "存4线"
            positions = planned_positions_for_batch(
                batch=group,
                target_line=staging_line,
                cars=planning_cars,
                depot_assignment=depot_assignment,
                batch_nos=set(drop),
            )
            if len(positions) != len(group):
                return None
            put_lines.append(staging_line)
            steps.append(physical.plan_step("Put", staging_line, drop, positions))
            physical.apply_physical_put_order(planning_cars, staging_line, list(drop), positions)
            del remaining[start:]
            deferred.append((target_line, drop, staging_line))
            continue
        put_lines.append(target_line)
        steps.append(physical.plan_step("Put", target_line, drop, positions))
        physical.apply_physical_put_order(planning_cars, target_line, list(drop), positions)
        del remaining[start:]
        ready_deferred: list[tuple[str, tuple[str, ...], str]] = []
        for item in deferred:
            deferred_target, _deferred_drop, _staging_line = item
            blocking_inner = _depot_inner_blocked_by_outer_target(deferred_target)
            if not blocking_inner or not any(target_by_no.get(no) == blocking_inner for no in remaining):
                ready_deferred.append(item)
        for item in ready_deferred:
            deferred.remove(item)
            deferred_target, deferred_drop, staging_line = item
            deferred_group = [no_to_car[no] for no in deferred_drop]
            steps.append(physical.plan_step("Get", staging_line, deferred_drop))
            physical.apply_physical_get_order(planning_cars, staging_line, deferred_drop)
            remaining.extend(deferred_drop)
            positions = _depot_inbound_release_positions_for_batch(
                batch=deferred_group,
                target_line=deferred_target,
                cars=planning_cars,
                depot_assignment=depot_assignment,
                batch_nos=set(remaining),
            )
            if len(positions) != len(deferred_group):
                return None
            put_lines.append(deferred_target)
            steps.append(physical.plan_step("Put", deferred_target, deferred_drop, positions))
            physical.apply_physical_put_order(planning_cars, deferred_target, list(deferred_drop), positions)
            del remaining[-len(deferred_drop):]
    if deferred:
        return None
    return tuple(steps), tuple(put_lines), tuple(carry)


def _depot_inbound_next_release_drop(
    *,
    remaining: list[str],
    target_by_no: dict[str, str],
    no_to_car: dict[str, dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
) -> tuple[int, str, tuple[str, ...], dict[str, int]] | None:
    target_line = target_by_no[remaining[-1]]
    same_target_start = len(remaining) - 1
    while same_target_start > 0 and target_by_no.get(remaining[same_target_start - 1]) == target_line:
        same_target_start -= 1
    for start in range(same_target_start, len(remaining)):
        drop = tuple(remaining[start:])
        group = [no_to_car[no] for no in drop]
        positions = _depot_inbound_release_positions_for_batch(
            batch=group,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=set(drop),
        )
        if len(positions) == len(group):
            return start, target_line, drop, positions
    return None


def _depot_inbound_release_positions_for_batch(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    batch_nos: set[str],
) -> dict[str, int]:
    if target_line in physical.DEPOT_LINES:
        occupied = {
            int(car.get("Position") or 0)
            for car in cars
            if car["Line"] == target_line and physical.car_no(car) not in batch_nos
        }
        locked_tail = physical.depot_locked_tail_positions(cars, target_line, depot_assignment)
        slot_positions = [
            int(slot.position)
            for car in batch
            for slot in [depot_assignment.slots.get(physical.car_no(car))]
            if slot is not None and slot.line == target_line
        ]
        minimum_position = max(
            [max(occupied or {0}) + len(batch), *slot_positions]
            or [len(batch)]
        )
        capacity = physical.depot_line_capacity(
            depot_assignment,
            target_line,
            minimum_position=minimum_position,
        )
        best: tuple[tuple[int, int], dict[str, int]] | None = None
        for start in range(1, max(capacity - len(batch) + 2, 1)):
            planned = {
                physical.car_no(car): start + index
                for index, car in enumerate(batch)
            }
            positions = set(planned.values())
            if positions & occupied or positions & locked_tail:
                continue
            if len(positions) != len(batch):
                continue
            if not _depot_inbound_release_positions_allowed(
                batch=batch,
                target_line=target_line,
                cars=cars,
                depot_assignment=depot_assignment,
                planned=planned,
                capacity=capacity,
            ):
                continue
            slot_deviation = 0
            for car in batch:
                no = physical.car_no(car)
                slot = depot_assignment.slots.get(no)
                if slot is not None and slot.line == target_line:
                    slot_deviation += abs(planned[no] - int(slot.position))
            score = (slot_deviation, start)
            if best is None or score < best[0]:
                best = (score, planned)
        return best[1] if best is not None else {}
    return planned_positions_for_batch(
        batch=batch,
        target_line=target_line,
        cars=cars,
        depot_assignment=depot_assignment,
        batch_nos=batch_nos,
    )


def _depot_inbound_release_positions_allowed(
    *,
    batch: list[dict[str, Any]],
    target_line: str,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    planned: dict[str, int],
    capacity: int,
) -> bool:
    projected = [dict(car) for car in cars]
    projected_by_no = {physical.car_no(car): car for car in projected}
    for car in batch:
        no = physical.car_no(car)
        position = planned[no]
        slot = depot_assignment.slots.get(no)
        if slot is not None and slot.line != target_line:
            return False
        if slot is not None and slot.locked and int(slot.position) != position:
            return False
        if not physical.depot_actual_position_allowed(car, target_line, position, capacity):
            return False
        projected_car = projected_by_no.get(no)
        if projected_car is None:
            projected_car = dict(car)
            projected.append(projected_car)
            projected_by_no[no] = projected_car
        projected_car["Line"] = target_line
        projected_car["Position"] = position
    for car in projected:
        if car["Line"] != target_line:
            continue
        position = int(car.get("Position") or 0)
        if not physical.depot_section_repair_position_allowed(car, target_line, position, projected):
            return False
    return True


def _depot_inner_blocked_by_outer_target(target_line: str) -> str:
    if target_line not in physical.DEPOT_OUTSIDE_LINES:
        return ""
    return target_line.replace("库外", "库内")


class DepotInboundMultiSourceAssemblySessionEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_multisource_assembly_session"
    frontier = AccessFrontier()
    min_source_count = 2
    min_grouped_count = 2
    max_source_count = 4

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        planned_lines = plan.temporary_line_by_no
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        if not pending_nos or not planned_lines:
            return
        dirty_lines = set(plan.purity_violation_lines)
        source_batches: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        carry: list[dict[str, Any]] = []
        best: tuple[tuple[int, int, int, int, int, int, str], Any] | None = None
        for source_line in _depot_inbound_source_lines(plan.source_lines, contract.source_lines):
            batch = self._source_prefix(
                cars=cars,
                graph=graph,
                loco_location=loco_location,
                source_line=source_line,
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=dirty_lines,
            )
            if not batch:
                continue
            if physical.pull_equivalent([*carry, *batch]) > physical.PULL_LIMIT_EQUIVALENT:
                continue
            source_batches.append((source_line, batch))
            carry.extend(batch)
            if len(source_batches) > self.max_source_count:
                break
            if len(source_batches) < self.min_source_count or len(carry) < self.min_grouped_count:
                continue
            candidate = self._validated_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_batches=tuple(source_batches),
                planned_lines=planned_lines,
            )
            if candidate is None:
                continue
            score = _depot_inbound_candidate_score(candidate=candidate, pending_nos=pending_nos)
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            yield self._envelope(best[1], contract)

    def _source_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        graph: Any,
        loco_location: Any,
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
    ) -> tuple[dict[str, Any], ...]:
        if source_line in physical.DEPOT_TARGET_LINES or source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return ()
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line = planned_lines.get(no, "")
            if no not in pending_nos or not target_line:
                break
            if target_line in dirty_lines:
                break
            if car.get("IsWeigh"):
                break
            batch.append(car)
        return tuple(batch)

    def _validated_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_batches: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
        planned_lines: dict[str, str],
    ) -> Any | None:
        target_by_no = {
            physical.car_no(car): planned_lines[physical.car_no(car)]
            for _source_line, batch in source_batches
            for car in batch
        }
        plan = _depot_inbound_multisource_stepwise_put_plan(
            cars=cars,
            depot_assignment=depot_assignment,
            source_batches=source_batches,
            target_by_no=target_by_no,
        )
        if plan is None:
            return None
        plan_steps, put_lines, carry = plan
        if set(put_lines) == {"存4线"}:
            return None
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_multisource_assembly_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=plan_steps[0].line,
            target_line=plan_steps[-1].line,
            batch=list(carry),
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};"
                f"sources={','.join(source_line for source_line, _batch in source_batches)};"
                f"put_lines={','.join(put_lines)};batch={len(carry)}"
            ),
            candidate_kind="vnext_depot_inbound_multisource_assembly_session",
        )


class DepotInboundAssemblySessionEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_assembly_session"
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        planned_lines = plan.temporary_line_by_no
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        if not pending_nos or not planned_lines:
            return
        candidates: list[tuple[tuple[int, int, int, int, int, int, str], Any]] = []
        for source_line in _depot_inbound_source_lines(plan.source_lines, contract.source_lines):
            candidate = self._candidate_for_source(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_line=source_line,
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=set(plan.purity_violation_lines),
            )
            if candidate is None:
                continue
            score = _depot_inbound_candidate_score(candidate=candidate, pending_nos=pending_nos)
            candidates.append((score, candidate))
        for _score, candidate in sorted(candidates, key=lambda item: item[0]):
            yield self._envelope(candidate, contract)

    def _candidate_for_source(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
    ) -> Any | None:
        if source_line in physical.DEPOT_TARGET_LINES or source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return None
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        batch: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        last_target = ""
        for car in line_cars:
            no = physical.car_no(car)
            target_line = planned_lines.get(no, "")
            if no not in pending_nos or not target_line:
                break
            if target_line in dirty_lines:
                break
            if car.get("IsWeigh"):
                break
            if target_line in seen_targets and target_line != last_target:
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
            seen_targets.add(target_line)
            last_target = target_line
        batch = _depot_inbound_lifo_safe_prefix(batch, planned_lines)
        if not batch:
            return None
        return self._validated_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            source_line=source_line,
            batch=batch,
            planned_lines=planned_lines,
        )

    def _validated_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        batch: list[dict[str, Any]],
        planned_lines: dict[str, str],
    ) -> Any | None:
        moving_nos = {physical.car_no(car) for car in batch}
        no_to_car = {physical.car_no(car): car for car in batch}
        remaining = [physical.car_no(car) for car in batch]
        steps = [physical.plan_step("Get", source_line, tuple(remaining))]
        put_lines: list[str] = []
        while remaining:
            target_line = planned_lines[remaining[-1]]
            if target_line in put_lines:
                return None
            start = len(remaining) - 1
            while start > 0 and planned_lines.get(remaining[start - 1]) == target_line:
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
                return None
            put_lines.append(target_line)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_assembly_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=plan_steps[-1].line,
            batch=batch,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};source={source_line};"
                f"put_lines={','.join(put_lines)};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_assembly_session",
        )


class DepotInboundAssemblyRebalanceEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_assembly_rebalance"
    frontier = AccessFrontier()
    source_lines = physical.DEPOT_INBOUND_ASSEMBLY_LINES

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        if not plan.ungrouped_nos:
            return
        inbound_nos = set(plan.inbound_nos)
        ungrouped_nos = set(plan.ungrouped_nos)
        planned_lines = plan.temporary_line_by_no
        contract_nos = set(contract.subject_nos)
        for source_line in self.source_lines:
            downstream_debt = serial.downstream_debt_nos(
                blocker_line=source_line,
                cars=cars,
                depot_assignment=depot_assignment,
                moving_nos=set(),
            )
            line_cars = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            if not line_cars:
                continue
            if any(physical.car_no(car) not in inbound_nos for car in line_cars):
                continue
            line_nos = {physical.car_no(car) for car in line_cars}
            contract_downstream_debt = set(downstream_debt) & contract_nos
            if not ((line_nos & contract_nos) or contract_downstream_debt):
                continue
            line_has_ungrouped = bool(line_nos & ungrouped_nos)
            planned_targets = {
                planned_lines.get(no, "")
                for no in line_nos
            }
            planned_targets.discard("")
            if len(planned_targets) != 1:
                continue
            preferred_target = next(iter(planned_targets))
            if not downstream_debt and not (line_nos & ungrouped_nos):
                continue
            if (
                len(line_cars) == 1
                and source_line != "存4线"
                and preferred_target != "存4线"
                and not contract_downstream_debt
            ):
                continue
            if physical.pull_equivalent(line_cars) > physical.PULL_LIMIT_EQUIVALENT:
                continue
            strict_cun4_checkpoint = not strategic_plan.depot_inbound_assembly_accepted
            for target_line in self._target_lines(
                source_line=source_line,
                preferred_target=preferred_target,
                has_depot_outbound_debt=bool(strategic_plan.depot_outbound.outbound_nos),
                cun4_vehicle_budget=plan.cun4_vehicle_budget,
                existing_cun4_inbound_count=self._existing_cun4_inbound_count(cars=cars, inbound_nos=inbound_nos),
                moving_count=len(line_cars),
                dirty_lines=set(plan.purity_violation_lines),
                strict_cun4_checkpoint=strict_cun4_checkpoint,
            ):
                if (
                    not line_has_ungrouped
                    and not self._stable_route_clear_target_allowed(
                        source_line=source_line,
                        target_line=target_line,
                    )
                ):
                    continue
                candidate = self._candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases or {},
                    source_line=source_line,
                    target_line=target_line,
                    batch=line_cars,
                )
                if candidate is None:
                    continue
                yield self._envelope(candidate, contract)
                return

    def _stable_route_clear_target_allowed(self, *, source_line: str, target_line: str) -> bool:
        inward_targets = {
            "机走棚": {"机南", "洗油北"},
        }
        return target_line in inward_targets.get(source_line, set())

    def _target_lines(
        self,
        *,
        source_line: str,
        preferred_target: str,
        has_depot_outbound_debt: bool,
        cun4_vehicle_budget: int,
        existing_cun4_inbound_count: int,
        moving_count: int,
        dirty_lines: set[str],
        strict_cun4_checkpoint: bool,
    ) -> tuple[str, ...]:
        if source_line == "机走棚":
            ordered = ("机南", "洗油北", "机走北", "存4线")
        elif source_line == "机南":
            ordered = ("机走棚", "机走北", "洗油北", "存4线")
        elif source_line == "机走北":
            ordered = ("洗油北", "机走棚", "机南", "存4线")
        elif source_line == "洗油北":
            ordered = ("机走北", "机走棚", "机南", "存4线")
        else:
            ordered = ("机南", "机走棚", "机走北", "洗油北", "存4线")
        if not has_depot_outbound_debt:
            ordered = ("存4线", *tuple(line for line in ordered if line != "存4线"))
        ordered = (preferred_target, *tuple(line for line in ordered if line != preferred_target))
        output: list[str] = []
        for line in ordered:
            if line == source_line or line in dirty_lines:
                continue
            if strict_cun4_checkpoint and line == "存4线" and preferred_target != "存4线":
                continue
            if (
                has_depot_outbound_debt
                and line == "存4线"
                and existing_cun4_inbound_count + moving_count > cun4_vehicle_budget
            ):
                continue
            output.append(line)
        return tuple(output)

    def _existing_cun4_inbound_count(
        self,
        *,
        cars: list[dict[str, Any]],
        inbound_nos: set[str],
    ) -> int:
        return sum(
            1
            for car in cars
            if car["Line"] == "存4线" and physical.car_no(car) in inbound_nos
        )

    def _candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        target_line: str,
        batch: list[dict[str, Any]],
    ) -> Any | None:
        if source_line == target_line:
            return None
        moving_nos = {physical.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(batch):
            return None
        move_nos = tuple(physical.car_no(car) for car in batch)
        steps = (
            physical.plan_step("Get", source_line, move_nos),
            physical.plan_step("Put", target_line, move_nos, positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_assembly_rebalance",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            batch=batch,
            steps=steps,
            reason=(
                f"vnext:{self.template_name};source={source_line};"
                f"target={target_line};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_assembly_rebalance",
        )


class DepotInboundRouteBlockerDigestEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_route_blocker_digest"
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        planned_lines = plan.temporary_line_by_no
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        if not pending_nos:
            return
        source_lines = self._pending_source_lines(cars=cars, pending_nos=pending_nos)
        if not source_lines:
            return
        loads = physical.line_loads(cars)
        best: tuple[tuple[int, int, str], Any] | None = None
        for blocker_line in _depot_inbound_route_blocker_lines(source_lines):
            candidate = self._candidate_for_blocker(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                blocker_line=blocker_line,
                pending_nos=pending_nos,
                active_source_lines=set(source_lines),
                planned_lines=planned_lines,
                dirty_lines=set(plan.purity_violation_lines),
                loads=loads,
            )
            if candidate is None:
                continue
            score = (
                _depot_inbound_route_blocker_rank(blocker_line),
                len(candidate.move_car_nos),
                candidate.candidate_id,
            )
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            yield self._envelope(best[1], contract)

    def _pending_source_lines(
        self,
        *,
        cars: list[dict[str, Any]],
        pending_nos: set[str],
    ) -> tuple[str, ...]:
        source_by_no = {
            physical.car_no(car): car["Line"]
            for car in cars
            if physical.car_no(car) in pending_nos
        }
        ordered = [
            line
            for line in DEPOT_INBOUND_SOURCE_PRIORITY
            if line in set(source_by_no.values())
        ]
        ordered.extend(
            line
            for line in source_by_no.values()
            if line not in ordered
        )
        return tuple(dict.fromkeys(ordered))

    def _candidate_for_blocker(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        blocker_line: str,
        pending_nos: set[str],
        active_source_lines: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        loads: Any,
    ) -> Any | None:
        line_cars = tuple(
            physical.line_cars_in_access_order(
                cars=cars,
                line=blocker_line,
                graph=graph,
                loco_location=loco_location,
            )
        )
        if not line_cars:
            return None
        if any(car.get("IsWeigh") for car in line_cars):
            return None
        if physical.pull_equivalent(line_cars) > physical.PULL_LIMIT_EQUIVALENT:
            return None
        target_by_no = self._target_by_no(
            cars=cars,
            depot_assignment=depot_assignment,
            batch=line_cars,
            source_line=blocker_line,
            pending_nos=pending_nos,
            active_source_lines=active_source_lines,
            planned_lines=planned_lines,
            dirty_lines=dirty_lines,
            loads=loads,
        )
        if len(target_by_no) != len(line_cars):
            return None
        plan = _depot_inbound_stepwise_put_plan(
            cars=cars,
            depot_assignment=depot_assignment,
            source_line=blocker_line,
            batch=line_cars,
            target_by_no=target_by_no,
        )
        if plan is None:
            return None
        plan_steps, put_lines = plan
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_route_blocker_digest",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=blocker_line,
            target_line=plan_steps[-1].line,
            batch=list(line_cars),
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};source={blocker_line};"
                f"put_lines={','.join(put_lines)};batch={len(line_cars)}"
            ),
            candidate_kind="vnext_depot_inbound_route_blocker_digest",
        )

    def _target_by_no(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        batch: tuple[dict[str, Any], ...],
        source_line: str,
        pending_nos: set[str],
        active_source_lines: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        loads: Any,
    ) -> dict[str, str]:
        output: dict[str, str] = {}
        pending_source_lines = self._pending_source_lines(cars=cars, pending_nos=pending_nos)
        for car in batch:
            no = physical.car_no(car)
            if no in planned_lines:
                target_line = planned_lines[no]
                if not target_line or target_line in dirty_lines:
                    return {}
                output[no] = target_line
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if (
                not target_line
                or target_line == source_line
                or target_line in physical.RUNNING_LINES
                or target_line in physical.DEPOT_INBOUND_DESTINATION_LINES
                or target_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
                or (target_line in active_source_lines and target_line in pending_source_lines)
            ):
                return {}
            output[no] = target_line
        return output


def _depot_inbound_route_blocker_rank(line: str) -> int:
    try:
        return DEPOT_INBOUND_ROUTE_BLOCKER_PRIORITY.index(line)
    except ValueError:
        return len(DEPOT_INBOUND_ROUTE_BLOCKER_PRIORITY)


class DepotInboundPrefixAssemblySessionEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_prefix_assembly_session"
    frontier = AccessFrontier()
    min_target_batch = 1
    max_blocker_target_ratio = 2

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        planned_lines = plan.temporary_line_by_no
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        if not pending_nos or not planned_lines:
            return
        best: tuple[tuple[int, int, int, int, int, int, str], Any] | None = None
        for source_line in _depot_inbound_source_lines(plan.source_lines, contract.source_lines):
            candidate = self._candidate_for_source(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_line=source_line,
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=set(plan.purity_violation_lines),
            )
            if candidate is None:
                continue
            score = _depot_inbound_candidate_score(candidate=candidate, pending_nos=pending_nos)
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            yield self._envelope_with_source_return(best[1], contract)

    def _candidate_for_source(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
    ) -> Any | None:
        if source_line in physical.DEPOT_TARGET_LINES or source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return None
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        blockers: list[dict[str, Any]] = []
        target_batch: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        last_target = ""
        saw_target = False
        for car in line_cars:
            no = physical.car_no(car)
            target_line = planned_lines.get(no, "")
            if not saw_target:
                if no not in pending_nos or not target_line:
                    if car.get("IsWeigh"):
                        return None
                    if physical.pull_equivalent([*blockers, car]) > physical.PULL_LIMIT_EQUIVALENT:
                        return None
                    blockers.append(car)
                    continue
                saw_target = True
            if no not in pending_nos or not target_line:
                break
            if target_line in dirty_lines:
                break
            if car.get("IsWeigh"):
                break
            if target_line in seen_targets and target_line != last_target:
                break
            if physical.pull_equivalent([*blockers, *target_batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            target_batch.append(car)
            seen_targets.add(target_line)
            last_target = target_line
        target_batch = _depot_inbound_lifo_safe_prefix(target_batch, planned_lines)
        if not blockers or len(target_batch) < self.min_target_batch:
            return None
        if len(blockers) > len(target_batch) * self.max_blocker_target_ratio:
            return None
        return self._validated_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            source_line=source_line,
            blockers=blockers,
            target_batch=target_batch,
            planned_lines=planned_lines,
        )

    def _validated_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        blockers: list[dict[str, Any]],
        target_batch: list[dict[str, Any]],
        planned_lines: dict[str, str],
    ) -> Any | None:
        moving_nos = {physical.car_no(car) for car in [*blockers, *target_batch]}
        no_to_car = {physical.car_no(car): car for car in target_batch}
        target_nos = [physical.car_no(car) for car in target_batch]
        blocker_nos = tuple(physical.car_no(car) for car in blockers)
        steps = [physical.plan_step("Get", source_line, tuple([*blocker_nos, *target_nos]))]
        remaining = list(target_nos)
        put_lines: list[str] = []
        while remaining:
            target_line = planned_lines[remaining[-1]]
            if target_line in put_lines:
                return None
            start = len(remaining) - 1
            while start > 0 and planned_lines.get(remaining[start - 1]) == target_line:
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
                return None
            put_lines.append(target_line)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
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
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_prefix_assembly_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=source_line,
            batch=[*blockers, *target_batch],
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};source={source_line};"
                f"blockers={','.join(blocker_nos)};put_lines={','.join(put_lines)};"
                f"batch={len(target_batch)}"
            ),
            candidate_kind="vnext_depot_inbound_prefix_assembly_session",
        )

    def _envelope_with_source_return(self, candidate: Any, contract: FlowContract) -> CandidateEnvelope:
        restored_nos = tuple(candidate.plan_steps[-1].move_car_nos)
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line=candidate.source_line,
            target_line=candidate.target_line,
            move_nos=tuple(candidate.move_car_nos),
            intent=self.intent,
            same_plan_source_return_nos=restored_nos,
        )
        return CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=self.intent,
            resource_request=request,
            template_name=self.template_name,
        )


class DepotInboundDirtyCleanoutEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_dirty_cleanout"
    frontier = AccessFrontier()
    cun4_holding_lines = (
        "存3线",
        "存2线",
        "存1线",
        "存5线南",
        "存5线北",
        "调梁线北",
        "油漆线",
        "洗罐站",
        "机库线",
        "预修线",
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        dirty_nos = set(plan.purity_violation_nos)
        if not dirty_nos:
            return
        loads = physical.line_loads(cars)
        for dirty_line in plan.purity_violation_lines:
            batch = self._dirty_prefix(
                cars=cars,
                dirty_line=dirty_line,
                dirty_nos=dirty_nos,
            )
            if not batch:
                continue
            for target_line in self._target_lines(
                cars=cars,
                depot_assignment=depot_assignment,
                loads=loads,
                dirty_line=dirty_line,
                batch=batch,
            ):
                candidate = self._candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases or {},
                    dirty_line=dirty_line,
                    target_line=target_line,
                    batch=batch,
                )
                if candidate is None:
                    continue
                yield self._envelope(candidate, contract)

    def _dirty_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        dirty_line: str,
        dirty_nos: set[str],
    ) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(cars=cars, line=dirty_line):
            no = physical.car_no(car)
            if no not in dirty_nos:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _target_lines(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        loads: Any,
        dirty_line: str,
        batch: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        planned_targets: list[str] = []
        for car in batch:
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            planned_targets.append(target_line)
        targets: list[str] = []
        if len(set(planned_targets)) == 1 and self._final_target_allowed(planned_targets[0], dirty_line=dirty_line):
            targets.append(planned_targets[0])
        elif set(planned_targets) == {"存4线"} and self._cun4_holding_allowed(batch):
            targets.extend(
                line
                for line in self.cun4_holding_lines
                if line != dirty_line
            )
        return tuple(targets)

    def _cun4_holding_allowed(self, batch: list[dict[str, Any]]) -> bool:
        return all(
            car.get("_InitialLine", car["Line"]) not in physical.DEPOT_LINES
            for car in batch
        )

    def _final_target_allowed(self, target_line: str, *, dirty_line: str) -> bool:
        return bool(
            target_line
            and target_line != dirty_line
            and target_line not in physical.RUNNING_LINES
            and target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES
            and target_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES
        )

    def _candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        dirty_line: str,
        target_line: str,
        batch: list[dict[str, Any]],
    ) -> Any | None:
        move_nos = tuple(physical.car_no(car) for car in batch)
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=set(move_nos),
        )
        if len(positions) != len(batch):
            return None
        steps = (
            physical.plan_step("Get", dirty_line, move_nos),
            physical.plan_step("Put", target_line, move_nos, positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_dirty_cleanout",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=dirty_line,
            target_line=target_line,
            batch=batch,
            steps=steps,
            reason=(
                f"vnext:{self.template_name};source={dirty_line};"
                f"target={target_line};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_dirty_cleanout",
        )


class DepotInboundDirtyExchangeSessionEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_dirty_exchange_session"
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        if not plan.purity_violation_nos:
            return
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        planned_lines = plan.temporary_line_by_no
        if not pending_nos or not planned_lines:
            return
        loads = physical.line_loads(cars)
        for dirty_line in plan.purity_violation_lines:
            candidate = self._candidate_for_dirty_line(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                dirty_line=dirty_line,
                dirty_nos=set(plan.purity_violation_nos),
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=set(plan.purity_violation_lines),
                loads=loads,
            )
            if candidate is None:
                continue
            yield self._envelope(candidate, contract)
            return

    def _candidate_for_dirty_line(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        dirty_line: str,
        dirty_nos: set[str],
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        loads: Any,
    ) -> Any | None:
        if dirty_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return None
        dirty_batch, exchange_line = self._dirty_prefix(
            cars=cars,
            depot_assignment=depot_assignment,
            dirty_line=dirty_line,
            dirty_nos=dirty_nos,
            loads=loads,
        )
        if not dirty_batch or not exchange_line:
            return None
        exchange_window = self._exchange_line_window(
            cars=cars,
            depot_assignment=depot_assignment,
            exchange_line=exchange_line,
            dirty_batch=dirty_batch,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            dirty_lines=dirty_lines,
            loads=loads,
        )
        if not exchange_window:
            return None
        target_maps = self._target_maps(
            cars=cars,
            depot_assignment=depot_assignment,
            dirty_batch=dirty_batch,
            exchange_window=exchange_window,
            exchange_line=exchange_line,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            loads=loads,
        )
        moving_nos = tuple(physical.car_no(car) for car in [*dirty_batch, *exchange_window])
        for mode, target_by_no in target_maps:
            destination_segments = self._destination_segments(moving_nos, target_by_no)
            if len(destination_segments) != len(set(destination_segments)):
                continue
            candidate = self._validated_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases,
                dirty_line=dirty_line,
                exchange_line=exchange_line,
                dirty_batch=dirty_batch,
                exchange_window=exchange_window,
                target_by_no=target_by_no,
                mode=mode,
            )
            if candidate is not None:
                return candidate
        return None

    def _dirty_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        dirty_line: str,
        dirty_nos: set[str],
        loads: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        batch: list[dict[str, Any]] = []
        exchange_line = ""
        for car in physical.line_cars_in_access_order(cars=cars, line=dirty_line):
            no = physical.car_no(car)
            if no not in dirty_nos:
                break
            if car.get("IsWeigh"):
                break
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if not self._exchange_target_allowed(target_line):
                break
            if exchange_line and target_line != exchange_line:
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            exchange_line = target_line
            batch.append(car)
        return batch, exchange_line

    def _exchange_target_allowed(self, target_line: str) -> bool:
        return bool(
            target_line
            and target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES
            and target_line not in physical.DEPOT_INBOUND_ASSEMBLY_LINES
        )

    def _exchange_line_window(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        exchange_line: str,
        dirty_batch: list[dict[str, Any]],
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        loads: Any,
    ) -> tuple[dict[str, Any], ...]:
        window: list[dict[str, Any]] = []
        last_pending_index = -1
        for car in physical.line_cars_in_access_order(cars=cars, line=exchange_line):
            no = physical.car_no(car)
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*dirty_batch, *window, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            if no in pending_nos:
                target_line = planned_lines.get(no, "")
                if not target_line or target_line in dirty_lines:
                    break
                window.append(car)
                last_pending_index = len(window) - 1
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if not self._exchange_target_allowed(target_line):
                break
            window.append(car)
        if last_pending_index < 0:
            return ()
        return tuple(window[: last_pending_index + 1])

    def _target_maps(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        dirty_batch: list[dict[str, Any]],
        exchange_window: tuple[dict[str, Any], ...],
        exchange_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        loads: Any,
    ) -> tuple[tuple[str, dict[str, str]], ...]:
        maps: list[tuple[str, dict[str, str]]] = []
        finalize = self._target_by_no(
            cars=cars,
            depot_assignment=depot_assignment,
            dirty_batch=dirty_batch,
            exchange_window=exchange_window,
            exchange_line=exchange_line,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            loads=loads,
            preserve_exchange_prefix=False,
        )
        if finalize:
            maps.append(("finalize_exchange_prefix", finalize))
        preserve = self._target_by_no(
            cars=cars,
            depot_assignment=depot_assignment,
            dirty_batch=dirty_batch,
            exchange_window=exchange_window,
            exchange_line=exchange_line,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            loads=loads,
            preserve_exchange_prefix=True,
        )
        if preserve and preserve not in (target_by_no for _mode, target_by_no in maps):
            maps.append(("preserve_exchange_prefix", preserve))
        return tuple(maps)

    def _target_by_no(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        dirty_batch: list[dict[str, Any]],
        exchange_window: tuple[dict[str, Any], ...],
        exchange_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        loads: Any,
        preserve_exchange_prefix: bool,
    ) -> dict[str, str]:
        target_by_no: dict[str, str] = {
            physical.car_no(car): exchange_line
            for car in dirty_batch
        }
        for car in exchange_window:
            no = physical.car_no(car)
            if no in pending_nos:
                target_by_no[no] = planned_lines[no]
                continue
            if preserve_exchange_prefix:
                target_by_no[no] = exchange_line
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            target_by_no[no] = target_line
        return target_by_no

    def _destination_segments(
        self,
        nos: tuple[str, ...],
        target_by_no: dict[str, str],
    ) -> tuple[str, ...]:
        remaining = list(nos)
        segments: list[str] = []
        while remaining:
            target_line = target_by_no[remaining[-1]]
            start = len(remaining) - 1
            while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                start -= 1
            segments.append(target_line)
            del remaining[start:]
        return tuple(segments)

    def _validated_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        dirty_line: str,
        exchange_line: str,
        dirty_batch: list[dict[str, Any]],
        exchange_window: tuple[dict[str, Any], ...],
        target_by_no: dict[str, str],
        mode: str,
    ) -> Any | None:
        dirty_nos = tuple(physical.car_no(car) for car in dirty_batch)
        exchange_nos = tuple(physical.car_no(car) for car in exchange_window)
        moving_nos = {*dirty_nos, *exchange_nos}
        no_to_car = {
            physical.car_no(car): car
            for car in [*dirty_batch, *exchange_window]
        }
        remaining = [*dirty_nos, *exchange_nos]
        steps = [
            physical.plan_step("Get", dirty_line, dirty_nos),
            physical.plan_step("Get", exchange_line, exchange_nos),
        ]
        put_lines: list[str] = []
        while remaining:
            target_line = target_by_no[remaining[-1]]
            if target_line in put_lines:
                return None
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
            if len(positions) != len(group) and target_line == exchange_line:
                positions = {no: index for index, no in enumerate(drop, start=1)}
            if len(positions) != len(group):
                return None
            put_lines.append(target_line)
            steps.append(physical.plan_step("Put", target_line, drop, positions))
            del remaining[start:]
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_dirty_exchange_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=dirty_line,
            target_line=steps[-1].line,
            batch=[*dirty_batch, *exchange_window],
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};dirty_line={dirty_line};"
                f"mode={mode};"
                f"exchange_line={exchange_line};dirty={','.join(dirty_nos)};"
                f"inbound={','.join(no for no in exchange_nos if target_by_no[no] != exchange_line)};"
                f"put_lines={','.join(put_lines)}"
            ),
            candidate_kind="vnext_depot_inbound_dirty_exchange_session",
        )


class DepotInboundMixedExtractionSessionEpisode(Episode):
    intent = IntentKind.DEPOT_INBOUND_ASSEMBLY
    template_name = "depot_inbound_mixed_extraction_session"
    frontier = AccessFrontier()
    min_cut_prefix_grouped_count = 2

    def applies(self, contract: FlowContract) -> bool:
        return contract.family == ContractFamily.REMOTE_SESSION

    def _envelope(self, candidate: Any, contract: FlowContract) -> CandidateEnvelope:
        source_return_nos = tuple(
            no
            for step in physical.candidate_plan_steps(candidate)
            if step.action == "Put" and step.line == candidate.source_line
            for no in step.move_car_nos
        )
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line=candidate.source_line,
            target_line=candidate.target_line,
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        plan = strategic_plan.depot_inbound
        planned_lines = plan.temporary_line_by_no
        pending_nos = set(plan.ungrouped_nos) & set(contract.subject_nos)
        if not pending_nos or not planned_lines:
            return
        loads = physical.line_loads(cars)
        candidates: list[tuple[tuple[int, int, int, int, int, int, str], Any]] = []
        for source_line in _depot_inbound_source_lines(plan.source_lines, contract.source_lines):
            candidates.extend(self._candidates_for_source(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_line=source_line,
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=set(plan.purity_violation_lines),
                loads=loads,
            ))
        for _score, candidate in sorted(candidates, key=lambda item: item[0]):
            yield self._envelope(candidate, contract)

    def _candidates_for_source(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        loads: Any,
    ) -> tuple[tuple[tuple[int, int, int, int, int, int, str], Any], ...]:
        if source_line in physical.DEPOT_TARGET_LINES or source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            return ()
        line_cars = physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        )
        pull_window: list[dict[str, Any]] = []
        for car in line_cars:
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*pull_window, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            pull_window.append(car)
        candidate_windows = self._candidate_windows(
            pull_window=pull_window,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
        )
        if not candidate_windows:
            return ()
        max_candidate_window_len = len(candidate_windows[-1])
        candidates: list[tuple[tuple[int, int, int, int, int, int, str], Any]] = []
        for window in candidate_windows:
            grouped_count = self._grouped_count(window=window, pending_nos=pending_nos)
            if (
                len(window) < max_candidate_window_len
                and grouped_count < self.min_cut_prefix_grouped_count
            ):
                continue
            output_variants = self._output_line_variants_by_no(
                cars=cars,
                window=window,
                depot_assignment=depot_assignment,
                loads=loads,
                source_line=source_line,
                pending_nos=pending_nos,
                planned_lines=planned_lines,
                dirty_lines=dirty_lines,
            )
            for mode_rank, mode, target_by_no in output_variants:
                if not any(physical.car_no(car) not in pending_nos for car in window):
                    continue
                destination_segments = self._destination_segments(
                    tuple(physical.car_no(car) for car in window),
                    target_by_no,
                )
                candidate = self._validated_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases,
                    source_line=source_line,
                    window=window,
                    target_by_no=target_by_no,
                    mode=mode,
                )
                if candidate is None:
                    continue
                local_score = self._window_score(
                    window=window,
                    pending_nos=pending_nos,
                    destination_segments=destination_segments,
                    mode_rank=mode_rank,
                )
                score = (
                    local_score[0],
                    local_score[1],
                    local_score[2],
                    local_score[3],
                    local_score[4],
                    _depot_inbound_source_rank(source_line),
                    candidate.candidate_id,
                )
                candidates.append((score, candidate))
        return tuple(candidates)

    def _candidate_windows(
        self,
        *,
        pull_window: list[dict[str, Any]],
        pending_nos: set[str],
        planned_lines: dict[str, str],
    ) -> tuple[tuple[dict[str, Any], ...], ...]:
        windows: list[tuple[dict[str, Any], ...]] = []
        for index, car in enumerate(pull_window):
            no = physical.car_no(car)
            if no in pending_nos and planned_lines.get(no):
                windows.append(tuple(pull_window[: index + 1]))
        return tuple(windows)

    def _window_score(
        self,
        *,
        window: tuple[dict[str, Any], ...],
        pending_nos: set[str],
        destination_segments: tuple[str, ...],
        mode_rank: int,
    ) -> tuple[int, int, int, int, int]:
        nos = tuple(physical.car_no(car) for car in window)
        grouped_count = sum(1 for no in nos if no in pending_nos)
        non_inbound_count = len(nos) - grouped_count
        return (
            -grouped_count,
            mode_rank,
            len(destination_segments),
            non_inbound_count,
            len(nos),
        )

    def _grouped_count(
        self,
        *,
        window: tuple[dict[str, Any], ...],
        pending_nos: set[str],
    ) -> int:
        return sum(1 for car in window if physical.car_no(car) in pending_nos)

    def _output_line_variants_by_no(
        self,
        *,
        cars: list[dict[str, Any]],
        window: tuple[dict[str, Any], ...],
        depot_assignment: Any,
        loads: Any,
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
    ) -> tuple[tuple[int, str, dict[str, str]], ...]:
        variants: list[tuple[int, str, dict[str, str]]] = []
        clear = self._output_line_by_no(
            cars=cars,
            window=window,
            depot_assignment=depot_assignment,
            loads=loads,
            source_line=source_line,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            dirty_lines=dirty_lines,
            preserve_non_inbound_prefix=False,
        )
        if clear:
            variants.append((0, "clear_non_inbound_prefix", clear))
        preserve = self._output_line_by_no(
            cars=cars,
            window=window,
            depot_assignment=depot_assignment,
            loads=loads,
            source_line=source_line,
            pending_nos=pending_nos,
            planned_lines=planned_lines,
            dirty_lines=dirty_lines,
            preserve_non_inbound_prefix=True,
        )
        if preserve and preserve not in (target_by_no for _rank, _mode, target_by_no in variants):
            variants.append((1, "preserve_non_inbound_prefix", preserve))
        return tuple(variants)

    def _output_line_by_no(
        self,
        *,
        cars: list[dict[str, Any]],
        window: tuple[dict[str, Any], ...],
        depot_assignment: Any,
        loads: Any,
        source_line: str,
        pending_nos: set[str],
        planned_lines: dict[str, str],
        dirty_lines: set[str],
        preserve_non_inbound_prefix: bool,
    ) -> dict[str, str]:
        output: dict[str, str] = {}
        has_pending = False
        has_non_inbound = False
        for car in window:
            no = physical.car_no(car)
            if no in pending_nos:
                target_line = planned_lines.get(no, "")
                if not target_line or target_line in dirty_lines:
                    return {}
                output[no] = target_line
                has_pending = True
                continue
            if no in planned_lines:
                return {}
            if preserve_non_inbound_prefix:
                output[no] = source_line
                has_non_inbound = True
                continue
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if (
                not target_line
                or target_line == source_line
                or target_line in physical.RUNNING_LINES
                or target_line in physical.DEPOT_INBOUND_DESTINATION_LINES
                or target_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES
            ):
                return {}
            output[no] = target_line
            has_non_inbound = True
        if not has_pending or not has_non_inbound:
            return {}
        return output

    def _destination_segments(
        self,
        nos: tuple[str, ...],
        target_by_no: dict[str, str],
    ) -> tuple[str, ...]:
        remaining = list(nos)
        segments: list[str] = []
        while remaining:
            target_line = target_by_no[remaining[-1]]
            start = len(remaining) - 1
            while start > 0 and target_by_no.get(remaining[start - 1]) == target_line:
                start -= 1
            segments.append(target_line)
            del remaining[start:]
        return tuple(segments)

    def _validated_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        window: tuple[dict[str, Any], ...],
        target_by_no: dict[str, str],
        mode: str,
    ) -> Any | None:
        plan = _depot_inbound_stepwise_put_plan(
            cars=cars,
            depot_assignment=depot_assignment,
            source_line=source_line,
            batch=window,
            target_by_no=target_by_no,
        )
        if plan is None:
            return None
        plan_steps, put_lines = plan
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_mixed_extraction_session",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=plan_steps[-1].line,
            batch=list(window),
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};source={source_line};"
                f"mode={mode};"
                f"put_lines={','.join(put_lines)};batch={len(window)}"
            ),
            candidate_kind="vnext_depot_inbound_mixed_extraction_session",
        )


class DepotInboundCun4OpenReleaseEpisode(Episode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "depot_inbound_cun4_open_release"
    frontier = AccessFrontier()
    parking_lines = ("修4库外", "修3库外", "修2库外", "修1库外")
    blocker_lines = ("机走棚", "机南", "洗油北", "机走北")

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None:
            return
        if not strategic_plan.depot_inbound.assembly_complete:
            return
        if not strategic_plan.depot_outbound.outbound_nos:
            return
        batch = self._cun4_depot_inbound_prefix(
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            subject_nos=set(contract.subject_nos),
        )
        if not batch:
            return
        for target_line in self.parking_lines:
            candidate = self._candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                target_line=target_line,
                batch=batch,
            )
            if candidate is None:
                continue
            yield self._envelope(candidate, contract)
            return
        candidate = self._split_parking_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            source_batches=(("存4线", batch),),
        )
        if candidate is not None:
            yield self._envelope(candidate, contract)
            return
        subject_nos = set(contract.subject_nos)
        blocker_batches: list[tuple[str, list[dict[str, Any]]]] = []
        for blocker_line in self.blocker_lines:
            blocker_batch = self._depot_inbound_prefix_for_line(
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                source_line=blocker_line,
                subject_nos=subject_nos,
            )
            if not blocker_batch:
                continue
            blocker_batches.append((blocker_line, blocker_batch))
            for target_line in self.parking_lines:
                candidate = self._blockers_then_cun4_candidate(
                    case_id=case_id,
                    hook_index=hook_index,
                    cars=cars,
                    depot_assignment=depot_assignment,
                    graph=graph,
                    loco_location=loco_location,
                    serial_gate_leases=serial_gate_leases or {},
                    target_line=target_line,
                    blocker_batches=tuple(blocker_batches),
                    cun4_batch=batch,
                )
                if candidate is None:
                    continue
                yield self._envelope(candidate, contract)
                return
            candidate = self._split_parking_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_batches=(*tuple(blocker_batches), ("存4线", batch)),
            )
            if candidate is not None:
                yield self._envelope(candidate, contract)
                return

    def _cun4_depot_inbound_prefix(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        subject_nos: set[str],
    ) -> list[dict[str, Any]]:
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line="存4线",
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in subject_nos or target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _depot_inbound_prefix_for_line(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        source_line: str,
        subject_nos: set[str],
    ) -> list[dict[str, Any]]:
        loads = physical.line_loads(cars)
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in subject_nos or target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _candidate(
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
        batch: list[dict[str, Any]],
    ) -> Any | None:
        moving_nos = {physical.car_no(car) for car in batch}
        positions = planned_positions_for_batch(
            batch=batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=moving_nos,
        )
        if len(positions) != len(batch):
            return None
        move_nos = tuple(physical.car_no(car) for car in batch)
        steps = (
            physical.plan_step("Get", "存4线", move_nos),
            physical.plan_step("Put", target_line, move_nos, positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line="存4线",
            target_line=target_line,
            batch=batch,
            steps=steps,
            reason=(
                f"vnext:{self.template_name};target={target_line};"
                f"batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        )

    def _blockers_then_cun4_candidate(
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
        blocker_batches: tuple[tuple[str, list[dict[str, Any]]], ...],
        cun4_batch: list[dict[str, Any]],
    ) -> Any | None:
        planning_cars = [dict(car) for car in cars]
        cun4_nos = tuple(physical.car_no(car) for car in cun4_batch)
        steps: list[Any] = []
        all_blocker_cars: list[dict[str, Any]] = []
        blocker_line_names: list[str] = []
        for blocker_line, blocker_batch in blocker_batches:
            blocker_nos = tuple(physical.car_no(car) for car in blocker_batch)
            physical.apply_physical_get_order(planning_cars, blocker_line, blocker_nos)
            blocker_positions = planned_positions_for_batch(
                batch=blocker_batch,
                target_line=target_line,
                cars=planning_cars,
                depot_assignment=depot_assignment,
                batch_nos=set(blocker_nos),
            )
            if len(blocker_positions) != len(blocker_batch):
                return None
            physical.apply_physical_put_order(planning_cars, target_line, list(blocker_nos), blocker_positions)
            steps.append(physical.plan_step("Get", blocker_line, blocker_nos))
            steps.append(physical.plan_step("Put", target_line, blocker_nos, blocker_positions))
            all_blocker_cars.extend(blocker_batch)
            blocker_line_names.append(blocker_line)
        physical.apply_physical_get_order(planning_cars, "存4线", cun4_nos)
        cun4_positions = planned_positions_for_batch(
            batch=cun4_batch,
            target_line=target_line,
            cars=planning_cars,
            depot_assignment=depot_assignment,
            batch_nos=set(cun4_nos),
        )
        if len(cun4_positions) != len(cun4_batch):
            return None
        steps.extend(
            (
                physical.plan_step("Get", "存4线", cun4_nos),
                physical.plan_step("Put", target_line, cun4_nos, cun4_positions),
            )
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=tuple(steps),
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=steps[0].line,
            target_line=target_line,
            batch=[*all_blocker_cars, *cun4_batch],
            steps=tuple(steps),
            reason=(
                f"vnext:{self.template_name};blockers={','.join(blocker_line_names)};"
                f"target={target_line};blocker_batch={len(all_blocker_cars)};"
                f"cun4_batch={len(cun4_batch)}"
            ),
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        )

    def _split_parking_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_batches: tuple[tuple[str, list[dict[str, Any]]], ...],
    ) -> Any | None:
        planning_cars = [dict(car) for car in cars]
        steps: list[Any] = []
        carry: list[dict[str, Any]] = []
        put_lines: list[str] = []
        for source_line, batch in source_batches:
            move_nos = tuple(physical.car_no(car) for car in batch)
            physical.apply_physical_get_order(planning_cars, source_line, move_nos)
            steps.append(physical.plan_step("Get", source_line, move_nos))
            parking_steps = self._split_parking_put_steps(
                planning_cars=planning_cars,
                depot_assignment=depot_assignment,
                batch=batch,
            )
            if parking_steps is None:
                return None
            for step in parking_steps:
                steps.append(step)
                put_lines.append(step.line)
                physical.apply_physical_put_order(
                    planning_cars,
                    step.line,
                    list(step.move_car_nos),
                    step.planned_positions,
                )
            carry.extend(batch)
        plan_steps = tuple(steps)
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=plan_steps[0].line,
            target_line=plan_steps[-1].line,
            batch=carry,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};mode=split_parking;"
                f"sources={','.join(source_line for source_line, _batch in source_batches)};"
                f"put_lines={','.join(put_lines)};batch={len(carry)}"
            ),
            candidate_kind="vnext_depot_inbound_cun4_open_release",
        )

    def _split_parking_put_steps(
        self,
        *,
        planning_cars: list[dict[str, Any]],
        depot_assignment: Any,
        batch: list[dict[str, Any]],
    ) -> tuple[Any, ...] | None:
        remaining = [physical.car_no(car) for car in batch]
        no_to_car = {physical.car_no(car): car for car in batch}
        steps: list[Any] = []
        for target_line in self.parking_lines:
            if not remaining:
                break
            selected_drop: tuple[str, ...] = ()
            selected_positions: dict[str, int] = {}
            for size in range(len(remaining), 0, -1):
                drop = tuple(remaining[-size:])
                group = [no_to_car[no] for no in drop]
                if self._batch_length(group) > self._line_free_m(planning_cars=planning_cars, line=target_line) + physical.LINE_LENGTH_TOLERANCE_M:
                    continue
                positions = planned_positions_for_batch(
                    batch=group,
                    target_line=target_line,
                    cars=planning_cars,
                    depot_assignment=depot_assignment,
                    batch_nos=set(drop),
                )
                if len(positions) != len(group):
                    continue
                selected_drop = drop
                selected_positions = positions
                break
            if not selected_drop:
                continue
            steps.append(physical.plan_step("Put", target_line, selected_drop, selected_positions))
            del remaining[-len(selected_drop):]
        if remaining:
            return None
        return tuple(steps)

    def _line_free_m(self, *, planning_cars: list[dict[str, Any]], line: str) -> float:
        return physical.TRACK_SPECS[line].length_m - physical.line_length_load(planning_cars, line)

    def _batch_length(self, batch: list[dict[str, Any]]) -> float:
        return sum(physical.car_length(car) for car in batch)


class DepotInboundAssemblyReleaseEpisode(Episode):
    intent = IntentKind.REMOTE_DEPOT
    template_name = "depot_inbound_assembly_release"
    frontier = AccessFrontier()
    release_source_order = ("机走北", "机走棚", "机南", "洗油北")

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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        if strategic_plan is None or not strategic_plan.depot_inbound.assembly_complete:
            return
        loads = physical.line_loads(cars)
        subject_nos = set(contract.subject_nos)
        multisource_candidate = self._multisource_release_candidate(
            case_id=case_id,
            hook_index=hook_index,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            subject_nos=subject_nos,
            loads=loads,
        )
        if multisource_candidate is not None:
            yield self._envelope(multisource_candidate, contract)
            return
        for source_line in physical.DEPOT_INBOUND_ASSEMBLY_LINES:
            if source_line == "存4线":
                continue
            line_cars = physical.line_cars_in_access_order(
                cars=cars,
                line=source_line,
                graph=graph,
                loco_location=loco_location,
            )
            batch: list[dict[str, Any]] = []
            target_by_no: dict[str, str] = {}
            for car in line_cars:
                no = physical.car_no(car)
                target_line, _position, _reason = physical.planned_target_for_car(
                    car,
                    cars,
                    depot_assignment,
                    loads,
                )
                if no not in subject_nos or target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                    break
                if car.get("IsWeigh"):
                    break
                if physical.pull_equivalent([*batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                    break
                batch.append(car)
                target_by_no[no] = target_line
            if not batch:
                continue
            candidate = self._validated_release_candidate(
                case_id=case_id,
                hook_index=hook_index,
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                serial_gate_leases=serial_gate_leases or {},
                source_line=source_line,
                batch=batch,
                target_by_no=target_by_no,
            )
            if candidate is None:
                continue
            yield self._envelope(candidate, contract)
            return

    def _multisource_release_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        subject_nos: set[str],
        loads: Any,
    ) -> Any | None:
        source_batches: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        target_by_no: dict[str, str] = {}
        carry: list[dict[str, Any]] = []
        for source_line in self.release_source_order:
            batch = self._release_batch_for_source(
                cars=cars,
                depot_assignment=depot_assignment,
                graph=graph,
                loco_location=loco_location,
                source_line=source_line,
                subject_nos=subject_nos,
                loads=loads,
                carry=carry,
            )
            if not batch:
                continue
            source_batches.append((source_line, tuple(batch)))
            carry.extend(batch)
            for car in batch:
                no = physical.car_no(car)
                target_by_no[no] = physical.planned_target_for_car(
                    car,
                    cars,
                    depot_assignment,
                    loads,
                )[0]
        if len(source_batches) < 2:
            return None
        plan = _depot_inbound_multisource_stepwise_put_plan(
            cars=cars,
            depot_assignment=depot_assignment,
            source_batches=tuple(source_batches),
            target_by_no=target_by_no,
        )
        if plan is None:
            return None
        plan_steps, put_lines, batch = plan
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_assembly_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=plan_steps[0].line,
            target_line=plan_steps[-1].line,
            batch=list(batch),
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};mode=multisource;"
                f"sources={','.join(source for source, _batch in source_batches)};"
                f"targets={','.join(put_lines)};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_assembly_release",
        )

    def _release_batch_for_source(
        self,
        *,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        source_line: str,
        subject_nos: set[str],
        loads: Any,
        carry: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        for car in physical.line_cars_in_access_order(
            cars=cars,
            line=source_line,
            graph=graph,
            loco_location=loco_location,
        ):
            no = physical.car_no(car)
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            if no not in subject_nos or target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                break
            if car.get("IsWeigh"):
                break
            if physical.pull_equivalent([*carry, *batch, car]) > physical.PULL_LIMIT_EQUIVALENT:
                break
            batch.append(car)
        return batch

    def _validated_release_candidate(
        self,
        *,
        case_id: str,
        hook_index: int,
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        graph: Any,
        loco_location: Any,
        serial_gate_leases: dict[str, Any],
        source_line: str,
        batch: list[dict[str, Any]],
        target_by_no: dict[str, str],
    ) -> Any | None:
        plan = _depot_inbound_stepwise_put_plan(
            cars=cars,
            depot_assignment=depot_assignment,
            source_line=source_line,
            batch=tuple(batch),
            target_by_no=target_by_no,
        )
        if plan is None:
            return None
        plan_steps, put_lines = plan
        if not self.frontier.plan_steps_are_reachable(
            steps=plan_steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases,
            candidate_kind="vnext_depot_inbound_assembly_release",
        ):
            return None
        return physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=plan_steps[-1].line,
            batch=batch,
            steps=plan_steps,
            reason=(
                f"vnext:{self.template_name};source={source_line};"
                f"targets={','.join(put_lines)};batch={len(batch)}"
            ),
            candidate_kind="vnext_depot_inbound_assembly_release",
        )


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


class SourcePrefixReleaseEpisode(Episode):
    intent = IntentKind.FRONT_PREP
    template_name = "source_prefix_release"
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
        strategic_plan: Any | None = None,
    ) -> Iterable[CandidateEnvelope]:
        del strategic_plan
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
        source_blockers, target_batch = self._prefix_parts(
            line_cars=line_cars,
            cars=cars,
            depot_assignment=depot_assignment,
            contract=contract,
            source_line=source_line,
        )
        if not source_blockers or not target_batch:
            return
        if any(car.get("IsWeigh") for car in (*source_blockers, *target_batch)):
            return
        prefix = [*source_blockers, *target_batch]
        if physical.pull_equivalent(prefix) > physical.PULL_LIMIT_EQUIVALENT:
            return

        target_nos = tuple(physical.car_no(car) for car in target_batch)
        blocker_nos = tuple(physical.car_no(car) for car in source_blockers)
        positions = planned_positions_for_batch(
            batch=target_batch,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=set(target_nos),
        )
        if len(positions) != len(target_batch):
            return
        restore_positions = {
            physical.car_no(car): int(car.get("Position") or index)
            for index, car in enumerate(source_blockers, start=1)
        }
        steps = (
            physical.plan_step("Get", source_line, tuple(physical.car_no(car) for car in prefix)),
            physical.plan_step("Put", target_line, target_nos, positions),
            physical.plan_step("Put", source_line, blocker_nos, restore_positions),
        )
        if not self.frontier.plan_steps_are_reachable(
            steps=steps,
            cars=cars,
            depot_assignment=depot_assignment,
            graph=graph,
            loco_location=loco_location,
            serial_gate_leases=serial_gate_leases or {},
            candidate_kind="vnext_source_prefix_release",
        ):
            return
        candidate = physical.build_planlet_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            batch=prefix,
            steps=steps,
            reason=(
                f"vnext:{self.template_name};"
                f"blockers={','.join(blocker_nos)};"
                f"targets={','.join(target_nos)}"
            ),
            candidate_kind="vnext_source_prefix_release",
        )
        intent = self._intent_for(source_line=source_line, target_line=target_line)
        request = ResourceRequest(
            contract_id=contract.contract_id,
            family=contract.family,
            candidate_id=candidate.candidate_id,
            resources=(),
            source_line=source_line,
            target_line=target_line,
            move_nos=tuple(candidate.move_car_nos),
            intent=intent,
            same_plan_source_return_nos=blocker_nos,
        )
        yield CandidateEnvelope(
            candidate=candidate,
            contract=contract,
            intent=intent,
            resource_request=request,
            template_name=self.template_name,
        )

    def _intent_for(self, *, source_line: str, target_line: str) -> IntentKind:
        if source_line == "存4线" and target_line == "卸轮线":
            return IntentKind.CUN4_RELEASE_ACCEPT
        return self.intent

    def _prefix_parts(
        self,
        *,
        line_cars: list[dict[str, Any]],
        cars: list[dict[str, Any]],
        depot_assignment: Any,
        contract: FlowContract,
        source_line: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        loads = physical.line_loads(cars)
        contract_nos = set(contract.subject_nos)
        blockers: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        saw_target = False
        for car in line_cars:
            no = physical.car_no(car)
            if no in contract_nos:
                saw_target = True
                targets.append(car)
                continue
            if not saw_target:
                target_line, _position, _reason = physical.planned_target_for_car(
                    car,
                    cars,
                    depot_assignment,
                    loads,
                )
                if target_line != source_line:
                    return [], []
                blockers.append(car)
                continue
            break
        return blockers, targets


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
    DepotInboundDirtyCleanoutEpisode(),
    DepotInboundDirtyExchangeSessionEpisode(),
    DepotInboundAssemblyRebalanceEpisode(),
    DepotInboundRouteBlockerDigestEpisode(),
    DepotInboundMultiSourceAssemblySessionEpisode(),
    DepotInboundAssemblySessionEpisode(),
    DepotInboundMixedExtractionSessionEpisode(),
    DepotInboundPrefixAssemblySessionEpisode(),
    DepotInboundAssemblyReleaseEpisode(),
    Cun4UnwheelReleaseEpisode(),
    DepotCun4SourceRepackExchangeEpisode(),
    DepotCun4InboundOutboundExchangeEpisode(),
    DepotOutboundSessionEpisode(),
    Cun4OutboundAssemblyReleaseEpisode(),
    Cun4ReleaseAcceptEpisode(),
    Cun4ReleaseGroupAssemblyEpisode(),
    DepotInboundGatherSessionEpisode(),
    RemoteSessionPrefixDigestEpisode(),
    RemoteSessionEpisode(),
    SourcePrefixReleaseEpisode(),
    DirectMoveEpisode(),
    SpottingRepackEpisode(),
    TailBlockerPeelDigestEpisode(),
    DepotSlotSwapEpisode(),
    TailCloseoutEpisode(),
)
