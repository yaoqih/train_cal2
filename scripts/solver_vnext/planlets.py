from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import legacy_adapter as legacy
from .placement import planned_positions_for_batch


@dataclass(frozen=True)
class TailDigestPlan:
    candidate: Any
    progressed_nos: tuple[str, ...]
    source_return_nos: tuple[str, ...]
    put_lines: tuple[str, ...]


def build_tail_digest_planlet(
    *,
    case_id: str,
    hook_index: int,
    source_line: str,
    prefix: list[dict[str, Any]],
    cars: list[dict[str, Any]],
    depot_assignment: Any,
    target_line_for_car: Any,
    restore_remaining_to_source: bool,
    reason: str,
    candidate_kind: str,
) -> TailDigestPlan | None:
    if not prefix:
        return None
    carried = [legacy.car_no(car) for car in prefix]
    remaining = list(carried)
    steps = [legacy.plan_step("Get", source_line, tuple(carried))]
    progressed_nos: list[str] = []
    put_lines: list[str] = []

    while remaining:
        tail_no = remaining[-1]
        tail_car = next(car for car in prefix if legacy.car_no(car) == tail_no)
        target_line = target_line_for_car(tail_car)
        if not target_line or target_line == source_line:
            break
        drop: list[str] = []
        for no in reversed(remaining):
            car = next(item for item in prefix if legacy.car_no(item) == no)
            if target_line_for_car(car) != target_line:
                break
            drop.append(no)
        drop = list(reversed(drop))
        group = [car for car in prefix if legacy.car_no(car) in set(drop)]
        group_nos = {legacy.car_no(car) for car in group}
        positions = planned_positions_for_batch(
            batch=group,
            target_line=target_line,
            cars=cars,
            depot_assignment=depot_assignment,
            batch_nos=group_nos,
        )
        if len(positions) != len(group):
            return None
        group_candidate = legacy.build_direct_candidate(
            case_id=case_id,
            hook_index=hook_index,
            source_line=source_line,
            target_line=target_line,
            batch=group,
            cars=cars,
            depot_assignment=depot_assignment,
            reason="vnext:tail_digest_position_probe",
            candidate_kind="vnext_position_probe",
            planned_positions=positions,
        )
        if group_candidate is None:
            return None
        steps.append(legacy.plan_step("Put", target_line, tuple(drop), group_candidate.planned_positions))
        put_lines.append(target_line)
        progressed_nos.extend(drop)
        remaining = remaining[: -len(drop)]

    if not progressed_nos:
        return None

    source_return_nos: tuple[str, ...] = ()
    if remaining:
        if not restore_remaining_to_source:
            return None
        source_return_nos = tuple(remaining)
        restored_positions = {
            no: int(next(car for car in prefix if legacy.car_no(car) == no).get("Position") or 0)
            for no in source_return_nos
        }
        steps.append(legacy.plan_step("Put", source_line, source_return_nos, restored_positions))
        put_lines.append(source_line)

    candidate = legacy.build_planlet_candidate(
        case_id=case_id,
        hook_index=hook_index,
        source_line=source_line,
        target_line=steps[-1].line,
        batch=prefix,
        steps=tuple(steps),
        reason=reason,
        candidate_kind=candidate_kind,
    )
    return TailDigestPlan(
        candidate=candidate,
        progressed_nos=tuple(progressed_nos),
        source_return_nos=source_return_nos,
        put_lines=tuple(dict.fromkeys(put_lines)),
    )
