#!/usr/bin/env bash
# Cleanly materialize project-juggler as its own host-side checkout.
#
# Run this on the Mac host, not inside a container:
#
#   bash /Users/kevin/Development/external/claude-session-search-eval/scripts/separate-project-juggler-host.sh
#
# Optional: after the new checkout is verified, archive the old mixed folder:
#
#   bash /Users/kevin/Development/external/claude-session-search-eval/scripts/separate-project-juggler-host.sh --archive-old

set -euo pipefail

SOURCE="/Users/kevin/Development/external/claude-session-search-eval"
DEST="/Users/kevin/Development/external/project-juggler"
REMOTE="https://github.com/k7lim/project-juggler.git"
BRANCH="master"
ARCHIVE_OLD=0

usage() {
  cat <<'EOF'
Usage: separate-project-juggler-host.sh [options]

Options:
  --source PATH       Old mixed checkout path
  --dest PATH         New project-juggler checkout path
  --remote URL        Git remote to clone/pull
  --branch NAME       Branch to check out (default: master)
  --archive-old       Rename the old mixed folder after the new checkout verifies
  -h, --help          Show this help

Default action:
  1. Clone or update https://github.com/k7lim/project-juggler.git
     into /Users/kevin/Development/external/project-juggler.
  2. Verify pyproject metadata, pj CLI help, and tests.
  3. Leave the old claude-session-search-eval folder untouched.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="$2"
      shift 2
      ;;
    --dest)
      DEST="$2"
      shift 2
      ;;
    --remote)
      REMOTE="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --archive-old)
      ARCHIVE_OLD=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd git
require_cmd python3

if [[ "$SOURCE" == "$DEST" ]]; then
  echo "Source and destination must be different paths." >&2
  exit 1
fi

if [[ -e "$DEST" && ! -d "$DEST/.git" ]]; then
  echo "Destination exists but is not a git checkout: $DEST" >&2
  exit 1
fi

echo "Source:      $SOURCE"
echo "Destination: $DEST"
echo "Remote:      $REMOTE"
echo "Branch:      $BRANCH"
echo

if [[ -d "$DEST/.git" ]]; then
  echo "Updating existing project-juggler checkout..."
  if [[ -n "$(git -C "$DEST" status --porcelain)" ]]; then
    echo "Destination has uncommitted changes. Commit, stash, or move them first:" >&2
    git -C "$DEST" status --short >&2
    exit 1
  fi
  git -C "$DEST" remote set-url origin "$REMOTE"
  git -C "$DEST" fetch origin
  git -C "$DEST" checkout "$BRANCH"
  git -C "$DEST" pull --ff-only origin "$BRANCH"
else
  echo "Cloning project-juggler..."
  mkdir -p "$(dirname "$DEST")"
  git clone --branch "$BRANCH" "$REMOTE" "$DEST"
fi

echo
echo "Verifying standalone checkout..."
test -f "$DEST/pyproject.toml"
test -d "$DEST/pj"
test -f "$DEST/README.md"

(
  cd "$DEST"
  python3 -m pj --help >/dev/null
  python3 -m py_compile $(git ls-files 'pj/*.py' 'pj/**/*.py')
  if [[ -d tests ]]; then
    if python3 -c 'import pytest' >/dev/null 2>&1; then
      python3 -m pytest -q tests/test_pj.py
    else
      echo "pytest is not installed; skipping test suite."
    fi
  fi
)

if [[ "$ARCHIVE_OLD" -eq 1 ]]; then
  if [[ ! -d "$SOURCE" ]]; then
    echo "Archive requested, but source folder does not exist: $SOURCE" >&2
    exit 1
  fi
  ARCHIVE_PATH="${SOURCE}.archive-$(date +%Y%m%d-%H%M%S)"
  echo
  echo "Archiving old mixed folder:"
  echo "  $SOURCE"
  echo "  -> $ARCHIVE_PATH"
  mv "$SOURCE" "$ARCHIVE_PATH"
fi

echo
echo "Done."
echo "Project Juggler is ready at:"
echo "  $DEST"
echo
echo "Next commands:"
echo "  cd \"$DEST\""
echo "  python3 -m pj list --pretty"
echo
echo "Optional editable install:"
echo "  python3 -m venv .venv"
echo "  . .venv/bin/activate"
echo "  python3 -m pip install -e ."
