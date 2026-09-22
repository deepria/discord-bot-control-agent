import asyncio
import hmac
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psutil
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

app = FastAPI(title="Rio Agent")

SERVICE = "rio-bot.service"
TOKEN = os.environ["RIO_AGENT_TOKEN"]
EVENT_LOG_PATH = Path(os.getenv("RIO_EVENT_LOG_PATH", "/opt/rio-discord-bot/data/logs/events.jsonl"))
STATUS_PATH = Path(os.getenv("RIO_STATUS_PATH", "/opt/rio-discord-bot/data/logs/status.json"))
BOT_REPO = Path(os.getenv("RIO_BOT_REPO", "/opt/rio-discord-bot"))
AGENT_REPO = Path(os.getenv("RIO_AGENT_REPO", "/opt/rio-agent"))
BOT_PYTHON = os.getenv("RIO_BOT_PYTHON", str(BOT_REPO / ".venv" / "bin" / "python"))


class DiscordIdentity(BaseModel):
    user_id: str


class RuntimeSettingWrite(BaseModel):
    value: str
    request_id: UUID
    actor_id: str


class RuntimeSettingReset(BaseModel):
    request_id: UUID
    actor_id: str


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


def command_value(cmd: list[str]) -> str:
    result = run(cmd)
    return result.stdout.strip() if result.returncode == 0 else ""


def systemd_properties(unit: str, properties: list[str]) -> dict[str, str]:
    result = run(["systemctl", "show", unit, *[f"--property={name}" for name in properties]])
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def deployment_status(name: str, repo: Path, service: str, timer: str) -> dict:
    revision = command_value(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
    remote_revision = command_value(
        ["git", "-C", str(repo), "rev-parse", "--short", "origin/main"]
    )
    dirty = bool(command_value(["git", "-C", str(repo), "status", "--porcelain"]))
    service_state = systemd_properties(
        service,
        ["ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainExitTimestamp"],
    )
    timer_state = systemd_properties(
        timer,
        ["ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec"],
    )
    return {
        "component": name,
        "revision": revision or None,
        "remote_revision": remote_revision or None,
        "update_available": bool(revision and remote_revision and revision != remote_revision),
        "working_tree_dirty": dirty,
        "service": service_state,
        "timer": timer_state,
    }


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


@app.get("/deployments")
def deployments(authorization: str | None = Header(default=None)):
    verify_token(authorization)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deployments": [
            deployment_status("bot", BOT_REPO, "rio-bot-deploy.service", "rio-bot-deploy.timer"),
            deployment_status(
                "agent", AGENT_REPO, "rio-agent-deploy.service", "rio-agent-deploy.timer"
            ),
        ],
    }


@app.post("/bot/start")
def bot_start(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    result = run(["systemctl", "start", SERVICE])

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stderr.strip()
        )

    return {
        "ok": True,
        "action": "start"
    }


@app.post("/bot/stop")
def bot_stop(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    result = run(["systemctl", "stop", SERVICE])

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stderr.strip()
        )

    return {
        "ok": True,
        "action": "stop"
    }


@app.post("/bot/restart")
def bot_restart(authorization: str | None = Header(default=None)):
    verify_token(authorization)

    result = run(["systemctl", "restart", SERVICE])

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stderr.strip()
        )

    return {
        "ok": True,
        "action": "restart"
    }

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
