from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plan_api import pipeline
from plan_api import server
from plan_api import launcher
from plan_api.pipeline import (
    PipelineOptionError,
    PipelineOptions,
    atomic_write_json,
    validate_plan_request,
)
from plan_api.server import (
    JobManager,
    ServiceShuttingDownError,
    normalize_case_id,
    parse_single_submission,
)


def _request(no: str = "1000001") -> dict:
    return {
        "StartStatus": [
            {
                "Line": "存1线",
                "Position": 1,
                "RepairProcess": "段",
                "Type": "C70",
                "No": no,
                "Length": 14.3,
                "TargetLines": ["存1线"],
            }
        ],
        "TerminalLines": [
            {"Line": "修1库内", "IsInspectionMode": False},
            {"Line": "修2库内", "IsInspectionMode": False},
            {"Line": "修3库内", "IsInspectionMode": False},
            {"Line": "修4库内", "IsInspectionMode": False},
        ],
        "locoNode": {"Line": "机库线", "End": "North"},
    }


def _empty_response() -> dict:
    return {"Data": {"Operations": []}}


def _stage1(status: str = "complete") -> dict:
    return {
        "response": _empty_response(),
        "summary": {"case_id": "0101Z", "status": status, "blocking_reasons": []},
        "trace": [],
    }


def _stage2(status: str = "complete", response: dict | None = None) -> dict:
    combined = response or _empty_response()
    return {
        "stage2_request": {},
        "response": _empty_response(),
        "combined_response": combined,
        "summary": {
            "case_id": "0101Z",
            "status": status,
            "blocking_reasons": ["stage2_probe"] if status != "complete" else [],
        },
        "trace": [],
    }


def _stage3(status: str = "complete") -> dict:
    return {
        "stage3_request": {},
        "response": _empty_response(),
        "combined_response": _empty_response(),
        "summary": {"case_id": "0101Z", "status": status, "blocking_reasons": []},
        "trace": [],
    }


def _stage4(status: str = "complete") -> dict:
    return {
        "stage4_request": {},
        "response": _empty_response(),
        "combined_response": _empty_response(),
        "summary": {"case_id": "0101Z", "status": status, "blocking_reasons": []},
        "trace": [],
    }


def _run_fake_pipeline(tmp_path: Path, **patches):
    request = _request()
    input_path = tmp_path / "input" / "validation_api_0101Z.json"
    atomic_write_json(input_path, request)
    defaults = {
        "_prepare_case": (request, object()),
        "_run_stage1": _stage1(),
        "_run_stage2": _stage2(),
        "_run_stage3": _stage3(),
        "_run_stage4": _stage4(),
    }
    defaults.update(patches)
    with (
        patch.object(pipeline, "_prepare_case", return_value=defaults["_prepare_case"]),
        patch.object(pipeline, "_run_stage1", return_value=defaults["_run_stage1"]) as stage1,
        patch.object(pipeline, "_run_stage2", return_value=defaults["_run_stage2"]) as stage2,
        patch.object(pipeline, "_run_stage3", return_value=defaults["_run_stage3"]) as stage3,
        patch.object(pipeline, "_run_stage4", return_value=defaults["_run_stage4"]) as stage4,
    ):
        result = pipeline.run_pipeline(
            input_path=input_path,
            job_dir=tmp_path,
            case_id="0101Z",
            options=PipelineOptions(),
        )
    return result, (stage1, stage2, stage3, stage4)


def test_pipeline_runs_four_stages_in_order_and_returns_combined_response(tmp_path: Path) -> None:
    calls: list[str] = []
    invalid_delta = {
        "Data": {
            "Operations": [
                {
                    "Index": 99,
                    "Action": "Get",
                    "Line": "存1线",
                    "MoveCars": ["DELTA_ONLY"],
                    "TrainCars": ["DELTA_ONLY"],
                    "PassbyPath": ["存1线"],
                }
            ]
        }
    }
    stage1_result = _stage1()
    stage2_result = _stage2()
    stage2_result["response"] = invalid_delta
    stage3_result = _stage3()
    stage3_result["response"] = invalid_delta
    stage4_result = _stage4()
    stage4_result["response"] = invalid_delta

    def stage1(*_args, **_kwargs):
        calls.append("stage1")
        return stage1_result

    def stage2(case_id, request_arg, stage1_response, _options):
        calls.append("stage2")
        assert case_id == "0101Z"
        assert request_arg is request
        assert stage1_response is stage1_result["response"]
        return stage2_result

    def stage3(case_id, request_arg, stage2_combined_response, _options):
        calls.append("stage3")
        assert case_id == "0101Z"
        assert request_arg is request
        assert stage2_combined_response is stage2_result["combined_response"]
        return stage3_result

    def stage4(case_id, request_arg, depot_assignment, received_stage3_result, _options):
        calls.append("stage4")
        assert case_id == "0101Z"
        assert request_arg is request
        assert depot_assignment is assignment
        assert received_stage3_result is stage3_result
        return stage4_result

    request = _request()
    assignment = object()
    input_path = tmp_path / "input" / "validation_api_0101Z.json"
    atomic_write_json(input_path, request)
    with (
        patch.object(pipeline, "_prepare_case", return_value=(request, assignment)),
        patch.object(pipeline, "_run_stage1", side_effect=stage1),
        patch.object(pipeline, "_run_stage2", side_effect=stage2),
        patch.object(pipeline, "_run_stage3", side_effect=stage3),
        patch.object(pipeline, "_run_stage4", side_effect=stage4),
    ):
        result = pipeline.run_pipeline(
            input_path=input_path,
            job_dir=tmp_path,
            case_id="0101Z",
            options=PipelineOptions(),
        )

    assert calls == ["stage1", "stage2", "stage3", "stage4"]
    assert result["solve_status"] == "complete"
    assert result["completed_stage"] == 4
    assert result["response"]["Success"] is True
    assert result["response"]["Data"]["GeneratedEndStatus"] == [
        {"No": "1000001", "Line": "存1线", "Position": 1}
    ]
    assert (tmp_path / "stage4/0101Z_combined_response.json").exists()
    assert all(result["replay_gates"][str(stage)]["ok"] for stage in range(1, 5))


def test_pipeline_stops_at_first_partial_and_returns_latest_safe_plan(tmp_path: Path) -> None:
    result, (_stage1_mock, _stage2_mock, stage3, stage4) = _run_fake_pipeline(
        tmp_path,
        _run_stage2=_stage2(status="partial"),
    )

    assert result["solve_status"] == "partial"
    assert result["completed_stage"] == 1
    assert result["last_safe_stage"] == 2
    assert result["attempted_stage"] == 2
    assert result["response"]["Success"] is False
    assert "stage2_probe" in result["response"]["Message"]
    stage3.assert_not_called()
    stage4.assert_not_called()


def test_pipeline_replay_gate_rejects_invalid_downstream_response(tmp_path: Path) -> None:
    invalid = {
        "Data": {
            "Operations": [
                {
                    "Index": 1,
                    "Action": "Get",
                    "Line": "存1线",
                    "MoveCars": ["UNKNOWN"],
                    "TrainCars": ["UNKNOWN"],
                    "PassbyPath": ["机库线", "存1线"],
                }
            ]
        }
    }
    result, (_stage1_mock, _stage2_mock, stage3, stage4) = _run_fake_pipeline(
        tmp_path,
        _run_stage2=_stage2(response=invalid),
    )

    assert result["solve_status"] == "failed"
    assert result["completed_stage"] == 1
    assert result["attempted_stage"] == 2
    assert result["replay_gates"]["2"]["ok"] is False
    assert result["response"]["Data"]["Operations"] == []
    stage3.assert_not_called()
    stage4.assert_not_called()


def test_options_and_submission_envelope_are_validated() -> None:
    case_id, request, options = parse_single_submission(
        {
            "case_id": "0104w",
            "request": _request(),
            "options": {"stage1": {"max_hooks": 120}, "stage4": {"max_macros": 200}},
        }
    )

    assert case_id == "0104W"
    assert request["StartStatus"][0]["No"] == "1000001"
    assert options.stage1_max_hooks == 120
    assert options.stage4_max_macros == 200

    for removed_field in ("portfolio", "heavy_repack_policy", "ranking_mode"):
        try:
            PipelineOptions.from_mapping({"stage4": {removed_field: "unused"}})
        except PipelineOptionError as exc:
            assert "未知 options.stage4 字段" in str(exc)
        else:
            raise AssertionError(f"removed Stage4 strategy option accepted: {removed_field}")

    try:
        PipelineOptions.from_mapping({"stage2": {"allow_depot_in_buffer": True}})
    except PipelineOptionError as exc:
        assert "未知 options.stage2 字段" in str(exc)
    else:
        raise AssertionError("unsafe stage2 option must not be public")

    for bad_value in (1.9, float("nan"), float("inf")):
        try:
            PipelineOptions.from_mapping({"stage1": {"max_hooks": bad_value}})
        except PipelineOptionError:
            pass
        else:
            raise AssertionError(f"invalid integer option accepted: {bad_value!r}")

    try:
        PipelineOptions.from_mapping({"stage1": {"time_budget_seconds": float("nan")}})
    except PipelineOptionError:
        pass
    else:
        raise AssertionError("NaN time budget must be rejected")

    for invalid_options in (
        {"stage1": False},
        {"stage1": {"time_budget_seconds": 10**1000}},
        {
            "stage1": {"time_budget_seconds": 900},
            "stage2": {"time_budget_seconds": 900},
            "stage3": {"time_budget_seconds": 900},
            "stage4": {"time_budget_seconds": 900},
        },
    ):
        try:
            PipelineOptions.from_mapping(invalid_options)
        except PipelineOptionError:
            pass
        else:
            raise AssertionError(f"invalid options accepted: {invalid_options!r}")


def test_stage4_api_invokes_one_structural_solver() -> None:
    expected = _stage4()
    options = PipelineOptions(
        stage4_time_budget_seconds=123.0,
        stage4_max_macros=77,
        stage4_max_candidates_per_step=55,
    )

    with patch("stage4_simple.solve.run_solver", return_value=expected) as run_solver:
        result = pipeline._run_stage4(
            "0101Z",
            _request(),
            object(),
            _stage3(),
            options,
        )

    assert result is expected
    run_solver.assert_called_once()
    args = run_solver.call_args.kwargs["args"]
    assert vars(args) == {
        "time_budget_seconds": 123.0,
        "max_macros": 77,
        "max_candidates_per_step": 55,
    }


def test_malformed_nested_input_returns_validation_errors_instead_of_crashing() -> None:
    request = _request()
    request["StartStatus"] = [None]

    assert validate_plan_request(request) == ["StartStatus[1]_not_object"]


def test_request_validation_rejects_coercion_prone_field_types() -> None:
    mutations = {
        "Position": 1.9,
        "Length": float("nan"),
        "TargetLines": "存1线",
        "IsHeavy": "false",
    }
    expected_fragments = {
        "Position": "Position_not_nonnegative_integer",
        "Length": "Length_not_positive_finite_number",
        "TargetLines": "TargetLines_missing_or_not_nonempty_list",
        "IsHeavy": "IsHeavy_not_boolean",
    }
    for field, value in mutations.items():
        request = _request()
        request["StartStatus"][0][field] = value
        errors = validate_plan_request(request)
        assert any(expected_fragments[field] in error for error in errors), (field, errors)

    request = _request()
    request["TerminalLines"][0]["IsInspectionMode"] = "false"
    assert any(
        "IsInspectionMode_not_boolean" in error
        for error in validate_plan_request(request)
    )


def test_missing_case_id_is_deterministic_and_job_storage_uses_uuid() -> None:
    request = _request()

    assert normalize_case_id(None, request) == normalize_case_id(None, request)
    assert normalize_case_id(None, request).endswith("Z")
    try:
        normalize_case_id("٠١٠٤W", request)
    except ValueError:
        pass
    else:
        raise AssertionError("case_id must use ASCII digits")


def test_frozen_runtime_uses_executable_root_and_internal_worker_mode() -> None:
    job_dir = Path("/tmp/train-cal-frozen-job")
    executable = "/opt/train-cal-server/train-cal-server.exe"
    with patch.object(server.sys, "frozen", True, create=True), patch.object(
        server.sys,
        "executable",
        executable,
    ):
        assert server._runtime_root() == Path(executable).resolve().parent
        assert pipeline._runtime_root() == Path(executable).resolve().parent
        assert server._worker_command(job_dir) == [
            executable,
            "--worker",
            str(job_dir),
        ]


def test_launcher_worker_mode_dispatches_without_starting_uvicorn(tmp_path: Path) -> None:
    with patch.object(launcher.multiprocessing, "freeze_support") as freeze_support, patch.object(
        pipeline,
        "worker_process_initializer",
    ) as initializer, patch.object(pipeline, "execute_job") as execute_job:
        exit_code = launcher.main(["--worker", str(tmp_path)])

    assert exit_code == 0
    freeze_support.assert_called_once_with()
    initializer.assert_called_once_with()
    execute_job.assert_called_once_with(str(tmp_path.resolve()))


_ACTIVE = 0
_PEAK_ACTIVE = 0
_ACTIVE_LOCK = threading.Lock()


def _parallel_probe_worker(job_dir_text: str) -> dict:
    global _ACTIVE, _PEAK_ACTIVE
    job_dir = Path(job_dir_text)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    with _ACTIVE_LOCK:
        _ACTIVE += 1
        _PEAK_ACTIVE = max(_PEAK_ACTIVE, _ACTIVE)
    try:
        time.sleep(0.08)
        request = json.loads((job_dir / job["input_file"]).read_text(encoding="utf-8"))
        return {"job_id": job["job_id"], "car_no": request["StartStatus"][0]["No"]}
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE -= 1


def _failing_probe_worker(_job_dir_text: str) -> dict:
    raise RuntimeError("probe worker crashed")


def _api_result_worker(job_dir_text: str) -> dict:
    job_dir = Path(job_dir_text)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    result = {
        "job_id": job["job_id"],
        "case_id": job["case_id"],
        "solve_status": "complete",
        "completed_stage": 4,
        "last_safe_stage": 4,
        "attempted_stage": 4,
        "operation_count": 0,
        "get_put_hook_count": 0,
        "weigh_operation_count": 0,
        "stage_summaries": {"4": {"status": "complete"}},
        "replay_gates": {str(stage): {"ok": True} for stage in range(1, 5)},
        "response": {
            "Success": True,
            "Message": "",
            "StatusCode": 200,
            "Data": {"Operations": [], "GeneratedEndStatus": []},
        },
        "error": None,
    }
    atomic_write_json(job_dir / "result.json", result)
    job.update(
        {
            "status": "succeeded",
            "solve_status": "complete",
            "completed_stage": 4,
            "current_stage": 4,
            "current_stage_name": "finished",
            "result_file": "result.json",
        }
    )
    atomic_write_json(job_dir / "job.json", job)
    return {"job_id": job["job_id"], "status": "succeeded"}


def _result_then_raise_worker(job_dir_text: str) -> dict:
    job_dir = Path(job_dir_text)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    atomic_write_json(
        job_dir / "result.json",
        {
            "job_id": job["job_id"],
            "case_id": job["case_id"],
            "solve_status": "complete",
            "completed_stage": 4,
            "last_safe_stage": 4,
            "attempted_stage": 4,
            "stage_summaries": {"4": {"status": "complete"}},
            "error": None,
        },
    )
    raise RuntimeError("crashed after result commit")


def _result_without_job_update_worker(job_dir_text: str) -> dict:
    job_dir = Path(job_dir_text)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    atomic_write_json(
        job_dir / "result.json",
        {
            "job_id": job["job_id"],
            "case_id": job["case_id"],
            "solve_status": "complete",
            "completed_stage": 4,
            "last_safe_stage": 4,
            "attempted_stage": 4,
            "stage_summaries": {"4": {"status": "complete"}},
            "error": None,
        },
    )
    return {"job_id": job["job_id"], "status": "complete_result_committed"}


def _asgi_json_request(
    method: str,
    target: str,
    *,
    payload: object | None = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict]:
    parsed = urlsplit(target)
    body = raw_body if raw_body is not None else (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    )
    header_rows = {
        "host": "testserver",
        "content-type": "application/json",
        "content-length": str(len(body)),
        **(headers or {}),
    }
    sent: list[dict] = []

    async def invoke() -> None:
        delivered = False

        async def receive() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parsed.path,
            "raw_path": parsed.path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": [(key.lower().encode("ascii"), value.encode("utf-8")) for key, value in header_rows.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        }
        await server.app(scope, receive, send)

    asyncio.run(invoke())
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return start["status"], response_headers, json.loads(response_body)


def test_job_manager_runs_independent_cases_in_parallel(tmp_path: Path) -> None:
    global _ACTIVE, _PEAK_ACTIVE
    _ACTIVE = 0
    _PEAK_ACTIVE = 0
    executor = ThreadPoolExecutor(max_workers=2)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=2,
        max_pending=4,
        executor=executor,
        worker=_parallel_probe_worker,
    )
    try:
        submitted = manager.submit_many(
            [
                ("0101Z", _request("1000001"), PipelineOptions()),
                ("0102W", _request("1000002"), PipelineOptions()),
            ]
        )
        results = [future.result(timeout=2) for _job, future in submitted]
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    assert _PEAK_ACTIVE == 2
    assert {result["car_no"] for result in results} == {"1000001", "1000002"}
    job_ids = [job["job_id"] for job, _future in submitted]
    assert len(set(job_ids)) == 2
    assert all((tmp_path / "jobs" / job_id / "job.json").exists() for job_id in job_ids)


def test_job_manager_marks_unexpected_worker_failure_terminal(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_failing_probe_worker,
    )
    try:
        job, future = manager.submit(
            case_id="0101Z",
            request_payload=_request(),
            options=PipelineOptions(),
        )
        try:
            future.result(timeout=2)
        except RuntimeError:
            pass
        else:
            raise AssertionError("failing worker future must raise")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = manager.get_job(job["job_id"])
            if current and current.get("status") == "failed":
                break
            time.sleep(0.01)
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    current = manager.get_job(job["job_id"])
    assert current is not None
    assert current["status"] == "failed"
    assert current["current_stage_name"] == "worker_process_failed"
    assert current["error"] == "worker_process_failed:RuntimeError"


def test_job_manager_recovers_result_written_before_final_job_update(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    job_id = "a" * 32
    job_dir = root / job_id
    atomic_write_json(
        job_dir / "job.json",
        {
            "job_id": job_id,
            "case_id": "0101Z",
            "status": "running",
            "solve_status": None,
            "current_stage": 4,
            "current_stage_name": "stage4_residual_closure",
            "completed_stage": 3,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "stage_summaries": {},
            "error": None,
        },
    )
    atomic_write_json(
        job_dir / "result.json",
        {
            "solve_status": "complete",
            "completed_stage": 4,
            "attempted_stage": 4,
            "stage_summaries": {"4": {"status": "complete"}},
            "error": None,
        },
    )
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=root,
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_parallel_probe_worker,
    )
    try:
        recovered = manager.get_job(job_id)
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    assert recovered is not None
    assert recovered["status"] == "succeeded"
    assert recovered["solve_status"] == "complete"
    assert recovered["result_file"] == "result.json"


def test_done_callback_prefers_committed_result_over_future_exception(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_result_then_raise_worker,
    )
    try:
        job, future = manager.submit(
            case_id="0101Z",
            request_payload=_request(),
            options=PipelineOptions(),
        )
        try:
            future.result(timeout=2)
        except RuntimeError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = manager.get_job(job["job_id"])
            if current and current.get("status") == "succeeded":
                break
            time.sleep(0.01)
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    current = manager.get_job(job["job_id"])
    assert current is not None
    assert current["status"] == "succeeded"
    assert current["solve_status"] == "complete"


def test_done_callback_reconciles_committed_result_after_normal_future_return(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_result_without_job_update_worker,
    )
    try:
        job, future = manager.submit(
            case_id="0101Z",
            request_payload=_request(),
            options=PipelineOptions(),
        )
        future.result(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = manager.get_job(job["job_id"])
            if current and current.get("status") == "succeeded":
                break
            time.sleep(0.01)
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    current = manager.get_job(job["job_id"])
    assert current is not None
    assert current["status"] == "succeeded"
    assert current["result_file"] == "result.json"


def test_job_root_allows_only_one_manager_instance(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    first_executor = ThreadPoolExecutor(max_workers=1)
    second_executor = ThreadPoolExecutor(max_workers=1)
    first = JobManager(
        root=root,
        max_workers=1,
        max_pending=1,
        executor=first_executor,
        worker=_parallel_probe_worker,
    )
    try:
        try:
            JobManager(
                root=root,
                max_workers=1,
                max_pending=1,
                executor=second_executor,
                worker=_parallel_probe_worker,
            )
        except RuntimeError as exc:
            assert "只允许一个 API 服务实例" in str(exc)
        else:
            raise AssertionError("second manager must not acquire the same JOB_ROOT")
    finally:
        first.shutdown()
        first_executor.shutdown(wait=True)
        second_executor.shutdown(wait=True)


def test_manager_rejects_submit_after_shutdown_begins(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_parallel_probe_worker,
    )
    manager.shutdown()
    server.app.state.job_manager = manager
    try:
        status, _headers, body = _asgi_json_request("GET", "/readyz")
        assert status == 503
        assert body["status"] == "not_ready"
        manager.submit(
            case_id="0101Z",
            request_payload=_request(),
            options=PipelineOptions(),
        )
    except ServiceShuttingDownError:
        pass
    else:
        raise AssertionError("submit must be rejected after shutdown")
    finally:
        server.app.state.job_manager = None
        executor.shutdown(wait=True)


def test_job_cleanup_removes_only_expired_terminal_jobs(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    old_job_id = "b" * 32
    recent_job_id = "c" * 32
    for job_id, finished_at in (
        (old_job_id, "2020-01-01T00:00:00+00:00"),
        (recent_job_id, "2999-01-01T00:00:00+00:00"),
    ):
        atomic_write_json(
            root / job_id / "job.json",
            {
                "job_id": job_id,
                "case_id": "0101Z",
                "status": "succeeded",
                "solve_status": "complete",
                "finished_at": finished_at,
            },
        )
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=root,
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_parallel_probe_worker,
    )
    try:
        removed = manager.cleanup_expired(ttl_hours=24)
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)

    assert removed == 1
    assert not (root / old_job_id).exists()
    assert (root / recent_job_id).exists()


def test_shutdown_keeps_service_lock_until_inflight_cleanup_finishes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "jobs"
    old_job_id = "d" * 32
    atomic_write_json(
        root / old_job_id / "job.json",
        {
            "job_id": old_job_id,
            "case_id": "0101Z",
            "status": "succeeded",
            "solve_status": "complete",
            "finished_at": "2020-01-01T00:00:00+00:00",
        },
    )
    executor = ThreadPoolExecutor(max_workers=1)
    contender_executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=root,
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_parallel_probe_worker,
    )
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    shutdown_done = threading.Event()
    cleanup_thread: threading.Thread | None = None
    shutdown_thread: threading.Thread | None = None
    real_rmtree = server.shutil.rmtree

    def blocking_rmtree(path: Path) -> None:
        cleanup_entered.set()
        if not release_cleanup.wait(timeout=5):
            raise AssertionError("cleanup release timed out")
        real_rmtree(path)

    def run_shutdown() -> None:
        manager.shutdown()
        shutdown_done.set()

    try:
        with patch.object(server.shutil, "rmtree", side_effect=blocking_rmtree):
            cleanup_thread = threading.Thread(
                target=manager.cleanup_expired,
                args=(24,),
            )
            cleanup_thread.start()
            assert cleanup_entered.wait(timeout=2)

            shutdown_thread = threading.Thread(target=run_shutdown)
            shutdown_thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not manager.is_shutting_down():
                time.sleep(0.01)
            assert manager.is_shutting_down()
            assert not shutdown_done.is_set()

            try:
                JobManager(
                    root=root,
                    max_workers=1,
                    max_pending=1,
                    executor=contender_executor,
                    worker=_parallel_probe_worker,
                )
            except RuntimeError as exc:
                assert "只允许一个 API 服务实例" in str(exc)
            else:
                raise AssertionError("service lock must cover in-flight cleanup")

            release_cleanup.set()
            cleanup_thread.join(timeout=2)
            shutdown_thread.join(timeout=2)
            assert not cleanup_thread.is_alive()
            assert not shutdown_thread.is_alive()
            assert shutdown_done.is_set()
    finally:
        release_cleanup.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2)
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=2)
        if not shutdown_done.is_set():
            manager.shutdown()
        executor.shutdown(wait=True)
        contender_executor.shutdown(wait=True)

    successor_executor = ThreadPoolExecutor(max_workers=1)
    successor = JobManager(
        root=root,
        max_workers=1,
        max_pending=1,
        executor=successor_executor,
        worker=_parallel_probe_worker,
    )
    successor.shutdown()
    successor_executor.shutdown(wait=True)


def test_http_capacity_is_reserved_before_expensive_validation(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_api_result_worker,
    )
    server.app.state.job_manager = manager
    request_count = 8
    barrier = threading.Barrier(request_count)
    validation_entered = threading.Event()
    release_validation = threading.Event()
    rejected = threading.Event()
    result_lock = threading.Lock()
    validation_calls = 0
    results: list[int] = []
    thread_errors: list[BaseException] = []

    def blocking_validation(_payload: dict) -> list[str]:
        nonlocal validation_calls
        with result_lock:
            validation_calls += 1
        validation_entered.set()
        if not release_validation.wait(timeout=5):
            return ["validation release timed out"]
        return []

    def invoke() -> None:
        try:
            barrier.wait(timeout=5)
            status, _headers, _body = _asgi_json_request(
                "POST",
                "/api/plan/generate?async=true&case_id=0101Z",
                payload=_request(),
            )
            with result_lock:
                results.append(status)
                if len(results) >= request_count - 1:
                    rejected.set()
        except BaseException as exc:
            with result_lock:
                thread_errors.append(exc)
            rejected.set()

    threads = [threading.Thread(target=invoke) for _ in range(request_count)]
    try:
        with patch.object(server, "API_KEY", ""), patch.object(
            server,
            "validate_plan_request",
            side_effect=blocking_validation,
        ):
            for thread in threads:
                thread.start()
            assert validation_entered.wait(timeout=2)
            assert rejected.wait(timeout=2)
            with result_lock:
                assert not thread_errors
                assert validation_calls == 1
                assert results == [429] * (request_count - 1)
            release_validation.set()
            for thread in threads:
                thread.join(timeout=2)
            assert all(not thread.is_alive() for thread in threads)
    finally:
        release_validation.set()
        for thread in threads:
            thread.join(timeout=2)
        manager.shutdown()
        executor.shutdown(wait=True)
        server.app.state.job_manager = None

    assert not thread_errors
    assert sorted(results) == [202] + [429] * (request_count - 1)


def test_slow_single_request_body_does_not_reserve_job_capacity(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
        executor=executor,
        worker=_api_result_worker,
    )
    server.app.state.job_manager = manager

    async def run() -> None:
        body_read_started = asyncio.Event()
        release_body = asyncio.Event()

        async def receive() -> dict:
            body_read_started.set()
            await release_body.wait()
            return {
                "type": "http.request",
                "body": json.dumps(_request()).encode("utf-8"),
                "more_body": False,
            }

        request = server.Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/plan/generate",
                "raw_path": b"/api/plan/generate",
                "query_string": b"async=true&case_id=0101Z",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": server.app,
            },
            receive,
        )
        task = asyncio.create_task(server.generate(request))
        await asyncio.wait_for(body_read_started.wait(), timeout=1)
        assert manager.metrics()["validation_reserved_slots"] == 0
        reservation = manager.reserve_capacity()
        assert manager.metrics()["validation_reserved_slots"] == 1
        reservation.release()
        task.cancel()
        release_body.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("slow body request must remain cancellable")

    try:
        asyncio.run(run())
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)
        server.app.state.job_manager = None


def test_cancelled_validation_waits_for_underlying_thread() -> None:
    validation_entered = threading.Event()
    release_validation = threading.Event()

    def blocking_validation(_payload: dict) -> list[str]:
        validation_entered.set()
        if not release_validation.wait(timeout=5):
            return ["validation release timed out"]
        return []

    async def run() -> None:
        task = asyncio.create_task(server._validate_request(_request()))
        entered = await asyncio.to_thread(validation_entered.wait, 2)
        assert entered
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release_validation.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled validation task must stay cancelled")

    try:
        with patch.object(
            server,
            "validate_plan_request",
            side_effect=blocking_validation,
        ):
            asyncio.run(run())
    finally:
        release_validation.set()


def test_http_async_auth_status_result_and_nonstandard_json_rejection(tmp_path: Path) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=4,
        executor=executor,
        worker=_api_result_worker,
    )
    server.app.state.job_manager = manager
    try:
        with patch.object(server, "API_KEY", "secret"):
            status, _headers, openapi = _asgi_json_request(
                "GET",
                "/api/plan/openapi.json",
            )
            assert status == 200
            assert "BearerAuth" in openapi["components"]["securitySchemes"]
            assert "requestBody" in openapi["paths"]["/api/plan/generate"]["post"]

            status, headers, body = _asgi_json_request(
                "POST",
                "/api/plan/generate?async=true&case_id=0101Z",
                payload=_request(),
            )
            assert status == 401
            assert headers["cache-control"] == "no-store, private"

            status, headers, body = _asgi_json_request(
                "POST",
                "/api/plan/generate?async=true&case_id=0101Z",
                payload=_request(),
                headers={"authorization": "Bearer secret"},
            )
            assert status == 202
            assert body["Data"]["StatusUrl"].startswith("/api/plan/jobs/")
            assert body["Data"]["ResultUrl"].endswith("/result")
            assert "testserver" not in body["Data"]["StatusUrl"]
            job_id = body["Data"]["JobId"]

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and manager.get_result(job_id) is None:
                time.sleep(0.01)

            status, _headers, result_body = _asgi_json_request(
                "GET",
                f"/api/plan/jobs/{job_id}/result",
                headers={"authorization": "Bearer secret"},
            )
            assert status == 200
            assert result_body["Success"] is True
            assert result_body["Meta"]["SolveStatus"] == "complete"
            assert result_body["Meta"]["GetPutHookCount"] == 0

            status, _headers, invalid_body = _asgi_json_request(
                "POST",
                "/api/plan/generate?async=true&case_id=0101Z",
                raw_body=b'{"unused": NaN}',
                headers={"authorization": "Bearer secret"},
            )
            assert status == 400
            assert "JSON" in invalid_body["Message"]
    finally:
        manager.shutdown()
        executor.shutdown(wait=True)
        server.app.state.job_manager = None


def test_real_supervised_subprocess_completes_minimal_four_stage_job(tmp_path: Path) -> None:
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
    )
    options = PipelineOptions(
        stage1_time_budget_seconds=0.1,
        stage2_time_budget_seconds=0.1,
        stage3_time_budget_seconds=0.1,
        stage4_time_budget_seconds=0.1,
    )
    try:
        job, future = manager.submit(
            case_id="0101Z",
            request_payload=_request(),
            options=options,
        )
        future.result(timeout=10)
        result = manager.get_result(job["job_id"])
    finally:
        manager.shutdown()

    assert result is not None
    assert result["solve_status"] == "complete"
    assert result["completed_stage"] == 4
    assert all(result["replay_gates"][str(stage)]["ok"] for stage in range(1, 5))


def test_supervisor_enforces_hard_wall_clock_timeout(tmp_path: Path) -> None:
    manager = JobManager(
        root=tmp_path / "jobs",
        max_workers=1,
        max_pending=1,
    )
    options = PipelineOptions(
        stage1_time_budget_seconds=0.1,
        stage2_time_budget_seconds=0.1,
        stage3_time_budget_seconds=0.1,
        stage4_time_budget_seconds=0.1,
    )
    try:
        with patch.object(server, "JOB_TIMEOUT_GRACE_SECONDS", -0.39):
            job, future = manager.submit(
                case_id="0101Z",
                request_payload=_request(),
                options=options,
            )
            future.result(timeout=10)
        result = manager.get_result(job["job_id"])
    finally:
        manager.shutdown()

    assert result is not None
    assert result["solve_status"] == "failed"
    assert result["error"] == "job_wall_clock_timeout"
