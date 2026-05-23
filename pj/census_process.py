from __future__ import annotations

"""Background process controls for the census web dashboard."""

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from . import paths


STATE_FILE = "census-server.json"
LOG_FILE = "census-server.log"


def state_path() -> Path:
    return paths.data_dir() / STATE_FILE


def log_path() -> Path:
    return paths.data_dir() / LOG_FILE


def load_state() -> dict[str, Any] | None:
    path = state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def remove_state() -> None:
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def _save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _is_process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return False
    return not result.stdout.lstrip().startswith("Z")


def _health_url(state: dict[str, Any]) -> str:
    return f"http://{state['host']}:{state['port']}/api/health"


def _control_url(state: dict[str, Any]) -> str:
    return f"http://{state['host']}:{state['port']}/api/control/stop"


def _request_json(url: str, *, token: str | None = None, timeout: float = 0.5) -> dict[str, Any] | None:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-PJ-Control-Token"] = token
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError):
        return None


def _post_json(url: str, *, token: str, timeout: float = 1.0) -> dict[str, Any] | None:
    req = request.Request(
        url,
        data=b"",
        headers={"Accept": "application/json", "X-PJ-Control-Token": token},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError):
        return None


def status() -> dict[str, Any]:
    state = load_state()
    if state is None:
        return {
            "status": "stopped",
            "running": False,
            "state_file": str(state_path()),
            "log_file": str(log_path()),
        }

    pid = state.get("pid")
    pid_alive = _is_process_alive(pid)
    health = _request_json(_health_url(state)) if pid_alive else None
    running = bool(pid_alive and health and health.get("success"))
    public_state = {key: value for key, value in state.items() if key != "control_token"}
    result = {
        **public_state,
        "status": "running" if running else ("unreachable" if pid_alive else "stale"),
        "running": running,
        "pid_alive": pid_alive,
        "health_ok": bool(health and health.get("success")),
        "state_file": str(state_path()),
        "log_file": str(log_path()),
    }
    if health and isinstance(health.get("data"), dict):
        result["health"] = health["data"]
    return result


def start(
    *,
    host: str,
    port: int,
    limit: int,
    check_interval: int,
    wait_secs: float = 5.0,
) -> dict[str, Any]:
    current = status()
    if current["running"]:
        return {**current, "started": False, "already_running": True}

    if current["status"] == "stale":
        remove_state()

    token = secrets.token_urlsafe(32)
    url = f"http://{host}:{port}/"
    command = [
        sys.executable,
        "-m",
        "pj",
        "census",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--limit",
        str(limit),
        "--check-interval",
        str(check_interval),
    ]

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PJ_CENSUS_CONTROL_TOKEN"] = token
    env["PJ_CENSUS_BACKGROUND"] = "1"

    with log.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    state = {
        "pid": process.pid,
        "host": host,
        "port": port,
        "url": url,
        "limit": limit,
        "check_interval": check_interval,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "control_token": token,
    }
    _save_state(state)

    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _request_json(_health_url(state), timeout=0.2):
            return {**status(), "started": True, "already_running": False}
        time.sleep(0.1)

    current = status()
    if not current["running"]:
        current.update(started=False, already_running=False)
        return current
    return {**current, "started": True, "already_running": False}


def stop(*, wait_secs: float = 5.0) -> dict[str, Any]:
    state = load_state()
    current = status()
    if not current.get("pid"):
        return {**current, "stopped": False}

    if not current["pid_alive"]:
        remove_state()
        return {**current, "stopped": False, "state_removed": True}

    token = state.get("control_token") if state else None
    if not token:
        return {**current, "stopped": False, "error": "missing control token"}

    response = _post_json(_control_url(current), token=token)
    deadline = time.monotonic() + wait_secs
    while time.monotonic() < deadline:
        if not _is_process_alive(current["pid"]):
            remove_state()
            stopped = {key: value for key, value in current.items() if key != "health"}
            stopped.update(
                status="stopped",
                running=False,
                pid_alive=False,
                health_ok=False,
                stopped=True,
                state_removed=True,
                control_response=response,
            )
            return stopped
        time.sleep(0.1)

    return {**status(), "stopped": False, "control_response": response}
