import asyncio
import hmac
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI(title="Rio Agent")

SERVICE = "rio-bot.service"
TOKEN = os.environ["RIO_AGENT_TOKEN"]
EVENT_LOG_PATH = Path(os.getenv("RIO_EVENT_LOG_PATH", "/opt/rio-discord-bot/data/logs/events.jsonl"))
STATUS_PATH = Path(os.getenv("RIO_STATUS_PATH", "/opt/rio-discord-bot/data/logs/status.json"))
BOT_REPO = Path(os.getenv("RIO_BOT_REPO", "/opt/rio-discord-bot"))
AGENT_REPO = Path(os.getenv("RIO_AGENT_REPO", "/opt/rio-agent"))


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
