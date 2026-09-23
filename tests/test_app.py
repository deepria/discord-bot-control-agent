import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("RIO_AGENT_TOKEN", "test-token")

from fastapi.testclient import TestClient

import app as app_module


client = TestClient(app_module.app)
HEADERS = {"Authorization": "Bearer test-token"}


def deployment_record(component: str, verified_at: datetime) -> dict:
    timestamp = verified_at.isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "deployment_id": f"dep-{component}-test",
        "component": component,
        "target_revision": "abc1234",
        "running_revision": "abc1234",
        "status": "succeeded",
        "phase": "readiness",
        "started_at": timestamp,
        "finished_at": timestamp,
        "verified_at": timestamp,
        "checks": [{"name": "service", "status": "passed", "at": timestamp}],
        "previous_revision": None,
        "log_ref": "journal:dep-test",
        "error": None,
    }


def test_deployments_return_unknown_when_producers_are_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BOT_DEPLOY_STATUS_PATH", tmp_path / "bot.json")
    monkeypatch.setattr(app_module, "AGENT_DEPLOY_STATUS_PATH", tmp_path / "agent.json")

    response = client.get("/deployments", headers=HEADERS)

    assert response.status_code == 200
    assert [item["status"] for item in response.json()["deployments"]] == [
        "unknown",
        "unknown",
    ]


def test_deployment_status_preserves_fresh_verified_success(monkeypatch, tmp_path):
    status_path = tmp_path / "bot.json"
    status_path.write_text(
        json.dumps(deployment_record("bot", datetime.now(timezone.utc))), encoding="utf-8"
    )
    monkeypatch.setattr(app_module, "BOT_DEPLOY_STATUS_PATH", status_path)

    result = app_module.deployment_status("bot", status_path)

    assert result["status"] == "succeeded"
    assert result["running_revision"] == "abc1234"


def test_deployment_status_marks_expired_evidence_stale(monkeypatch, tmp_path):
    status_path = tmp_path / "agent.json"
    status_path.write_text(
        json.dumps(
            deployment_record("agent", datetime.now(timezone.utc) - timedelta(hours=1))
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "DEPLOYMENT_STATUS_MAX_AGE_SECONDS", 60)

    result = app_module.deployment_status("agent", status_path)

    assert result["status"] == "stale"
    assert "freshness window" in result["error"]


def test_deployment_status_rejects_revision_mismatch(tmp_path):
    status_path = tmp_path / "bot.json"
    record = deployment_record("bot", datetime.now(timezone.utc))
    record["running_revision"] = "def5678"
    status_path.write_text(json.dumps(record), encoding="utf-8")

    result = app_module.deployment_status("bot", status_path)

    assert result["status"] == "unknown"
    assert "do not match" in result["error"]


def test_bot_control_writes_content_free_persistent_operation(monkeypatch, tmp_path):
    operations_path = tmp_path / "operations.jsonl"
    monkeypatch.setattr(app_module, "OPERATIONS_PATH", operations_path)
    monkeypatch.setattr(app_module, "require_console_admin", lambda actor_id: None)

    class Completed:
        returncode = 0
        stdout = "active\n"

    monkeypatch.setattr(app_module, "run", lambda command: Completed())
    response = client.post(
        "/bot/restart",
        headers=HEADERS,
        json={
            "actor_id": "123456789012345678",
            "request_id": "8c2e2390-fbf1-445c-8f97-d680903aac47",
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == "success"
    assert response.json()["post_check"] == "healthy"
    stored = json.loads(operations_path.read_text(encoding="utf-8"))
    assert stored["kind"] == "bot.restart"
    assert stored["actor_id"] == "123456789012345678"
    assert "stderr" not in stored


def test_operations_returns_newest_first(monkeypatch, tmp_path):
    operations_path = tmp_path / "operations.jsonl"
    operations_path.write_text(
        "\n".join(
            [
                json.dumps({"operation_id": "older", "result": "success"}),
                json.dumps({"operation_id": "newer", "result": "failure"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "OPERATIONS_PATH", operations_path)

    response = client.get("/operations", headers=HEADERS)

    assert response.status_code == 200
    assert [item["operation_id"] for item in response.json()["operations"]] == [
        "newer",
        "older",
    ]


def test_trace_endpoint_filters_content_and_tolerates_corrupt_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "not-json\n" + json.dumps({
            "event": "turn.completed", "turn_id": "9f5f7fad-70d1-4e69-8b2b-5d7b5ebf6978",
            "provider": "gemini", "content": "must-not-leak",
        }) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(app_module, "EVENT_LOG_PATH", path)

    response = client.get("/traces", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["traces"] == [{
        "event": "turn.completed", "turn_id": "9f5f7fad-70d1-4e69-8b2b-5d7b5ebf6978",
        "provider": "gemini",
    }]


def test_memory_reader_uses_read_only_sqlite_metadata(monkeypatch, tmp_path):
    database = tmp_path / "rio.sqlite3"
    connection = __import__("sqlite3").connect(database)
    connection.execute("CREATE TABLE structured_memory_items (id INTEGER, owner_id TEXT, origin_realm TEXT, origin_channel_id TEXT, kind TEXT, disclosure TEXT, confidence REAL, created_at TEXT, content TEXT)")
    connection.execute("INSERT INTO structured_memory_items VALUES (1, 'u', 'r', 'c', 'fact', 'owner_private', .8, '2026-09-23', 'secret')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(app_module, "BOT_DB_PATH", database)

    response = client.get("/memory", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["memory"][0]["kind"] == "fact"
    assert "content" not in response.text
