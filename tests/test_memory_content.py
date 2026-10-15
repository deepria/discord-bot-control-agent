import hmac
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

os.environ.setdefault("RIO_AGENT_TOKEN", "test-token")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as module

client = TestClient(module.app)
TOKEN = {"Authorization": "Bearer test-token"}
SECRET = "test-memory-content-signature"


def identity(actor="42", minutes=0):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    signature = hmac.new(SECRET.encode(), f"{actor}.{at}".encode(), "sha256").hexdigest()
    return {**TOKEN, "X-Rio-Actor-Id": actor, "X-Rio-Actor-Timestamp": at,
            "X-Rio-Actor-Signature": signature}


@pytest.fixture
def database(monkeypatch, tmp_path):
    path = tmp_path / "memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE structured_memory_items (
      id INTEGER PRIMARY KEY, owner_id TEXT, owner_type TEXT, origin_realm TEXT,
      origin_channel_id TEXT, origin_public_at_capture INTEGER, kind TEXT, content TEXT,
      disclosure TEXT, confidence REAL, status TEXT, superseded_by INTEGER,
      created_at TEXT, updated_at TEXT, source_message_ids TEXT, source_turn_ids TEXT)""")
    connection.execute("INSERT INTO structured_memory_items VALUES "
                       "(1,'99','user','guild:10','20',1,'preference','private candidate',"
                       "'owner_private',.9,'active',NULL,'2026-10-01 12:00:00',"
                       "'2026-10-01 12:00:00','100,101','turn-100,')")
    connection.execute("INSERT INTO structured_memory_items VALUES "
                       "(2,'88','user','dm:88','30',0,'fact','other candidate',"
                       "'owner_private',.8,'deleted',NULL,'2026-10-01 12:00:00',"
                       "'2026-10-01 12:00:00','200','turn-200')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(module, "BOT_DB_PATH", path)
    monkeypatch.setattr(module, "MEMORY_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setenv("RIO_CONSOLE_IDENTITY_SECRET", SECRET)
    def admin(actor):
        if actor != "42":
            raise HTTPException(status_code=403, detail="Administrator required")
    monkeypatch.setattr(module, "require_console_admin", admin)
    return path


def test_content_is_explicit_admin_read_with_audit_and_no_database_writes(database):
    before = database.read_bytes()
    response = client.get("/memory/1?include_content=true", headers=identity())
    assert response.status_code == 200
    item = response.json()["memory"]
    assert item["content"] == "private candidate"
    assert item["status"] == "active" and item["usage_state"] == "shadow_only"
    assert item["created_at"] == "2026-10-01T12:00:00+00:00"
    assert item["sources"] == [
        {"message_id": "100", "turn_id": "turn-100", "message_url": "https://discord.com/channels/10/20/100"},
        {"message_id": "101", "turn_id": None, "message_url": "https://discord.com/channels/10/20/101"},
    ]
    assert "other candidate" not in response.text
    assert response.headers["cache-control"] == "no-store"
    audit = module.MEMORY_AUDIT_PATH.read_text()
    assert json.loads(audit)["kind"] == "memory.content.read"
    assert "private candidate" not in audit and "turn-100" not in audit
    assert database.read_bytes() == before


def test_public_list_and_default_detail_remain_metadata_only(database):
    public = client.get("/memory", headers=TOKEN)
    metadata = client.get("/memory/1", headers=identity())
    assert public.status_code == metadata.status_code == 200
    for response in (public, metadata):
        assert "private candidate" not in response.text
        assert "content" not in response.text
    assert client.get("/memory/2?include_content=true", headers=identity()).json()["memory"]["sources"][0]["message_url"] is None


@pytest.mark.parametrize("headers,expected", [(None, 401), (TOKEN, 401),
    (identity("7"), 403), (identity(minutes=6), 401),
    ({**identity(), "X-Rio-Actor-Signature": "forged"}, 401)])
def test_no_content_read_before_bearer_signature_and_admin_checks(monkeypatch, database, headers, expected):
    def no_read(*args):
        pytest.fail("unauthorized request read memory content")
    monkeypatch.setattr(module, "read_memory_item_content", no_read)
    response = client.get("/memory/1?include_content=true", headers=headers)
    assert response.status_code == expected
    assert "private candidate" not in response.text


def test_audit_failure_does_not_release_content(monkeypatch, database):
    def fail(record):
        raise HTTPException(status_code=503, detail="Memory access audit is unavailable")
    monkeypatch.setattr(module, "append_memory_audit", fail)
    response = client.get("/memory/1?include_content=true", headers=identity())
    assert response.status_code == 503
    assert "private candidate" not in response.text


def test_missing_item_or_database_is_not_replaced_with_empty_content(database, monkeypatch):
    assert client.get("/memory/999?include_content=true", headers=identity()).status_code == 404
    missing = database.parent / "absent.sqlite3"
    monkeypatch.setattr(module, "BOT_DB_PATH", missing)
    assert client.get("/memory/1?include_content=true", headers=identity()).status_code == 503
    assert not missing.exists()
