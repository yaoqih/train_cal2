from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import traceback
from typing import Any, Callable, Mapping
import uuid


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = _runtime_root()
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


CASE_ID_PATTERN = r"[0-9]{4}[WZ]"
MAX_REQUEST_CARS = 1000
MAX_TOTAL_TIME_BUDGET_SECONDS = 1800.0
STAGE_NAMES = {
    1: "stage1_assembly",
    2: "stage2_depot_outbound",
    3: "stage3_depot_inbound",
    4: "stage4_residual_closure",
}


class PipelineOptionError(ValueError):
    """Raised when a public pipeline option is invalid."""


@dataclass(frozen=True)
class PipelineOptions:
    stage1_max_hooks: int = 80
    stage1_time_budget_seconds: float = 300.0
    stage1_profile: str = "balanced"
    stage2_time_budget_seconds: float = 300.0
    stage3_time_budget_seconds: float = 180.0
    stage4_time_budget_seconds: float = 300.0
    stage4_max_macros: int = 160
    stage4_max_candidates_per_step: int = 96

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PipelineOptions":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise PipelineOptionError("options 必须是 JSON 对象")

        allowed_top = {"stage1", "stage2", "stage3", "stage4"}
        unknown_top = sorted(set(raw) - allowed_top)
        if unknown_top:
            raise PipelineOptionError(f"未知 options 字段: {','.join(unknown_top)}")

        stage1 = _option_section(raw, "stage1", {"max_hooks", "time_budget_seconds", "profile"})
        stage2 = _option_section(raw, "stage2", {"time_budget_seconds"})
        stage3 = _option_section(raw, "stage3", {"time_budget_seconds"})
        stage4 = _option_section(
            raw,
            "stage4",
            {"time_budget_seconds", "max_macros", "max_candidates_per_step"},
        )

        options = cls(
            stage1_max_hooks=_bounded_int(stage1.get("max_hooks", 80), "stage1.max_hooks", 1, 500),
            stage1_time_budget_seconds=_bounded_float(
                stage1.get("time_budget_seconds", 300.0),
                "stage1.time_budget_seconds",
                0.1,
                900.0,
            ),
            stage1_profile=_choice(
                stage1.get("profile", "balanced"),
                "stage1.profile",
                {"baseline", "balanced", "stage3", "stage4"},
            ),
            stage2_time_budget_seconds=_bounded_float(
                stage2.get("time_budget_seconds", 300.0),
                "stage2.time_budget_seconds",
                0.1,
                900.0,
            ),
            stage3_time_budget_seconds=_bounded_float(
                stage3.get("time_budget_seconds", 180.0),
                "stage3.time_budget_seconds",
                0.1,
                900.0,
            ),
            stage4_time_budget_seconds=_bounded_float(
                stage4.get("time_budget_seconds", 300.0),
                "stage4.time_budget_seconds",
                0.1,
                900.0,
            ),
            stage4_max_macros=_bounded_int(
                stage4.get("max_macros", 160),
                "stage4.max_macros",
                1,
                500,
            ),
            stage4_max_candidates_per_step=_bounded_int(
                stage4.get("max_candidates_per_step", 96),
                "stage4.max_candidates_per_step",
                1,
                256,
            ),
        )
        total_budget = (
            options.stage1_time_budget_seconds
            + options.stage2_time_budget_seconds
            + options.stage3_time_budget_seconds
            + options.stage4_time_budget_seconds
        )
        if total_budget > MAX_TOTAL_TIME_BUDGET_SECONDS:
            raise PipelineOptionError(
                f"四阶段 time_budget_seconds 合计不能超过 {MAX_TOTAL_TIME_BUDGET_SECONDS:g} 秒"
            )
        return options

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "stage1": {
                "max_hooks": self.stage1_max_hooks,
                "time_budget_seconds": self.stage1_time_budget_seconds,
                "profile": self.stage1_profile,
            },
            "stage2": {"time_budget_seconds": self.stage2_time_budget_seconds},
            "stage3": {"time_budget_seconds": self.stage3_time_budget_seconds},
            "stage4": {
                "time_budget_seconds": self.stage4_time_budget_seconds,
                "max_macros": self.stage4_max_macros,
                "max_candidates_per_step": self.stage4_max_candidates_per_step,
            },
        }


def _option_section(
    raw: Mapping[str, Any],
    name: str,
    allowed: set[str],
) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise PipelineOptionError(f"options.{name} 必须是 JSON 对象")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PipelineOptionError(f"未知 options.{name} 字段: {','.join(unknown)}")
    return value


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PipelineOptionError(f"{name} 必须是整数")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise PipelineOptionError(f"{name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PipelineOptionError(f"{name} 必须是整数") from exc
    if parsed < minimum or parsed > maximum:
        raise PipelineOptionError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise PipelineOptionError(f"{name} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PipelineOptionError(f"{name} 必须是数字") from exc
    if not math.isfinite(parsed):
        raise PipelineOptionError(f"{name} 必须是有限数字")
    if parsed < minimum or parsed > maximum:
        raise PipelineOptionError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return parsed


def _choice(value: Any, name: str, choices: set[str]) -> str:
    parsed = str(value or "")
    if parsed not in choices:
        raise PipelineOptionError(f"{name} 必须是: {','.join(sorted(choices))}")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_process_initializer() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.umask(0o077)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_plan_request(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["请求体必须是 JSON 对象"]

    errors = _strict_request_errors(payload)
    if errors:
        return list(dict.fromkeys(errors))

    from solver_vnext import physical
    import replay_validator as rv

    for index, car in enumerate(payload["StartStatus"], start=1):
        for target_index, target in enumerate(car["TargetLines"], start=1):
            if rv.norm(target) not in rv.TRACK_LEN:
                errors.append(f"StartStatus[{index}].TargetLines[{target_index}]_unknown:{target}")
        if car.get("ForceTargetPosition") and len(car["TargetLines"]) > 1:
            normalized_targets = {rv.norm(target) for target in car["TargetLines"]}
            if not normalized_targets <= rv.DEPOT:
                errors.append(
                    f"StartStatus[{index}].ForceTargetPosition_multiple_non_depot_targets"
                )
    for index, line in enumerate(payload["TerminalLines"], start=1):
        if rv.norm(line["Line"]) not in rv.DEPOT:
            errors.append(f"TerminalLines[{index}].Line_not_depot_inner:{line['Line']}")
    loco_line = rv.norm(payload["locoNode"]["Line"])
    if loco_line not in rv.TRACK_LEN and loco_line not in rv.RUNNING:
        errors.append(f"locoNode.Line_unknown:{payload['locoNode']['Line']}")
    if errors:
        return list(dict.fromkeys(errors))

    try:
        _ok, physical_errors = physical.validate_input(payload)
        errors.extend(physical_errors)
    except Exception as exc:
        errors.append(f"input_validation_exception:{type(exc).__name__}")
        return list(dict.fromkeys(errors))
    try:
        replayed, violations = rv.replay(payload, {"Data": {"Operations": []}})
        del replayed
    except Exception as exc:
        errors.append(f"replay_validation_exception:{type(exc).__name__}")
        return list(dict.fromkeys(errors))
    errors.extend(
        f"{item.code}:{item.detail}" if item.detail else item.code
        for item in violations
        if item.kind == "schema"
    )
    return list(dict.fromkeys(errors))


def _strict_request_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    start_status = payload.get("StartStatus")
    if not isinstance(start_status, list):
        errors.append("StartStatus_missing_or_not_list")
    else:
        if len(start_status) > MAX_REQUEST_CARS:
            errors.append(f"StartStatus_too_many_cars:{len(start_status)}>{MAX_REQUEST_CARS}")
        for index, car in enumerate(start_status, start=1):
            prefix = f"StartStatus[{index}]"
            if not isinstance(car, dict):
                errors.append(f"{prefix}_not_object")
                continue
            for key in ("Line", "RepairProcess", "Type", "No"):
                value = car.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{prefix}.{key}_missing_or_not_string")
            position = car.get("Position")
            if type(position) is not int or position < 0:
                errors.append(f"{prefix}.Position_not_nonnegative_integer")
            length = car.get("Length")
            try:
                numeric_length = float(length)
            except (TypeError, ValueError, OverflowError):
                numeric_length = math.nan
            if isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(numeric_length) or numeric_length <= 0.0:
                errors.append(f"{prefix}.Length_not_positive_finite_number")
            targets = car.get("TargetLines")
            if not isinstance(targets, list) or not targets:
                errors.append(f"{prefix}.TargetLines_missing_or_not_nonempty_list")
            elif any(not isinstance(target, str) or not target.strip() for target in targets):
                errors.append(f"{prefix}.TargetLines_contains_invalid_line")
            force_positions = car.get("ForceTargetPosition")
            if force_positions is not None:
                if not isinstance(force_positions, list):
                    errors.append(f"{prefix}.ForceTargetPosition_not_list")
                elif any(type(value) is not int or value <= 0 for value in force_positions):
                    errors.append(f"{prefix}.ForceTargetPosition_contains_invalid_position")
            for key in ("IsHeavy", "IsWeigh", "IsClosedDoor"):
                if key in car and type(car[key]) is not bool:
                    errors.append(f"{prefix}.{key}_not_boolean")

    terminal_lines = payload.get("TerminalLines")
    if not isinstance(terminal_lines, list):
        errors.append("TerminalLines_missing_or_not_list")
    else:
        for index, line in enumerate(terminal_lines, start=1):
            prefix = f"TerminalLines[{index}]"
            if not isinstance(line, dict):
                errors.append(f"{prefix}_not_object")
                continue
            if not isinstance(line.get("Line"), str) or not line["Line"].strip():
                errors.append(f"{prefix}.Line_missing_or_not_string")
            if "IsInspectionMode" in line and type(line["IsInspectionMode"]) is not bool:
                errors.append(f"{prefix}.IsInspectionMode_not_boolean")

    loco = payload.get("locoNode")
    if not isinstance(loco, dict):
        errors.append("locoNode_missing_or_not_object")
    else:
        if not isinstance(loco.get("Line"), str) or not loco["Line"].strip():
            errors.append("locoNode.Line_missing_or_not_string")
        if type(loco.get("End")) is not str or loco.get("End") not in {"North", "South"}:
            errors.append("locoNode.End_invalid")
    return errors


def execute_job(job_dir_text: str) -> dict[str, Any]:
    """Process-pool entry point. All mutable state is isolated by job directory."""

    job_dir = Path(job_dir_text)
    job_path = job_dir / "job.json"
    job = read_json(job_path)
    case_id = str(job["case_id"])
    options = PipelineOptions.from_mapping(job.get("options"))
    input_path = job_dir / str(job["input_file"])

    _update_job(
        job_dir,
        status="running",
        solve_status=None,
        started_at=utc_now(),
        current_stage=0,
        current_stage_name="preparing",
    )

    def progress(event: dict[str, Any]) -> None:
        changes = dict(event)
        changes["updated_at"] = utc_now()
        _update_job(job_dir, **changes)

    try:
        result = run_pipeline(
            input_path=input_path,
            job_dir=job_dir,
            case_id=case_id,
            options=options,
            progress=progress,
        )
    except Exception as exc:  # defensive boundary around the complete worker
        logs_dir = job_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs_dir.chmod(0o700)
        pipeline_log = logs_dir / "pipeline.log"
        pipeline_log.write_text(traceback.format_exc(), encoding="utf-8")
        pipeline_log.chmod(0o600)
        result = _failed_result(
            case_id=case_id,
            message=f"pipeline_exception:{type(exc).__name__}",
        )

    result["job_id"] = job.get("job_id")
    atomic_write_json(job_dir / "result.json", result)
    solve_status = str(result.get("solve_status") or "failed")
    job_status = {
        "complete": "succeeded",
        "partial": "partial",
        "failed": "failed",
    }.get(solve_status, "failed")
    _update_job(
        job_dir,
        status=job_status,
        solve_status=solve_status,
        current_stage=result.get("attempted_stage", 0),
        current_stage_name="finished",
        completed_stage=result.get("completed_stage", 0),
        last_safe_stage=result.get("last_safe_stage", 0),
        stage_summaries=result.get("stage_summaries", {}),
        finished_at=utc_now(),
        result_file="result.json",
        error=result.get("error"),
    )
    return {
        "job_id": job.get("job_id"),
        "case_id": case_id,
        "status": job_status,
        "solve_status": solve_status,
    }


def _update_job(job_dir: Path, **changes: Any) -> None:
    job_path = job_dir / "job.json"
    current = read_json(job_path) if job_path.exists() else {}
    current.update(changes)
    current["updated_at"] = changes.get("updated_at") or utc_now()
    atomic_write_json(job_path, current)


def run_pipeline(
    *,
    input_path: Path,
    job_dir: Path,
    case_id: str,
    options: PipelineOptions,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    progress = progress or (lambda _event: None)
    request = read_json(input_path)
    prepared_request, depot_assignment = _prepare_case(input_path)
    request = prepared_request

    stage_summaries: dict[str, Any] = {}
    replay_gates: dict[str, Any] = {}
    latest_safe_response: dict[str, Any] = {"Data": {"Operations": []}}
    completed_stage = 0
    last_safe_stage = 0
    attempted_stage = 0
    solve_status = "failed"
    error: str | None = None

    stage_calls: list[tuple[int, Callable[[], dict[str, Any]], str]] = [
        (1, lambda: _run_stage1(input_path, options), "response"),
    ]

    while stage_calls:
        stage_number, stage_call, response_key = stage_calls.pop(0)
        attempted_stage = stage_number
        stage_name = STAGE_NAMES[stage_number]
        progress(
            {
                "status": "running",
                "current_stage": stage_number,
                "current_stage_name": stage_name,
            }
        )
        started = time.monotonic()
        try:
            result = _call_with_stage_log(job_dir, stage_number, stage_call)
        except Exception as exc:
            error = f"stage{stage_number}_exception:{type(exc).__name__}"
            _append_stage_exception(job_dir, stage_number, exc)
            solve_status = "failed"
            break

        _write_stage_artifacts(job_dir, case_id, stage_number, result)
        summary = deepcopy(result.get("summary") or {})
        summary["api_elapsed_seconds"] = round(time.monotonic() - started, 3)
        stage_summaries[str(stage_number)] = summary

        response = result.get(response_key)
        if not isinstance(response, dict):
            error = f"stage{stage_number}_response_missing:{response_key}"
            solve_status = "failed"
            break

        gate = replay_gate(request, response)
        replay_gates[str(stage_number)] = gate
        atomic_write_json(job_dir / f"stage{stage_number}" / f"{case_id}_replay_gate.json", gate)
        progress(
            {
                "current_stage": stage_number,
                "current_stage_name": stage_name,
                "stage_summaries": stage_summaries,
            }
        )
        if not gate["ok"]:
            error = f"stage{stage_number}_replay_failed"
            solve_status = "failed"
            break

        latest_safe_response = response
        last_safe_stage = stage_number
        progress(
            {
                "current_stage": stage_number,
                "current_stage_name": stage_name,
                "completed_stage": completed_stage,
                "last_safe_stage": last_safe_stage,
                "stage_summaries": stage_summaries,
            }
        )
        stage_status = str(summary.get("status") or "partial")
        if stage_status != "complete":
            solve_status = "partial" if stage_status == "partial" else "failed"
            if solve_status == "failed":
                error = f"stage{stage_number}_status:{stage_status}"
            break
        completed_stage = stage_number
        progress(
            {
                "current_stage": stage_number,
                "current_stage_name": stage_name,
                "completed_stage": completed_stage,
                "last_safe_stage": last_safe_stage,
                "stage_summaries": stage_summaries,
            }
        )

        if stage_number == 1:
            stage1_response = result["response"]
            stage_calls.append(
                (
                    2,
                    lambda stage1_response=stage1_response: _run_stage2(
                        case_id,
                        request,
                        stage1_response,
                        options,
                    ),
                    "combined_response",
                )
            )
        elif stage_number == 2:
            stage2_combined = result["combined_response"]
            stage_calls.append(
                (
                    3,
                    lambda stage2_combined=stage2_combined: _run_stage3(
                        case_id,
                        request,
                        stage2_combined,
                        options,
                    ),
                    "combined_response",
                )
            )
        elif stage_number == 3:
            stage3_result = result
            stage_calls.append(
                (
                    4,
                    lambda stage3_result=stage3_result: _run_stage4(
                        case_id,
                        request,
                        depot_assignment,
                        stage3_result,
                        options,
                    ),
                    "combined_response",
                )
            )
        else:
            solve_status = "complete"

    if completed_stage == 4 and stage_summaries.get("4", {}).get("status") == "complete" and not error:
        solve_status = "complete"
    elif solve_status not in {"partial", "failed"}:
        solve_status = "partial"

    public_response = build_public_response(
        request=request,
        response=latest_safe_response,
        solve_status=solve_status,
        stage_summaries=stage_summaries,
        attempted_stage=attempted_stage,
        error=error,
    )
    operations = ((public_response.get("Data") or {}).get("Operations") or [])
    return {
        "case_id": case_id,
        "solve_status": solve_status,
        "completed_stage": completed_stage,
        "last_safe_stage": last_safe_stage,
        "attempted_stage": attempted_stage,
        "operation_count": len(operations),
        "get_put_hook_count": sum(
            1 for row in operations if str(row.get("Action") or "") in {"Get", "Put"}
        ),
        "weigh_operation_count": sum(
            1 for row in operations if str(row.get("Action") or "") == "Weigh"
        ),
        "stage_summaries": stage_summaries,
        "replay_gates": replay_gates,
        "response": public_response,
        "error": error,
    }


def _prepare_case(input_path: Path) -> tuple[dict[str, Any], Any]:
    from solver_vnext import physical

    _case_id, request, _cars, depot_assignment, _loco = physical.read_case(input_path)
    return request, depot_assignment


def _run_stage1(input_path: Path, options: PipelineOptions) -> dict[str, Any]:
    from stage1_simple.solve import Stage1Solver

    return Stage1Solver(
        input_path,
        max_hooks=options.stage1_max_hooks,
        time_budget_seconds=options.stage1_time_budget_seconds,
        profile=options.stage1_profile,
    ).solve()


def _run_stage2(
    case_id: str,
    request: dict[str, Any],
    stage1_response: dict[str, Any],
    options: PipelineOptions,
) -> dict[str, Any]:
    from stage2_simple.solve import Stage2Solver

    return Stage2Solver(
        case_id,
        request,
        stage1_response,
        time_budget_seconds=options.stage2_time_budget_seconds,
        allow_depot_in_buffer=False,
        accept_upper_bound=False,
    ).solve()


def _run_stage3(
    case_id: str,
    request: dict[str, Any],
    stage2_combined_response: dict[str, Any],
    options: PipelineOptions,
) -> dict[str, Any]:
    from stage3_simple.solve import Stage3Solver

    return Stage3Solver(
        case_id,
        request,
        stage2_combined_response,
        time_budget_seconds=options.stage3_time_budget_seconds,
    ).solve()


def _run_stage4(
    case_id: str,
    request: dict[str, Any],
    depot_assignment: Any,
    stage3_result: dict[str, Any],
    options: PipelineOptions,
) -> dict[str, Any]:
    from stage4_simple.solve import run_solver

    args = argparse.Namespace(
        time_budget_seconds=options.stage4_time_budget_seconds,
        max_macros=options.stage4_max_macros,
        max_candidates_per_step=options.stage4_max_candidates_per_step,
    )
    return run_solver(
        case_id=case_id,
        request=request,
        depot_assignment=depot_assignment,
        stage3_request=stage3_result["stage3_request"],
        stage3_response=stage3_result["response"],
        stage3_combined_response=stage3_result["combined_response"],
        args=args,
    )


def _call_with_stage_log(
    job_dir: Path,
    stage_number: int,
    call: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    logs_dir = job_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs_dir.chmod(0o700)
    log_path = logs_dir / f"stage{stage_number}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        log_path.chmod(0o600)
        with redirect_stdout(handle), redirect_stderr(handle):
            return call()


def _append_stage_exception(job_dir: Path, stage_number: int, exc: Exception) -> None:
    logs_dir = job_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs_dir.chmod(0o700)
    log_path = logs_dir / f"stage{stage_number}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        log_path.chmod(0o600)
        handle.write(f"\n{type(exc).__name__}: {exc}\n")
        handle.write(traceback.format_exc())


def _write_stage_artifacts(
    job_dir: Path,
    case_id: str,
    stage_number: int,
    result: dict[str, Any],
) -> None:
    stage_dir = job_dir / f"stage{stage_number}"
    stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage_dir.chmod(0o700)
    key_names = {
        1: {
            "response": f"{case_id}_response.json",
            "summary": f"{case_id}_summary.json",
            "trace": f"{case_id}_trace.json",
        },
        2: {
            "stage2_request": f"{case_id}_stage2_request.json",
            "response": f"{case_id}_response.json",
            "combined_response": f"{case_id}_combined_response.json",
            "summary": f"{case_id}_summary.json",
            "trace": f"{case_id}_trace.json",
        },
        3: {
            "stage3_request": f"{case_id}_stage3_request.json",
            "response": f"{case_id}_response.json",
            "combined_response": f"{case_id}_combined_response.json",
            "summary": f"{case_id}_summary.json",
            "trace": f"{case_id}_trace.json",
        },
        4: {
            "stage4_request": f"{case_id}_stage4_request.json",
            "response": f"{case_id}_response.json",
            "combined_response": f"{case_id}_combined_response.json",
            "summary": f"{case_id}_summary.json",
            "trace": f"{case_id}_trace.json",
        },
    }[stage_number]
    for key, filename in key_names.items():
        if key in result:
            atomic_write_json(stage_dir / filename, result[key])


def replay_gate(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    import replay_validator as rv

    replayed, violations = rv.replay(request, response)
    del replayed
    rows = [
        {
            "index": int(item.index),
            "kind": str(item.kind),
            "code": str(item.code),
            "detail": str(item.detail),
        }
        for item in violations
    ]
    return {"ok": not rows, "violation_count": len(rows), "violations": rows}


def build_public_response(
    *,
    request: dict[str, Any],
    response: dict[str, Any],
    solve_status: str,
    stage_summaries: dict[str, Any],
    attempted_stage: int,
    error: str | None,
) -> dict[str, Any]:
    import replay_validator as rv

    operations = deepcopy(((response.get("Data") or {}).get("Operations") or []))
    replay_response = {"Data": {"Operations": operations}}
    replayed, violations = rv.replay(request, replay_response)
    generated = [
        {
            "No": rv.car_no(car),
            "Line": rv.norm(car.get("Line")),
            "Position": int(car.get("Position") or 0),
        }
        for car in replayed
    ]
    data = {"Operations": operations, "GeneratedEndStatus": generated}

    if solve_status == "complete" and not violations:
        return {"Success": True, "Message": "", "StatusCode": 200, "Data": data}

    if solve_status == "partial":
        summary = stage_summaries.get(str(attempted_stage), {})
        reasons = [str(item) for item in summary.get("blocking_reasons") or []][:3]
        suffix = f": {'; '.join(reasons)}" if reasons else ""
        message = f"第 {attempted_stage} 阶段返回部分解{suffix}"
        return {"Success": False, "Message": message, "StatusCode": 200, "Data": data}

    detail = error or "pipeline_failed"
    return {
        "Success": False,
        "Message": f"四阶段求解失败: {detail}",
        "StatusCode": 500,
        "Data": data,
    }


def _failed_result(case_id: str, message: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "solve_status": "failed",
        "completed_stage": 0,
        "last_safe_stage": 0,
        "attempted_stage": 0,
        "operation_count": 0,
        "get_put_hook_count": 0,
        "weigh_operation_count": 0,
        "stage_summaries": {},
        "replay_gates": {},
        "response": {
            "Success": False,
            "Message": f"四阶段求解失败: {message}",
            "StatusCode": 500,
            "Data": {"Operations": [], "GeneratedEndStatus": []},
        },
        "error": message,
    }


def write_terminal_failure(job_dir_text: str, error_code: str) -> dict[str, Any]:
    """Persist a terminal failure after an external supervisor stops a worker."""

    job_dir = Path(job_dir_text)
    result_path = job_dir / "result.json"
    if result_path.exists():
        try:
            existing = read_json(result_path)
        except Exception:
            existing = None
        if isinstance(existing, dict):
            return existing

    job = read_json(job_dir / "job.json")
    case_id = str(job.get("case_id") or "")
    stage_summaries = job.get("stage_summaries") or {}
    replay_gates: dict[str, Any] = {}
    latest_response: dict[str, Any] = {"Data": {"Operations": []}}
    last_safe_stage = 0
    response_names = {
        1: f"{case_id}_response.json",
        2: f"{case_id}_combined_response.json",
        3: f"{case_id}_combined_response.json",
        4: f"{case_id}_combined_response.json",
    }
    for stage_number in range(1, 5):
        stage_dir = job_dir / f"stage{stage_number}"
        gate_path = stage_dir / f"{case_id}_replay_gate.json"
        response_path = stage_dir / response_names[stage_number]
        if not gate_path.exists() or not response_path.exists():
            continue
        try:
            gate = read_json(gate_path)
            response = read_json(response_path)
        except Exception:
            continue
        replay_gates[str(stage_number)] = gate
        if gate.get("ok") is True:
            latest_response = response
            last_safe_stage = stage_number

    input_path = job_dir / str(job.get("input_file") or "")
    try:
        request = read_json(input_path)
        public_response = build_public_response(
            request=request,
            response=latest_response,
            solve_status="failed",
            stage_summaries=stage_summaries,
            attempted_stage=int(job.get("current_stage") or 0),
            error=error_code,
        )
    except Exception:
        public_response = _failed_result(case_id, error_code)["response"]

    operations = ((public_response.get("Data") or {}).get("Operations") or [])
    result = {
        "job_id": job.get("job_id"),
        "case_id": case_id,
        "solve_status": "failed",
        "completed_stage": int(job.get("completed_stage") or 0),
        "last_safe_stage": max(int(job.get("last_safe_stage") or 0), last_safe_stage),
        "attempted_stage": int(job.get("current_stage") or 0),
        "operation_count": len(operations),
        "get_put_hook_count": sum(
            1 for row in operations if str(row.get("Action") or "") in {"Get", "Put"}
        ),
        "weigh_operation_count": sum(
            1 for row in operations if str(row.get("Action") or "") == "Weigh"
        ),
        "stage_summaries": stage_summaries,
        "replay_gates": replay_gates,
        "response": public_response,
        "error": error_code,
    }
    atomic_write_json(result_path, result)
    _update_job(
        job_dir,
        status="failed",
        solve_status="failed",
        current_stage_name="finished",
        completed_stage=result["completed_stage"],
        last_safe_stage=result["last_safe_stage"],
        finished_at=utc_now(),
        result_file="result.json",
        error=error_code,
    )
    return result
