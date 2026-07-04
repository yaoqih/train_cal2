from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import physical


ASSEMBLY_LINES = physical.DEPOT_INBOUND_ASSEMBLY_LINES


@dataclass(frozen=True)
class DepotInboundAssemblyGroup:
    line: str
    vehicle_nos: tuple[str, ...]
    length_m: float
    capacity_m: float
    free_m: float


@dataclass(frozen=True)
class DepotInboundAssemblyPlan:
    status: str
    reason: str
    inbound_nos: tuple[str, ...]
    ungrouped_nos: tuple[str, ...]
    grouped_nos: tuple[str, ...]
    unassigned_nos: tuple[str, ...]
    purity_violation_nos: tuple[str, ...]
    purity_violation_lines: tuple[str, ...]
    purity_exempt_nos: tuple[str, ...]
    source_lines: tuple[str, ...]
    target_lines: tuple[str, ...]
    total_length_m: float
    pullout_required_m: float
    depot_free_m: float
    depot_surplus_after_pull_m: float
    cun4_budget_m: float
    cun4_vehicle_budget: int
    assembly_capacity_m: float
    assembly_free_m: float
    groups: tuple[DepotInboundAssemblyGroup, ...]

    @property
    def group_count(self) -> int:
        return sum(1 for group in self.groups if group.vehicle_nos)

    @property
    def temporary_line_by_no(self) -> dict[str, str]:
        return {
            no: group.line
            for group in self.groups
            for no in group.vehicle_nos
        }

    @property
    def assembly_complete(self) -> bool:
        return not self.ungrouped_nos and not self.unassigned_nos and not self.purity_violation_nos


def build_depot_inbound_assembly_plan(
    *,
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    cun4_outbound_hold_nos: set[str],
    depot_outbound_nos: set[str],
    strict_cun4_unwheel_only: bool = True,
) -> DepotInboundAssemblyPlan:
    """Describe the inbound-to-depot temporary assembly skeleton.

    Depot-bound cars are first assembled on the five human staging lines.  The
    computation is capacity-derived: existing pure depot-bound cars on those
    lines consume capacity and count as grouped; remaining cars are assigned by
    line free length.  Non-depot cars on those lines are a hard diagnostic.
    """
    loads = physical.line_loads(cars)
    inbound_rows: list[tuple[dict[str, Any], str]] = []
    purity_violations: list[str] = []
    purity_lines: list[str] = []
    purity_exemptions: list[str] = []
    for car in physical.unsatisfied_cars(cars, depot_assignment):
        initial_line = car.get("_InitialLine", car["Line"])
        target_line, _position, _reason = physical.planned_target_for_car(
            car,
            cars,
            depot_assignment,
            loads,
        )
        if target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
            continue
        if initial_line in physical.DEPOT_INBOUND_DESTINATION_LINES:
            continue
        if car["Line"] in physical.DEPOT_INBOUND_DESTINATION_LINES:
            continue
        inbound_rows.append((car, target_line))
    if inbound_rows:
        for car in cars:
            target_line, _position, _reason = physical.planned_target_for_car(
                car,
                cars,
                depot_assignment,
                loads,
            )
            no = physical.car_no(car)
            if car["Line"] in ASSEMBLY_LINES and target_line not in physical.DEPOT_INBOUND_DESTINATION_LINES:
                if car["Line"] == "存4线" and not strict_cun4_unwheel_only:
                    purity_exemptions.append(no)
                    continue
                if _is_cun4_outbound_hold_exempt(
                    car=car,
                    no=no,
                    target_line=target_line,
                    cun4_outbound_hold_nos=cun4_outbound_hold_nos,
                ):
                    purity_exemptions.append(no)
                    continue
                purity_violations.append(no)
                purity_lines.append(car["Line"])

    if not inbound_rows and not purity_violations:
        return _empty_plan("no_depot_inbound_assembly_debt")

    ordered_rows = sorted(
        inbound_rows,
        key=lambda row: (
            _source_rank(row[0]["Line"]),
            row[0]["Line"],
            int(row[0].get("Position") or 0),
            _target_rank(row[1]),
            physical.car_no(row[0]),
        ),
    )
    dirty_lines = set(purity_lines)
    inbound_nos = tuple(physical.car_no(car) for car, _target_line in ordered_rows)
    cun4_vehicle_budget = _cun4_vehicle_budget(depot_outbound_nos=depot_outbound_nos)
    already_grouped = _already_grouped_nos(
        rows=ordered_rows,
        cun4_vehicle_budget=cun4_vehicle_budget,
        strict_cun4_unwheel_only=strict_cun4_unwheel_only,
    )
    moving_rows = [
        (car, target_line)
        for car, target_line in ordered_rows
        if physical.car_no(car) not in already_grouped
    ]
    grouped = {line: [] for line in ASSEMBLY_LINES}
    for car, _target_line in ordered_rows:
        if physical.car_no(car) in already_grouped:
            grouped[car["Line"]].append(physical.car_no(car))
    total_length_m = sum(physical.car_length(car) for car, _target_line in ordered_rows)
    pullout_required_m = total_length_m + physical.LOCO_LENGTH_M if ordered_rows else 0.0
    depot_free_m = sum(_line_free(line=line, cars=cars) for line in sorted(physical.DEPOT_TARGET_LINES))
    depot_surplus_after_pull_m = depot_free_m - pullout_required_m
    cun4_budget_m = _line_free(line="存4线", cars=cars)
    existing_cun4_grouped_m = _length_for_nos(cars, set(grouped["存4线"]))
    remaining_capacity = {
        line: _line_assignment_capacity(
            line=line,
            cars=cars,
            dirty_lines=dirty_lines,
            cun4_budget_m=cun4_budget_m,
            existing_cun4_grouped_m=existing_cun4_grouped_m,
        )
        for line in ASSEMBLY_LINES
    }
    remaining_vehicle_slots = {
        line: _line_vehicle_slot_capacity(
            line=line,
            grouped=grouped,
            cun4_vehicle_budget=cun4_vehicle_budget,
        )
        for line in ASSEMBLY_LINES
    }
    unassigned: list[str] = []
    for source_line, source_rows in _rows_by_source(moving_rows):
        for car, _target_line in source_rows:
            no = physical.car_no(car)
            length = physical.car_length(car)
            placed_line = _choose_line(
                source_line=source_line,
                target_line=_target_line,
                length_m=length,
                remaining_capacity=remaining_capacity,
                remaining_vehicle_slots=remaining_vehicle_slots,
                depot_outbound_nos=depot_outbound_nos,
            )
            if not placed_line:
                unassigned.append(no)
                continue
            grouped[placed_line].append(no)
            remaining_capacity[placed_line] -= length
            remaining_vehicle_slots[placed_line] -= 1

    groups = tuple(
        DepotInboundAssemblyGroup(
            line=line,
            vehicle_nos=tuple(grouped[line]),
            length_m=round(_length_for_nos(cars, set(grouped[line])), 3),
            capacity_m=round(physical.TRACK_SPECS[line].length_m, 3),
            free_m=round(max(0.0, remaining_capacity[line]), 3),
        )
        for line in ASSEMBLY_LINES
    )
    assembly_capacity_m = sum(physical.TRACK_SPECS[line].length_m for line in ASSEMBLY_LINES)
    assembly_free_m = sum(_line_free(line=line, cars=cars) for line in ASSEMBLY_LINES)
    if purity_violations:
        status = "fail"
        reason = "inbound_assembly_line_purity_violation"
    elif unassigned:
        status = "fail"
        reason = "inbound_assembly_capacity_insufficient"
    elif moving_rows:
        status = "warn"
        reason = "inbound_assembly_required"
    else:
        status = "pass"
        reason = "inbound_assembly_grouped"

    return DepotInboundAssemblyPlan(
        status=status,
        reason=reason,
        inbound_nos=inbound_nos,
        ungrouped_nos=tuple(physical.car_no(car) for car, _target_line in moving_rows),
        grouped_nos=tuple(no for no in inbound_nos if no in already_grouped),
        unassigned_nos=tuple(unassigned),
        purity_violation_nos=tuple(sorted(set(purity_violations))),
        purity_violation_lines=tuple(dict.fromkeys(purity_lines)),
        purity_exempt_nos=tuple(sorted(set(purity_exemptions))),
        source_lines=tuple(dict.fromkeys(car["Line"] for car, _target_line in ordered_rows)),
        target_lines=tuple(dict.fromkeys(target_line for _car, target_line in ordered_rows)),
        total_length_m=round(total_length_m, 3),
        pullout_required_m=round(pullout_required_m, 3),
        depot_free_m=round(depot_free_m, 3),
        depot_surplus_after_pull_m=round(depot_surplus_after_pull_m, 3),
        cun4_budget_m=round(cun4_budget_m, 3),
        cun4_vehicle_budget=cun4_vehicle_budget,
        assembly_capacity_m=round(assembly_capacity_m, 3),
        assembly_free_m=round(assembly_free_m, 3),
        groups=groups,
    )


def _empty_plan(reason: str) -> DepotInboundAssemblyPlan:
    return DepotInboundAssemblyPlan(
        status="pass",
        reason=reason,
        inbound_nos=(),
        ungrouped_nos=(),
        grouped_nos=(),
        unassigned_nos=(),
        purity_violation_nos=(),
        purity_violation_lines=(),
        purity_exempt_nos=(),
        source_lines=(),
        target_lines=(),
        total_length_m=0.0,
        pullout_required_m=0.0,
        depot_free_m=0.0,
        depot_surplus_after_pull_m=0.0,
        cun4_budget_m=0.0,
        cun4_vehicle_budget=0,
        assembly_capacity_m=0.0,
        assembly_free_m=0.0,
        groups=(),
    )


def _is_cun4_outbound_hold_exempt(
    *,
    car: dict[str, Any],
    no: str,
    target_line: str,
    cun4_outbound_hold_nos: set[str],
) -> bool:
    return (
        car["Line"] == "存4线"
        and no in cun4_outbound_hold_nos
        and car.get("_InitialLine") in physical.DEPOT_LINES
        and target_line == "存4线"
    )


def _line_free(
    *,
    line: str,
    cars: list[dict[str, Any]],
) -> float:
    spec = physical.TRACK_SPECS[line]
    return spec.length_m - physical.line_length_load(cars, line)


def _length_for_nos(cars: list[dict[str, Any]], nos: set[str]) -> float:
    return sum(physical.car_length(car) for car in cars if physical.car_no(car) in nos)


def _cun4_vehicle_budget(*, depot_outbound_nos: set[str]) -> int:
    return 10**9


def _already_grouped_nos(
    *,
    rows: list[tuple[dict[str, Any], str]],
    cun4_vehicle_budget: int,
    strict_cun4_unwheel_only: bool,
) -> set[str]:
    grouped: set[str] = set()
    cun4_count = 0
    for car, target_line in rows:
        line = car["Line"]
        if line not in ASSEMBLY_LINES:
            continue
        no = physical.car_no(car)
        if line != "存4线":
            if _is_cun4_final_target(target_line):
                continue
            grouped.add(no)
            continue
        if strict_cun4_unwheel_only and not _is_cun4_final_target(target_line):
            continue
        if cun4_count < cun4_vehicle_budget:
            grouped.add(no)
            cun4_count += 1
    return grouped


def _line_vehicle_slot_capacity(
    *,
    line: str,
    grouped: dict[str, list[str]],
    cun4_vehicle_budget: int,
) -> int:
    if line != "存4线":
        return 10**9
    return max(0, cun4_vehicle_budget - len(grouped["存4线"]))


def _line_assignment_capacity(
    *,
    line: str,
    cars: list[dict[str, Any]],
    dirty_lines: set[str],
    cun4_budget_m: float,
    existing_cun4_grouped_m: float,
) -> float:
    if line in dirty_lines:
        return 0.0
    free_m = _line_free(line=line, cars=cars)
    if line != "存4线":
        return max(0.0, free_m)
    return max(0.0, min(free_m, cun4_budget_m - existing_cun4_grouped_m))


def _rows_by_source(rows: list[tuple[dict[str, Any], str]]) -> tuple[tuple[str, tuple[tuple[dict[str, Any], str], ...]], ...]:
    grouped: dict[str, list[tuple[dict[str, Any], str]]] = {}
    order: list[str] = []
    for row in rows:
        source_line = row[0]["Line"]
        if source_line not in grouped:
            grouped[source_line] = []
            order.append(source_line)
        grouped[source_line].append(row)
    return tuple((line, tuple(grouped[line])) for line in order)


def _choose_line(
    *,
    source_line: str,
    target_line: str,
    length_m: float,
    remaining_capacity: dict[str, float],
    remaining_vehicle_slots: dict[str, int],
    depot_outbound_nos: set[str],
) -> str:
    for line in _preferred_lines_for_source(
        source_line,
        target_line=target_line,
        has_depot_outbound_debt=bool(depot_outbound_nos),
    ):
        if remaining_vehicle_slots[line] <= 0:
            continue
        if remaining_capacity[line] + physical.LINE_LENGTH_TOLERANCE_M >= length_m:
            return line
    return ""


def _preferred_lines_for_source(
    source_line: str,
    *,
    target_line: str,
    has_depot_outbound_debt: bool,
) -> tuple[str, ...]:
    if _is_cun4_final_target(target_line):
        return ("存4线",)
    if has_depot_outbound_debt:
        if source_line in {"洗罐线北", "洗罐站", "油漆线", "抛丸线", "卸轮线"}:
            return ("机南", "机走棚", "机走北", "洗油北")
        if source_line in {"预修线", "调梁棚", "调梁线北", "机库线"}:
            return ("机走棚", "机走北", "洗油北", "机南")
        return ("机南", "机走棚", "机走北", "洗油北")
    if source_line in {"洗罐线北", "洗罐站", "油漆线", "抛丸线", "卸轮线"}:
        return ("机南", "洗油北", "机走棚", "机走北")
    if source_line in {"预修线", "调梁棚", "调梁线北", "机库线"}:
        return ("机南", "机走棚", "机走北", "洗油北")
    if source_line in {"存1线", "存2线", "存3线", "存5线北", "存5线南", "存4南"}:
        return ("机南", "机走棚", "机走北", "洗油北")
    return ("机南", "机走棚", "机走北", "洗油北")


def _is_cun4_final_target(target_line: str) -> bool:
    return target_line == "卸轮线"


def _source_rank(line: str) -> int:
    priority_order = (
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
        "存4线",
        "机南",
        "机走棚",
        "机走北",
        "洗油北",
    )
    priority = {name: index for index, name in enumerate(priority_order)}
    return priority.get(line, 20)


def _target_rank(line: str) -> int:
    if line in physical.DEPOT_LINES:
        return 0
    if line in physical.DEPOT_OUTSIDE_LINES:
        return 1
    if line == "卸轮线":
        return 2
    raise ValueError(f"unexpected_depot_inbound_target:{line}")
