import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RIO_AGENT_TOKEN", "test-token")

import app as app_module
import retained_telemetry as reader

client = TestClient(app_module.app)
HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "telemetry.sqlite3"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            PRAGMA user_version=1;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE traces (at TEXT, turn_id TEXT, event TEXT, payload TEXT,
                                 PRIMARY KEY(at, turn_id, event));
            CREATE INDEX traces_turn ON traces(turn_id, at);
            CREATE TABLE usage (id INTEGER PRIMARY KEY, at TEXT, provider TEXT,
                model TEXT, status TEXT, total_tokens INTEGER, elapsed_ms INTEGER);
            CREATE INDEX usage_at ON usage(at);
        ''')
        conn.execute("PRAGMA application_id=" + str(reader.APPLICATION_ID))
        conn.execute("INSERT INTO meta VALUES ('started_at', ?)",
                     ((now - timedelta(days=100)).isoformat(),))
    monkeypatch.setattr(app_module, "TELEMETRY_DB_PATH", path)
    return path, now


def insert_trace(path, at, *, turn_id=None):
    turn_id = turn_id or str(uuid4())
    row = {"at": at.isoformat(), "event": "turn.completed", "turn_id": turn_id,
           "status": "ok", "content": "private"}
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO traces VALUES (?, ?, ?, ?)",
                     (row["at"], turn_id, row["event"], json.dumps(row)))
    return turn_id


def test_retained_trace_pagination_cutoff_and_read_only(database):
    path, now = database
    ids = [insert_trace(path, now - timedelta(days=age)) for age in (1, 2, 29, 31)]
    before = path.read_bytes(), path.stat().st_mtime_ns
    first = client.get("/traces?limit=2", headers=HEADERS).json()
    assert [r["turn_id"] for r in first["traces"]] == ids[:2]
    assert first["coverage"]["mode"] == "retained"
    assert first["coverage"]["cleanup_pending"] is True
    assert "private" not in json.dumps(first)
    second = client.get("/traces", params={"limit": 2, "cursor": first["next_cursor"]},
                        headers=HEADERS).json()
    assert [r["turn_id"] for r in second["traces"]] == ids[2:3]
    assert second["next_cursor"] is None
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    detail = client.get(f"/traces/{ids[0]}", headers=HEADERS)
    assert detail.status_code == 200
    assert client.get(f"/traces/{ids[-1]}", headers=HEADERS).status_code == 404


def test_usage_90_days_and_exact_percentiles_and_unknown_tokens(database):
    path, now = database
    with sqlite3.connect(path) as conn:
        for age, latency, tokens, status in [(1, 10, 5, "completed"), (50, 20, None, "error"),
                                              (89, 100, 7, "completed"), (91, 999, 999, "error")]:
            conn.execute("INSERT INTO usage(at, provider, model, status, total_tokens, elapsed_ms) "
                         "VALUES (?, 'gemini', 'test', ?, ?, ?)",
                         ((now - timedelta(days=age)).isoformat(), status, tokens, latency))
    result = client.get("/analytics/usage", params={
        "from": (now - timedelta(days=90)).isoformat(), "to": now.isoformat(),
    }, headers=HEADERS)
    assert result.status_code == 200
    group = result.json()["groups"][0]
    assert group["calls"] == 3 and group["errors"] == 1
    assert group["total_tokens"] == 12 and group["unknown_token_calls"] == 1
    assert (group["p50_latency_ms"], group["p95_latency_ms"]) == (20, 100)
    assert result.json()["coverage"]["retention_days"] == 90


def test_legacy_tail_is_explicit_and_corrupt_retained_store_never_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "telemetry.sqlite3"
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"event": "turn.completed", "turn_id": str(uuid4())}) + "\n")
    monkeypatch.setattr(app_module, "TELEMETRY_DB_PATH", path)
    monkeypatch.setattr(app_module, "EVENT_LOG_PATH", events)
    result = client.get("/traces", headers=HEADERS).json()
    assert result["coverage"]["mode"] == "legacy_tail"
    assert result["coverage"]["truncated"] is True
    path.write_text("private-invalid-db")
    result = client.get("/traces", headers=HEADERS).json()
    assert result["traces"] == []
    assert result["source_status"] == "UNAVAILABLE"
    assert result["coverage"]["mode"] == "retained"
    assert "private" not in json.dumps(result)


def test_usage_limit_reports_partial_not_full_total(database, monkeypatch):
    path, now = database
    with sqlite3.connect(path) as conn:
        for age in (1, 2, 3):
            conn.execute("INSERT INTO usage(at, model, status, total_tokens, elapsed_ms) "
                         "VALUES (?, 'test', 'completed', 1, 1)",
                         ((now - timedelta(hours=age)).isoformat(),))
    monkeypatch.setattr(reader, "MAX_USAGE_ROWS", 2)
    result = client.get("/analytics/usage", headers=HEADERS).json()
    assert result["coverage"]["truncated"] is True
    assert result["groups"][0]["calls"] == 2


def test_request_timezones_and_query_windows_are_validated(database):
    _, now = database
    assert client.get("/traces?from=2026-10-01T00:00:00", headers=HEADERS).status_code == 422
    params = {"from": (now - timedelta(days=91)).isoformat(), "to": now.isoformat()}
    assert client.get("/analytics/usage", params=params, headers=HEADERS).status_code == 422
    params["from"] = (now - timedelta(days=32)).isoformat()
    assert client.get("/traces", params=params, headers=HEADERS).status_code == 422
    assert client.get("/traces").status_code == 401


def test_history_start_and_old_records_do_not_imply_source_failure(database):
    path, now = database
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='started_at'", (now.isoformat(),))
    result = client.get("/traces", params={"from": (now - timedelta(days=2)).isoformat()},
                        headers=HEADERS).json()
    assert result["coverage"]["history_limited"] is True
    assert result["source_status"] == "HEALTHY"
    assert result["traces"] == []
