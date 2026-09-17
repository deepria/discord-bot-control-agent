import asyncio
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

import psutil
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI(title="Rio Agent")

SERVICE = "rio-bot.service"
TOKEN = os.environ["RIO_AGENT_TOKEN"]
EVENT_LOG_PATH = Path(os.getenv("RIO_EVENT_LOG_PATH", "/opt/rio-discord-bot/data/logs/events.jsonl"))
STATUS_PATH = Path(os.getenv("RIO_STATUS_PATH", "/opt/rio-discord-bot/data/logs/status.json"))


def run(cmd: list[str]):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )


def verify_token(authorization: str | None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")


def read_status() -> dict | None:
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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
            events.append(value)
    return events

    expected = f"Bearer {TOKEN}"

    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


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
