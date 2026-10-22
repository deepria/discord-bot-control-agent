"""Read-only access to Bot's dedicated, versioned telemetry store."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPLICATION_ID = 0x52494F54
MAX_USAGE_ROWS = 100_000
MAX_TRACE_BYTES = 1_048_576


def read_retained(path: Path, kind: str, *, start=None, end=None, turn_id=None,
                  after=None, limit=50, now=None):
    now = now or datetime.now(timezone.utc)
    days = 30 if kind == "traces" else 90
    cutoff = now - timedelta(days=days)
    coverage = {
        "mode": "retained", "retention_days": days, "collection_started_at": None,
        "oldest_at": None, "newest_at": None, "truncated": False,
        "history_limited": False, "cleanup_pending": False,
    }
    source = {"status": "HEALTHY", "last_success_at": now.isoformat(), "error_code": None}
    if not path.exists() and not path.is_symlink():
        return None  # Only an absent store permits explicit legacy fallback.
    connection = None
    try:
        if path.is_symlink():
            raise ValueError("Invalid telemetry path")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
        connection.execute("PRAGMA query_only=ON")
        # Bound query work as well as rows; a very large/corrupt store cannot stall the API.
        steps = 0

        def budget():
            nonlocal steps
            steps += 1
            return int(steps > 5000)

        connection.set_progress_handler(budget, 1000)
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != 1):
            raise ValueError("Unsupported telemetry database")
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        coverage["collection_started_at"] = meta.get("started_at")
        lower = max(start or cutoff, cutoff).astimezone(timezone.utc).isoformat()
        upper = min(end or now, now).astimezone(timezone.utc).isoformat()
        table = "traces" if kind == "traces" else "usage"
        bounds = connection.execute(
            f"SELECT MIN(at), MAX(at) FROM {table} WHERE at >= ? AND at <= ?",
            (cutoff.isoformat(), now.isoformat()),
        ).fetchone()
        coverage.update(oldest_at=bounds[0], newest_at=bounds[1])
        coverage["cleanup_pending"] = bool(connection.execute(
            f"SELECT 1 FROM {table} WHERE at < ? LIMIT 1", (cutoff.isoformat(),)).fetchone())
        began = meta.get("started_at")
        coverage["history_limited"] = bool(start and (
            start < cutoff or (began and start < datetime.fromisoformat(began))))
        params = [lower, upper]
        where = "at >= ? AND at <= ?"
        if kind == "traces":
            if turn_id:
                where += " AND turn_id = ?"
                params.append(turn_id)
            if after:
                where += " AND (at, turn_id, event) < (?, ?, ?)"
                params.extend(after)
            params.append(limit + 1)
            records = connection.execute(
                f"SELECT payload FROM traces WHERE {where} "
                "ORDER BY at DESC, turn_id DESC, event DESC LIMIT ?", params).fetchall()
            rows = []
            size = 0
            for payload, in records:
                size += len(payload.encode("utf-8"))
                if size > MAX_TRACE_BYTES:
                    coverage["truncated"] = True
                    break
                row = json.loads(payload)
                if not isinstance(row, dict):
                    raise ValueError("Invalid retained row")
                rows.append(row)
        else:
            params.append(MAX_USAGE_ROWS + 1)
            records = connection.execute(
                f"SELECT at, provider, model, status, total_tokens, elapsed_ms FROM usage WHERE {where} "
                "ORDER BY at DESC, id DESC LIMIT ?", params).fetchall()
            coverage["truncated"] = len(records) > MAX_USAGE_ROWS
            rows = [{"operation": "answer", **dict(zip(("at", "provider", "model", "status", "total_tokens", "elapsed_ms"), r))}
                    for r in records[:MAX_USAGE_ROWS]]
        return {"rows": rows, "coverage": coverage, "source": source}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        source.update(status="UNAVAILABLE", last_success_at=None, error_code="RETAINED_SOURCE_UNAVAILABLE")
        return {"rows": [], "coverage": coverage, "source": source}
    finally:
        if connection is not None:
            connection.close()


def legacy_coverage(kind):
    return {"mode": "legacy_tail", "retention_days": 30 if kind == "traces" else 90,
            "collection_started_at": None, "oldest_at": None, "newest_at": None,
            "truncated": True, "history_limited": True, "cleanup_pending": False}
