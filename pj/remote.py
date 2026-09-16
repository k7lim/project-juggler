from __future__ import annotations

"""Bounded HTTP client for the host-backed, read-only history service."""

import http.client
import ipaddress
import json
import os
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


REMOTE_URL_ENV = "PJ_REMOTE_URL"
REMOTE_SOCKET_ENV = "PJ_REMOTE_SOCKET"
REMOTE_TOKEN_ENV = "PJ_REMOTE_TOKEN"
REQUEST_TIMEOUT_SECS = 5.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_TERMS = 8
MAX_QUERY_TERM_CHARS = 256
MAX_PROJECT_CHARS = 512
MAX_LIMIT = 100
MAX_LAST = 1000
MAX_OFFSET = 100000
MAX_SOCKET_PATH_BYTES = 100
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class RemoteError(ValueError):
    """A safe-to-display remote configuration or transport error."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except OSError:
            connection.close()
            raise
        self.sock = connection


def configured() -> bool:
    """Whether remote mode was explicitly requested, including an empty value."""
    return REMOTE_URL_ENV in os.environ or REMOTE_SOCKET_ENV in os.environ


def health() -> dict:
    return _get("/api/health")


def search(
    query: list[str],
    *,
    project: str | None,
    sort: str,
    match: str,
    limit: int,
) -> dict:
    terms = _bounded_terms(query)
    params: list[tuple[str, str | int]] = [("q", term) for term in terms]
    if project is not None:
        params.append(("project", _bounded_text(project, "project", MAX_PROJECT_CHARS)))
    params.extend((("sort", sort), ("match", match), ("limit", _bounded_limit(limit))))
    return _get("/api/search", params)


def chats(project: str, *, limit: int) -> dict:
    params = [
        ("project", _bounded_text(project, "project", MAX_PROJECT_CHARS)),
        ("limit", _bounded_limit(limit)),
    ]
    return _get("/api/chats", params)


def chat(
    session_id: str,
    *,
    include_tools: bool,
    all_branches: bool,
    roles: str | None,
    limit: int | None,
    offset: int,
    last: int | None,
) -> dict:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise RemoteError("remote session ID must be an exact, bounded identifier")
    params: list[tuple[str, str | int]] = [
        ("include_tools", _bool_param(include_tools)),
        ("all_branches", _bool_param(all_branches)),
        ("offset", _bounded_nonnegative(offset, "offset", MAX_OFFSET)),
    ]
    if roles is not None:
        params.append(("roles", _bounded_roles(roles)))
    if limit is not None:
        params.append(("limit", _bounded_limit(limit)))
    if last is not None:
        params.append(("last", _bounded_positive(last, "last", MAX_LAST)))
    return _get(f"/api/chat/{quote(session_id, safe='')}", params)


def _get(path: str, params: list[tuple[str, str | int]] | None = None) -> dict:
    transport, target = _remote_transport()
    token = _remote_token()
    query = urlencode(params or [], doseq=True)
    request_target = path + (f"?{query}" if query else "")
    if transport == "unix":
        return _get_unix(target, request_target, token)
    return _get_http(target, request_target, token)


def _get_http(origin: str, request_target: str, token: str) -> dict:
    url = f"{origin}{request_target}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECS) as response:
            return _decode_response(response.getcode(), response.headers, response)
    except RemoteError:
        raise
    except HTTPError as exc:
        raise RemoteError(f"remote history request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise RemoteError("remote history service is unavailable") from None


def _get_unix(socket_path: str, request_target: str, token: str) -> dict:
    connection = _UnixHTTPConnection(socket_path, REQUEST_TIMEOUT_SECS)
    try:
        connection.request(
            "GET",
            request_target,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Host": "localhost",
            },
        )
        response = connection.getresponse()
        return _decode_response(response.status, response.headers, response)
    except RemoteError:
        raise
    except (http.client.HTTPException, TimeoutError, socket.timeout, OSError):
        raise RemoteError("remote history service is unavailable") from None
    finally:
        connection.close()


def _decode_response(status: int, headers: Any, response: Any) -> dict:
    if status < 200 or status >= 300:
        raise RemoteError(f"remote history request failed (HTTP {status})")
    if headers.get_content_type() != "application/json":
        raise RemoteError("remote history response was not JSON")
    length = headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > MAX_RESPONSE_BYTES:
                raise RemoteError("remote history response exceeded the size limit")
        except ValueError:
            raise RemoteError("remote history response had an invalid length") from None
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RemoteError("remote history response exceeded the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RemoteError("remote history response was invalid") from None
    return _validate_envelope(payload)


def _remote_transport() -> tuple[str, str]:
    has_url = REMOTE_URL_ENV in os.environ
    has_socket = REMOTE_SOCKET_ENV in os.environ
    if has_url and has_socket:
        raise RemoteError("configure exactly one remote history transport")
    if has_socket:
        return "unix", _remote_socket_path()
    return "http", _remote_origin()


def _remote_origin() -> str:
    value = os.environ.get(REMOTE_URL_ENV, "")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RemoteError("PJ_REMOTE_URL must be an HTTP loopback origin") from None
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RemoteError("PJ_REMOTE_URL must be an HTTP loopback origin")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise RemoteError("PJ_REMOTE_URL must use a literal loopback address") from None
    if not address.is_loopback:
        raise RemoteError("PJ_REMOTE_URL must use a literal loopback address")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit(("http", netloc, "", "", ""))


def _remote_socket_path() -> str:
    value = os.environ.get(REMOTE_SOCKET_ENV, "")
    if (
        not value
        or len(os.fsencode(value)) > MAX_SOCKET_PATH_BYTES
        or _has_control(value)
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
    ):
        raise RemoteError("PJ_REMOTE_SOCKET must be an exact absolute socket path")
    return value


def _remote_token() -> str:
    token = os.environ.get(REMOTE_TOKEN_ENV, "")
    if (
        not token
        or len(token) > 512
        or not token.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise RemoteError("PJ_REMOTE_TOKEN is required for remote history access")
    return token


def _validate_envelope(payload: Any) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"success", "data", "meta"}:
        raise RemoteError("remote history response was invalid")
    if not isinstance(payload["success"], bool) or not isinstance(payload["meta"], dict):
        raise RemoteError("remote history response was invalid")
    if not isinstance(payload["data"], (list, dict)):
        raise RemoteError("remote history response was invalid")
    if not payload["success"]:
        error = payload["meta"].get("error")
        if not isinstance(error, str) or not error or len(error) > 1024 or _has_control(error):
            raise RemoteError("remote history request was rejected")
    return payload


def _bounded_terms(query: list[str]) -> list[str]:
    if not query or len(query) > MAX_QUERY_TERMS:
        raise RemoteError("remote search requires a bounded query")
    return [_bounded_text(term, "query term", MAX_QUERY_TERM_CHARS) for term in query]


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or _has_control(value):
        raise RemoteError(f"remote {label} must be bounded text")
    return value


def _bounded_roles(value: str) -> str:
    roles = value.split(",")
    if not roles or len(roles) > 16 or any(not re.fullmatch(r"[a-z_]{1,32}", role) for role in roles):
        raise RemoteError("remote roles must be a bounded comma-separated list")
    return ",".join(roles)


def _bounded_limit(value: int) -> int:
    return _bounded_positive(value, "limit", MAX_LIMIT)


def _bounded_positive(value: int, label: str, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise RemoteError(f"remote {label} must be between 1 and {maximum}")
    return value


def _bounded_nonnegative(value: int, label: str, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise RemoteError(f"remote {label} must be between 0 and {maximum}")
    return value


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)
