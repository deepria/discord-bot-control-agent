import asyncio
import base64
import hmac
import json
import math
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psutil
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

app = FastAPI(title="Rio Agent")

SERVICE = "rio-bot.service"
TOKEN = os.environ["RIO_AGENT_TOKEN"]
EVENT_LOG_PATH = Path(os.getenv("RIO_EVENT_LOG_PATH", "/opt/rio-discord-bot/data/logs/events.jsonl"))
USAGE_LOG_PATH = Path(os.getenv("RIO_USAGE_LOG_PATH", "/opt/rio-discord-bot/data/logs/usage.jsonl"))
BOT_DB_PATH = Path(os.getenv("RIO_BOT_DB_PATH", "/opt/rio-discord-bot/data/rio.sqlite3"))
STATUS_PATH = Path(os.getenv("RIO_STATUS_PATH", "/opt/rio-discord-bot/data/logs/status.json"))
BOT_REPO = Path(os.getenv("RIO_BOT_REPO", "/opt/rio-discord-bot"))
AGENT_REPO = Path(os.getenv("RIO_AGENT_REPO", "/opt/rio-agent"))
BOT_PYTHON = os.getenv("RIO_BOT_PYTHON", str(BOT_REPO / ".venv" / "bin" / "python"))
BOT_DEPLOY_STATUS_PATH = Path(
    os.getenv("RIO_BOT_DEPLOY_STATUS_PATH", "/run/rio-agent/deployments/bot.json")
)
AGENT_DEPLOY_STATUS_PATH = Path(
    os.getenv("RIO_AGENT_DEPLOY_STATUS_PATH", "/run/rio-agent/deployments/agent.json")
)
DEPLOYMENT_STATUS_MAX_AGE_SECONDS = max(
    60, int(os.getenv("RIO_DEPLOYMENT_STATUS_MAX_AGE_SECONDS", "900"))
)
OPERATIONS_PATH = Path(
    os.getenv("RIO_OPERATIONS_PATH", "/opt/rio-agent/data/operations.jsonl")
)
MEMORY_AUDIT_PATH = Path(
    os.getenv("RIO_MEMORY_AUDIT_PATH", "/opt/rio-agent/data/memory-access.jsonl")
)
TELEMETRY_MAX_AGE_SECONDS = max(60, int(os.getenv("RIO_TELEMETRY_MAX_AGE_SECONDS", "900")))
JSONL_READ_MAX_BYTES = max(4096, int(os.getenv("RIO_JSONL_READ_MAX_BYTES", "1048576")))
MAX_QUERY_WINDOW = timedelta(days=31)


class DiscordIdentity(BaseModel):
    user_id: str


class RuntimeSettingWrite(BaseModel):
    value: str
    request_id: UUID
    actor_id: str


class RuntimeSettingReset(BaseModel):
    request_id: UUID
    actor_id: str


class PolicyWrite(BaseModel):
    value: str
    request_id: UUID
    actor_id: str


class BotControlRequest(BaseModel):
    actor_id: str
    request_id: UUID


class DeploymentCheck(BaseModel):
    name: str
    status: Literal["passed", "failed", "skipped", "unknown"]
    at: datetime
    detail: str | None = None


class DeploymentStatusRecord(BaseModel):
    schema_version: Literal[1]
    deployment_id: str
    component: Literal["bot", "agent"]
    target_revision: str | None = None
    running_revision: str | None = None
    status: Literal["queued", "running", "succeeded", "failed", "stale", "unknown"]
    phase: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    verified_at: datetime | None = None
    checks: list[DeploymentCheck] = Field(default_factory=list)
    previous_revision: str | None = None
    log_ref: str | None = None
    error: str | None = None


def run(cmd: list[str]):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )


def verify_token(authorization: str | None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")

    expected = f"Bearer {TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def json_safe(value):
    """Convert persisted runtime data into strict JSON-compatible values."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_operation(record: dict) -> None:
    """Persist minimal, content-free control evidence outside Bot data."""
    try:
        OPERATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OPERATIONS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Operation audit is unavailable") from exc


def read_operations(limit: int) -> list[dict]:
    try:
        rows = OPERATIONS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Operation audit is unavailable") from exc
    records = []
    for row in reversed(rows):
        try:
            value = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(json_safe(value))
    return records


def read_status() -> dict | None:
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return json_safe(value) if isinstance(value, dict) else None


def read_events(lines: int) -> list[dict]:
    try:
        rows = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-lines:]
    except OSError:
        return []
    events = []
    for row in rows:
        try:
            value = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(json_safe(value))
    return events


def source_freshness(path: Path) -> dict:
    """Return source state without treating old data as current data."""
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except FileNotFoundError:
        return {"status": "UNAVAILABLE", "last_success_at": None, "error_code": "SOURCE_MISSING"}
    except OSError:
        return {"status": "UNAVAILABLE", "last_success_at": None, "error_code": "SOURCE_UNREADABLE"}
    status = "HEALTHY" if datetime.now(timezone.utc) - modified_at <= timedelta(seconds=TELEMETRY_MAX_AGE_SECONDS) else "STALE"
    return {"status": status, "last_success_at": modified_at.isoformat().replace("+00:00", "Z"), "error_code": None}


def read_jsonl(path: Path, limit: int) -> tuple[list[dict], str]:
    """Read a byte- and row-bounded JSONL tail; one corrupt row never fails the source."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - JSONL_READ_MAX_BYTES))
            chunk = handle.read(JSONL_READ_MAX_BYTES)
    except FileNotFoundError:
        return [], "UNAVAILABLE"
    except OSError:
        return [], "UNAVAILABLE"
    rows = chunk.decode("utf-8", errors="replace").splitlines()
    if size > JSONL_READ_MAX_BYTES:
        rows = rows[1:]
    rows = rows[-limit:]
    values = []
    for row in rows:
        try:
            value = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values, source_freshness(path)["status"]


def safe_trace(row: dict) -> dict:
    allowed = {"at", "schema_version", "event", "turn_id", "operation", "status", "provider",
               "model", "routing", "latency_ms", "tokens", "web_search_calls", "memory_lifecycle",
               "error_type"}
    return {key: json_safe(row[key]) for key in allowed if key in row}


def parse_cursor(cursor: str | None) -> tuple[str, str, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        parsed = (value["at"], value["turn_id"], value["event"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Invalid cursor") from None
    if not all(isinstance(item, str) for item in parsed):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    return parsed


def encode_cursor(row: dict) -> str:
    value = {key: str(row.get(key, "")) for key in ("at", "turn_id", "event")}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def validate_period(start: datetime | None, end: datetime | None) -> None:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if start and end and end - start > MAX_QUERY_WINDOW:
        raise HTTPException(status_code=422, detail="Requested period exceeds 31 days")


def within_period(row: dict, start: datetime | None, end: datetime | None) -> bool:
    at = row.get("at")
    if not isinstance(at, str):
        return start is None and end is None
    try:
        value = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (start is None or value >= start) and (end is None or value <= end)


def traces(turn_id: str | None, limit: int, start: datetime | None = None,
           end: datetime | None = None, cursor: str | None = None) -> tuple[list[dict], str, str | None]:
    rows, source = read_jsonl(EVENT_LOG_PATH, limit * 16)
    result = [safe_trace(row) for row in rows if row.get("event", "").startswith("turn.")]
    if turn_id:
        result = [row for row in result if row.get("turn_id") == turn_id]
    result = [row for row in result if within_period(row, start, end)]
    result.sort(key=lambda row: (str(row.get("at", "")), str(row.get("turn_id", "")), str(row.get("event", ""))), reverse=True)
    after = parse_cursor(cursor)
    if after:
        result = [row for row in result if (str(row.get("at", "")), str(row.get("turn_id", "")), str(row.get("event", ""))) < after]
    page = result[:limit]
    return page, source, encode_cursor(page[-1]) if len(result) > limit else None


def usage_analytics(limit: int, group_by: Literal["provider", "model"], start: datetime | None = None,
                    end: datetime | None = None) -> tuple[list[dict], str]:
    rows, source = read_jsonl(USAGE_LOG_PATH, limit * 8)
    groups: dict[str, dict] = {}
    for row in rows:
        if row.get("operation") != "answer" or not within_period(row, start, end):
            continue
        key = str(row.get(group_by) or "unknown")
        group = groups.setdefault(key, {group_by: key, "calls": 0, "errors": 0, "total_tokens": 0,
                                        "latency_ms": []})
        group["calls"] += 1
        group["errors"] += int(row.get("status") == "error")
        if isinstance(row.get("total_tokens"), int):
            group["total_tokens"] += row["total_tokens"]
        if isinstance(row.get("elapsed_ms"), int):
            group["latency_ms"].append(row["elapsed_ms"])
    result = []
    for group in groups.values():
        latency = sorted(group.pop("latency_ms"))
        group["success_rate"] = (group["calls"] - group["errors"]) / group["calls"]
        group["p50_latency_ms"] = latency[len(latency) // 2] if latency else None
        group["p95_latency_ms"] = latency[min(len(latency) - 1, int(len(latency) * .95))] if latency else None
        result.append(group)
    return result, source


def read_memory_metadata(limit: int, scope: Literal["channel", "owner_private"] | None = None,
                         cursor: int | None = None) -> tuple[list[dict], str, int | None]:
    if not BOT_DB_PATH.exists():
        return [], "UNAVAILABLE", None
    try:
        connection = sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        predicates, values = [], []
        if scope:
            predicates.append("disclosure = ?")
            values.append(scope)
        if cursor is not None:
            predicates.append("id < ?")
            values.append(cursor)
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        rows = connection.execute(
            "SELECT id, owner_id, origin_realm, origin_channel_id, kind, disclosure, confidence, created_at "
            f"FROM structured_memory_items{where} ORDER BY id DESC LIMIT ?", (*values, limit + 1)
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return [], "UNAVAILABLE", None
    result = [dict(zip(("id", "owner_id", "origin_realm", "origin_channel_id", "kind", "disclosure",
                         "confidence", "created_at"), row)) for row in rows[:limit]]
    next_cursor = result[-1]["id"] if len(rows) > limit and result else None
    return result, source_freshness(BOT_DB_PATH)["status"], next_cursor


def read_memory_item_metadata(item_id: int) -> tuple[dict | None, str]:
    """Read only display-safe metadata. Memory content never leaves the Bot DB here."""
    try:
        connection = sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT id, owner_id, origin_realm, origin_channel_id, kind, disclosure, confidence, created_at "
            "FROM structured_memory_items WHERE id = ?", (item_id,)
        ).fetchone()
        connection.close()
    except sqlite3.Error:
        return None, "UNAVAILABLE"
    if row is None:
        return None, source_freshness(BOT_DB_PATH)["status"]
    return dict(zip(("id", "owner_id", "origin_realm", "origin_channel_id", "kind", "disclosure",
                     "confidence", "created_at"), row)), source_freshness(BOT_DB_PATH)["status"]


def require_signed_console_admin(
    actor_id: str | None, actor_timestamp: str | None, actor_signature: str | None
) -> str:
    """Verify Console's short-lived, HMAC-bound OAuth identity before sensitive reads."""
    secret = os.getenv("RIO_CONSOLE_IDENTITY_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Memory detail authorization is unavailable")
    if not actor_id or not actor_timestamp or not actor_signature:
        raise HTTPException(status_code=401, detail="Signed Console identity is required")
    try:
        timestamp = datetime.fromisoformat(actor_timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Console identity") from None
    if timestamp.tzinfo is None or abs((datetime.now(timezone.utc) - timestamp).total_seconds()) > 300:
        raise HTTPException(status_code=401, detail="Expired Console identity")
    expected = hmac.new(secret.encode(), f"{actor_id}.{actor_timestamp}".encode(), "sha256").hexdigest()
    if not hmac.compare_digest(actor_signature, expected):
        raise HTTPException(status_code=401, detail="Invalid Console identity")
    require_console_admin(actor_id)
    return actor_id


def append_memory_audit(record: dict) -> None:
    try:
        MEMORY_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MEMORY_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Memory access audit is unavailable") from exc


def unknown_deployment_status(component: Literal["bot", "agent"], detail: str) -> dict:
    return DeploymentStatusRecord(
        schema_version=1,
        deployment_id=f"{component}-observation-unconfigured",
        component=component,
        status="unknown",
        phase="observation",
        error=detail,
    ).model_dump(mode="json")


def deployment_status(
    component: Literal["bot", "agent"], status_path: Path
) -> dict:
    """Return deploy-produced evidence, never infer success from service state."""
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return unknown_deployment_status(
            component, "Deployment status has not been configured yet."
        )
    except (OSError, json.JSONDecodeError):
        return unknown_deployment_status(component, "Deployment status file is unreadable.")
    try:
        record = DeploymentStatusRecord.model_validate(value)
    except ValidationError:
        return unknown_deployment_status(component, "Invalid deployment status data.")

    if record.component != component:
        return unknown_deployment_status(
            component, "Deployment status component does not match its endpoint."
        )
    if record.status == "succeeded":
        if not record.target_revision or not record.running_revision:
            return unknown_deployment_status(
                component, "A successful deployment is missing revision attestation."
            )
        if record.target_revision != record.running_revision:
            return unknown_deployment_status(
                component, "Deployment target and running revisions do not match."
            )
        if record.verified_at is None:
            return unknown_deployment_status(
                component, "A successful deployment is missing its verification time."
            )
        if not record.checks or any(check.status != "passed" for check in record.checks):
            return unknown_deployment_status(
                component, "A successful deployment is missing passing readiness checks."
            )
        age_seconds = (datetime.now(timezone.utc) - record.verified_at).total_seconds()
        if age_seconds > DEPLOYMENT_STATUS_MAX_AGE_SECONDS:
            record.status = "stale"
            record.error = "Deployment verification is older than the freshness window."
    return record.model_dump(mode="json")


def read_runtime_settings() -> dict:
    """Ask the bot package for a read-only, display-safe settings snapshot."""
    script = (
        "import json; "
        "from rio_bot.core.config import Settings; "
        "from rio_bot.core.runtime_settings_snapshot import runtime_settings_snapshot; "
        "print(json.dumps({'settings': runtime_settings_snapshot(Settings.load())}, "
        "ensure_ascii=False))"
    )
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    try:
        result = subprocess.run(
            [BOT_PYTHON, "-c", script],
            capture_output=True,
            cwd=BOT_REPO,
            env=environment,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), list):
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable")
    return payload


def read_policy_snapshot() -> dict:
    script = (
        "import json; "
        "from rio_bot.core.config import Settings; "
        "from rio_bot.core.runtime_settings_snapshot import policy_snapshot; "
        "print(json.dumps({'policies': policy_snapshot(Settings.load())}, ensure_ascii=False))"
    )
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
    try:
        result = subprocess.run([BOT_PYTHON, "-c", script], capture_output=True, cwd=BOT_REPO, env=environment, text=True, timeout=10)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Policy settings are unavailable") from exc
    if result.returncode != 0 or not isinstance(payload, dict) or not isinstance(payload.get("policies"), list):
        raise HTTPException(status_code=503, detail="Policy settings are unavailable")
    return payload


def read_runtime_config_audit_events(limit: int) -> dict:
    """Ask the bot package for a content-free, read-only audit snapshot."""
    script = (
        "import json; "
        "from rio_bot.core.config import Settings; "
        "from rio_bot.core.runtime_settings_snapshot import runtime_config_audit_snapshot; "
        f"print(json.dumps({{'events': runtime_config_audit_snapshot(Settings.load(), limit={limit})}}, "
        "ensure_ascii=False))"
    )
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    try:
        result = subprocess.run(
            [BOT_PYTHON, "-c", script],
            capture_output=True,
            cwd=BOT_REPO,
            env=environment,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Runtime audit is unavailable") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="Runtime audit is unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Runtime audit is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise HTTPException(status_code=503, detail="Runtime audit is unavailable")
    return payload


def write_runtime_setting(
    *, key: str, value: str | None, actor_id: str, request_id: str
) -> dict:
    """Invoke the Bot-owned write service; values never enter agent logs or audit rows."""
    script = """
import json, sys
from rio_bot.core.config import Settings
from rio_bot.core.store import Store
from rio_bot.core.runtime_config import RUNTIME_SETTING_SPECS, RuntimeConfigAudit, RuntimeSettings, runtime_setting_attr
from rio_bot.core.runtime_settings_service import apply_runtime_setting
base = Settings.load()
store = Store(base.db_path, base.history_turns)
try:
    result = apply_runtime_setting(
        RuntimeSettings(base, store), key=sys.argv[1], value=None if sys.argv[2] == "__RESET__" else sys.argv[2],
        actor_kind="console", actor_id=sys.argv[3], request_id=sys.argv[4],
    )
    print(json.dumps({"ok": True, "setting": result}, ensure_ascii=False))
except ValueError as exc:
    try:
        attr = runtime_setting_attr(sys.argv[1])
        if not any(row["request_id"] == sys.argv[4] for row in settings.audit_rows()):
            settings.record_audit(RuntimeConfigAudit(
                actor_kind="console", actor_id=sys.argv[3],
                action="runtime_config.set" if sys.argv[2] != "__RESET__" else "runtime_config.reset",
                target=RUNTIME_SETTING_SPECS[attr].env_name, outcome="failure", request_id=sys.argv[4],
            ))
    except ValueError:
        pass
    print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False))
finally:
    store.close()
"""
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    try:
        result = subprocess.run(
            [BOT_PYTHON, "-c", script, key, value if value is not None else "__RESET__", actor_id, request_id],
            capture_output=True, cwd=BOT_REPO, env=environment, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable") from exc
    if result.returncode != 0 or not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable")
    if payload.get("ok") is False:
        detail = payload.get("detail")
        raise HTTPException(
            status_code=400,
            detail=detail if isinstance(detail, str) and len(detail) <= 300 else "Invalid runtime setting",
        )
    if not isinstance(payload.get("setting"), dict):
        raise HTTPException(status_code=503, detail="Runtime settings are unavailable")
    return payload["setting"]


def write_policy_setting(*, policy: str, scope: str, value: str, actor_id: str, request_id: str) -> dict:
    script = """
import json, sys
from rio_bot.core.config import Settings
from rio_bot.core.store import Store
from rio_bot.core.policy_settings_service import set_policy_override
base = Settings.load()
store = Store(base.db_path, base.history_turns)
try:
    result = set_policy_override(store, policy=sys.argv[1], scope=sys.argv[2], value=sys.argv[3], actor_kind='console', actor_id=sys.argv[4], request_id=sys.argv[5])
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
except ValueError as exc:
    print(json.dumps({'ok': False, 'detail': str(exc)}, ensure_ascii=False))
finally:
    store.close()
"""
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    environment["PYTHONPATH"] = source_root if not environment.get("PYTHONPATH") else f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
    try:
        result = subprocess.run([BOT_PYTHON, "-c", script, policy, scope, value, actor_id, request_id], capture_output=True, cwd=BOT_REPO, env=environment, text=True, timeout=10)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Policy settings are unavailable") from exc
    if result.returncode != 0 or not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="Policy settings are unavailable")
    if payload.get("ok") is False:
        raise HTTPException(status_code=400, detail=str(payload.get("detail", "Invalid policy setting"))[:300])
    if not isinstance(payload.get("result"), dict):
        raise HTTPException(status_code=503, detail="Policy settings are unavailable")
    return payload["result"]


def discord_console_role(user_id: str) -> str:
    """Resolve a Discord identity against the Bot's current admin configuration."""
    if not user_id.isdigit() or len(user_id) > 30:
        raise HTTPException(status_code=400, detail="Invalid Discord identity")
    script = (
        "import json; "
        "from rio_bot.core.config import Settings; "
        "print(json.dumps({'bot_admin_ids': [str(item) for item in Settings.load().bot_admin_ids]}))"
    )
    environment = os.environ.copy()
    source_root = str(BOT_REPO / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    try:
        result = subprocess.run(
            [BOT_PYTHON, "-c", script],
            capture_output=True,
            cwd=BOT_REPO,
            env=environment,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Authorization is unavailable") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="Authorization is unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Authorization is unavailable") from exc
    admin_ids = payload.get("bot_admin_ids") if isinstance(payload, dict) else None
    if not isinstance(admin_ids, list) or not all(isinstance(item, str) for item in admin_ids):
        raise HTTPException(status_code=503, detail="Authorization is unavailable")
    return "admin" if user_id in admin_ids else "viewer"


@app.get("/health")
def health(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    return {
        "ok": True
    }


@app.get("/status")
def status(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    active = run(["systemctl", "is-active", SERVICE])

    show = run([
        "systemctl",
        "show",
        SERVICE,
        "--property=MainPID"
    ])

    values = {}

    for line in show.stdout.strip().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    pid = int(values.get("MainPID", "0") or 0)

    memory_mb = None
    cpu_percent = None
    uptime_seconds = None

    if pid > 0:
        try:
            process = psutil.Process(pid)

            memory_mb = round(
                process.memory_info().rss / 1024 / 1024,
                1
            )

            cpu_percent = round(
                process.cpu_percent(interval=0.1),
                1
            )

            uptime_seconds = int(
                time.time() - process.create_time()
            )

        except psutil.Error:
            pass

    return {
        "service": SERVICE,
        "online": active.stdout.strip() == "active",
        "pid": pid,
        "memory_mb": memory_mb,
        "cpu_percent": cpu_percent,
        "uptime_seconds": uptime_seconds,
        "runtime": read_status(),
    }


@app.get("/settings/runtime")
def runtime_settings(authorization: str | None = Header(default=None)):
    verify_token(authorization)
    return read_runtime_settings()


@app.get("/settings/policies")
def policies(authorization: str | None = Header(default=None)):
    verify_token(authorization)
    return read_policy_snapshot()


@app.put("/settings/policies/{policy}/{scope:path}")
def set_policy_setting(policy: str, scope: str, write: PolicyWrite, authorization: str | None = Header(default=None)):
    verify_token(authorization)
    require_console_admin(write.actor_id)
    return write_policy_setting(policy=policy, scope=scope, value=write.value, actor_id=write.actor_id, request_id=str(write.request_id))


def require_console_admin(actor_id: str) -> None:
    if discord_console_role(actor_id) != "admin":
        raise HTTPException(status_code=403, detail="Discord bot administrator access is required")


@app.put("/settings/runtime/{key}")
def set_runtime_setting(
    key: str, write: RuntimeSettingWrite, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    require_console_admin(write.actor_id)
    return write_runtime_setting(
        key=key, value=write.value, actor_id=write.actor_id, request_id=str(write.request_id)
    )


@app.delete("/settings/runtime/{key}")
def reset_runtime_setting(
    key: str, reset: RuntimeSettingReset, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    require_console_admin(reset.actor_id)
    return write_runtime_setting(
        key=key, value=None, actor_id=reset.actor_id, request_id=str(reset.request_id)
    )


@app.post("/auth/discord-user")
def authorize_discord_user(
    identity: DiscordIdentity, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    if not identity.user_id.isdigit() or len(identity.user_id) > 30:
        raise HTTPException(status_code=400, detail="Invalid Discord identity")
    return {"id": identity.user_id, "role": discord_console_role(identity.user_id)}


@app.get("/settings/audit-events")
def runtime_config_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    return read_runtime_config_audit_events(limit)


@app.get("/logs")
def logs(
    lines: int = 100,
    authorization: str | None = Header(default=None)
):
    verify_token(authorization)

    lines = max(1, min(lines, 500))

    result = run([
        "journalctl",
        "-u",
        SERVICE,
        "-n",
        str(lines),
        "--no-pager",
        "-o",
        "short-iso"
    ])

    return {
        "logs": result.stdout.splitlines()
    }


@app.get("/events")
def events(
    lines: int = 100,
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    return {"events": read_events(max(1, min(lines, 500)))}


@app.get("/traces")
def trace_list(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    validate_period(from_, to)
    rows, source, next_cursor = traces(None, limit, from_, to, cursor)
    return {"source": source_freshness(EVENT_LOG_PATH), "source_status": source,
            "traces": rows, "next_cursor": next_cursor}


@app.get("/traces/{turn_id}")
def trace_detail(turn_id: UUID, authorization: str | None = Header(default=None)):
    verify_token(authorization)
    rows, source, _ = traces(str(turn_id), 100)
    if not rows and source == "UNAVAILABLE":
        raise HTTPException(status_code=503, detail="Trace source is unavailable")
    if not rows:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"source": source_freshness(EVENT_LOG_PATH), "source_status": source, "trace": rows}


@app.get("/analytics/usage")
def analytics_usage(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    group_by: Literal["provider", "model"] = "provider",
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    validate_period(from_, to)
    rows, source = usage_analytics(limit, group_by, from_, to)
    return {"source": source_freshness(USAGE_LOG_PATH), "source_status": source,
            "group_by": group_by, "groups": rows}


@app.get("/memory")
def memory_list(
    limit: int = Query(default=50, ge=1, le=100),
    scope: Literal["channel", "owner_private"] | None = None,
    cursor: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    rows, source, next_cursor = read_memory_metadata(limit, scope, cursor)
    return {"source": source_freshness(BOT_DB_PATH), "source_status": source,
            "memory": rows, "next_cursor": next_cursor}


@app.get("/memory/{item_id}")
def memory_detail(
    item_id: int,
    actor_id: str | None = Header(default=None, alias="X-Rio-Actor-Id"),
    actor_timestamp: str | None = Header(default=None, alias="X-Rio-Actor-Timestamp"),
    actor_signature: str | None = Header(default=None, alias="X-Rio-Actor-Signature"),
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    verified_actor = require_signed_console_admin(actor_id, actor_timestamp, actor_signature)
    item, source = read_memory_item_metadata(item_id)
    if source == "UNAVAILABLE":
        raise HTTPException(status_code=503, detail="Memory source is unavailable")
    if item is None:
        raise HTTPException(status_code=404, detail="Memory item not found")
    append_memory_audit({
        "at": utc_now(), "kind": "memory.metadata.read", "actor_kind": "console",
        "actor_id": verified_actor, "item_id": item_id, "outcome": "success",
    })
    return {"source": source_freshness(BOT_DB_PATH), "source_status": source, "memory": item}


@app.get("/data-sources")
def data_sources(authorization: str | None = Header(default=None)):
    verify_token(authorization)
    _, events_status = read_jsonl(EVENT_LOG_PATH, 1)
    _, usage_status = read_jsonl(USAGE_LOG_PATH, 1)
    _, memory_status, _ = read_memory_metadata(1)
    return {"sources": {
        "events": source_freshness(EVENT_LOG_PATH), "usage": source_freshness(USAGE_LOG_PATH),
        "memory": source_freshness(BOT_DB_PATH),
    }, "source_status": {"events": events_status, "usage": usage_status, "memory": memory_status}}


@app.get("/deployments")
def deployments(authorization: str | None = Header(default=None)):
    verify_token(authorization)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deployments": [
            deployment_status("bot", BOT_DEPLOY_STATUS_PATH),
            deployment_status("agent", AGENT_DEPLOY_STATUS_PATH),
        ],
    }


@app.get("/operations")
def operations(
    limit: int = Query(default=50, ge=1, le=100),
    authorization: str | None = Header(default=None),
):
    verify_token(authorization)
    return {"operations": read_operations(limit)}


def bot_control(
    action: Literal["start", "stop", "restart"], request: BotControlRequest
) -> dict:
    require_console_admin(request.actor_id)
    requested_at = utc_now()
    result = run(["systemctl", action, SERVICE])
    post_check = run(["systemctl", "is-active", SERVICE]).stdout.strip()
    expected_state = "inactive" if action == "stop" else "active"
    succeeded = result.returncode == 0 and post_check == expected_state
    completed_at = utc_now()
    operation = {
        "operation_id": str(uuid4()),
        "kind": f"bot.{action}",
        "actor_kind": "console",
        "actor_id": request.actor_id,
        "request_id": str(request.request_id),
        "requested_at": requested_at,
        "completed_at": completed_at,
        "result": "success" if succeeded else "failure",
        "post_check": "healthy" if succeeded else "failed",
        "service_state": post_check or "unknown",
        "error": None if succeeded else "Bot control command or post-check failed.",
    }
    append_operation(operation)
    if not succeeded:
        raise HTTPException(status_code=502, detail="Bot control command or post-check failed")
    return operation


@app.post("/bot/start")
def bot_start(
    request: BotControlRequest, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    return bot_control("start", request)


@app.post("/bot/stop")
def bot_stop(
    request: BotControlRequest, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    return bot_control("stop", request)



@app.post("/bot/restart")
def bot_restart(
    request: BotControlRequest, authorization: str | None = Header(default=None)
):
    verify_token(authorization)
    return bot_control("restart", request)

@app.get("/logs/stream")
async def logs_stream(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    async def event_generator():
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u", SERVICE,
            "-f",
            "-n", "20",
            "-o", "short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            while True:
                line = await process.stdout.readline()

                if not line:
                    break

                text = line.decode(errors="replace").rstrip()
                yield f"data: {text}\n\n"

        finally:
            if process.returncode is None:
                process.terminate()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
