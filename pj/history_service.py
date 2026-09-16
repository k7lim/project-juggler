"""Explicitly configured, authenticated read-only history HTTP service.

Deliberately independent of discover, fs_store, census and host HOME defaults.
See docs/history-service-contract.md. No listener starts on import.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from socketserver import UnixStreamServer
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .envelope import err, ok

MAX_FILE = 8 * 1024 * 1024
MAX_CORPUS = 64 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_FILES = 2000
WORKER_TIMEOUT = 3
REQUEST_TIMEOUT = 5
AGENTS = {"claude_code", "codex"}
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _open_regular(path: str) -> int:
    """Open through directory descriptors so no path component follows a symlink."""
    parts = Path(path).parts
    if not os.path.isabs(path) or ".." in parts:
        raise ValueError("invalid path")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("invalid file")
        return descriptor
    finally:
        os.close(directory)


def _private_directory(path: str) -> None:
    if not os.path.isabs(path) or str(Path(path).resolve()) != os.path.normpath(path):
        raise ValueError("invalid private directory")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("directory must be owner-only")


def load_config(path: str) -> dict:
    """Read one protected, host-controlled configuration; never infer roots."""
    _private_directory(str(Path(path).parent))
    with os.fdopen(_open_regular(path), "rb") as handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("config must be owner-only")
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("config too large")
    config = json.loads(raw)
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("invalid config")
    # Unknown config fields are ignored for forward compatibility. They can never
    # enable capabilities: only explicitly validated keys are retained below.
    audience = config.get("audience", "private")
    digest = config.get("token_sha256", "")
    if audience not in {"private", "approved-public"} or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("invalid config")
    expires = datetime.fromisoformat(config["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        raise ValueError("expiry requires timezone")
    roots = config.get("roots", [])
    snapshots = config.get("snapshots", [])
    entries = roots if audience == "private" else snapshots
    if (not isinstance(entries, list) or not entries or
            len(entries) > (32 if audience == "private" else MAX_FILES) or
            (audience == "private" and snapshots) or
            (audience == "approved-public" and roots)):
        raise ValueError("invalid corpus")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("agent") not in AGENTS:
            raise ValueError("invalid corpus")
        path_value = entry.get("path")
        if (not isinstance(path_value, str) or not os.path.isabs(path_value) or
                ".." in Path(path_value).parts or any(ord(c) < 32 for c in path_value)):
            raise ValueError("invalid corpus")
        item = {"agent": entry["agent"], "path": path_value}
        if audience == "approved-public":
            value = entry.get("sha256", "")
            if not isinstance(value, str) or not DIGEST.fullmatch(value):
                raise ValueError("invalid snapshot")
            item["sha256"] = value
        normalized.append(item)
    temp_dir = config.get("temp_dir", str(Path(path).parent / "parser-tmp"))
    if not isinstance(temp_dir, str):
        raise ValueError("invalid temp directory")
    if not os.path.exists(temp_dir):
        _private_directory(str(Path(temp_dir).parent))
        os.mkdir(temp_dir, 0o700)
    _private_directory(temp_dir)
    unix_socket = config.get("unix_socket")
    if unix_socket is not None:
        if not isinstance(unix_socket, str) or len(os.fsencode(unix_socket)) > 100:
            raise ValueError("invalid socket")
        _private_directory(str(Path(unix_socket).parent))
    index_dir = config.get("index_dir")
    if index_dir is not None:
        if audience != "private" or not isinstance(index_dir, str):
            raise ValueError("invalid index directory")
        _private_directory(index_dir)
    return {"audience": audience, "token_sha256": digest,
            "expires": expires.timestamp(), "entries": normalized,
            "temp_dir": temp_dir, "unix_socket": unix_socket, "index_dir": index_dir}


def _text(value: str, maximum: int) -> str:
    if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value) or "%" in value or ".." in value:
        raise RequestError(400, "invalid request")
    return value


def parse_request_target(target: str) -> dict:
    if len(target.encode("utf-8")) > 4096:
        raise RequestError(413, "request limit exceeded")
    url = urlsplit(target)
    if url.scheme or url.netloc or url.fragment:
        raise RequestError(400, "invalid request")
    path = url.path
    if path == "/api/health":
        route, allowed = "health", set()
    elif path == "/api/search":
        route, allowed = "search", {"q", "project", "sort", "match", "limit", "offset"}
    elif path == "/api/chats":
        route, allowed = "chats", {"project", "limit", "offset"}
    elif path.startswith("/api/chat/"):
        route, allowed = "chat", {"limit", "offset", "last", "roles", "include_tools", "all_branches"}
        if not ID.fullmatch(path[len("/api/chat/"):]):
            raise RequestError(400, "invalid request")
    else:
        raise RequestError(404, "not found")
    try:
        fields = parse_qs(url.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=20, errors="strict") if url.query else {}
    except (ValueError, UnicodeError):
        raise RequestError(400, "invalid request") from None
    if set(fields) - allowed or any(len(v) != 1 for k, v in fields.items() if k != "q"):
        raise RequestError(400, "invalid request")
    request = {"route": route, "limit": 100 if route == "chat" else 20, "offset": 0}
    for key in ("limit", "offset", "last"):
        if key in fields:
            value = fields[key][0]
            maximum = {"limit": 100, "offset": 100000, "last": 1000}[key]
            if not value.isascii() or not value.isdigit() or len(value) > 6 or not (0 if key == "offset" else 1) <= int(value) <= maximum:
                raise RequestError(400, "invalid request")
            request[key] = int(value)
    if "project" in fields:
        request["project"] = _text(fields["project"][0], 512)
    if route == "search":
        terms = fields.get("q", [])
        if not 1 <= len(terms) <= 8:
            raise RequestError(400, "invalid request")
        request["q"] = [_text(term, 256) for term in terms]
        for key, default, values in (("sort", "newest", {"newest", "oldest", "relevance"}), ("match", "any", {"any", "all"})):
            value = fields.get(key, [default])[0]
            if value not in values:
                raise RequestError(400, "invalid request")
            request[key] = value
    if route == "chat":
        request["id"] = path[len("/api/chat/"):]
        for key, default in (("include_tools", "true"), ("all_branches", "false")):
            value = fields.get(key, [default])[0]
            if value not in {"true", "false"}:
                raise RequestError(400, "invalid request")
            request[key] = value == "true"
        if "roles" in fields:
            roles = fields["roles"][0].split(",")
            if not roles or len(roles) > 4 or set(roles) - {"user", "assistant", "tool", "system"}:
                raise RequestError(400, "invalid request")
            request["roles"] = roles
    return request


def _capture(config: dict) -> list[tuple[dict, bytes]]:
    candidates = []
    visited = 0
    for entry in config["entries"]:
        root = entry["path"]
        if config["audience"] == "approved-public" or not os.path.isdir(root):
            candidates.append(entry)
        else:
            # Resolve is only a rejection check. Every final read below uses
            # openat/O_NOFOLLOW for the entire chain to cover replacement races.
            if str(Path(root).resolve()) != os.path.normpath(root):
                raise RequestError(503, "corpus unavailable")
            def walk_error(_error):
                raise RequestError(503, "corpus unavailable")
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                visited += len(dirs) + len(files)
                if visited > 10000:
                    raise RequestError(413, "corpus limit exceeded")
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)))
                for name in sorted(files):
                    if name.endswith(".jsonl"):
                        candidates.append({"agent": entry["agent"], "path": os.path.join(directory, name)})
                if len(candidates) > MAX_FILES:
                    raise RequestError(413, "corpus limit exceeded")
    if len(candidates) > MAX_FILES:
        raise RequestError(413, "corpus limit exceeded")
    captured = []
    total = 0
    for entry in candidates:
        with os.fdopen(_open_regular(entry["path"]), "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_FILE:
                raise RequestError(413, "corpus limit exceeded")
            raw = handle.read(MAX_FILE + 1)
        total += len(raw)
        if len(raw) > MAX_FILE or total > MAX_CORPUS:
            raise RequestError(413, "corpus limit exceeded")
        if config["audience"] == "approved-public" and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), entry["sha256"]):
            raise RequestError(503, "corpus unavailable")
        captured.append((entry, raw))
    return captured


def _iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value else None


def _sessions(config: dict, request: dict) -> list[dict]:
    from .parsers import claude_code, codex
    captured = _capture(config)
    sessions = []
    with tempfile.TemporaryDirectory(prefix="pj-history-", dir=config["temp_dir"]) as temporary:
        for idx, (entry, raw) in enumerate(captured):
            original = Path(entry["path"])
            # Preserve basename and parent name for legacy parser fallback IDs
            # and encoded-workspace directory names; copies never escape temp.
            parent_name = original.parent.name
            if entry["agent"] == "claude_code":
                for part_idx, part in enumerate(original.parts[:-1]):
                    if part == "projects":
                        parent_name = original.parts[part_idx + 1]
                        break
            target = Path(temporary) / str(idx) / parent_name / original.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if entry["agent"] == "claude_code":
                session = claude_code.parse_session_tree(
                    str(target), all_branches=request.get("all_branches", False),
                    include_tools=request.get("include_tools", True),
                    roles=set(request["roles"]) if "roles" in request else None)
                if session is None:
                    # A filtered tree can be empty. Falling back to the legacy
                    # flat parser here would resurrect abandoned branches.
                    unfiltered = claude_code.parse_session_tree(
                        str(target), all_branches=request.get("all_branches", False))
                    if unfiltered is not None:
                        unfiltered.messages = []
                        session = unfiltered
                    else:
                        if not request.get("include_tools", True):
                            # Legacy records lack DAG UUIDs. Filter tool blocks
                            # in the private parser copy, never the source bytes.
                            filtered = []
                            for line in raw.splitlines():
                                try:
                                    record = json.loads(line)
                                except (ValueError, UnicodeError):
                                    continue
                                message = record.get("message") if isinstance(record, dict) else None
                                if isinstance(message, dict) and isinstance(message.get("content"), list):
                                    message["content"] = [block for block in message["content"]
                                                          if not isinstance(block, dict) or block.get("type") not in {"tool_use", "tool_result"}]
                                filtered.append(json.dumps(record))
                            target.write_text("\n".join(filtered))
                        session = claude_code.parse_session(str(target))
            else:
                session = codex.parse_session(str(target))
            if session is None:
                continue
            row = {"session_id": session.session_id, "agent": session.agent,
                   "workspace": session.workspace, "title": session.title,
                   "started_at": _iso(session.started_at), "ended_at": _iso(session.ended_at),
                   "model": session.model, "messages": [asdict(m) for m in session.messages]}
            if config["audience"] == "private":
                row["source_path"] = entry["path"]
            # A malformed/unaddressable ID must not create an inaccessible row.
            if not isinstance(row["session_id"], str) or not ID.fullmatch(row["session_id"]):
                raise RequestError(503, "corpus unavailable")
            sessions.append(row)
    return sessions


def _project(path: str) -> dict:
    return {"id": hashlib.sha256(path.encode()).hexdigest()[:8],
            "name": path.rstrip("/").rsplit("/", 1)[-1], "path": path}


def _summary(session: dict) -> dict:
    return {key: value for key, value in session.items()
            if key not in {"messages", "source_path", "workspace"}}


def _matches(text: str, terms: list[str], mode: str) -> bool:
    checks = [term.lower() in text.lower() for term in terms]
    return all(checks) if mode == "all" else any(checks)


def _query(config: dict, request: dict) -> dict:
    route = request["route"]
    if route == "health":
        return ok({"status": "ok"}, source="history", audience=config["audience"])
    if config.get("index_dir"):
        from . import history_index
        try:
            return history_index.query(config, request)
        except history_index.IndexError as exc:
            raise RequestError(exc.status, exc.message) from None
    sessions = _sessions(config, request)
    if request.get("project"):
        needle = request["project"].lower()
        sessions = [s for s in sessions if needle in (s["workspace"] or "").lower() or
                    _project(s["workspace"] or "")["id"].startswith(needle)]
    sessions.sort(key=lambda s: s["ended_at"] or s["started_at"] or "", reverse=True)
    meta = {"source": "history", "audience": config["audience"],
            "offset": request["offset"], "limit": request["limit"]}
    if route == "chat":
        found = [s for s in sessions if s["session_id"] == request["id"]]
        if len(found) != 1:
            raise RequestError(404, "not found")
        result = found[0]
        messages = result["messages"]
        if "roles" in request:
            messages = [m for m in messages if m["role"] in request["roles"]]
        if not request["include_tools"]:
            messages = [m for m in messages if m["role"] != "tool"]
        if "last" in request:
            messages = messages[-request["last"]:]
        meta["total"] = meta["total_messages"] = len(messages)
        result["messages"] = messages[request["offset"]:request["offset"] + request["limit"]]
        return ok(result, **meta)
    if route == "chats":
        results = [_summary(s) for s in sessions]
        workspaces = {s["workspace"] or "" for s in sessions}
        if len(workspaces) == 1:
            meta["project"] = _project(next(iter(workspaces)))
    else:
        groups = {}
        terms, mode = request["q"], request["match"]
        for session in sessions:
            path = session["workspace"] or ""
            project = _project(path)
            title = session["title"] or ""
            content = "\n".join(m["content"] for m in session["messages"])
            if not _matches("\n".join([path, project["name"], title, content]), terms, mode):
                continue
            row = groups.setdefault(path, {**project, "match_fields": [], "matching_sessions": [],
                                          "snippets": [], "query_terms": terms, "match_mode": mode, "regex": False})
            for field, text in (("name", project["name"]), ("path", path), ("session_title", title), ("content", content)):
                if _matches(text, terms, "any") and field not in row["match_fields"]:
                    row["match_fields"].append(field)
            positions = [content.lower().find(term.lower()) for term in terms]
            positions = [p for p in positions if p >= 0]
            snippet = content[max(0, min(positions) - 120):min(positions) + 256] if positions else ""
            count = sum(content.lower().count(term.lower()) for term in terms)
            row["matching_sessions"].append({**_summary(session), "snippet": snippet,
                                              "match_count": count, "match_type": "content" if positions else "title"})
            if snippet:
                row["snippets"].append(snippet)
        results = list(groups.values())
        if request["sort"] == "oldest":
            results.sort(key=lambda row: min(s["ended_at"] or s["started_at"] or "" for s in row["matching_sessions"]))
        elif request["sort"] == "relevance":
            results.sort(key=lambda row: sum(s["match_count"] for s in row["matching_sessions"]), reverse=True)
        # Nested previews are bounded; counts and chats expose the remainder.
        for row in results:
            row["matching_session_count"] = len(row["matching_sessions"])
            row["matching_sessions"] = row["matching_sessions"][:100]
            row["snippets"] = row["snippets"][:3]
        meta.update(query=terms, sort=request["sort"], match=mode, regex=False,
                    project=request.get("project"))
    meta["total"] = len(results)
    return ok(results[request["offset"]:request["offset"] + request["limit"]], **meta)


def _encode(status: int, payload: dict) -> bytes:
    raw = json.dumps([status, payload], ensure_ascii=True, separators=(",", ":")).encode()
    if len(raw) > MAX_OUTPUT:
        return json.dumps([413, err("response limit exceeded")]).encode()
    return raw


def _worker() -> None:
    # This entry point is invoked only by the authenticated host process. The
    # bounded input contains validated config/request, never a client file path.
    try:
        config, request = json.loads(sys.stdin.buffer.read(131073))
        started = time.monotonic()
        payload = _query(config, request)
        payload["meta"]["latency_ms"] = int((time.monotonic() - started) * 1000)
        output = _encode(200, payload)
    except RequestError as exc:
        output = _encode(exc.status, err(exc.message))
    except Exception:
        output = _encode(503, err("corpus unavailable"))
    sys.stdout.buffer.write(output)


def _execute(config: dict, request: dict) -> tuple[int, dict]:
    if request["route"] == "health":
        payload = _query(config, request)
        payload["meta"]["latency_ms"] = 0
        return 200, payload
    try:
        # Parent owns cleanup even if a malformed parser hangs and is killed.
        with tempfile.TemporaryDirectory(prefix="request-", dir=config["temp_dir"]) as temporary:
            worker_config = {**config, "temp_dir": temporary}
            result = subprocess.run([sys.executable, "-B", "-m", "pj.history_service", "--worker"],
                                    input=json.dumps([worker_config, request]).encode(), capture_output=True,
                                    timeout=WORKER_TIMEOUT, check=True,
                                    cwd=str(Path(__file__).resolve().parent.parent))
        if len(result.stdout) > MAX_OUTPUT:
            raise RequestError(413, "response limit exceeded")
        status, payload = json.loads(result.stdout)
        return status, payload
    except RequestError:
        raise
    except Exception:
        raise RequestError(503, "corpus unavailable") from None


class HistoryServer(HTTPServer):
    request_queue_size = 8

    def __init__(self, config: dict, port: int = 0):
        self.config = config
        super().__init__(("127.0.0.1", port), HistoryHandler)


class UnixHistoryServer(UnixStreamServer):
    request_queue_size = 8

    def __init__(self, config: dict):
        self.config = config
        self._socket_path = config["unix_socket"]
        self._socket_identity = None
        # Parent mode 0700 excludes other users even during bind/chmod.
        super().__init__(self._socket_path, HistoryHandler)
        os.chmod(self._socket_path, 0o600)
        info = os.stat(self._socket_path)
        self._socket_identity = (info.st_dev, info.st_ino)

    def server_close(self):
        super().server_close()
        try:
            info = os.stat(self._socket_path, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == self._socket_identity:
                os.unlink(self._socket_path)
        except FileNotFoundError:
            pass


class HistoryHandler(BaseHTTPRequestHandler):
    server_version = "History"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(2)
        self._deadline = threading.Timer(REQUEST_TIMEOUT, self._close_connection)
        self._deadline.daemon = True
        self._deadline.start()

    def _close_connection(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._deadline.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, _format, *args):
        pass  # Never log raw URLs or auth headers.

    def send_error(self, code, message=None, explain=None):
        self._reply(code, err("invalid request"))

    def _reply(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        if len(body) > MAX_OUTPUT:
            status, body = 413, json.dumps(err("response limit exceeded")).encode()
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass

    def _handle(self):
        config = self.server.config
        headers = self.headers.get_all("Authorization", [])
        value = headers[0] if len(headers) == 1 else ""
        token = value[7:] if value.startswith("Bearer ") else ""
        valid = bool(token) and len(token) <= 512 and hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), config["token_sha256"])
        if not valid or time.time() >= config["expires"]:
            self._reply(401, err("unauthorized"))
            return
        try:
            if sum(len(k) + len(v) for k, v in self.headers.items()) > 8192:
                raise RequestError(413, "request limit exceeded")
            if self.headers.get("Origin") is not None:
                raise RequestError(400, "invalid request")
            if self.command != "GET":
                raise RequestError(405, "method not allowed")
            if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Content-Length", "0") != "0":
                raise RequestError(400, "invalid request")
            request = parse_request_target(self.path)
            status, payload = _execute(config, request)
        except RequestError as exc:
            status, payload = exc.status, err(exc.message)
        except Exception:
            status, payload = 503, err("service unavailable")
        self._reply(status, payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _handle

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--index", action="store_true", help="Build/update the private host-owned history index, then exit")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        _worker()
        return 0
    if not args.config or not 0 <= args.port <= 65535:
        parser.error("a protected --config and valid port are required")
    try:
        config = load_config(os.path.abspath(args.config))
        if args.index:
            from . import history_index
            try:
                result = history_index.build(config)
            except history_index.IndexError as exc:
                print(json.dumps(err(exc.message, source="history-index")), file=sys.stderr)
                return 2
            except Exception:
                print(json.dumps(err("history index build failed", source="history-index")), file=sys.stderr)
                return 2
            print(json.dumps(ok(result, source="history-index")))
            return 0
        if config["unix_socket"] and args.port:
            raise ValueError("socket and port are mutually exclusive")
        server = UnixHistoryServer(config) if config["unix_socket"] else HistoryServer(config, args.port)
    except Exception:
        print(json.dumps(err("invalid service configuration")), file=sys.stderr)
        return 2
    endpoint = {"transport": "unix"} if config["unix_socket"] else {"host": "127.0.0.1", "port": server.server_port}
    print(json.dumps(ok(endpoint, source="history")), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
