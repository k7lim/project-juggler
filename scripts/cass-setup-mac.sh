#!/usr/bin/env bash
# One-time setup for dual-dotfile CASS indexing on Mac.
# Creates the yolobox HOME shim and indexes both ~/.claude and ~/.claude-yolobox.
#
# Usage: bash scripts/cass-setup-mac.sh
set -euo pipefail

YOLOBOX_HOME="$HOME/.claude-yolobox-home"
YOLOBOX_DB_DIR="$HOME/.local/share/cass-yolobox"

echo "=== CASS dual-dotfile setup ==="

# 1. Check CASS is installed
if ! command -v cass &>/dev/null; then
    echo "CASS not found. Install with: brew install cass  (or cargo install cass)"
    exit 1
fi
echo "CASS: $(cass --version 2>&1 | grep -v warning || echo unknown)"

# 2. Check yolobox dotfiles exist
if [ ! -d "$HOME/.claude-yolobox/projects" ]; then
    echo "ERROR: $HOME/.claude-yolobox/projects not found"
    echo "Is the sandbox volume mounted?"
    exit 1
fi
echo "Yolobox projects: $(ls "$HOME/.claude-yolobox/projects" | wc -l | tr -d ' ') directories"

# 3. Create fake HOME shim (symlink .claude -> .claude-yolobox)
mkdir -p "$YOLOBOX_HOME"
if [ -L "$YOLOBOX_HOME/.claude" ]; then
    echo "Yolobox HOME shim already exists: $YOLOBOX_HOME/.claude"
elif [ -e "$YOLOBOX_HOME/.claude" ]; then
    echo "WARNING: $YOLOBOX_HOME/.claude exists but is not a symlink — skipping"
else
    ln -s "$HOME/.claude-yolobox" "$YOLOBOX_HOME/.claude"
    echo "Created shim: $YOLOBOX_HOME/.claude -> $HOME/.claude-yolobox"
fi

# 4. Index host ~/.claude (standard)
# CASS exits non-zero on minor shard mismatches — tolerate that
echo ""
echo "=== Indexing host sessions (~/.claude) ==="
if cass index --full 2>&1 | tail -5; then
    echo "Host index done."
else
    echo "Host index completed with warnings (non-fatal)."
fi

# 5. Index yolobox ~/.claude-yolobox (via HOME shim)
echo ""
echo "=== Indexing yolobox sessions (~/.claude-yolobox) ==="
mkdir -p "$YOLOBOX_DB_DIR"
if HOME="$YOLOBOX_HOME" CASS_DATA_DIR="$YOLOBOX_DB_DIR" cass index --full 2>&1 | tail -5; then
    echo "Yolobox index done."
else
    echo "Yolobox index completed with warnings (non-fatal)."
fi

# 6. Verify both DBs
echo ""
echo "=== Verification ==="
python3 -c "
import sqlite3, os

dbs = {
    'host': os.path.expanduser('~/Library/Application Support/com.coding-agent-search.coding-agent-search/agent_search.db'),
    'yolobox': '$YOLOBOX_DB_DIR/agent_search.db',
}

for label, db in dbs.items():
    if not os.path.exists(db):
        print(f'{label}: DB not found at {db}')
        continue
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    ws = conn.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0]
    conv = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
    msgs = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    print(f'{label}: {ws} workspaces, {conv} conversations, {msgs} messages')
    conn.close()
"

# 7. Print env var for shell profile
HOST_DB="$HOME/Library/Application Support/com.coding-agent-search.coding-agent-search/agent_search.db"
echo ""
echo "=== Add to ~/.zshrc ==="
echo ""
echo "export PJ_CASS_DBS=\"$HOST_DB:$YOLOBOX_DB_DIR/agent_search.db\""
echo ""
echo "Then: pj search hackathon --pretty"
