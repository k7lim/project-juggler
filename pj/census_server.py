from __future__ import annotations

"""Local stdlib HTTP server for the live census dashboard."""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import cache, census, envelope


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pj Census</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface-2: #1c2128; --border: #30363d;
    --text: #c9d1d9; --text-dim: #8b949e; --text-bright: #f0f6fc;
    --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --purple: #bc8cff; --orange: #db6d28; --cyan: #39d2c0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px 24px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 13px; line-height: 1.5;
  }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
  h1 { color: var(--text-bright); font-size: 20px; margin: 0 0 2px; font-weight: 650; }
  .subtitle { color: var(--text-dim); font-size: 12px; margin: 0; }
  .stats { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
  .stat {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; min-width: 74px;
  }
  .stat-val { font-size: 18px; font-weight: 650; color: var(--text-bright); }
  .stat-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .controls { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; align-items: center; }
  input[type="text"], select, button {
    background: var(--surface); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 5px 9px; font-size: 12px; outline: none;
  }
  input[type="text"]:focus, select:focus, button:focus { border-color: var(--accent); }
  input[type="text"] { width: min(260px, 100%); }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); color: var(--text-bright); }
  label { color: var(--text-dim); font-size: 11px; cursor: pointer; display: flex; align-items: center; gap: 3px; }
  input[type="checkbox"] { accent-color: var(--accent); }
  #row-count { color: var(--text-dim); font-size: 11px; margin-left: auto; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; background: var(--surface); }
  th, td { padding: 5px 8px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th {
    background: var(--surface-2); color: var(--text-dim); font-weight: 650; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; user-select: none;
    position: sticky; top: 0; z-index: 2;
  }
  th:hover, th.sorted { color: var(--accent); }
  th .arr { font-size: 9px; margin-left: 2px; opacity: 0.35; }
  th.sorted .arr { opacity: 1; }
  td { font-size: 12px; }
  td.name { color: var(--text-bright); font-weight: 550; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
  td.path { color: var(--text-dim); font-size: 10px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .state-active { color: var(--green); }
  .state-stale { color: var(--yellow); }
  .state-dormant { color: var(--red); }
  .state-blocked, .state-archived { color: var(--text-dim); }
  .cat-teaching { color: var(--purple); }
  .cat-research { color: var(--accent); }
  .cat-projects { color: var(--green); }
  .cat-sandbox { color: var(--text-dim); }
  .cat-external { color: var(--orange); }
  .cat-host { color: var(--cyan); }
  .cat-other { color: var(--text-dim); }
  .origin-yolobox { color: var(--cyan); }
  .origin-mac { color: var(--text-dim); }
  tr:hover { background: rgba(88,166,255,0.06); }
  .badge { display: inline-block; min-width: 18px; text-align: center; background: var(--border); border-radius: 10px; padding: 0 5px; font-size: 10px; }
  .badge-hot { background: var(--accent); color: var(--bg); }
  .dur-bar { display: inline-block; height: 8px; border-radius: 2px; background: var(--accent); opacity: 0.5; vertical-align: middle; margin-right: 3px; }
  .mtag { display: inline-block; padding: 0 4px; border-radius: 3px; font-size: 9px; margin-right: 1px; border: 1px solid var(--border); color: var(--text-dim); }
  .m-opus { color: var(--purple); border-color: color-mix(in srgb, var(--purple) 40%, transparent); }
  .m-sonnet { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .m-haiku { color: var(--text-dim); }
  .beads-dot { color: var(--green); font-weight: bold; }
  .git-dot { color: var(--text-dim); }
  .note-cell { max-width: 180px; overflow: hidden; text-overflow: ellipsis; color: var(--text-dim); font-size: 11px; }
  .error { color: var(--red); margin-bottom: 10px; display: none; }
  @media (max-width: 720px) {
    body { padding: 14px; }
    header { display: block; }
    #row-count { width: 100%; margin-left: 0; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>pj Project Census</h1>
    <p class="subtitle" id="subtitle">Loading...</p>
  </div>
  <button id="refresh">Refresh</button>
</header>

<div class="stats" id="stats"></div>
<div class="error" id="error"></div>

<div class="controls">
  <input type="text" id="search" placeholder="Filter name / path / note..." autofocus>
  <select id="stateFilter"><option value="">All states</option></select>
  <select id="catFilter"><option value="">All categories</option></select>
  <select id="originFilter"><option value="">All origins</option></select>
  <label><input type="checkbox" id="beadsOnly"> Beads</label>
  <span id="row-count"></span>
</div>

<div class="table-wrap">
<table>
<thead>
<tr>
  <th data-key="name">Name <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="category">Cat <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="origin">Origin <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="state">State <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="sessions" data-type="num">Sess <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="duration_hrs" data-type="num">Hours <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="models">Models <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="first_session_ts" data-type="num">Started <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="last_active_ts" data-type="num">Last Active <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="beads" data-type="num">Bd <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="has_git" data-type="num">Git <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="priority">Pri <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="note">Note <span class="arr">&#x25B2;&#x25BC;</span></th>
  <th data-key="path">Path <span class="arr">&#x25B2;&#x25BC;</span></th>
</tr>
</thead>
<tbody id="tbody"></tbody>
</table>
</div>

<script>
let DATA = [];
let META = {};
let sortKey = "last_active_ts";
let sortDir = -1;
let maxDur = 1;

function esc(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
}

function mtags(models) {
  if (!models) return "";
  return String(models).split(", ").filter(Boolean).map(x => {
    const lower = x.toLowerCase();
    const cls = lower.includes("opus") ? "m-opus" : lower.includes("sonnet") ? "m-sonnet" : lower.includes("haiku") ? "m-haiku" : "";
    return `<span class="mtag ${cls}">${esc(x)}</span>`;
  }).join("");
}

function durCell(hrs) {
  if (!hrs) return "";
  const w = Math.max(2, Math.min(80, (hrs / maxDur) * 80));
  return `<span class="dur-bar" style="width:${w}px"></span>${hrs}`;
}

function sessCell(n) {
  return n > 20 ? `<span class="badge badge-hot">${n}</span>` : n;
}

function updateSelect(id, values, labels) {
  const el = document.getElementById(id);
  const selected = el.value;
  const first = el.options[0].textContent;
  el.innerHTML = `<option value="">${first}</option>` + values.map(v => {
    const count = labels?.[v];
    return `<option value="${esc(v)}">${esc(v)}${count === undefined ? "" : ` (${count})`}</option>`;
  }).join("");
  el.value = values.includes(selected) ? selected : "";
}

function filteredRows() {
  const q = document.getElementById("search").value.toLowerCase();
  const sf = document.getElementById("stateFilter").value;
  const cf = document.getElementById("catFilter").value;
  const of_ = document.getElementById("originFilter").value;
  const bf = document.getElementById("beadsOnly").checked;

  let rows = DATA.filter(r => {
    if (q && !r.name.toLowerCase().includes(q) && !r.path.toLowerCase().includes(q) && !r.note.toLowerCase().includes(q)) return false;
    if (sf && r.state !== sf) return false;
    if (cf && r.category !== cf) return false;
    if (of_ && r.origin !== of_) return false;
    if (bf && !r.has_beads) return false;
    return true;
  });

  const isNum = document.querySelector(`th[data-key="${sortKey}"]`)?.dataset.type === "num";
  rows.sort((a, b) => {
    let va = a[sortKey], vb = b[sortKey];
    if (typeof va === "boolean") { va = va ? 1 : 0; vb = vb ? 1 : 0; }
    if (isNum) return ((va || 0) - (vb || 0)) * sortDir;
    return String(va || "").localeCompare(String(vb || "")) * sortDir;
  });
  return rows;
}

function render() {
  maxDur = Math.max(...DATA.map(r => r.duration_hrs || 0), 1);
  const rows = filteredRows();
  document.getElementById("tbody").innerHTML = rows.map(r => `
    <tr>
      <td class="name" title="${esc(r.name)}">${esc(r.name)}</td>
      <td class="cat-${esc(r.category)}">${esc(r.category)}</td>
      <td class="origin-${esc(r.origin)}">${esc(r.origin)}</td>
      <td class="state-${esc(r.state)}">${esc(r.state)}</td>
      <td class="num">${sessCell(r.sessions)}</td>
      <td class="num">${durCell(r.duration_hrs)}</td>
      <td>${mtags(r.models)}</td>
      <td>${esc(r.first_session)}</td>
      <td>${esc(r.last_active)}</td>
      <td class="beads-dot">${r.has_beads ? (r.beads > 0 ? r.beads : "&#10003;") : ""}</td>
      <td class="git-dot">${r.has_git ? "&#10003;" : ""}</td>
      <td>${r.priority !== "none" ? esc(r.priority) : ""}</td>
      <td class="note-cell" title="${esc(r.note)}">${esc(r.note)}</td>
      <td class="path" title="${esc(r.path)}">${esc(r.path)}</td>
    </tr>
  `).join("");

  document.getElementById("row-count").textContent = `${rows.length} of ${DATA.length}`;
  const sessions = rows.reduce((s, r) => s + (r.sessions || 0), 0);
  const hours = rows.reduce((s, r) => s + (r.duration_hrs || 0), 0);
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="stat-val">${rows.length}</div><div class="stat-label">Projects</div></div>
    <div class="stat"><div class="stat-val">${sessions.toLocaleString()}</div><div class="stat-label">Sessions</div></div>
    <div class="stat"><div class="stat-val">${Math.round(hours).toLocaleString()}h</div><div class="stat-label">Agent Hours</div></div>
    <div class="stat"><div class="stat-val">${rows.filter(r => r.state === "active").length}</div><div class="stat-label">Active</div></div>
    <div class="stat"><div class="stat-val">${rows.filter(r => r.state === "stale").length}</div><div class="stat-label">Stale</div></div>
    <div class="stat"><div class="stat-val">${rows.filter(r => r.state === "dormant").length}</div><div class="stat-label">Dormant</div></div>
    <div class="stat"><div class="stat-val">${rows.filter(r => r.has_beads).length}</div><div class="stat-label">Beads</div></div>
    <div class="stat"><div class="stat-val">${rows.filter(r => r.has_git).length}</div><div class="stat-label">Git</div></div>
  `;

  document.querySelectorAll("th").forEach(t => t.classList.remove("sorted"));
  document.querySelector(`th[data-key="${sortKey}"]`)?.classList.add("sorted");
}

async function loadData(force = false) {
  const error = document.getElementById("error");
  try {
    const response = await fetch(`/api/census${force ? "?refresh=1" : ""}`, { cache: "no-store" });
    const payload = await response.json();
    if (!payload.success) throw new Error(payload.meta?.error || "request failed");
    DATA = payload.data;
    META = payload.meta || {};
    const generated = META.generated_at ? new Date(META.generated_at).toLocaleString() : "";
    document.getElementById("subtitle").textContent = `${generated} | ${META.total ?? DATA.length} projects | ${(META.session_total ?? 0).toLocaleString()} sessions | ${Math.round(META.duration_hrs_total ?? 0).toLocaleString()} agent-hours`;
    updateSelect("stateFilter", Object.keys(META.state_counts || {}).sort(), META.state_counts || {});
    updateSelect("catFilter", Object.keys(META.category_counts || {}).sort(), META.category_counts || {});
    updateSelect("originFilter", Object.keys(META.origin_counts || {}).sort(), META.origin_counts || {});
    error.style.display = "none";
    render();
  } catch (err) {
    error.textContent = String(err);
    error.style.display = "block";
  }
}

document.querySelectorAll("th").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (sortKey === key) sortDir *= -1;
    else { sortKey = key; sortDir = ["last_active_ts", "first_session_ts", "sessions", "duration_hrs", "beads", "has_git"].includes(key) ? -1 : 1; }
    render();
  });
});

["search", "stateFilter", "catFilter", "originFilter", "beadsOnly"].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener(el.tagName === "INPUT" && el.type === "text" ? "input" : "change", render);
});
document.getElementById("refresh").addEventListener("click", () => loadData(true));
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadData(false); });
setInterval(() => { if (!document.hidden) loadData(false); }, 5 * 60 * 1000);
loadData(false);
</script>
</body>
</html>
"""


class CensusCache:
    def __init__(
        self,
        *,
        limit: int = 10000,
        check_interval: int = 60,
        snapshot_fn: Callable[[int], dict] = census.snapshot,
        signatures_fn: Callable[[], dict] = cache.signatures,
    ) -> None:
        self.limit = limit
        self.check_interval = check_interval
        self.snapshot_fn = snapshot_fn
        self.signatures_fn = signatures_fn
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._signature: dict | None = None
        self._next_check = 0.0
        self._last_scan_secs = 0.0

    def get(self, *, force: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            signature_changed = False
            checked = False

            if force or self._snapshot is None or now >= self._next_check:
                checked = True
                signature = self.signatures_fn()
                signature_changed = signature != self._signature
                if force or self._snapshot is None or signature_changed:
                    started = time.monotonic()
                    self._snapshot = self.snapshot_fn(self.limit)
                    self._last_scan_secs = time.monotonic() - started
                    self._signature = signature
                self._next_check = now + self.check_interval

            result = dict(self._snapshot or {"rows": [], "meta": census.summarize([])})
            meta = dict(result.get("meta", {}))
            meta.update(
                signature_checked=checked,
                signature_changed=signature_changed,
                check_interval=self.check_interval,
                last_scan_secs=round(self._last_scan_secs, 3),
            )
            result["meta"] = meta
            return result


def make_handler(census_cache: CensusCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pj-census/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send_text(HTML, "text/html; charset=utf-8")
                return

            if parsed.path == "/api/health":
                body = envelope.to_json(envelope.ok({"status": "running"}))
                self._send_text(body, "application/json; charset=utf-8")
                return

            if parsed.path == "/api/census":
                params = parse_qs(parsed.query)
                force = params.get("refresh", ["0"])[0] in ("1", "true", "yes")
                snapshot = census_cache.get(force=force)
                body = envelope.to_json(envelope.ok(snapshot["rows"], **snapshot["meta"]))
                self._send_text(body, "application/json; charset=utf-8")
                return

            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path == "/api/control/stop":
                token = os.environ.get("PJ_CENSUS_CONTROL_TOKEN")
                supplied = self.headers.get("X-PJ-Control-Token")
                if not token or supplied != token:
                    self.send_error(403)
                    return
                body = envelope.to_json(envelope.ok({"status": "stopping"}))
                self._send_text(body, "application/json; charset=utf-8")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            self.send_error(404)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def _send_text(self, body: str, content_type: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, *, limit: int = 10000, check_interval: int = 60) -> None:
    census_cache = CensusCache(limit=limit, check_interval=check_interval)
    server = ThreadingHTTPServer((host, port), make_handler(census_cache))
    bound_host, bound_port = server.server_address[:2]
    print(f"pj census serving at http://{bound_host}:{bound_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping pj census server.")
    finally:
        server.server_close()
