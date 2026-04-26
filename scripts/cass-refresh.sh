#!/usr/bin/env bash
# Refresh both CASS indexes (host + yolobox).
# Discovers new project directories and indexes new sessions.
# Run manually or via cron.
#
# Usage: bash scripts/cass-refresh.sh
#   Cron: 0 3 * * * bash /path/to/scripts/cass-refresh.sh >> /tmp/cass-refresh.log 2>&1
set -uo pipefail

YOLOBOX_HOME="$HOME/.claude-yolobox-home"
YOLOBOX_DB_DIR="$HOME/.local/share/cass-yolobox"

if ! command -v cass &>/dev/null; then
    echo "$(date): CASS not found" >&2
    exit 1
fi

echo "$(date): cass-refresh starting"

# Host index — re-discovers new project dirs under ~/.claude
cass index --full --quiet 2>&1 | tail -3

# Yolobox index — re-discovers new project dirs under ~/.claude-yolobox
if [ -d "$YOLOBOX_HOME/.claude" ]; then
    mkdir -p "$YOLOBOX_DB_DIR"
    HOME="$YOLOBOX_HOME" CASS_DATA_DIR="$YOLOBOX_DB_DIR" cass index --full --quiet 2>&1 | tail -3
else
    echo "Yolobox shim not found — run cass-setup-mac.sh first"
fi

echo "$(date): cass-refresh done"
