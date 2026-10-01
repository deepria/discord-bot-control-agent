import json
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("RIO_AGENT_TOKEN", "test-token")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as module

client = TestClient(module.app)
headers = {"Authorization": "Bearer test-token"}


def body(**kwargs):
    return {"mode": "manual", "status": "dnd", "activity_type": "playing",
            "activity_text": "코드 수정 중", "actor_id": "123",
            "request_id": str(uuid4()), **kwargs}


def test_presence_requires_token_and_bot_admin(monkeypatch):
    assert client.get("/bot/presence").status_code == 401
    monkeypatch.setattr(module, "discord_console_role", lambda user_id: "viewer")
    assert client.put("/bot/presence", headers=headers, json=body()).status_code == 403


@pytest.mark.parametrize("changes", [{"status": "offline"}, {"activity_type": "streaming"},
                                     {"mode": "unknown"}, {"activity_text": "x" * 129},
                                     {"request_id": "invalid"}, {"role": "admin"}])
def test_presence_schema_rejects_bad_values(changes):
    assert client.put("/bot/presence", headers=headers, json=body(**changes)).status_code == 422


def test_pending_and_request_id_query(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "require_console_admin", lambda actor: calls.append(actor))

    def service(action, **kwargs):
        calls.append((action, kwargs))
        return {"operation": {"state": "queued", "request_id": kwargs["request_id"]}}

    monkeypatch.setattr(module, "presence_call", service)
    value = body()
    assert client.put("/bot/presence", headers=headers, json=value).status_code == 202
    assert calls[0] == "123"
    assert "actor_id" not in calls[1][1]["payload"]
    result = client.get(f"/bot/presence?request_id={value['request_id']}", headers=headers)
    assert result.status_code == 200
    assert calls[-1][1]["request_id"] == value["request_id"]


def test_subprocess_does_not_put_activity_in_argv(monkeypatch):
    def run(args, **kwargs):
        assert "코드 수정 중" not in " ".join(args)
        assert json.loads(kwargs["input"])["payload"]["activity_text"] == "코드 수정 중"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "presence": {"connected": True}}))

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.presence_call("put", payload=body())["connected"]


def test_service_errors_are_preserved_without_stderr(monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"ok": False, "status_code": 503, "detail": "Discord 연결 끊김"}),
        stderr="secret traceback",
    ))
    with pytest.raises(HTTPException) as error:
        module.presence_call("get")
    assert error.value.status_code == 503
    assert error.value.detail == "Discord 연결 끊김"


def test_real_bot_service_mailbox_end_to_end(monkeypatch, tmp_path):
    # Test an actual subprocess boundary, using only synthetic settings and a temporary DB.
    presence = pytest.importorskip("rio_bot.core.presence", reason="Bot package required for cross-repository integration")
    AUTO, PresenceStore = presence.AUTO, presence.PresenceStore
    from rio_bot.core.store import Store

    monkeypatch.setattr(module, "BOT_REPO", tmp_path)
    monkeypatch.setattr(module, "BOT_PYTHON", sys.executable)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "presence.sqlite3"))
    store = Store(str(tmp_path / "presence.sqlite3"))
    mailbox = PresenceStore(store.db)
    mailbox.initialize("test-run")
    mailbox.heartbeat(True)
    value = body()
    try:
        result = module.presence_call("put", request_id=value["request_id"], actor_id="123", payload=value)
        assert result["operation"]["state"] == "queued"
        row = mailbox.claim()
        mailbox.applied({**AUTO, "mode": "manual", "status": "dnd", "activity_text": "코드 수정 중"}, row["request_id"])
        result = module.presence_call("get", request_id=value["request_id"])
        assert result["operation"]["state"] == "success"
        assert result["configured"]["status"] == "dnd"
        assert result["audit"][0]["actor_id"] == "123"
    finally:
        store.close()
