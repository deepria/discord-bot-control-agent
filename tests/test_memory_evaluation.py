import json
import os
import subprocess
import sys
from types import SimpleNamespace

os.environ.setdefault("RIO_AGENT_TOKEN", "test-token")

import pytest
from fastapi.testclient import TestClient

import app as module

client = TestClient(module.app)
HEADERS = {"Authorization": "Bearer test-token"}


def report():
    return {
        "source_status": "HEALTHY",
        "items": {"total": 38, "active": 38, "superseded": 0, "deleted": 0, "other_status": 0},
        "provenance": {"active_message_linked": 38, "active_turn_linked": 38},
        "observation": {"first_item_at": "2026-09-24T00:00:00Z",
                        "last_item_at": "2026-10-15T00:00:00Z", "elapsed_days": 21},
        "readiness": {"minimum_active_items": 50, "minimum_days": 7,
                      "ready_for_metadata_review": False, "reasons": ["insufficient_active_items"]},
    }


def test_evaluation_authenticates_before_starting_subprocess(monkeypatch):
    def no_run(*args, **kwargs):
        pytest.fail("unauthenticated request reached the Bot")
    monkeypatch.setattr(module.subprocess, "run", no_run)
    assert client.get("/memory/evaluation").status_code == 401


def test_evaluation_uses_fixed_configured_read_only_tool_and_strips_extra_fields(monkeypatch, tmp_path):
    database = tmp_path / "memory.sqlite3"
    database.touch()
    monkeypatch.setattr(module, "BOT_DB_PATH", database)
    value = report()
    value["content"] = "private canary"
    value["items"]["owner_id"] = "private owner"

    def run(args, **kwargs):
        assert args == [module.BOT_PYTHON, "-m", "rio_bot.tooling.memory_evaluation", "--db", str(database)]
        assert kwargs["timeout"] == 5
        assert kwargs["cwd"] == module.BOT_REPO
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(module.subprocess, "run", run)
    response = client.get("/memory/evaluation?db=/private/other&min_items=1", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["evaluation"]["readiness"]["minimum_active_items"] == 50
    assert body["source"]["status"] == "HEALTHY"
    assert body["checked_at"]
    assert "private" not in response.text


@pytest.mark.parametrize("failure", ["timeout", "missing_tool", "invalid_json", "invalid_shape"])
def test_evaluation_returns_safe_unavailability(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private command", 5, stderr="private secret")
        if failure == "missing_tool":
            return SimpleNamespace(returncode=1, stdout="", stderr="private secret traceback")
        value = report()
        value["items"]["active"] = -1
        return SimpleNamespace(returncode=0, stdout="private invalid JSON" if failure == "invalid_json" else json.dumps(value))
    monkeypatch.setattr(module.subprocess, "run", run)
    response = client.get("/memory/evaluation", headers=HEADERS)
    assert response.status_code == 503
    assert "private" not in response.text
    assert "평가" in response.json()["detail"]


def test_evaluation_real_bot_tool_is_read_only(monkeypatch, tmp_path):
    pytest.importorskip("rio_bot.tooling.memory_evaluation", reason="Bot package required for subprocess integration")
    from rio_bot.core.store import Store

    database = tmp_path / "memory ?#.sqlite3"
    store = Store(str(database))
    store.close()
    before = database.read_bytes()
    monkeypatch.setattr(module, "BOT_REPO", tmp_path)
    monkeypatch.setattr(module, "BOT_DB_PATH", database)
    monkeypatch.setattr(module, "BOT_PYTHON", sys.executable)
    response = client.get("/memory/evaluation", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()["evaluation"]
    assert result["source_status"] == "HEALTHY"
    assert result["items"]["active"] == 0
    assert result["readiness"]["ready_for_metadata_review"] is False
    assert database.read_bytes() == before
