"""Durable per-attempt checkpoints, so a killed run resumes mid-bug instead of
restarting a bug's whole `repair_with_retries` loop from attempt 1.

Bug-level resume already existed (a bug recorded in the ledger is skipped on
the next run). This is the same idea one level deeper: within an unfinished
bug, each attempt is appended here as it completes, so a run interrupted
after attempt 2 of 5 (e.g. hitting a usage cap) picks back up at attempt 3
next time instead of re-paying for attempts 1-2.

Cleared once the bug's outcome is written to the ledger -- at that point
bug-level resume takes over and this file is never read again.
"""
import json
from pathlib import Path


def read_checkpoint(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_checkpoint(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def clear_checkpoint(path: Path) -> None:
    path = Path(path)
    if path.exists():
        path.unlink()
