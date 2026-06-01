from __future__ import annotations

"""Local stdlib HTTP server for the live census dashboard."""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import annotate, cache, census, discover, envelope, resume, schedule
from . import search as search_mod
from .project_sessions import project_session_data, resolve_project_detail
from .session_store import get_store


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
  .tabs { display: flex; gap: 6px; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
  .tab-button {
    border-bottom-left-radius: 0; border-bottom-right-radius: 0; border-bottom-color: transparent;
  }
  .tab-button.active {
    color: var(--text-bright); border-color: var(--border); border-bottom-color: var(--surface);
    background: var(--surface);
  }
  .view[hidden] { display: none; }
  .project-link {
    color: var(--accent); text-decoration: none; font-weight: 650;
  }
  .project-link:hover { color: var(--text-bright); text-decoration: underline; }
  .reason-cell { max-width: 420px; white-space: normal; color: var(--text); }
  .badge { display: inline-block; min-width: 18px; text-align: center; background: var(--border); border-radius: 10px; padding: 0 5px; font-size: 10px; }
  .badge-hot { background: var(--accent); color: var(--bg); }
  .dur-bar { display: inline-block; height: 8px; border-radius: 2px; background: var(--accent); opacity: 0.5; vertical-align: middle; margin-right: 3px; }
  .mtag { display: inline-block; padding: 0 4px; border-radius: 3px; font-size: 9px; margin-right: 1px; border: 1px solid var(--border); color: var(--text-dim); }
  .m-opus { color: var(--purple); border-color: color-mix(in srgb, var(--purple) 40%, transparent); }
  .m-sonnet { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .m-haiku { color: var(--text-dim); }
  .beads-dot { color: var(--green); font-weight: bold; }
  .git-dot { color: var(--text-dim); }
  .live-links { display: flex; gap: 4px; }
  .live-link {
    color: var(--cyan); border: 1px solid var(--border); border-radius: 3px;
    padding: 0 4px; text-decoration: none; font-size: 10px;
  }
  .live-link:hover { border-color: var(--cyan); color: var(--text-bright); }
  .note-cell { max-width: 180px; overflow: hidden; text-overflow: ellipsis; color: var(--text-dim); font-size: 11px; }
  .error { color: var(--red); margin-bottom: 10px; display: none; }
  .search-panel {
    display: none; border: 1px solid var(--border); border-radius: 6px;
    background: var(--surface); margin-bottom: 12px;
  }
  .search-head {
    display: flex; justify-content: space-between; gap: 12px; align-items: center;
    padding: 6px 8px; border-bottom: 1px solid var(--border);
  }
  .search-title { color: var(--text-bright); font-weight: 650; }
  .search-meta { color: var(--text-dim); font-size: 11px; }
  .search-results { display: grid; }
  .search-result { padding: 8px; border-bottom: 1px solid var(--border); }
  .search-result:last-child { border-bottom: 0; }
  .search-result:hover { background: rgba(88,166,255,0.05); }
  .result-top { display: flex; gap: 8px; justify-content: space-between; align-items: baseline; }
  .result-name { color: var(--text-bright); font-weight: 650; }
  .result-fields { color: var(--text-dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; }
  .result-path { color: var(--text-dim); font-size: 11px; overflow-wrap: anywhere; }
  .result-note, .snippet { color: var(--text); margin-top: 4px; }
  .session-hit { margin-top: 6px; padding-left: 8px; border-left: 2px solid var(--border); }
  .session-title { color: var(--text-bright); }
  .session-meta { color: var(--text-dim); font-size: 11px; }
  .resume-row { display: flex; gap: 6px; align-items: center; margin-top: 4px; }
  .resume-cmd {
    color: var(--cyan); background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; padding: 3px 5px; font-size: 11px; overflow-wrap: anywhere;
  }
  .copy-resume { padding: 3px 6px; font-size: 11px; flex: 0 0 auto; }
  .drawer-backdrop {
    position: fixed; inset: 0; background: rgba(1,4,9,0.58); z-index: 10;
    display: none;
  }
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(520px, 100%);
    background: var(--surface); border-left: 1px solid var(--border); z-index: 11;
    transform: translateX(100%); transition: transform 150ms ease; overflow-y: auto;
    box-shadow: -12px 0 28px rgba(1,4,9,0.35);
  }
  .drawer.open { transform: translateX(0); }
  .drawer-backdrop.open { display: block; }
  .drawer-head {
    position: sticky; top: 0; display: flex; justify-content: space-between; gap: 12px;
    align-items: flex-start; padding: 14px 16px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .drawer-title { min-width: 0; }
  .drawer-title h2 {
    color: var(--text-bright); font-size: 17px; line-height: 1.25; margin: 0 0 3px;
    overflow-wrap: anywhere;
  }
  .drawer-path { color: var(--text-dim); font-size: 11px; overflow-wrap: anywhere; }
  .drawer-close { flex: 0 0 auto; padding: 4px 8px; }
  .drawer-body { padding: 14px 16px 22px; }
  .drawer-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 12px; }
  .detail-item { border: 1px solid var(--border); border-radius: 6px; padding: 7px 8px; background: var(--bg); min-width: 0; }
  .detail-label { color: var(--text-dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .detail-value { color: var(--text-bright); overflow-wrap: anywhere; }
  .drawer-section { margin-top: 14px; }
  .drawer-section h3 { color: var(--text-bright); font-size: 12px; margin: 0 0 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  .annotation-actions { display: grid; gap: 7px; }
  .action-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .action-row input[type="text"] { flex: 1 1 150px; width: auto; }
  .action-row select { flex: 0 0 auto; }
  .action-row button { flex: 0 0 auto; }
  .action-status { color: var(--text-dim); font-size: 11px; min-height: 16px; }
  .archive-action { border-top: 1px solid var(--border); padding-top: 7px; }
  .danger { border-color: color-mix(in srgb, var(--red) 55%, var(--border)); color: var(--red); }
  .danger:disabled { cursor: not-allowed; opacity: 0.45; }
  .session-list { display: grid; gap: 8px; }
  .session-card { border: 1px solid var(--border); border-radius: 6px; padding: 8px; background: var(--bg); }
  .session-card .session-title { font-weight: 650; overflow-wrap: anywhere; }
  .session-extra { color: var(--text-dim); font-size: 11px; margin-top: 3px; overflow-wrap: anywhere; }
  .transcript-controls { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 7px; }
  .transcript-controls select { max-width: 126px; }
  .transcript-controls input[type="text"] { width: 58px; }
  .transcript-view { display: none; margin-top: 8px; border-top: 1px solid var(--border); padding-top: 8px; }
  .transcript-view.open { display: grid; gap: 7px; }
  .transcript-message { border-left: 2px solid var(--border); padding-left: 7px; min-width: 0; }
  .transcript-role { color: var(--text-dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
  .transcript-content { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }
  .transcript-error { color: var(--red); }
  tr { cursor: pointer; }
  tr.selected { background: rgba(88,166,255,0.1); }
  @media (max-width: 720px) {
    body { padding: 14px; }
    header { display: block; }
    #row-count { width: 100%; margin-left: 0; }
    .drawer { width: 100%; }
    .drawer-grid { grid-template-columns: 1fr; }
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

<nav class="tabs" aria-label="Dashboard views">
  <button class="tab-button active" data-view="census">Census</button>
  <button class="tab-button" data-view="next">Next Queue</button>
</nav>

<section class="view" id="censusView">
<div class="controls">
  <input type="text" id="search" placeholder="Search projects and sessions..." autofocus>
  <input type="text" id="tableFilter" placeholder="Filter census table...">
  <select id="stateFilter"><option value="">All states</option></select>
  <select id="catFilter"><option value="">All categories</option></select>
  <select id="originFilter"><option value="">All origins</option></select>
  <label><input type="checkbox" id="beadsOnly"> Beads</label>
  <span id="row-count"></span>
</div>

<section class="search-panel" id="searchPanel" aria-live="polite">
  <div class="search-head">
    <span class="search-title" id="searchTitle">Search results</span>
    <span class="search-meta" id="searchMeta"></span>
  </div>
  <div class="search-results" id="searchResults"></div>
</section>

<div class="table-wrap">
<table id="censusTable">
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
  <th data-key="live_port_count" data-type="num">Live <span class="arr">&#x25B2;&#x25BC;</span></th>
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
</section>

<div class="drawer-backdrop" id="drawerBackdrop"></div>
<aside class="drawer" id="projectDrawer" aria-hidden="true" aria-labelledby="drawerProjectName">
  <div class="drawer-head">
    <div class="drawer-title">
      <h2 id="drawerProjectName">Project</h2>
      <div class="drawer-path" id="drawerProjectPath"></div>
    </div>
    <button class="drawer-close" id="drawerClose" aria-label="Close project details">Close</button>
  </div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>

<section class="view" id="nextView" hidden>
<div class="table-wrap">
<table id="nextTable">
<thead>
<tr>
  <th>Rank</th>
  <th>Score</th>
  <th>Project</th>
  <th>State</th>
  <th>Pri</th>
  <th>Reason</th>
  <th>Path</th>
</tr>
</thead>
<tbody id="nextTbody"></tbody>
</table>
</div>
</section>

<script>
let DATA = [];
let META = {};
let NEXT_DATA = [];
let NEXT_META = {};
let sortKey = "last_active_ts";
let sortDir = -1;
let maxDur = 1;
let searchTimer = null;
let searchRequestId = 0;
let detailRequestId = 0;
let activeProjectId = null;
let activeView = "census";
let nextLoaded = false;

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

function compactLiveUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.host;
  } catch {
    return String(url ?? "");
  }
}

function liveUrlsCell(urls) {
  if (!Array.isArray(urls) || urls.length === 0) return "";
  return `<div class="live-links">${urls.map(url => `<a class="live-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(compactLiveUrl(url))}</a>`).join("")}</div>`;
}

function projectRef(row) {
  return row?.id || row?.path || row?.name || "";
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
  const q = document.getElementById("tableFilter").value.toLowerCase();
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

function compactDate(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

function renderResume(cmd) {
  if (!cmd) return "";
  return `<div class="resume-row"><code class="resume-cmd">${esc(cmd)}</code><button class="copy-resume" data-cmd="${esc(cmd)}">Copy</button></div>`;
}

function renderSessionHit(session) {
  const title = session.title || "(untitled)";
  const when = compactDate(session.ended_at || session.started_at);
  const sid = session.session_id ? ` · ${String(session.session_id).slice(0, 12)}` : "";
  const agent = session.agent ? `${session.agent}` : "";
  const meta = [agent, when].filter(Boolean).join(" · ") + sid;
  return `<div class="session-hit">
    <div class="session-title">${esc(title)}</div>
    ${meta ? `<div class="session-meta">${esc(meta)}</div>` : ""}
    ${session.snippet ? `<div class="snippet">${esc(session.snippet)}</div>` : ""}
    ${renderResume(session.resume_cmd)}
  </div>`;
}

function durationText(seconds) {
  if (!seconds) return "";
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
}

function listText(value) {
  if (!value) return "";
  return Array.isArray(value) ? value.filter(Boolean).join(", ") : String(value);
}

function detailItem(label, value) {
  if (value === undefined || value === null || value === "") return "";
  return `<div class="detail-item"><div class="detail-label">${esc(label)}</div><div class="detail-value">${esc(value)}</div></div>`;
}

function sessionActivityTime(session) {
  return session.ended_at || session.updated_at || session.started_at || "";
}

function renderDetailSession(session) {
  const title = session.title || "(untitled)";
  const sessionId = session.session_id || "";
  const sid = session.session_id ? String(session.session_id).slice(0, 12) : "";
  const bits = [
    session.agent,
    compactDate(sessionActivityTime(session)),
    sid,
  ].filter(Boolean);
  const extras = [
    session.model,
    listText(session.models),
    listText((session.versions || []).map(v => `v${v}`)),
    durationText(session.duration_secs),
  ].filter(Boolean);
  const controls = sessionId ? `<div class="transcript-controls">
      <button class="transcript-open" data-session-id="${esc(sessionId)}">Open</button>
      <select class="transcript-roles" aria-label="Transcript roles">
        <option value="user,assistant">User + assistant</option>
        <option value="">All roles</option>
        <option value="user">User</option>
        <option value="assistant">Assistant</option>
      </select>
      <label>Last <input type="text" class="transcript-last" value="50" inputmode="numeric" aria-label="Last messages"></label>
      <label><input type="checkbox" class="transcript-hide-tools" checked> Hide tools</label>
    </div>
    <div class="transcript-view" data-session-id="${esc(sessionId)}"></div>` : "";
  return `<article class="session-card">
    <div class="session-title">${esc(title)}</div>
    ${bits.length ? `<div class="session-meta">${esc(bits.join(" · "))}</div>` : ""}
    ${extras.length ? `<div class="session-extra">${esc(extras.join(" | "))}</div>` : ""}
    ${controls}
  </article>`;
}

function messageText(content) {
  if (content === undefined || content === null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map(item => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") return item.text || item.content || JSON.stringify(item);
      return String(item ?? "");
    }).filter(Boolean).join("\\n");
  }
  if (typeof content === "object") return content.text || content.content || JSON.stringify(content, null, 2);
  return String(content);
}

function renderTranscript(messages) {
  if (!messages.length) return `<div class="transcript-message"><div class="transcript-content">No messages</div></div>`;
  return messages.map(message => {
    const role = message.role || "message";
    return `<div class="transcript-message">
      <div class="transcript-role">${esc(role)}</div>
      <div class="transcript-content">${esc(messageText(message.content))}</div>
    </div>`;
  }).join("");
}

async function openTranscript(button) {
  const card = button.closest(".session-card");
  const sessionId = button.dataset.sessionId;
  const view = card?.querySelector(".transcript-view");
  if (!sessionId || !card || !view) return;

  const roles = card.querySelector(".transcript-roles")?.value || "";
  const last = card.querySelector(".transcript-last")?.value.trim() || "";
  const hideTools = card.querySelector(".transcript-hide-tools")?.checked;
  const params = new URLSearchParams();
  if (roles) params.set("roles", roles);
  if (/^\\d+$/.test(last) && Number(last) > 0) params.set("last", last);
  if (hideTools) params.set("no_tools", "1");

  button.disabled = true;
  button.textContent = "Loading";
  view.classList.add("open");
  view.innerHTML = `<div class="transcript-message"><div class="transcript-content">Loading...</div></div>`;
  try {
    const query = params.toString();
    const response = await fetch(`/api/chat/${encodeURIComponent(sessionId)}${query ? `?${query}` : ""}`, { cache: "no-store" });
    const payload = await response.json();
    if (!document.body.contains(view)) return;
    if (!payload.success) throw new Error(payload.meta?.error || "transcript failed");
    view.innerHTML = renderTranscript((payload.data || {}).messages || []);
    button.textContent = "Reload";
  } catch (err) {
    if (!document.body.contains(view)) return;
    view.innerHTML = `<div class="transcript-message transcript-error"><div class="transcript-content">${esc(err)}</div></div>`;
    button.textContent = "Retry";
  } finally {
    button.disabled = false;
  }
}

function renderProjectDetail(project) {
  document.getElementById("drawerProjectName").textContent = project.name || "(unnamed)";
  document.getElementById("drawerProjectPath").textContent = project.path || "";
  const sessions = project.sessions || [];
  const projectId = project.id || project.path || "";
  const detailRows = [
    detailItem("ID", project.id),
    detailItem("State", project.state),
    detailItem("Priority", project.priority || "none"),
    detailItem("Sessions", project.session_count ?? sessions.length),
    detailItem("Agents", listText(project.agents)),
    detailItem("Models", listText(project.models)),
    detailItem("Tags", listText(project.tags)),
    detailItem("Last active", compactDate(project.last_active)),
  ].join("");
  const note = project.latest_note ? `<div class="drawer-section"><h3>Note</h3><div class="detail-item"><div class="detail-value">${esc(project.latest_note)}</div></div></div>` : "";
  const actions = `<div class="drawer-section">
      <h3>Actions</h3>
      <div class="annotation-actions" data-project-id="${esc(projectId)}">
        <div class="action-row">
          <input type="text" class="annotation-note" placeholder="Add note">
          <button class="annotation-submit" data-action="note">Note</button>
        </div>
        <div class="action-row">
          <select class="annotation-priority" aria-label="Priority">
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
            <option value="none">None</option>
          </select>
          <button class="annotation-submit" data-action="prioritize">Prioritize</button>
        </div>
        <div class="action-row">
          <input type="text" class="annotation-tag" placeholder="Tag">
          <button class="annotation-submit" data-action="tag">Tag</button>
        </div>
        <div class="action-row archive-action">
          <label><input type="checkbox" class="annotation-archive-confirm"> Confirm archive</label>
          <button class="annotation-submit danger" data-action="archive" disabled>Archive</button>
        </div>
        <div class="action-status" aria-live="polite"></div>
      </div>
    </div>`;
  const resume = project.resume_cmd ? `<div class="drawer-section"><h3>Resume</h3>${renderResume(project.resume_cmd)}</div>` : "";
  const sessionList = sessions.length
    ? sessions.map(renderDetailSession).join("")
    : `<div class="detail-item"><div class="detail-value">No recent sessions</div></div>`;
  document.getElementById("drawerBody").innerHTML = `
    <div class="drawer-grid">${detailRows}</div>
    ${note}
    ${actions}
    ${resume}
    <div class="drawer-section">
      <h3>Recent sessions</h3>
      <div class="session-list">${sessionList}</div>
    </div>`;
}

function setDrawerOpen(open) {
  const drawer = document.getElementById("projectDrawer");
  const backdrop = document.getElementById("drawerBackdrop");
  drawer.classList.toggle("open", open);
  backdrop.classList.toggle("open", open);
  drawer.setAttribute("aria-hidden", open ? "false" : "true");
  if (!open) {
    activeProjectId = null;
    document.querySelectorAll("tr.selected").forEach(row => row.classList.remove("selected"));
  }
}

function closeDrawer() {
  detailRequestId++;
  setDrawerOpen(false);
}

async function openProjectDetail(projectId) {
  if (!projectId) return;
  activeProjectId = projectId;
  const requestId = ++detailRequestId;
  document.querySelectorAll("tr.selected").forEach(row => row.classList.toggle("selected", row.dataset.projectId === projectId));
  document.getElementById("drawerProjectName").textContent = "Loading...";
  document.getElementById("drawerProjectPath").textContent = "";
  document.getElementById("drawerBody").innerHTML = "";
  setDrawerOpen(true);
  try {
    const response = await fetch(`/api/show?project=${encodeURIComponent(projectId)}&sessions=10`, { cache: "no-store" });
    const payload = await response.json();
    if (requestId !== detailRequestId) return;
    if (!payload.success) throw new Error(payload.meta?.error || "project detail failed");
    renderProjectDetail(payload.data || {});
  } catch (err) {
    if (requestId !== detailRequestId) return;
    document.getElementById("drawerProjectName").textContent = "Project detail";
    document.getElementById("drawerBody").innerHTML = `<div class="detail-item" style="color: var(--red);">${esc(err)}</div>`;
  }
}

function annotationBody(actions, action) {
  const body = { project: actions.dataset.projectId || activeProjectId };
  if (action === "note") body.text = actions.querySelector(".annotation-note")?.value.trim() || "";
  if (action === "prioritize") body.level = actions.querySelector(".annotation-priority")?.value || "";
  if (action === "tag") body.tag = actions.querySelector(".annotation-tag")?.value.trim() || "";
  return body;
}

async function submitAnnotation(button) {
  const actions = button.closest(".annotation-actions");
  const action = button.dataset.action;
  if (!actions || !action) return;
  const status = actions.querySelector(".action-status");
  const confirmArchive = actions.querySelector(".annotation-archive-confirm");
  if (action === "archive" && !confirmArchive?.checked) {
    if (status) status.textContent = "Confirm archive first.";
    return;
  }

  const body = annotationBody(actions, action);
  button.disabled = true;
  if (status) status.textContent = "Saving...";
  try {
    const response = await fetch(`/api/annotations/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!payload.success) throw new Error(payload.meta?.error || "annotation failed");
    if (action === "note") actions.querySelector(".annotation-note").value = "";
    if (action === "tag") actions.querySelector(".annotation-tag").value = "";
    if (action === "archive" && confirmArchive) confirmArchive.checked = false;
    if (status) status.textContent = "Saved.";
    if (activeView === "next") await loadNextQueue(true);
    else await loadData(true);
    if (activeProjectId) await openProjectDetail(activeProjectId);
  } catch (err) {
    if (status) status.textContent = String(err);
  } finally {
    if (action === "archive") button.disabled = !confirmArchive?.checked;
    else button.disabled = false;
  }
}

function renderSearchResults(results, meta) {
  const panel = document.getElementById("searchPanel");
  const q = document.getElementById("search").value.trim();
  if (!q) {
    panel.style.display = "none";
    document.getElementById("searchResults").innerHTML = "";
    return;
  }

  panel.style.display = "block";
  document.getElementById("searchTitle").textContent = `Search: ${q}`;
  document.getElementById("searchMeta").textContent = `${meta.total ?? results.length} results${meta.latency_ms === undefined ? "" : ` · ${meta.latency_ms} ms`}`;

  if (!results.length) {
    const hint = meta.hint ? `<div class="result-note">${esc(meta.hint)}</div>` : "";
    document.getElementById("searchResults").innerHTML = `<div class="search-result">No results.${hint}</div>`;
    return;
  }

  document.getElementById("searchResults").innerHTML = results.map(r => {
    const fields = (r.match_fields || []).join(", ");
    const sessions = (r.matching_sessions || []).slice(0, 3).map(renderSessionHit).join("");
    const snippets = sessions ? "" : (r.snippets || []).slice(0, 2).map(s => `<div class="snippet">${esc(s)}</div>`).join("");
    const note = r.latest_note || r.note || "";
    return `<article class="search-result">
      <div class="result-top">
        <span class="result-name">${esc(r.name || "(unnamed)")}</span>
        <span class="result-fields">${esc(fields)}</span>
      </div>
      <div class="result-path">${esc(r.path || "")}</div>
      ${note ? `<div class="result-note">${esc(note)}</div>` : ""}
      ${sessions}
      ${snippets}
    </article>`;
  }).join("");
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  const q = document.getElementById("search").value.trim();
  if (!q) {
    renderSearchResults([], {});
    return;
  }
  document.getElementById("searchPanel").style.display = "block";
  document.getElementById("searchTitle").textContent = `Search: ${q}`;
  document.getElementById("searchMeta").textContent = "Searching...";
  searchTimer = setTimeout(() => runSearch(q), 300);
}

async function runSearch(q) {
  const requestId = ++searchRequestId;
  try {
    const response = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8&sort=relevance`, { cache: "no-store" });
    const payload = await response.json();
    if (requestId !== searchRequestId) return;
    if (!payload.success) throw new Error(payload.meta?.error || "search failed");
    renderSearchResults(payload.data || [], payload.meta || {});
  } catch (err) {
    if (requestId !== searchRequestId) return;
    document.getElementById("searchPanel").style.display = "block";
    document.getElementById("searchMeta").textContent = "";
    document.getElementById("searchResults").innerHTML = `<div class="search-result" style="color: var(--red);">${esc(err)}</div>`;
  }
}

function render() {
  maxDur = Math.max(...DATA.map(r => r.duration_hrs || 0), 1);
  const rows = filteredRows();
  document.getElementById("tbody").innerHTML = rows.map(r => `
    <tr data-project-id="${esc(r.id)}">
      <td class="name" title="${esc(r.name)}">${esc(r.name)}</td>
      <td class="cat-${esc(r.category)}">${esc(r.category)}</td>
      <td class="origin-${esc(r.origin)}">${esc(r.origin)}</td>
      <td class="state-${esc(r.state)}">${esc(r.state)}</td>
      <td class="num">${sessCell(r.sessions)}</td>
      <td class="num">${durCell(r.duration_hrs)}</td>
      <td>${mtags(r.models)}</td>
      <td>${esc(r.first_session)}</td>
      <td>${esc(r.last_active)}</td>
      <td>${liveUrlsCell(r.live_urls)}</td>
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
  if (activeProjectId) {
    document.querySelectorAll("tr").forEach(row => row.classList.toggle("selected", row.dataset.projectId === activeProjectId));
  }
}

function renderNextQueue() {
  document.getElementById("nextTbody").innerHTML = NEXT_DATA.map((r, index) => {
    const ref = projectRef(r);
    const name = r.name || (r.path ? String(r.path).split("/").filter(Boolean).pop() : "(unnamed)");
    return `<tr data-project-id="${esc(ref)}">
      <td class="num">${index + 1}</td>
      <td class="num">${esc(r.score ?? "")}</td>
      <td class="name"><a href="#" class="project-link" data-project-id="${esc(ref)}">${esc(name)}</a></td>
      <td class="state-${esc(r.state)}">${esc(r.state || "")}</td>
      <td>${r.priority && r.priority !== "none" ? esc(r.priority) : ""}</td>
      <td class="reason-cell">${esc(r.reason || "")}</td>
      <td class="path" title="${esc(r.path)}">${esc(r.path || "")}</td>
    </tr>`;
  }).join("");

  document.getElementById("row-count").textContent = `${NEXT_DATA.length} recommendations`;
  const actionable = NEXT_DATA.filter(r => Number(r.factors?.actionable || 0) > 0).length;
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="stat-val">${NEXT_DATA.length}</div><div class="stat-label">Queued</div></div>
    <div class="stat"><div class="stat-val">${NEXT_DATA.filter(r => r.priority && r.priority !== "none").length}</div><div class="stat-label">Prioritized</div></div>
    <div class="stat"><div class="stat-val">${actionable}</div><div class="stat-label">Next Steps</div></div>
    <div class="stat"><div class="stat-val">${NEXT_DATA.filter(r => r.state === "active").length}</div><div class="stat-label">Active</div></div>
  `;
  document.getElementById("subtitle").textContent = `Ranked by pj next | ${NEXT_META.total ?? NEXT_DATA.length} recommendations${NEXT_META.latency_ms === undefined ? "" : ` | ${NEXT_META.latency_ms} ms`}`;
}

function switchView(view) {
  activeView = view;
  document.querySelectorAll(".tab-button").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.getElementById("censusView").hidden = view !== "census";
  document.getElementById("nextView").hidden = view !== "next";
  if (view === "next") {
    if (!nextLoaded) loadNextQueue(false);
    else renderNextQueue();
  } else {
    render();
  }
}

async function loadData(force = false) {
  const error = document.getElementById("error");
  try {
    const response = await fetch(`/api/census?include_ports=1${force ? "&refresh=1" : ""}`, { cache: "no-store" });
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

async function loadNextQueue(force = false) {
  const error = document.getElementById("error");
  try {
    const response = await fetch(`/api/next?limit=20${force ? "&refresh=1" : ""}`, { cache: "no-store" });
    const payload = await response.json();
    if (!payload.success) throw new Error(payload.meta?.error || "request failed");
    NEXT_DATA = payload.data || [];
    NEXT_META = payload.meta || {};
    nextLoaded = true;
    error.style.display = "none";
    renderNextQueue();
  } catch (err) {
    error.textContent = String(err);
    error.style.display = "block";
  }
}

document.querySelectorAll("#censusTable th").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (sortKey === key) sortDir *= -1;
    else { sortKey = key; sortDir = ["last_active_ts", "first_session_ts", "sessions", "duration_hrs", "beads", "has_git"].includes(key) ? -1 : 1; }
    render();
  });
});

document.querySelectorAll(".tab-button").forEach(button => {
  button.addEventListener("click", () => switchView(button.dataset.view || "census"));
});

["tableFilter", "stateFilter", "catFilter", "originFilter", "beadsOnly"].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener(el.tagName === "INPUT" && el.type === "text" ? "input" : "change", render);
});
document.getElementById("search").addEventListener("input", scheduleSearch);
document.getElementById("searchResults").addEventListener("click", event => {
  const button = event.target.closest(".copy-resume");
  if (!button) return;
  navigator.clipboard?.writeText(button.dataset.cmd || "");
});
document.getElementById("tbody").addEventListener("click", event => {
  if (event.target.closest("a")) return;
  const row = event.target.closest("tr[data-project-id]");
  if (row) openProjectDetail(row.dataset.projectId);
});
document.getElementById("nextTbody").addEventListener("click", event => {
  event.preventDefault();
  const target = event.target.closest("[data-project-id]");
  const row = event.target.closest("tr[data-project-id]");
  const projectId = target?.dataset.projectId || row?.dataset.projectId;
  if (projectId) openProjectDetail(projectId);
});
document.getElementById("drawerClose").addEventListener("click", closeDrawer);
document.getElementById("drawerBackdrop").addEventListener("click", closeDrawer);
document.getElementById("projectDrawer").addEventListener("click", event => {
  const button = event.target.closest(".copy-resume");
  if (button) {
    navigator.clipboard?.writeText(button.dataset.cmd || "");
    return;
  }
  const annotationButton = event.target.closest(".annotation-submit");
  if (annotationButton) {
    submitAnnotation(annotationButton);
    return;
  }
  const transcriptButton = event.target.closest(".transcript-open");
  if (transcriptButton) openTranscript(transcriptButton);
});
document.getElementById("projectDrawer").addEventListener("change", event => {
  const confirmArchive = event.target.closest(".annotation-archive-confirm");
  if (!confirmArchive) return;
  const actions = confirmArchive.closest(".annotation-actions");
  const archiveButton = actions?.querySelector('.annotation-submit[data-action="archive"]');
  if (archiveButton) archiveButton.disabled = !confirmArchive.checked;
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeDrawer();
});
document.getElementById("refresh").addEventListener("click", () => {
  if (activeView === "next") loadNextQueue(true);
  else loadData(true);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    if (activeView === "next") loadNextQueue(false);
    else loadData(false);
  }
});
setInterval(() => {
  if (!document.hidden) {
    if (activeView === "next") loadNextQueue(false);
    else loadData(false);
  }
}, 5 * 60 * 1000);
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
        snapshot_fn: Callable[..., dict] = census.snapshot,
        signatures_fn: Callable[[], dict] = cache.signatures,
    ) -> None:
        self.limit = limit
        self.check_interval = check_interval
        self.snapshot_fn = snapshot_fn
        self.signatures_fn = signatures_fn
        self._lock = threading.Lock()
        self._snapshots: dict[bool, dict] = {}
        self._signature: dict | None = None
        self._next_check = 0.0
        self._last_scan_secs = 0.0

    def get(self, *, force: bool = False, include_ports: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            signature_changed = False
            checked = False

            if force or include_ports not in self._snapshots or now >= self._next_check:
                checked = True
                signature = self.signatures_fn()
                signature_changed = signature != self._signature
                if force or include_ports not in self._snapshots or signature_changed:
                    if signature_changed:
                        self._snapshots.clear()
                    started = time.monotonic()
                    if include_ports:
                        self._snapshots[include_ports] = self.snapshot_fn(self.limit, include_ports=True)
                    else:
                        self._snapshots[include_ports] = self.snapshot_fn(self.limit)
                    self._last_scan_secs = time.monotonic() - started
                    self._signature = signature
                self._next_check = now + self.check_interval

            result = dict(self._snapshots.get(include_ports) or {"rows": [], "meta": census.summarize([])})
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
                include_ports = params.get("include_ports", ["0"])[0] in ("1", "true", "yes")
                snapshot = census_cache.get(force=force, include_ports=include_ports)
                body = envelope.to_json(envelope.ok(snapshot["rows"], **snapshot["meta"]))
                self._send_text(body, "application/json; charset=utf-8")
                return

            if parsed.path == "/api/next":
                self._handle_api_get(self._handle_next, parse_qs(parsed.query))
                return

            if parsed.path == "/api/search":
                self._handle_api_get(self._handle_search, parse_qs(parsed.query))
                return

            if parsed.path == "/api/show":
                self._handle_api_get(self._handle_show, parse_qs(parsed.query))
                return

            if parsed.path == "/api/chats":
                self._handle_api_get(self._handle_chats, parse_qs(parsed.query))
                return

            if parsed.path == "/api/chat":
                self._handle_api_get(self._handle_chat, parse_qs(parsed.query))
                return

            if parsed.path.startswith("/api/chat/"):
                params = parse_qs(parsed.query)
                params["session_id"] = [unquote(parsed.path.removeprefix("/api/chat/"))]
                self._handle_api_get(self._handle_chat, params)
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

            if parsed.path.startswith("/api/annotations/"):
                self._handle_annotation(parsed.path.removeprefix("/api/annotations/"))
                return

            self.send_error(404)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def _send_text(self, body: str, content_type: str, *, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: dict, *, status: int = 200) -> None:
            self._send_text(envelope.to_json(payload), "application/json; charset=utf-8", status=status)

        def _handle_api_get(self, handler: Callable[[dict[str, list[str]]], None], params: dict[str, list[str]]) -> None:
            try:
                handler(params)
            except ValueError as exc:
                self._send_json(envelope.err(str(exc), source="request"), status=400)

        def _handle_search(self, params: dict[str, list[str]]) -> None:
            query = params.get("q") or params.get("query") or []
            if not query:
                self._send_json(envelope.err("Missing required query parameter: q", source="search"), status=400)
                return
            try:
                started = time.monotonic()
                results = search_mod.search(
                    query,
                    limit=_int_param(params, "limit", 20),
                    sort=_str_param(params, "sort", "newest"),
                    project=_optional_str_param(params, "project"),
                    match=_str_param(params, "match", "any"),
                    regex=_bool_param(params, "regex", False),
                )
            except ValueError as exc:
                self._send_json(envelope.err(str(exc), source="search"), status=400)
                return
            results = _with_resume_commands(results)
            latency_ms = int((time.monotonic() - started) * 1000)
            hint = None
            regex = _bool_param(params, "regex", False)
            if not results and not regex and search_mod.looks_like_regex(query):
                hint = search_mod.regex_hint(query)
            self._send_json(
                envelope.ok(
                    results,
                    query=query,
                    project=_optional_str_param(params, "project"),
                    match=_str_param(params, "match", "any"),
                    regex=regex,
                    sort=_str_param(params, "sort", "newest"),
                    total=len(results),
                    limit=_int_param(params, "limit", 20),
                    latency_ms=latency_ms,
                    **({"hint": hint} if hint else {}),
                )
            )

        def _handle_next(self, params: dict[str, list[str]]) -> None:
            started = time.monotonic()
            limit = _int_param(params, "limit", 5)
            projects, _ = discover.discover(limit=9999)
            scored = schedule.score_projects(projects)[:limit]
            self._send_json(
                envelope.ok(
                    scored,
                    limit=limit,
                    total=len(scored),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )

        def _handle_show(self, params: dict[str, list[str]]) -> None:
            project_ref = _optional_str_param(params, "project")
            if not project_ref:
                self._send_json(envelope.err("Missing required query parameter: project", source="show"), status=400)
                return
            started = time.monotonic()
            data = resolve_project_detail(project_ref, _int_param(params, "sessions", 10))
            if data is None:
                self._send_json(envelope.err(f"No project matching {project_ref!r}", source="show"), status=404)
                return
            self._send_json(envelope.ok(data, latency_ms=int((time.monotonic() - started) * 1000)))

        def _handle_chats(self, params: dict[str, list[str]]) -> None:
            project_ref = _optional_str_param(params, "project")
            if not project_ref:
                self._send_json(envelope.err("Missing required query parameter: project", source="chats"), status=400)
                return
            project = discover.resolve_project(project_ref)
            if project is None:
                self._send_json(envelope.err(f"No project matching {project_ref!r}", source="chats"), status=404)
                return
            started = time.monotonic()
            limit = _int_param(params, "limit", 20)
            status_data = project_session_data(project, limit)
            sessions = status_data["sessions"]
            self._send_json(
                envelope.ok(
                    sessions,
                    project={"id": project.get("id"), "name": project.get("name"), "path": project.get("path")},
                    total=len(sessions),
                    limit=limit,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )

        def _handle_chat(self, params: dict[str, list[str]]) -> None:
            session_id = _optional_str_param(params, "session_id")
            if not session_id:
                self._send_json(envelope.err("Missing required query parameter: session_id", source="chat"), status=400)
                return
            started = time.monotonic()
            roles = _optional_str_param(params, "roles")
            result = get_store().get_session(
                session_id,
                all_branches=_bool_param(params, "all_branches", False),
                include_tools=not _bool_param(params, "no_tools", False),
                roles=set(roles.split(",")) if roles else None,
            )
            if result is None:
                self._send_json(envelope.err(f"Session {session_id!r} not found", source="chat"), status=404)
                return

            messages = result["messages"]
            last = _optional_int_param(params, "last")
            if last is not None:
                messages = messages[-last:]
            total = len(messages)
            offset = _int_param(params, "offset", 0)
            messages = messages[offset:]
            limit = _optional_int_param(params, "limit")
            if limit is not None:
                messages = messages[:limit]
            result["messages"] = messages

            self._send_json(
                envelope.ok(
                    result,
                    total_messages=total,
                    offset=offset,
                    limit=limit,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )

        def _handle_annotation(self, action: str) -> None:
            try:
                payload = self._read_json_body()
                project_ref = payload.get("project")
                if not isinstance(project_ref, str) or not project_ref:
                    self._send_json(
                        envelope.err("Missing required JSON field: project", source="annotations"),
                        status=400,
                    )
                    return
                project_path = _annotation_project_path(project_ref)
                started = time.monotonic()
                if action == "note":
                    text = payload.get("text")
                    if not isinstance(text, str) or not text:
                        raise ValueError("Missing required JSON field: text")
                    event = annotate.note(project_path, text)
                elif action == "prioritize":
                    level = payload.get("level")
                    if not isinstance(level, str) or not level:
                        raise ValueError("Missing required JSON field: level")
                    event = annotate.prioritize(project_path, level)
                elif action == "tag":
                    tag_name = payload.get("tag")
                    if not isinstance(tag_name, str) or not tag_name:
                        raise ValueError("Missing required JSON field: tag")
                    event = annotate.tag(project_path, tag_name)
                elif action == "archive":
                    event = annotate.archive(project_path)
                else:
                    self._send_json(envelope.err(f"Unknown annotation action: {action}", source="annotations"), status=404)
                    return
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(envelope.err(str(exc), source="annotations"), status=400)
                return

            self._send_json(envelope.ok(event, latency_ms=int((time.monotonic() - started) * 1000)))

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            data = self.rfile.read(length).decode("utf-8")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

    return Handler


def _annotation_project_path(project_ref: str) -> str:
    project = discover.resolve_project(project_ref)
    return project["path"] if project else project_ref


def _with_resume_commands(results: list[dict]) -> list[dict]:
    enriched = []
    for result in results:
        item = dict(result)
        path = str(item.get("path") or "")
        sessions = []
        for session in item.get("matching_sessions") or []:
            session_item = dict(session)
            session_id = session_item.get("session_id")
            agent = session_item.get("agent")
            if path and agent and session_id:
                session_item["resume_cmd"] = resume.full_resume_command(path, str(agent), str(session_id))
            sessions.append(session_item)
        if sessions:
            item["matching_sessions"] = sessions
            item["resume_cmd"] = sessions[0].get("resume_cmd")
        enriched.append(item)
    return enriched


def _str_param(params: dict[str, list[str]], name: str, default: str) -> str:
    return params.get(name, [default])[0]


def _optional_str_param(params: dict[str, list[str]], name: str) -> str | None:
    value = params.get(name, [None])[0]
    return value or None


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    return int(params.get(name, [str(default)])[0])


def _optional_int_param(params: dict[str, list[str]], name: str) -> int | None:
    value = params.get(name, [None])[0]
    return int(value) if value not in (None, "") else None


def _bool_param(params: dict[str, list[str]], name: str, default: bool) -> bool:
    value = params.get(name, [str(int(default))])[0].lower()
    return value in ("1", "true", "yes", "on")


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
