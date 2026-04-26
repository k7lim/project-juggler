#!/usr/bin/env python3
"""Check CASS indexing progress for both host and yolobox DBs."""
import sqlite3
import os

dbs = {
    "host": os.path.expanduser(
        "~/Library/Application Support/com.coding-agent-search.coding-agent-search/agent_search.db"
    ),
    "yolobox": os.path.expanduser("~/.local/share/cass-yolobox/agent_search.db"),
}

for label, db in dbs.items():
    if not os.path.exists(db):
        print(f"{label}: DB not created yet")
        continue
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ws = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    hermes = conn.execute(
        "SELECT path FROM workspaces WHERE path LIKE '%hermes%'"
    ).fetchall()
    print(f"{label}: {ws} workspaces, {conv} conversations, {msgs} messages")
    if hermes:
        print(f"  hermes: {[h[0] for h in hermes]}")
    conn.close()
