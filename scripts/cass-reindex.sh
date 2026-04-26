#!/usr/bin/env bash
# Full CASS reindex — run overnight or unattended.
# Usage: bash scripts/cass-reindex.sh
#   Or with nohup: nohup bash scripts/cass-reindex.sh > /tmp/cass-reindex.log 2>&1 &
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Ensure CASS is installed
if ! command -v cass &>/dev/null; then
    if [ -f "$REPO_DIR/repos/cass/install.sh" ]; then
        echo "Installing CASS..."
        DEST="${HOME}/.local/bin" bash "$REPO_DIR/repos/cass/install.sh" 2>&1 || \
        DEST="/tmp/cass-bin" bash "$REPO_DIR/repos/cass/install.sh" 2>&1
        export PATH="/tmp/cass-bin:${HOME}/.local/bin:$PATH"
    else
        echo "CASS not found and no install script available" >&2
        exit 1
    fi
fi

echo "CASS version: $(cass --version 2>&1 | grep -v warning || echo unknown)"

# Clean slate — remove old DB so CASS re-discovers all sessions
DB_DIR="${CASS_DATA_DIR:-${HOME}/.local/share/coding-agent-search}"
echo "DB dir: $DB_DIR"

if [ -f "$DB_DIR/agent_search.db" ]; then
    echo "Removing old DB for clean re-scan..."
    rm -f "$DB_DIR/agent_search.db" "$DB_DIR/agent_search.db-shm" "$DB_DIR/agent_search.db-wal"
    rm -rf "$DB_DIR/index"
    rm -f "$DB_DIR/index-run.lock"
fi

echo "Starting full index at $(date)..."
cass index --full 2>&1
echo "Index complete at $(date)."

# Verify
echo ""
echo "=== Verification ==="
python3 -c "
import sqlite3, os
db = '$DB_DIR/agent_search.db'
conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
ws = conn.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0]
conv = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
msgs = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
hermes = conn.execute(\"SELECT path FROM workspaces WHERE path LIKE '%hermes%'\").fetchall()
print(f'Workspaces: {ws}  Conversations: {conv}  Messages: {msgs}')
if hermes:
    print(f'Hermes projects: {[h[0] for h in hermes]}')
else:
    print('WARNING: hermes not found in index')
conn.close()
"

echo ""
echo "Done. Test with: python3 -m pj search hackathon --pretty"
