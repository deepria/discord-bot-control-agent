#!/usr/bin/env python3
"""Atomically publish a Bot or Agent deployment-status v1 record."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


VALID_STATUSES = {"queued", "running", "succeeded", "failed", "stale", "unknown"}
VALID_CHECK_STATUSES = {"passed", "failed", "skipped", "unknown"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_check(value: str) -> dict[str, str]:
    try:
        name, status = value.rsplit("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("check must use NAME=STATUS") from exc
    if not name or status not in VALID_CHECK_STATUSES:
        raise argparse.ArgumentTypeError(
            "check status must be passed, failed, skipped, or unknown"
        )
    return {"name": name, "status": status, "at": now()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--component", choices=["bot", "agent"], required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--status", choices=sorted(VALID_STATUSES), required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--target-revision")
    parser.add_argument("--running-revision")
    parser.add_argument("--previous-revision")
    parser.add_argument("--started-at")
    parser.add_argument("--finished-at")
    parser.add_argument("--verified-at")
    parser.add_argument("--log-ref")
    parser.add_argument("--error")
    parser.add_argument("--check", action="append", type=parse_check, default=[])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    timestamp = now()
    record = {
        "schema_version": 1,
        "deployment_id": args.deployment_id,
        "component": args.component,
        "target_revision": args.target_revision,
        "running_revision": args.running_revision,
        "status": args.status,
        "phase": args.phase,
        "started_at": args.started_at or timestamp,
        "finished_at": args.finished_at,
        "verified_at": args.verified_at,
        "checks": args.check,
        "previous_revision": args.previous_revision,
        "log_ref": args.log_ref,
        "error": args.error,
    }
    args.file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=args.file.parent, delete=False
    ) as temporary:
        json.dump(record, temporary, separators=(",", ":"))
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(args.file)


if __name__ == "__main__":
    main()
