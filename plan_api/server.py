from __future__ import annotations

import asyncio
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
from typing import Any, Callable, Mapping
import uuid

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .pipeline import (
    CASE_ID_PATTERN,
    MAX_REQUEST_CARS,
    PipelineOptionError,
    PipelineOptions,
    atomic_write_json,
    read_json,
    utc_now,
    validate_plan_request,
    write_terminal_failure,
)


IS_WINDOWS = sys.platform == "win32"


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = _runtime_root()
DEFAULT_JOB_ROOT = ROOT / "artifacts" / "api_jobs"
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
CASE_ID_RE = re.compile(CASE_ID_PATTERN)
LOGGER = logging.getLogger("train_cal.plan_api")


def _worker_command(job_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", str(job_dir)]
    return [sys.executable, "-m", "plan_api", "--worker", str(job_dir)]


class ApiProblem(Exception):
    def __init__(self, status_code: int, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


class QueueFullError(RuntimeError):
    pass


class ServiceShuttingDownError(RuntimeError):
    pass


class PartialBatchSubmissionError(RuntimeError):
    def __init__(
        self,
        submitted: list[tuple[dict[str, Any], Future[Any]]],
        cause: Exception,
    ) -> None:
        super().__init__(f"批量提交在部分任务入队后失败: {type(cause).__name__}")
        self.submitted = submitted
        self.cause = cause


class _CapacityReservation:
    def __init__(self, manager: "JobManager", count: int) -> None:
        self._manager = manager
        self.count = count
        self._active = True

    def release(self) -> None:
        self._manager._release_capacity_reservation(self)


class JobManager:
    def __init__(
        self,
        *,
        root: Path,
        max_workers: int,
        max_pending: int,
        executor: Executor | None = None,
        worker: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.max_workers = max_workers
        self.max_pending = max_pending
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="train-cal-job",
        )
        self._worker = worker or self._supervise_job
        self._futures: dict[str, Future[Any]] = {}
        self._child_processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.RLock()
        self._cleanup_condition = threading.Condition(self._lock)
        self._active_cleanups = 0
        self._reserved_capacity = 0
        self._shutting_down = False
        self._executor_broken_reason: str | None = None
        self._service_lock_path = self.root / ".service.lock"
        self._service_lock_handle: Any = None
        try:
            self._acquire_service_lock()
            self.recover_interrupted_jobs()
        except Exception:
            if self._owns_executor:
                self._executor.shutdown(wait=False, cancel_futures=True)
            self._release_service_lock()
            raise

    def recover_interrupted_jobs(self) -> None:
        for job_path in self.root.glob("*/job.json"):
            try:
                payload = read_json(job_path)
            except Exception:
                continue
            if self._reconcile_job_from_result(job_path, payload):
                continue
            if payload.get("status") not in {"queued", "running"}:
                continue
            payload.update(
                {
                    "status": "interrupted",
                    "solve_status": "failed",
                    "current_stage_name": "interrupted_by_service_restart",
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                    "error": "service_restarted_before_job_finished",
                }
            )
            atomic_write_json(job_path, payload)

    def submit(
        self,
        *,
        case_id: str,
        request_payload: dict[str, Any],
        options: PipelineOptions,
        reservation: _CapacityReservation | None = None,
    ) -> tuple[dict[str, Any], Future[Any]]:
        with self._lock:
            self._purge_finished_locked()
            if self._shutting_down:
                raise ServiceShuttingDownError("服务正在关闭")
            self._ensure_executor_locked()
            if reservation is None:
                if self._active_count_locked() + self._reserved_capacity >= self.max_pending:
                    raise QueueFullError("任务队列已满")
            else:
                self._consume_capacity_reservation_locked(reservation, 1)
            return self._submit_locked(
                case_id=case_id,
                request_payload=request_payload,
                options=options,
            )

    def submit_many(
        self,
        submissions: list[tuple[str, dict[str, Any], PipelineOptions]],
        *,
        reservation: _CapacityReservation | None = None,
    ) -> list[tuple[dict[str, Any], Future[Any]]]:
        with self._lock:
            self._purge_finished_locked()
            if self._shutting_down:
                raise ServiceShuttingDownError("服务正在关闭")
            self._ensure_executor_locked()
            if reservation is None:
                if (
                    self._active_count_locked()
                    + self._reserved_capacity
                    + len(submissions)
                    > self.max_pending
                ):
                    raise QueueFullError("任务队列剩余容量不足")
            else:
                self._consume_capacity_reservation_locked(reservation, len(submissions))
            submitted: list[tuple[dict[str, Any], Future[Any]]] = []
            for case_id, request_payload, options in submissions:
                try:
                    submitted.append(
                        self._submit_locked(
                            case_id=case_id,
                            request_payload=request_payload,
                            options=options,
                        )
                    )
                except Exception as exc:
                    raise PartialBatchSubmissionError(submitted, exc) from exc
            return submitted

    def reserve_capacity(self, count: int = 1) -> _CapacityReservation:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("capacity reservation count must be a positive integer")
        with self._lock:
            self._purge_finished_locked()
            if self._shutting_down:
                raise ServiceShuttingDownError("服务正在关闭")
            self._ensure_executor_locked()
            if (
                self._active_count_locked()
                + self._reserved_capacity
                + count
                > self.max_pending
            ):
                raise QueueFullError("任务队列剩余容量不足")
            self._reserved_capacity += count
            return _CapacityReservation(self, count)

    def _consume_capacity_reservation_locked(
        self,
        reservation: _CapacityReservation,
        count: int,
    ) -> None:
        if reservation._manager is not self:
            raise ValueError("capacity reservation belongs to another manager")
        if not reservation._active:
            raise ValueError("capacity reservation is no longer active")
        if reservation.count != count:
            raise ValueError("capacity reservation size does not match submission")
        self._reserved_capacity -= count
        reservation._active = False

    def _release_capacity_reservation(self, reservation: _CapacityReservation) -> None:
        with self._lock:
            if reservation._manager is not self or not reservation._active:
                return
            self._reserved_capacity -= reservation.count
            reservation._active = False

    def _submit_locked(
        self,
        *,
        case_id: str,
        request_payload: dict[str, Any],
        options: PipelineOptions,
    ) -> tuple[dict[str, Any], Future[Any]]:
        job_id = uuid.uuid4().hex
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        input_relative = Path("input") / f"validation_api_{case_id}.json"
        (job_dir / "input").mkdir(mode=0o700)
        atomic_write_json(job_dir / input_relative, request_payload)
        job = {
            "job_id": job_id,
            "case_id": case_id,
            "status": "queued",
            "solve_status": None,
            "current_stage": 0,
            "current_stage_name": "queued",
            "completed_stage": 0,
            "last_safe_stage": 0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "input_file": str(input_relative),
            "result_file": None,
            "options": options.to_public_dict(),
            "stage_summaries": {},
            "error": None,
        }
        atomic_write_json(job_dir / "job.json", job)
        try:
            future = self._executor.submit(self._worker, str(job_dir))
        except Exception as exc:
            self._executor_broken_reason = type(exc).__name__
            job.update(
                {
                    "status": "failed",
                    "solve_status": "failed",
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                    "error": "executor_submit_failed",
                }
            )
            atomic_write_json(job_dir / "job.json", job)
            raise
        self._futures[job_id] = future
        future.add_done_callback(
            lambda completed, submitted_job_id=job_id: self._handle_future_done(
                submitted_job_id,
                completed,
            )
        )
        return job, future

    def _supervise_job(self, job_dir_text: str) -> dict[str, Any]:
        job_dir = Path(job_dir_text)
        job = read_json(job_dir / "job.json")
        job_id = str(job["job_id"])
        options = PipelineOptions.from_mapping(job.get("options"))
        timeout_seconds = (
            options.stage1_time_budget_seconds
            + options.stage2_time_budget_seconds
            + options.stage3_time_budget_seconds
            + options.stage4_time_budget_seconds
            + JOB_TIMEOUT_GRACE_SECONDS
        )
        logs_dir = job_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs_dir.chmod(0o700)
        supervisor_log = logs_dir / "supervisor.log"
        with self._lock:
            if self._shutting_down:
                self._mark_job_interrupted(job_dir, "service_shutdown_before_job_started")
                return {"job_id": job_id, "status": "interrupted"}
        try:
            with supervisor_log.open("a", encoding="utf-8") as handle:
                supervisor_log.chmod(0o600)
                popen_options: dict[str, Any] = {}
                if IS_WINDOWS:
                    popen_options["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    popen_options["start_new_session"] = True
                process = subprocess.Popen(
                    _worker_command(job_dir),
                    cwd=ROOT,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    **popen_options,
                )
                with self._lock:
                    terminate_immediately = self._shutting_down
                    if not terminate_immediately:
                        self._child_processes[job_id] = process
                if terminate_immediately:
                    self._terminate_child_process(process)
                    self._mark_job_interrupted(job_dir, "service_shutdown_during_job_start")
                    return {"job_id": job_id, "status": "interrupted"}
                try:
                    return_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._terminate_child_process(process)
                    write_terminal_failure(job_dir_text, "job_wall_clock_timeout")
                    return {"job_id": job_id, "status": "failed", "reason": "timeout"}
                finally:
                    with self._lock:
                        self._child_processes.pop(job_id, None)
        except Exception as exc:
            with self._lock:
                shutting_down = self._shutting_down
            if not shutting_down:
                write_terminal_failure(job_dir_text, f"worker_supervisor:{type(exc).__name__}")
            return {
                "job_id": job_id,
                "status": "interrupted" if shutting_down else "failed",
            }

        with self._lock:
            shutting_down = self._shutting_down
        if not shutting_down and not (job_dir / "result.json").exists():
            error_code = (
                "worker_missing_result"
                if return_code == 0
                else f"worker_exit_code:{return_code}"
            )
            write_terminal_failure(job_dir_text, error_code)
        current = self.get_job(job_id) or {}
        return {"job_id": job_id, "status": current.get("status", "unknown")}

    @staticmethod
    def _terminate_child_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        if IS_WINDOWS:
            with suppress(Exception):
                process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                with suppress(Exception):
                    process.terminate()
        try:
            process.wait(timeout=JOB_TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        if IS_WINDOWS:
            with suppress(Exception):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                with suppress(Exception):
                    process.kill()
        with suppress(Exception):
            process.wait(timeout=1)

    def _handle_future_done(
        self,
        job_id: str,
        future: Future[Any],
    ) -> None:
        try:
            exception = future.exception()
        except Exception as exc:
            exception = exc
        with self._lock:
            job_dir = self._safe_job_dir(job_id)
            if job_dir is None:
                return
            job_path = job_dir / "job.json"
            if not job_path.exists():
                return
            try:
                job = read_json(job_path)
            except Exception:
                return
            if self._reconcile_job_from_result(job_path, job):
                return
            if exception is None:
                if job.get("status") in {"queued", "running"}:
                    if self._shutting_down:
                        self._mark_job_interrupted(
                            job_dir,
                            "service_shutdown_before_job_finished",
                        )
                    else:
                        write_terminal_failure(
                            str(job_dir),
                            "worker_missing_result_after_future",
                        )
                return
            if job.get("status") not in {"queued", "running"}:
                return
            job.update(
                {
                    "status": "failed",
                    "solve_status": "failed",
                    "current_stage_name": "worker_process_failed",
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                    "error": f"worker_process_failed:{type(exception).__name__}",
                }
            )
            atomic_write_json(job_path, job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job_dir = self._safe_job_dir(job_id)
        if job_dir is None:
            return None
        path = job_dir / "job.json"
        if not path.exists():
            return None
        try:
            return read_json(path)
        except Exception:
            return None

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        job_dir = self._safe_job_dir(job_id)
        if job_dir is None:
            return None
        path = job_dir / "result.json"
        if not path.exists():
            return None
        try:
            return read_json(path)
        except Exception:
            return None

    def has_capacity(self, count: int = 1) -> bool:
        with self._lock:
            self._purge_finished_locked()
            return (
                not self._shutting_down
                and self._active_count_locked() + self._reserved_capacity + count
                <= self.max_pending
            )

    def is_shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    @staticmethod
    def _mark_job_interrupted(job_dir: Path, error_code: str) -> None:
        job_path = job_dir / "job.json"
        try:
            job = read_json(job_path)
            if job.get("status") not in {"queued", "running"}:
                return
            job.update(
                {
                    "status": "interrupted",
                    "solve_status": "failed",
                    "current_stage_name": "interrupted_by_service_shutdown",
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                    "error": error_code,
                }
            )
            atomic_write_json(job_path, job)
        except Exception:
            return

    def ensure_healthy(self) -> bool:
        with self._lock:
            if self._shutting_down:
                return False
            try:
                self._ensure_executor_locked()
            except Exception:
                return False
            return self._executor_broken_reason is None

    def cleanup_expired(self, ttl_hours: int) -> int:
        if ttl_hours <= 0:
            return 0
        with self._cleanup_condition:
            if self._shutting_down:
                return 0
            self._active_cleanups += 1
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
            with self._lock:
                active_job_ids = {
                    job_id
                    for job_id, future in self._futures.items()
                    if not future.done()
                }
            removed = 0
            for job_dir in self.root.iterdir():
                if not job_dir.is_dir() or not JOB_ID_RE.fullmatch(job_dir.name):
                    continue
                if job_dir.name in active_job_ids:
                    continue
                job_path = job_dir / "job.json"
                try:
                    job = read_json(job_path)
                except Exception:
                    continue
                if job.get("status") not in {"succeeded", "partial", "failed", "interrupted"}:
                    continue
                timestamp_text = job.get("finished_at") or job.get("updated_at") or job.get("created_at")
                try:
                    timestamp = datetime.fromisoformat(str(timestamp_text))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if timestamp > cutoff:
                    continue
                try:
                    shutil.rmtree(job_dir)
                except OSError:
                    continue
                removed += 1
            return removed
        finally:
            with self._cleanup_condition:
                self._active_cleanups -= 1
                self._cleanup_condition.notify_all()

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            self._purge_finished_locked()
            active = self._active_count_locked()
            reserved_capacity = self._reserved_capacity
            executor_error = self._executor_broken_reason
            shutting_down = self._shutting_down
            active_job_ids = [
                job_id
                for job_id, future in self._futures.items()
                if not future.done()
            ]
        running = 0
        queued = 0
        for job_id in active_job_ids:
            job_path = self.root / job_id / "job.json"
            try:
                status = read_json(job_path).get("status")
            except Exception:
                continue
            running += int(status == "running")
            queued += int(status == "queued")
        return {
            "max_workers": self.max_workers,
            "max_pending": self.max_pending,
            "active_local_futures": active,
            "validation_reserved_slots": reserved_capacity,
            "running_jobs": running,
            "queued_jobs": queued,
            "executor_healthy": executor_error is None,
            "executor_error": executor_error,
            "shutting_down": shutting_down,
        }

    def shutdown(self) -> None:
        child_processes: list[subprocess.Popen[Any]] = []
        try:
            with self._lock:
                self._shutting_down = True
                active_job_ids = [
                    job_id
                    for job_id, future in self._futures.items()
                    if not future.done()
                ]
                for job_id in active_job_ids:
                    job_dir = self._safe_job_dir(job_id)
                    if job_dir is None:
                        continue
                    job_path = job_dir / "job.json"
                    try:
                        job = read_json(job_path)
                    except Exception:
                        continue
                    if job.get("status") not in {"queued", "running"}:
                        continue
                    try:
                        if self._reconcile_job_from_result(job_path, job):
                            continue
                        job.update(
                            {
                                "status": "interrupted",
                                "solve_status": "failed",
                                "current_stage_name": "interrupted_by_service_shutdown",
                                "finished_at": utc_now(),
                                "updated_at": utc_now(),
                                "error": "service_shutdown_before_job_finished",
                            }
                        )
                        atomic_write_json(job_path, job)
                    except Exception:
                        continue
                child_processes = list(self._child_processes.values())
            for process in child_processes:
                self._terminate_child_process(process)
            if self._owns_executor:
                with suppress(Exception):
                    self._executor.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                for job_id in active_job_ids:
                    job_dir = self._safe_job_dir(job_id)
                    if job_dir is None:
                        continue
                    job_path = job_dir / "job.json"
                    try:
                        job = read_json(job_path)
                    except Exception:
                        continue
                    if self._reconcile_job_from_result(job_path, job):
                        continue
                    self._mark_job_interrupted(
                        job_dir,
                        "service_shutdown_before_job_finished",
                    )
        finally:
            with self._cleanup_condition:
                while self._active_cleanups:
                    self._cleanup_condition.wait()
            self._release_service_lock()

    def _reconcile_job_from_result(self, job_path: Path, job: dict[str, Any]) -> bool:
        result_path = job_path.parent / "result.json"
        if not result_path.exists():
            return False
        try:
            result = read_json(result_path)
        except Exception:
            return False
        if not isinstance(result, dict):
            return False
        solve_status = str(result.get("solve_status") or "failed")
        job.update(
            {
                "status": {
                    "complete": "succeeded",
                    "partial": "partial",
                    "failed": "failed",
                }.get(solve_status, "failed"),
                "solve_status": solve_status,
                "current_stage": result.get("attempted_stage", 0),
                "current_stage_name": "finished",
                "completed_stage": result.get("completed_stage", 0),
                "last_safe_stage": result.get(
                    "last_safe_stage",
                    result.get("completed_stage", 0),
                ),
                "stage_summaries": result.get("stage_summaries", {}),
                "finished_at": job.get("finished_at") or utc_now(),
                "updated_at": utc_now(),
                "result_file": "result.json",
                "error": result.get("error"),
            }
        )
        atomic_write_json(job_path, job)
        return True

    def _safe_job_dir(self, job_id: str) -> Path | None:
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        candidate = (self.root / job_id).resolve()
        if candidate.parent != self.root:
            return None
        return candidate

    def _active_count_locked(self) -> int:
        return sum(1 for future in self._futures.values() if not future.done())

    def _purge_finished_locked(self) -> None:
        finished = [job_id for job_id, future in self._futures.items() if future.done()]
        for job_id in finished:
            self._futures.pop(job_id, None)

    def _ensure_executor_locked(self) -> None:
        if self._executor_broken_reason is None:
            return
        if not self._owns_executor:
            raise RuntimeError(f"executor unavailable: {self._executor_broken_reason}")
        old_executor = self._executor
        old_executor.shutdown(wait=False, cancel_futures=True)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="train-cal-job",
        )
        self._executor_broken_reason = None
        self._purge_finished_locked()

    def _acquire_service_lock(self) -> None:
        handle = self._service_lock_path.open("a+b")
        owner_offset = 0
        try:
            if IS_WINDOWS:
                owner_offset = 1
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                handle.seek(owner_offset)
                owner = handle.read().decode("ascii", errors="replace").strip() or "unknown"
            except OSError:
                owner = "unknown"
            handle.close()
            raise RuntimeError(
                f"JOB_ROOT 已由进程 {owner} 使用；只允许一个 API 服务实例"
            ) from exc
        with suppress(OSError):
            os.chmod(self._service_lock_path, 0o600)
        handle.seek(owner_offset)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._service_lock_handle = handle

    def _release_service_lock(self) -> None:
        handle = self._service_lock_handle
        if handle is None:
            return
        try:
            if IS_WINDOWS:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._service_lock_handle = None


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是 true/false")


DEFAULT_PIPELINE_WORKERS = min(2, max(1, (os.cpu_count() or 1) // 2))
PIPELINE_WORKERS = _env_int("TRAIN_CAL_API_WORKERS", DEFAULT_PIPELINE_WORKERS, 1, 32)
MAX_PENDING = _env_int("TRAIN_CAL_API_MAX_PENDING", PIPELINE_WORKERS * 4, PIPELINE_WORKERS, 1000)
MAX_BODY_BYTES = _env_int("TRAIN_CAL_API_MAX_BODY_BYTES", 10 * 1024 * 1024, 1024, 100 * 1024 * 1024)
MAX_BATCH_CASES = _env_int("TRAIN_CAL_API_MAX_BATCH_CASES", 100, 1, 1000)
JOB_TIMEOUT_GRACE_SECONDS = _env_int("TRAIN_CAL_API_JOB_TIMEOUT_GRACE_SECONDS", 120, 1, 3600)
JOB_TERMINATE_GRACE_SECONDS = _env_int("TRAIN_CAL_API_JOB_TERMINATE_GRACE_SECONDS", 5, 1, 60)
JOB_TTL_HOURS = _env_int("TRAIN_CAL_API_JOB_TTL_HOURS", 168, 0, 24 * 3650)
CLEANUP_INTERVAL_SECONDS = _env_int("TRAIN_CAL_API_CLEANUP_INTERVAL_SECONDS", 3600, 60, 24 * 3600)
MIN_FREE_DISK_MB = _env_int("TRAIN_CAL_API_MIN_FREE_DISK_MB", 512, 0, 1024 * 1024)
JOB_ROOT = Path(os.getenv("TRAIN_CAL_API_JOB_ROOT", str(DEFAULT_JOB_ROOT))).expanduser()
API_KEY = os.getenv("TRAIN_CAL_API_KEY", "")
ALLOW_UNAUTHENTICATED = _env_bool("TRAIN_CAL_ALLOW_UNAUTHENTICATED", False)

async def health(request: Request) -> Response:
    metrics = _request_manager(request).metrics()
    return JSONResponse(
        {
            "status": "ok" if metrics["executor_healthy"] else "degraded",
            "service": "train-cal-four-stage-api",
            "pipeline": metrics,
        }
    )


async def readiness(request: Request) -> Response:
    manager = _request_manager(request)
    manager.ensure_healthy()
    metrics = manager.metrics()
    disk = shutil.disk_usage(manager.root)
    minimum_free_bytes = MIN_FREE_DISK_MB * 1024 * 1024
    ready = bool(
        metrics["executor_healthy"]
        and not metrics["shutting_down"]
        and disk.free >= minimum_free_bytes
    )
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "service": "train-cal-four-stage-api",
            "pipeline": metrics,
            "disk_free_bytes": disk.free,
            "minimum_disk_free_bytes": minimum_free_bytes,
        },
        status_code=200 if ready else 503,
    )


async def generate(request: Request) -> Response:
    manager = _request_manager(request)
    if manager.is_shutting_down():
        raise ApiProblem(503, "服务正在关闭")
    if not manager.has_capacity():
        raise ApiProblem(429, "任务队列已满")

    payload = await _read_json_body(request)
    case_id, plan_request, options = parse_single_submission(payload)
    try:
        reservation = manager.reserve_capacity()
    except QueueFullError as exc:
        raise ApiProblem(429, str(exc)) from exc
    except ServiceShuttingDownError as exc:
        raise ApiProblem(503, str(exc)) from exc
    try:
        errors = await _validate_request(plan_request)
        if errors:
            raise ApiProblem(422, "请求参数校验失败", details=errors)

        try:
            job, future = manager.submit(
                case_id=case_id,
                request_payload=plan_request,
                options=options,
                reservation=reservation,
            )
        except QueueFullError as exc:
            raise ApiProblem(429, str(exc)) from exc
        except ServiceShuttingDownError as exc:
            raise ApiProblem(503, str(exc)) from exc
    finally:
        reservation.release()

    if _wants_async(request):
        return _accepted_response(job, request)

    try:
        await asyncio.shield(asyncio.wrap_future(future))
    except Exception as exc:
        status_url = f"/api/plan/jobs/{job['job_id']}"
        return JSONResponse(
            {
                "Success": False,
                "Message": f"求解工作进程异常: {type(exc).__name__}",
                "StatusCode": 500,
                "Data": {"JobId": job["job_id"], "StatusUrl": status_url},
            },
            status_code=500,
            headers={
                "X-Job-Id": str(job["job_id"]),
                "Location": status_url,
            },
        )
    result = manager.get_result(str(job["job_id"]))
    if result is None:
        raise ApiProblem(500, "任务已结束但结果文件缺失")
    public_response = result.get("response") or {}
    status_code = int(public_response.get("StatusCode") or 500)
    return JSONResponse(
        public_response,
        status_code=status_code,
        headers={
            "X-Job-Id": str(job["job_id"]),
            "X-Case-Id": case_id,
            "X-Solve-Status": str(result.get("solve_status") or "failed"),
        },
    )


async def generate_batch(request: Request) -> Response:
    manager = _request_manager(request)
    payload = await _read_json_body(request)
    if not isinstance(payload, dict):
        raise ApiProblem(422, "请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - {"cases", "options"})
    if unknown:
        raise ApiProblem(422, f"未知批量提交字段: {','.join(unknown)}")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ApiProblem(422, "cases 必须是非空数组")
    if len(cases) > MAX_BATCH_CASES:
        raise ApiProblem(422, f"单批最多提交 {MAX_BATCH_CASES} 个案例")

    try:
        reservation = manager.reserve_capacity(len(cases))
    except QueueFullError as exc:
        raise ApiProblem(429, str(exc)) from exc
    except ServiceShuttingDownError as exc:
        raise ApiProblem(503, str(exc)) from exc

    try:
        common_options = payload.get("options", {})
        if common_options is None:
            common_options = {}
        if not isinstance(common_options, Mapping):
            raise ApiProblem(422, "options 必须是 JSON 对象")

        submissions: list[tuple[str, dict[str, Any], PipelineOptions]] = []
        validation_errors: list[dict[str, Any]] = []
        for index, item in enumerate(cases):
            if not isinstance(item, dict):
                validation_errors.append({"index": index, "errors": ["案例必须是 JSON 对象"]})
                continue
            unknown = sorted(set(item) - {"case_id", "request", "options"})
            if unknown:
                validation_errors.append({
                    "index": index,
                    "errors": [f"未知案例字段: {','.join(unknown)}"],
                })
                continue
            plan_request = item.get("request")
            case_id_raw = item.get("case_id")
            if not isinstance(plan_request, dict):
                validation_errors.append({
                    "index": index,
                    "errors": ["request 必须是 JSON 对象"],
                })
                continue
            try:
                case_id = normalize_case_id(case_id_raw)
                item_options = item.get("options", {})
                if item_options is None:
                    item_options = {}
                merged_options = _deep_merge_options(common_options, item_options)
                options = PipelineOptions.from_mapping(merged_options)
            except (PipelineOptionError, ValueError) as exc:
                validation_errors.append({"index": index, "errors": [str(exc)]})
                continue
            errors = await _validate_request(plan_request)
            if errors:
                validation_errors.append(
                    {"index": index, "case_id": case_id, "errors": errors}
                )
                continue
            submissions.append((case_id, plan_request, options))

        if validation_errors:
            raise ApiProblem(422, "批量请求参数校验失败", details=validation_errors)

        try:
            jobs = manager.submit_many(submissions, reservation=reservation)
        except QueueFullError as exc:
            raise ApiProblem(429, str(exc)) from exc
        except ServiceShuttingDownError as exc:
            raise ApiProblem(503, str(exc)) from exc
        except PartialBatchSubmissionError as exc:
            accepted = [_accepted_data(job, request) for job, _future in exc.submitted]
            status_code = 207 if accepted else 503
            return JSONResponse(
                {
                    "Success": False,
                    "Message": str(exc),
                    "StatusCode": status_code,
                    "Data": {"AcceptedJobs": accepted},
                },
                status_code=status_code,
            )
    finally:
        reservation.release()

    data = [_accepted_data(job, request) for job, _future in jobs]
    return JSONResponse(
        {
            "Success": True,
            "Message": "批量任务已提交",
            "StatusCode": 202,
            "Data": {"Jobs": data},
        },
        status_code=202,
    )


async def job_status(request: Request) -> Response:
    manager = _request_manager(request)
    job_id = request.path_params["job_id"]
    job = manager.get_job(job_id)
    if job is None:
        raise ApiProblem(404, "任务不存在")
    return JSONResponse(
        {
            "Success": True,
            "Message": "",
            "StatusCode": 200,
            "Data": _public_job(job, request),
        }
    )


async def job_result(request: Request) -> Response:
    manager = _request_manager(request)
    job_id = request.path_params["job_id"]
    job = manager.get_job(job_id)
    if job is None:
        raise ApiProblem(404, "任务不存在")
    result = manager.get_result(job_id)
    if result is None:
        status = str(job.get("status") or "unknown")
        if status in {"queued", "running"}:
            return JSONResponse(
                {
                    "Success": True,
                    "Message": "任务尚未完成",
                    "StatusCode": 202,
                    "Data": _public_job(job, request),
                },
                status_code=202,
            )
        raise ApiProblem(409, f"任务没有可用结果，当前状态: {status}", details=job.get("error"))

    body = deepcopy(result.get("response") or {})
    body["Meta"] = {
        "JobId": job_id,
        "CaseId": result.get("case_id"),
        "SolveStatus": result.get("solve_status"),
        "CompletedStage": result.get("completed_stage"),
        "LastSafeStage": result.get("last_safe_stage"),
        "AttemptedStage": result.get("attempted_stage"),
        "OperationCount": result.get("operation_count"),
        "GetPutHookCount": result.get("get_put_hook_count"),
        "WeighOperationCount": result.get("weigh_operation_count"),
        "StageSummaries": result.get("stage_summaries"),
        "ReplayGates": result.get("replay_gates"),
    }
    status_code = int(body.get("StatusCode") or 500)
    return JSONResponse(body, status_code=status_code)


async def openapi(_request: Request) -> Response:
    return JSONResponse(_openapi_schema())


def parse_single_submission(
    payload: Any,
) -> tuple[str, dict[str, Any], PipelineOptions]:
    if not isinstance(payload, dict):
        raise ApiProblem(422, "请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - {"case_id", "request", "options"})
    if unknown:
        raise ApiProblem(422, f"未知提交字段: {','.join(unknown)}")

    plan_request = payload.get("request")
    case_id_raw = payload.get("case_id")
    raw_options = payload.get("options", {})
    if raw_options is None:
        raw_options = {}

    if not isinstance(plan_request, dict):
        raise ApiProblem(422, "request 必须是 JSON 对象")
    try:
        case_id = normalize_case_id(case_id_raw)
        options = PipelineOptions.from_mapping(raw_options)
    except (PipelineOptionError, ValueError) as exc:
        raise ApiProblem(422, str(exc)) from exc
    return case_id, plan_request, options


def normalize_case_id(value: Any) -> str:
    parsed = str(value or "").strip().upper()
    if not CASE_ID_RE.fullmatch(parsed):
        raise ValueError("case_id 必须符合 4 位数字加 W/Z，例如 0104W")
    return parsed


def _deep_merge_options(common: Mapping[str, Any], item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise PipelineOptionError("案例 options 必须是 JSON 对象")
    merged = deepcopy(dict(common))
    for stage, value in item.items():
        if stage in merged and isinstance(merged[stage], Mapping) and isinstance(value, Mapping):
            merged[stage] = {**dict(merged[stage]), **dict(value)}
        else:
            merged[stage] = deepcopy(value)
    return merged


async def _read_json_body(request: Request) -> Any:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise ApiProblem(413, f"请求体不能超过 {MAX_BODY_BYTES} 字节")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ApiProblem(413, f"请求体不能超过 {MAX_BODY_BYTES} 字节")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise ApiProblem(400, "请求体不能为空")
    try:
        return json.loads(
            body.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ApiProblem(400, f"JSON 解析失败: {exc}") from exc


async def _validate_request(payload: dict[str, Any]) -> list[str]:
    validation_task = asyncio.create_task(
        asyncio.to_thread(validate_plan_request, payload)
    )
    try:
        return await asyncio.shield(validation_task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await validation_task
        raise


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"不支持的 JSON 数字常量: {value}")


def _wants_async(request: Request) -> bool:
    query_value = str(request.query_params.get("async") or "false").strip().lower()
    if query_value in {"1", "true"}:
        return True
    if query_value in {"0", "false"}:
        return False
    raise ApiProblem(422, "async 必须是 true 或 false")


def _accepted_response(job: dict[str, Any], request: Request) -> Response:
    data = _accepted_data(job, request)
    return JSONResponse(
        {
            "Success": True,
            "Message": "任务已提交",
            "StatusCode": 202,
            "Data": data,
        },
        status_code=202,
        headers={"Location": data["StatusUrl"], "X-Job-Id": str(job["job_id"])},
    )


def _accepted_data(job: dict[str, Any], request: Request) -> dict[str, Any]:
    del request
    job_id = str(job["job_id"])
    status_path = f"/api/plan/jobs/{job_id}"
    result_path = f"/api/plan/jobs/{job_id}/result"
    return {
        "JobId": job_id,
        "CaseId": job.get("case_id"),
        "Status": job.get("status"),
        "StatusUrl": status_path,
        "ResultUrl": result_path,
    }


def _public_job(job: dict[str, Any], request: Request) -> dict[str, Any]:
    data = {
        "JobId": job.get("job_id"),
        "CaseId": job.get("case_id"),
        "Status": job.get("status"),
        "SolveStatus": job.get("solve_status"),
        "CurrentStage": job.get("current_stage"),
        "CurrentStageName": job.get("current_stage_name"),
        "CompletedStage": job.get("completed_stage"),
        "LastSafeStage": job.get("last_safe_stage"),
        "CreatedAt": job.get("created_at"),
        "StartedAt": job.get("started_at"),
        "FinishedAt": job.get("finished_at"),
        "Options": job.get("options"),
        "StageSummaries": job.get("stage_summaries") or {},
        "Error": job.get("error"),
    }
    if job.get("result_file"):
        data["ResultUrl"] = f"/api/plan/jobs/{job['job_id']}/result"
    return data


async def api_problem_handler(_request: Request, exc: ApiProblem) -> Response:
    body = {
        "Success": False,
        "Message": exc.message,
        "StatusCode": exc.status_code,
        "Data": None,
    }
    if exc.details is not None:
        body["Errors"] = exc.details
    return JSONResponse(body, status_code=exc.status_code)


async def unhandled_exception_handler(_request: Request, exc: Exception) -> Response:
    LOGGER.exception("Unhandled API exception", exc_info=exc)
    return JSONResponse(
        {
            "Success": False,
            "Message": "服务内部错误",
            "StatusCode": 500,
            "Data": None,
        },
        status_code=500,
    )


@asynccontextmanager
async def lifespan(_app: Starlette):
    if not API_KEY and not ALLOW_UNAUTHENTICATED:
        raise RuntimeError(
            "对外 API 必须设置 TRAIN_CAL_API_KEY；仅本地开发可显式设置 "
            "TRAIN_CAL_ALLOW_UNAUTHENTICATED=true"
        )
    manager = JobManager(
        root=JOB_ROOT,
        max_workers=PIPELINE_WORKERS,
        max_pending=MAX_PENDING,
    )
    _app.state.job_manager = manager
    cleanup_task: asyncio.Task[Any] | None = None
    try:
        try:
            await asyncio.to_thread(manager.cleanup_expired, JOB_TTL_HOURS)
        except Exception:
            LOGGER.exception("Initial API job cleanup failed")
        cleanup_task = asyncio.create_task(_cleanup_loop(manager))
        yield
    finally:
        try:
            if cleanup_task is not None:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await cleanup_task
        finally:
            manager.shutdown()


def _request_manager(request: Request) -> JobManager:
    manager = getattr(request.app.state, "job_manager", None)
    if not isinstance(manager, JobManager):
        raise ApiProblem(503, "任务管理器尚未启动")
    return manager


async def _cleanup_loop(manager: JobManager) -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(manager.cleanup_expired, JOB_TTL_HOURS)
        except Exception:
            LOGGER.exception("API job cleanup failed")


routes = [
    Route("/healthz", health, methods=["GET"], name="health"),
    Route("/readyz", readiness, methods=["GET"], name="readiness"),
    Route("/api/plan/openapi.json", openapi, methods=["GET"], name="openapi"),
    Route("/api/plan/generate", generate, methods=["POST"], name="generate"),
    Route("/api/plan/generate/batch", generate_batch, methods=["POST"], name="generate_batch"),
    Route("/api/plan/jobs/{job_id}", job_status, methods=["GET"], name="job_status"),
    Route("/api/plan/jobs/{job_id}/result", job_result, methods=["GET"], name="job_result"),
]

app = Starlette(
    debug=False,
    routes=routes,
    exception_handlers={ApiProblem: api_problem_handler, Exception: unhandled_exception_handler},
    lifespan=lifespan,
)


async def api_key_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    if request.url.path not in {"/healthz", "/readyz", "/api/plan/openapi.json"} and API_KEY:
        supplied = request.headers.get("X-API-Key", "")
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, API_KEY):
            response = JSONResponse(
                {
                    "Success": False,
                    "Message": "未授权",
                    "StatusCode": 401,
                    "Data": None,
                },
                status_code=401,
            )
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
    response = await call_next(request)
    if request.url.path.startswith("/api/plan"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=api_key_middleware)


cors_origins = [
    item.strip()
    for item in os.getenv("TRAIN_CAL_CORS_ORIGINS", "").split(",")
    if item.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )


def _openapi_schema() -> dict[str, Any]:
    security = [{"BearerAuth": []}, {"ApiKeyHeader": []}]
    standard_responses = {
        "400": {"description": "JSON 或请求格式错误"},
        "401": {"description": "未授权"},
        "413": {"description": "请求体过大"},
        "422": {"description": "业务请求字段校验失败"},
        "429": {"description": "任务队列已满"},
        "500": {"description": "求解或服务内部错误"},
        "503": {"description": "任务执行器不可用或批量提交部分失败"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Train Calculation Four-stage API",
            "version": "1.0.0",
            "description": "Stage1-4 串行求解；不同案例在受监管的独立子进程中并行。",
        },
        "paths": {
            "/healthz": {
                "get": {"summary": "存活检查", "responses": {"200": {"description": "服务进程存活"}}}
            },
            "/readyz": {
                "get": {
                    "summary": "就绪检查",
                    "responses": {
                        "200": {"description": "可接受任务"},
                        "503": {"description": "执行器或磁盘未就绪"},
                    },
                }
            },
            "/api/plan/generate": {
                "post": {
                    "summary": "提交单个调车计划；默认同步，async=true 异步",
                    "security": security,
                    "parameters": [
                        {
                            "name": "async",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/PlanSubmission"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "同步求解完成；可能是 complete 或合法 partial",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlanResultResponse"}}},
                        },
                        "202": {"description": "异步任务已接受"},
                        **standard_responses,
                    },
                }
            },
            "/api/plan/generate/batch": {
                "post": {
                    "summary": "并行提交多个案例",
                    "security": security,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BatchSubmission"}
                            }
                        },
                    },
                    "responses": {
                        "202": {"description": "批量任务已接受"},
                        "207": {"description": "部分任务已接受；逐项查看 AcceptedJobs"},
                        **standard_responses,
                    },
                }
            },
            "/api/plan/jobs/{job_id}": {
                "get": {
                    "summary": "查询任务状态",
                    "security": security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {
                        "200": {"description": "任务状态"},
                        "401": standard_responses["401"],
                        "404": {"description": "任务不存在"},
                    },
                }
            },
            "/api/plan/jobs/{job_id}/result": {
                "get": {
                    "summary": "读取最终四阶段结果",
                    "security": security,
                    "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {
                        "200": {
                            "description": "完整或部分结果",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PlanResultResponse"}}},
                        },
                        "202": {"description": "仍在运行"},
                        "401": standard_responses["401"],
                        "404": {"description": "任务不存在"},
                        "409": {"description": "任务终止且无可用结果"},
                        "500": standard_responses["500"],
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer"},
                "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            },
            "schemas": {
                "ApiResponse": {
                    "type": "object",
                    "required": ["Success", "Message", "StatusCode", "Data"],
                    "properties": {
                        "Success": {"type": "boolean"},
                        "Message": {"type": "string"},
                        "StatusCode": {"type": "integer"},
                        "Data": {"type": ["object", "null"]},
                        "Meta": {"type": "object"},
                        "Errors": {},
                    },
                },
                "PlanResultResponse": {
                    "type": "object",
                    "required": ["Success", "Message", "StatusCode", "Data"],
                    "properties": {
                        "Success": {"type": "boolean"},
                        "Message": {"type": "string"},
                        "StatusCode": {"type": "integer"},
                        "Data": {"$ref": "#/components/schemas/PlanResultData"},
                        "Meta": {"type": "object"},
                        "Errors": {},
                    },
                },
                "PlanResultData": {
                    "type": "object",
                    "required": ["Operations", "GeneratedEndStatus"],
                    "properties": {
                        "Operations": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/PlanOperation"},
                        },
                        "GeneratedEndStatus": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/GeneratedEndCar"},
                        },
                    },
                },
                "PlanOperation": {
                    "type": "object",
                    "required": [
                        "Index",
                        "Line",
                        "Action",
                        "MoveCars",
                        "TrainCars",
                        "PassbyPath",
                        "ByPassSwitch",
                    ],
                    "properties": {
                        "Index": {"type": "integer"},
                        "Line": {"type": "string"},
                        "Action": {"type": "string", "enum": ["Get", "Put", "Weigh"]},
                        "MoveCars": {"type": "array", "items": {"type": "string"}},
                        "TrainCars": {"type": "array", "items": {"type": "string"}},
                        "PassbyPath": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "四阶段求解器输出的完整物理路径。",
                        },
                        "ByPassSwitch": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^(L([1-9]|1[0-9])|Z[1-4])$"},
                            "description": "按行进顺序经过的现场 L/Z 道岔；不含起点和终点股道。",
                        },
                        "Positions": {
                            "type": "object",
                            "additionalProperties": {"type": "integer", "minimum": 1},
                        },
                    },
                },
                "GeneratedEndCar": {
                    "type": "object",
                    "required": ["No", "Line", "Position"],
                    "properties": {
                        "No": {"type": "string"},
                        "Line": {"type": "string"},
                        "Position": {"type": "integer"},
                    },
                },
                "PlanRequest": {
                    "type": "object",
                    "required": ["StartStatus", "TerminalLines", "locoNode"],
                    "properties": {
                        "StartStatus": {
                            "type": "array",
                            "maxItems": MAX_REQUEST_CARS,
                            "items": {"$ref": "#/components/schemas/StartCar"},
                        },
                        "TerminalLines": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TerminalLine"},
                        },
                        "locoNode": {"$ref": "#/components/schemas/LocoNode"},
                    },
                },
                "StartCar": {
                    "type": "object",
                    "required": ["Line", "Position", "RepairProcess", "Type", "No", "Length", "TargetLines"],
                    "properties": {
                        "Line": {"type": "string", "minLength": 1},
                        "Position": {"type": "integer", "minimum": 0},
                        "RepairProcess": {"type": "string", "minLength": 1},
                        "Type": {"type": "string", "minLength": 1},
                        "No": {"type": "string", "minLength": 1},
                        "Length": {"type": "number", "exclusiveMinimum": 0},
                        "IsHeavy": {"type": "boolean", "default": False},
                        "IsWeigh": {"type": "boolean", "default": False},
                        "IsClosedDoor": {"type": "boolean", "default": False},
                        "TargetLines": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "ForceTargetPosition": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                        },
                    },
                },
                "TerminalLine": {
                    "type": "object",
                    "required": ["Line"],
                    "properties": {
                        "Line": {"type": "string", "enum": ["修1库内", "修2库内", "修3库内", "修4库内"]},
                        "IsInspectionMode": {"type": "boolean", "default": False},
                    },
                },
                "LocoNode": {
                    "type": "object",
                    "required": ["Line", "End"],
                    "properties": {
                        "Line": {"type": "string", "minLength": 1},
                        "End": {"type": "string", "enum": ["North", "South"]},
                    },
                },
                "PipelineOptions": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "stage1": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "max_hooks": {"type": "integer", "minimum": 1, "maximum": 500},
                                "time_budget_seconds": {"type": "number", "minimum": 0.1, "maximum": 900},
                            },
                        },
                        "stage2": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "time_budget_seconds": {"type": "number", "minimum": 0.1, "maximum": 900}
                            },
                        },
                        "stage3": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "time_budget_seconds": {"type": "number", "minimum": 0.1, "maximum": 900}
                            },
                        },
                        "stage4": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "time_budget_seconds": {"type": "number", "minimum": 0.1, "maximum": 900},
                                "max_labels": {"type": "integer", "minimum": 1, "maximum": 4096},
                                "max_expansions": {"type": "integer", "minimum": 1, "maximum": 1000000},
                            },
                        },
                    },
                    "description": "四阶段 time_budget_seconds 合计最多 1800 秒。",
                },
                "PlanSubmission": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["case_id", "request"],
                    "properties": {
                        "case_id": {"type": "string", "pattern": "^[0-9]{4}[WZ]$"},
                        "request": {"$ref": "#/components/schemas/PlanRequest"},
                        "options": {"$ref": "#/components/schemas/PipelineOptions"},
                    },
                },
                "BatchSubmission": {
                    "type": "object",
                    "required": ["cases"],
                    "properties": {
                        "options": {"$ref": "#/components/schemas/PipelineOptions"},
                        "cases": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_BATCH_CASES,
                            "items": {"$ref": "#/components/schemas/PlanSubmission"},
                        },
                    },
                },
            },
        },
    }
