from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train calculation four-stage API server.")
    parser.add_argument("--host", default=os.getenv("TRAIN_CAL_API_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TRAIN_CAL_API_PORT", "8000")),
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default=os.getenv("TRAIN_CAL_API_LOG_LEVEL", "info").lower(),
    )
    parser.add_argument("--no-access-log", action="store_true")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = parse_args(argv)
    if args.worker is not None:
        from plan_api.pipeline import execute_job, worker_process_initializer

        worker_process_initializer()
        execute_job(str(args.worker.resolve()))
        return 0

    import uvicorn

    from plan_api.server import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        log_level=args.log_level,
        access_log=not args.no_access_log,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
