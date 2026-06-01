from __future__ import annotations

"""Local listening TCP port discovery.

This module intentionally keeps platform command parsing contained here. It
does not mutate processes or sockets; it only inspects command output and, when
available, process metadata exposed by the local OS.
"""

import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable

from . import discover, envelope

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=2, check=False)


def _parse_address(value: str) -> tuple[str | None, int | None]:
    value = value.strip()
    if "->" in value:
        value = value.split("->", 1)[0].strip()
    if value.startswith("["):
        match = re.match(r"^\[(?P<host>.*)]:(?P<port>\d+)$", value)
        if not match:
            return None, None
        return match.group("host"), int(match.group("port"))
    if ":" not in value:
        return None, None
    host, port_text = value.rsplit(":", 1)
    if not port_text.isdigit():
        return None, None
    host = host.strip() or "0.0.0.0"
    return host, int(port_text)


def _local_urls(host: str | None, port: int) -> list[str]:
    if host in (None, "", "*", "0.0.0.0", "::", "[::]"):
        return [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/"]
    if ":" in host and not host.startswith("["):
        return [f"http://[{host}]:{port}/"]
    return [f"http://{host}:{port}/"]


def _empty_record(*, source: str, host: str | None, port: int, pid: int | None = None,
                  command: str | None = None, cwd: str | None = None) -> dict:
    return {
        "project_id": None,
        "path": None,
        "live_urls": _local_urls(host, port),
        "pid": pid,
        "port": port,
        "host": host or "127.0.0.1",
        "command": command,
        "cwd": cwd,
        "confidence": "unknown",
        "source": source,
    }


def parse_lsof(output: str) -> list[dict]:
    """Parse standard `lsof -nP -iTCP -sTCP:LISTEN` table output."""
    records: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("COMMAND "):
            continue
        match = re.search(r"\bTCP\s+(?P<addr>.+?)\s+\(LISTEN\)$", line)
        if not match:
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        host, port = _parse_address(match.group("addr"))
        if port is None:
            continue
        records.append(
            _empty_record(
                source="lsof",
                host=host,
                port=port,
                pid=int(parts[1]),
                command=parts[0],
            )
        )
    return records


def parse_ss(output: str) -> list[dict]:
    """Parse `ss -H -ltnp` or headered `ss -ltnp` output."""
    records: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("State "):
            continue
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue
        host, port = _parse_address(parts[3])
        if port is None:
            continue
        pid = None
        command = None
        process_match = re.search(r'"(?P<command>[^"]+)".*?pid=(?P<pid>\d+)', line)
        if process_match:
            command = process_match.group("command")
            pid = int(process_match.group("pid"))
        records.append(_empty_record(source="ss", host=host, port=port, pid=pid, command=command))
    return records


def _ipv4_from_proc(hex_host: str) -> str:
    packed = bytes.fromhex(hex_host)
    return str(ipaddress.ip_address(packed[::-1]))


def _ipv6_from_proc(hex_host: str) -> str:
    packed = bytes.fromhex(hex_host)
    chunks = [packed[i:i + 4][::-1] for i in range(0, 16, 4)]
    return str(ipaddress.ip_address(b"".join(chunks)))


def parse_proc_net_tcp(output: str, *, source: str = "procfs") -> list[dict]:
    """Parse `/proc/net/tcp` or `/proc/net/tcp6` content."""
    records: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("sl"):
            continue
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        if ":" not in parts[1]:
            continue
        host_hex, port_hex = parts[1].split(":", 1)
        try:
            port = int(port_hex, 16)
            host = _ipv6_from_proc(host_hex) if len(host_hex) == 32 else _ipv4_from_proc(host_hex)
        except (OSError, ValueError):
            continue
        records.append(_empty_record(source=source, host=host, port=port))
    return records


def _pid_cwd(pid: int | None, *, runner: CommandRunner = _default_runner) -> str | None:
    if pid is None:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass

    try:
        result = runner(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and line[1:].strip():
            return line[1:].strip()
    return None


def _is_inside(path: str, parent: str) -> bool:
    try:
        real_path = os.path.realpath(path)
        real_parent = os.path.realpath(parent)
        return os.path.commonpath([real_path, real_parent]) == real_parent
    except (OSError, ValueError):
        return False


def _associate(record: dict, projects: Iterable[dict]) -> dict:
    projects = list(projects)
    cwd = record.get("cwd")
    command = (record.get("command") or "").lower()
    containing_projects = sorted(
        (
            project for project in projects
            if cwd and project.get("path") and _is_inside(str(cwd), str(project.get("path")))
        ),
        key=lambda project: len(os.path.realpath(str(project.get("path") or ""))),
        reverse=True,
    )
    for project in containing_projects:
        path = project.get("path")
        record["project_id"] = project.get("id")
        record["path"] = path
        record["confidence"] = "high"
        return record

    for project in sorted(projects, key=lambda project: len(str(project.get("path") or "")), reverse=True):
        path = str(project.get("path") or "")
        name = str(project.get("name") or "")
        if path and path.lower() in command:
            record["project_id"] = project.get("id")
            record["path"] = project.get("path")
            record["confidence"] = "medium"
            return record
        if name and name.lower() in command:
            record["project_id"] = project.get("id")
            record["path"] = project.get("path")
            record["confidence"] = "low"
            return record
    return record


def _collect_from_commands(runner: CommandRunner = _default_runner) -> tuple[list[dict], list[str], list[str]]:
    records: list[dict] = []
    sources: list[str] = []
    warnings: list[str] = []
    commands: list[tuple[str, list[str], Callable[[str], list[dict]]]] = [
        ("lsof", ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], parse_lsof),
        ("ss", ["ss", "-H", "-ltnp"], parse_ss),
    ]

    for source, argv, parser in commands:
        try:
            result = runner(argv)
        except FileNotFoundError:
            warnings.append(f"{source} not found")
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"{source} failed: {exc}")
            continue
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            warnings.append(f"{source} exited {result.returncode}" + (f": {detail}" if detail else ""))
            continue
        parsed = parser(result.stdout)
        sources.append(source)
        records.extend(parsed)

    proc_path = Path("/proc/net/tcp")
    if proc_path.exists():
        try:
            records.extend(parse_proc_net_tcp(proc_path.read_text(), source="procfs"))
            sources.append("procfs")
        except OSError as exc:
            warnings.append(f"procfs failed: {exc}")
    proc6_path = Path("/proc/net/tcp6")
    if proc6_path.exists():
        try:
            records.extend(parse_proc_net_tcp(proc6_path.read_text(), source="procfs"))
            if "procfs" not in sources:
                sources.append("procfs")
        except OSError as exc:
            warnings.append(f"procfs tcp6 failed: {exc}")

    return records, sources, warnings


def _dedupe(records: Iterable[dict]) -> list[dict]:
    seen: set[tuple[object, object, object]] = set()
    unique: list[dict] = []
    for record in records:
        key = (record.get("pid"), record.get("host"), record.get("port"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def discover_ports(*, project: dict | None = None, projects: list[dict] | None = None,
                   runner: CommandRunner = _default_runner) -> tuple[list[dict], dict]:
    records, sources, warnings = _collect_from_commands(runner)
    if project is not None:
        candidates = [project]
    else:
        candidates = projects if projects is not None else discover.discover(limit=9999)[0]

    normalized: list[dict] = []
    for record in records:
        record["cwd"] = record.get("cwd") or _pid_cwd(record.get("pid"), runner=runner)
        normalized.append(_associate(record, candidates))

    if project is not None:
        project_id = project.get("id")
        normalized = [record for record in normalized if record.get("project_id") == project_id]

    normalized = _dedupe(normalized)
    meta = {
        "total": len(normalized),
        "sources": sorted(set(sources)),
        "warnings": warnings,
    }
    return normalized, meta


def ports(project_query: str | None = None, *, runner: CommandRunner = _default_runner) -> dict:
    start = time.monotonic()
    try:
        project = None
        project_meta = None
        if project_query:
            project = discover.resolve_project(project_query)
            if project is None:
                return envelope.err(f"No project matching {project_query!r}", source="ports")
            project_meta = {
                "id": project.get("id"),
                "name": project.get("name"),
                "path": project.get("path"),
            }
        records, meta = discover_ports(project=project, runner=runner)
        latency_ms = int((time.monotonic() - start) * 1000)
        return envelope.ok(
            records,
            **meta,
            **({"project": project_meta} if project_meta else {}),
            latency_ms=latency_ms,
        )
    except Exception as exc:  # pragma: no cover - defensive facade boundary
        latency_ms = int((time.monotonic() - start) * 1000)
        return envelope.err(str(exc), source="ports", latency_ms=latency_ms, warnings=[])
