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
