#!/usr/bin/env bash
set -euo pipefail

# Smoke test for pj (Project Juggler)
# Runs on the host machine. Lazy-installs CASS if missing.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PJ="python3 -m pj"
PASS=0
FAIL=0
SKIP=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

log_pass() { PASS=$((PASS + 1)); printf "${GREEN}PASS${RESET}  %s\n" "$1"; }
log_fail() { FAIL=$((FAIL + 1)); printf "${RED}FAIL${RESET}  %s\n" "$1"; }
log_skip() { SKIP=$((SKIP + 1)); printf "${YELLOW}SKIP${RESET}  %s\n" "$1"; }
log_info() { printf "${BOLD}---${RESET}  %s\n" "$1"; }

# Run a pj command, capture stdout+stderr, check exit code
run_ok() {
    local desc="$1"; shift
    local output
    if output=$(cd "$PROJECT_ROOT" && $PJ "$@" 2>&1); then
        echo "$output"
        return 0
    else
        echo "$output"
        return 1
    fi
}

# Check JSON has a field
has_field() {
    python3 -c "import json,sys; d=json.load(sys.stdin); assert '$1' in d" 2>/dev/null
}

# Check JSON .success == true
is_success() {
    python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('success') is True" 2>/dev/null
}

###############################################################################
# CASS lazy install
###############################################################################
ensure_cass() {
    if command -v cass >/dev/null 2>&1; then
        log_info "CASS already installed: $(cass --version 2>&1 || echo 'unknown version')"
        return 0
    fi

    # Check if ~/.local/bin/cass exists but not in PATH
    if [ -x "$HOME/.local/bin/cass" ]; then
        export PATH="$HOME/.local/bin:$PATH"
        log_info "CASS found at ~/.local/bin/cass, added to PATH"
        return 0
    fi

    log_info "CASS not found — attempting install..."

    # Pick a writable install destination
    local dest="$HOME/.local/bin"
    if ! mkdir -p "$dest" 2>/dev/null || [ ! -w "$dest" ]; then
        dest="/tmp/cass-bin"
        mkdir -p "$dest"
    fi

    # Prefer the install script from the local repos/ checkout
    local install_script="$PROJECT_ROOT/repos/cass/install.sh"
    if [ -f "$install_script" ]; then
        log_info "Using local install script (dest=$dest): $install_script"
        DEST="$dest" bash "$install_script" --easy-mode --verify 2>&1 | tail -5
    else
        log_info "Fetching install script from GitHub..."
        curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/coding_agent_session_search/main/install.sh \
            | DEST="$dest" bash -s -- --easy-mode --verify 2>&1 | tail -5
    fi

    # Add to PATH for this session
    export PATH="$dest:$HOME/.local/bin:$PATH"

    if command -v cass >/dev/null 2>&1; then
        log_pass "CASS installed successfully: $(cass --version 2>&1 || echo '?')"
    else
        log_fail "CASS install failed — session data tests will be limited"
        return 1
    fi
}

###############################################################################
# Ensure CASS has an index
###############################################################################
ensure_cass_index() {
    if ! command -v cass >/dev/null 2>&1; then return 1; fi

    # Check if the CASS DB exists
    local db=""
    for candidate in \
        "$HOME/Library/Application Support/coding-agent-search/coding-agent-search/agent_search.db" \
        "$HOME/.local/share/coding-agent-search/agent_search.db"; do
        if [ -f "$candidate" ]; then
            db="$candidate"
            break
        fi
    done

    if [ -z "$db" ]; then
        log_info "No CASS index found — skipping (run 'cass index' manually first)"
        log_skip "CASS index not present"
    else
        log_info "CASS DB exists: $db"
    fi
}

###############################################################################
# Clean test annotations (isolated temp dir)
###############################################################################
TEST_DATA_DIR=""
setup_test_env() {
    TEST_DATA_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pj-smoke.XXXXXX")
    export PJ_DATA_DIR="$TEST_DATA_DIR"
    log_info "Test data dir: $TEST_DATA_DIR"
}

cleanup_test_env() {
    if [ -n "$TEST_DATA_DIR" ] && [ -d "$TEST_DATA_DIR" ]; then
        rm -rf "$TEST_DATA_DIR"
    fi
}
trap cleanup_test_env EXIT

###############################################################################
# Tests: Core CLI
###############################################################################
test_help() {
    local out
    out=$(cd "$PROJECT_ROOT" && $PJ --help 2>&1)
    if echo "$out" | grep -q "Project Juggler"; then
        log_pass "pj --help shows description"
    else
        log_fail "pj --help missing description"
    fi
}

test_list_empty() {
    local out
    out=$(run_ok "list empty" list 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj list returns success envelope (empty)"
    else
        log_fail "pj list envelope check"
    fi
    if echo "$out" | has_field "meta"; then
        log_pass "pj list has meta field"
    else
        log_fail "pj list missing meta"
    fi
}

test_list_pretty_empty() {
    local out
    out=$(run_ok "list pretty empty" list --pretty 2>&1)
    if echo "$out" | grep -qi "no projects\|ID.*STATE\|NAME"; then
        log_pass "pj list --pretty renders (empty or header)"
    else
        log_fail "pj list --pretty output unexpected: $out"
    fi
}

###############################################################################
# Tests: Actuators
###############################################################################
test_note() {
    local out
    out=$(run_ok "note" note test-project "Smoke test note" 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj note creates annotation"
    else
        log_fail "pj note: $out"
    fi
}

test_prioritize() {
    local out
    out=$(run_ok "prioritize" prioritize test-project high 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj prioritize sets priority"
    else
        log_fail "pj prioritize: $out"
    fi
}

test_tag() {
    local out
    out=$(run_ok "tag" tag test-project smoke 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj tag adds tag"
    else
        log_fail "pj tag: $out"
    fi
}

test_archive() {
    local out
    out=$(run_ok "archive" archive test-project 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj archive marks archived"
    else
        log_fail "pj archive: $out"
    fi
}

###############################################################################
# Tests: List with annotations
###############################################################################
test_list_with_data() {
    # Unarchive by adding a new note (project should reappear)
    run_ok "setup" note active-proj "Next: do the thing" >/dev/null 2>&1
    run_ok "setup" prioritize active-proj medium >/dev/null 2>&1
    run_ok "setup" tag active-proj tooling >/dev/null 2>&1

    local out
    out=$(run_ok "list" list 2>&1)
    if echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert len(d['data']) > 0" 2>/dev/null; then
        log_pass "pj list shows annotated projects"
    else
        log_fail "pj list empty after annotations"
    fi
}

test_list_state_filter() {
    local out
    out=$(run_ok "list --state" list --state dormant 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj list --state dormant filters"
    else
        log_fail "pj list --state filter"
    fi
}

test_list_tag_filter() {
    local out
    out=$(run_ok "list --tag" list --tag tooling 2>&1)
    if echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['success']
for p in d['data']:
    assert 'tooling' in p.get('tags', [])
" 2>/dev/null; then
        log_pass "pj list --tag tooling filters correctly"
    else
        log_fail "pj list --tag filter"
    fi
}

###############################################################################
# Tests: Status, Search, Next
###############################################################################
test_status() {
    local out
    out=$(run_ok "status" status active-proj 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj status returns project detail"
    else
        log_fail "pj status: $out"
    fi
}

test_status_pretty() {
    local out
    out=$(run_ok "status pretty" status active-proj --pretty 2>&1)
    if echo "$out" | grep -q "active-proj"; then
        log_pass "pj status --pretty renders project name"
    else
        log_fail "pj status --pretty"
    fi
}

test_status_fuzzy() {
    local out
    out=$(run_ok "status fuzzy" status active 2>&1)
    if echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'active-proj' in d['data'].get('name','')" 2>/dev/null; then
        log_pass "pj status resolves fuzzy name"
    else
        log_fail "pj status fuzzy resolution"
    fi
}

test_search() {
    local out
    out=$(run_ok "search" search "thing" 2>&1)
    if echo "$out" | is_success; then
        log_pass "pj search returns results"
    else
        log_fail "pj search"
    fi
}

test_search_pretty() {
    local out
    out=$(run_ok "search pretty" search "thing" --pretty 2>&1)
    if echo "$out" | grep -qi "result\|active-proj\|Search"; then
        log_pass "pj search --pretty renders"
    else
        log_fail "pj search --pretty: $out"
    fi
}

test_next() {
    local out
    out=$(run_ok "next" next 2>&1)
    if echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['success']
# archived test-project should NOT appear
names = [p['name'] for p in d['data']]
assert 'test-project' not in names, f'archived project in next: {names}'
" 2>/dev/null; then
        log_pass "pj next excludes archived, returns scored list"
    else
        log_fail "pj next"
    fi
}

test_next_pretty() {
    local out
    out=$(run_ok "next pretty" next --pretty 2>&1)
    if echo "$out" | grep -q "SCORE\|score\|active-proj"; then
        log_pass "pj next --pretty renders scores"
    else
        log_fail "pj next --pretty: $out"
    fi
}

test_resume_no_sessions() {
    local out
    out=$(cd "$PROJECT_ROOT" && $PJ resume active-proj 2>&1) || true
    if echo "$out" | grep -qi "no sessions"; then
        log_pass "pj resume gracefully errors without sessions"
    else
        log_fail "pj resume without sessions: $out"
    fi
}

###############################################################################
# Tests: Envelope shape
###############################################################################
test_envelope_latency() {
    local out
    out=$(run_ok "envelope" list 2>&1)
    if echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert 'latency_ms' in d['meta'], 'missing latency_ms'
assert isinstance(d['meta']['latency_ms'], (int, float)), 'latency_ms not numeric'
" 2>/dev/null; then
        log_pass "envelope has numeric latency_ms"
    else
        log_fail "envelope latency_ms"
    fi
}

###############################################################################
# Tests: CASS integration (only if CASS available)
###############################################################################
test_cass_integration() {
    if ! command -v cass >/dev/null 2>&1; then
        log_skip "CASS integration (not installed)"
        return
    fi

    # Check if CASS DB exists (facade reads SQLite directly)
    local db_exists
    db_exists=$(python3 -c "
from pj.cass_facade import db_path
p = db_path()
print('yes' if p else 'no')
" 2>/dev/null) || db_exists="no"

    if [ "$db_exists" != "yes" ]; then
        log_skip "CASS integration (no index DB)"
        return
    fi

    # Unset PJ_DATA_DIR so we see real data merged with CASS
    local out
    out=$(cd "$PROJECT_ROOT" && unset PJ_DATA_DIR && $PJ list --limit 5 2>&1)
    if echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['success']
if d['data']:
    p = d['data'][0]
    assert 'agents' in p, 'missing agents field'
    assert len(p['agents']) > 0, 'no agents detected'
    print(f'Found {d[\"meta\"][\"total\"]} projects, first: {p[\"name\"]} agents={p[\"agents\"]}')
else:
    print('No CASS projects (empty index)')
" 2>/dev/null; then
        log_pass "CASS integration: projects have agent data"
    else
        log_fail "CASS integration: list with CASS data"
    fi

    # Test that resume works with real sessions
    local first_proj
    first_proj=$(cd "$PROJECT_ROOT" && unset PJ_DATA_DIR && $PJ list --limit 1 2>&1 \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['data'][0]['name'] if d['data'] else '')" 2>/dev/null)

    if [ -n "$first_proj" ]; then
        local resume_out
        resume_out=$(cd "$PROJECT_ROOT" && unset PJ_DATA_DIR && $PJ resume "$first_proj" 2>&1) || true
        if echo "$resume_out" | grep -q "\-\-resume"; then
            log_pass "CASS integration: resume generates agent command"
        elif echo "$resume_out" | grep -qi "no sessions"; then
            log_pass "CASS integration: resume correctly reports no sessions"
        else
            log_fail "CASS integration: resume for $first_proj: $resume_out"
        fi
    fi

    # Test next with real CASS data
    out=$(cd "$PROJECT_ROOT" && unset PJ_DATA_DIR && $PJ next --limit 3 2>&1)
    if echo "$out" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['success']
for p in d['data']:
    assert 'score' in p, 'missing score'
    assert 'reason' in p, 'missing reason'
print(f'Next suggests {len(d[\"data\"])} projects')
" 2>/dev/null; then
        log_pass "CASS integration: next scores real projects"
    else
        log_fail "CASS integration: next with real data"
    fi
}

###############################################################################
# Run
###############################################################################
main() {
    printf "\n${BOLD}pj smoke test${RESET}\n"
    printf "═══════════════════════════════════════════\n\n"

    # Phase 0: CASS
    log_info "Phase 0: CASS dependency"
    ensure_cass || true
    ensure_cass_index || true

    # Phase 1: Isolated tests (temp annotations dir)
    log_info "Phase 1: Core CLI (isolated)"
    setup_test_env

    test_help
    test_list_empty
    test_list_pretty_empty

    log_info "Phase 2: Actuators"
    test_note
    test_prioritize
    test_tag
    test_archive

    log_info "Phase 3: Queries with annotation data"
    test_list_with_data
    test_list_state_filter
    test_list_tag_filter
    test_status
    test_status_pretty
    test_status_fuzzy
    test_search
    test_search_pretty
    test_next
    test_next_pretty
    test_resume_no_sessions
    test_envelope_latency

    log_info "Phase 4: CASS integration"
    test_cass_integration

    # Summary
    printf "\n═══════════════════════════════════════════\n"
    printf "${GREEN}PASS: $PASS${RESET}  ${RED}FAIL: $FAIL${RESET}  ${YELLOW}SKIP: $SKIP${RESET}\n"

    if [ "$FAIL" -gt 0 ]; then
        exit 1
    fi
}

main "$@"
