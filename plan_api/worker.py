from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import execute_job, worker_process_initializer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one supervised four-stage API job.")
    parser.add_argument("job_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worker_process_initializer()
    execute_job(str(args.job_dir.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
