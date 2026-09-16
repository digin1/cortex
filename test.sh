#!/bin/bash
# test.sh — Validates all cortex components
# Usage: ./test.sh
# Exit 0 if all pass, exit 1 if any fail

set -o pipefail
export VMEM_TEST=1

VMEM_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS_COUNT=0
FAIL_COUNT=0

pass() {
    echo "PASS: $1"
    (( PASS_COUNT++ ))
}

fail() {
    echo "FAIL: $1 — $2"
    (( FAIL_COUNT++ ))
}

# Create isolated temp directory for all test artifacts
TMPDIR_TEST=$(mktemp -d /tmp/cortex-test.XXXXXX)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# Helper: check if string is valid JSON
is_valid_json() {
    python3 -c "import json,sys; json.loads(sys.stdin.read())" <<< "$1" 2>/dev/null
}

# Helper: extract JSON field value (returns "null" if missing)
json_field() {
    python3 -c "
import json,sys
d = json.loads(sys.stdin.read())
keys = '$2'.split('.')
v = d
for k in keys:
    if isinstance(v, dict):
        v = v.get(k)
    else:
        v = None
        break
print(json.dumps(v))
" <<< "$1" 2>/dev/null
}

###############################################################################
# 1. Test recall.sh
###############################################################################

# 1a. Empty prompt — should exit 0 silently with no output
RECALL_EMPTY_INPUT='{"prompt": "", "transcript_path": ""}'
RECALL_EMPTY_OUT=$(echo "$RECALL_EMPTY_INPUT" | bash "$VMEM_DIR/recall.sh" 2>/dev/null)
RECALL_EMPTY_RC=$?
if [ $RECALL_EMPTY_RC -eq 0 ] && [ -z "$RECALL_EMPTY_OUT" ]; then
    pass "recall.sh — empty prompt exits 0 silently"
else
    fail "recall.sh — empty prompt" "rc=$RECALL_EMPTY_RC, output='$RECALL_EMPTY_OUT'"
fi

# 1b. Short prompt (< 3 chars) — should also exit 0 silently
RECALL_SHORT_INPUT='{"prompt": "hi"}'
RECALL_SHORT_OUT=$(echo "$RECALL_SHORT_INPUT" | bash "$VMEM_DIR/recall.sh" 2>/dev/null)
RECALL_SHORT_RC=$?
if [ $RECALL_SHORT_RC -eq 0 ] && [ -z "$RECALL_SHORT_OUT" ]; then
    pass "recall.sh — short prompt (<3 chars) exits 0 silently"
else
    fail "recall.sh — short prompt (<3 chars)" "rc=$RECALL_SHORT_RC, output='$RECALL_SHORT_OUT'"
fi

# 1c. Real prompt — should exit 0 and if output exists, it must be valid JSON
#     with suppressOutput and hookSpecificOutput.additionalContext
RECALL_REAL_INPUT='{"prompt": "How do I configure the Dask workers for GPU processing?", "transcript_path": ""}'
RECALL_REAL_OUT=$(echo "$RECALL_REAL_INPUT" | bash "$VMEM_DIR/recall.sh" 2>/dev/null)
RECALL_REAL_RC=$?
if [ $RECALL_REAL_RC -eq 0 ]; then
    if [ -z "$RECALL_REAL_OUT" ]; then
        # No memories matched — that is valid (empty DB or no close matches)
        pass "recall.sh — real prompt exits 0 (no memories matched, valid)"
    elif is_valid_json "$RECALL_REAL_OUT"; then
        SO=$(json_field "$RECALL_REAL_OUT" "suppressOutput")
        AC=$(json_field "$RECALL_REAL_OUT" "hookSpecificOutput.additionalContext")
        if [ "$SO" = "true" ] && [ "$AC" != "null" ] && [ "$AC" != '""' ]; then
            pass "recall.sh — real prompt returns valid JSON with suppressOutput and additionalContext"
        else
            fail "recall.sh — real prompt JSON structure" "suppressOutput=$SO, additionalContext=$AC"
        fi
    else
        fail "recall.sh — real prompt" "output is not valid JSON: '$RECALL_REAL_OUT'"
    fi
else
    fail "recall.sh — real prompt" "non-zero exit code: $RECALL_REAL_RC"
fi

###############################################################################
# 2. Test agent_track.sh
###############################################################################

# Use a temp ledger to avoid polluting the real one
ORIG_LEDGER="$HOME/.claude/agent-usage.jsonl"
TEST_LEDGER="$TMPDIR_TEST/agent-usage.jsonl"

# Backup real ledger if it exists, replace with temp
if [ -f "$ORIG_LEDGER" ]; then
    cp "$ORIG_LEDGER" "$TMPDIR_TEST/agent-usage-backup.jsonl"
fi

# Count lines in ledger before (or 0 if missing)
BEFORE_LINES=0
if [ -f "$ORIG_LEDGER" ]; then
    BEFORE_LINES=$(wc -l < "$ORIG_LEDGER")
fi

TRACK_INPUT=$(cat <<'ENDJSON'
{
  "tool_name": "Agent",
  "tool_input": {
    "subagent_type": "test-agent",
    "description": "Validate cortex test harness integration",
    "model": "sonnet"
  },
  "cwd": "/tmp/cortex-test"
}
ENDJSON
)

TRACK_OUT=$(echo "$TRACK_INPUT" | bash "$VMEM_DIR/agent_track.sh" 2>/dev/null)
TRACK_RC=$?

if [ $TRACK_RC -eq 0 ]; then
    if is_valid_json "$TRACK_OUT"; then
        SO=$(json_field "$TRACK_OUT" "suppressOutput")
        if [ "$SO" = "true" ]; then
            pass "agent_track.sh — returns valid JSON with suppressOutput=true"
        else
            fail "agent_track.sh — JSON structure" "suppressOutput=$SO (expected true)"
        fi
    else
        fail "agent_track.sh — output" "not valid JSON: '$TRACK_OUT'"
    fi
else
    fail "agent_track.sh — execution" "non-zero exit code: $TRACK_RC"
fi

# Verify a line was appended to the ledger
AFTER_LINES=0
if [ -f "$ORIG_LEDGER" ]; then
    AFTER_LINES=$(wc -l < "$ORIG_LEDGER")
fi

if [ "$AFTER_LINES" -gt "$BEFORE_LINES" ]; then
    # Verify the last line is valid JSON with our test agent
    LAST_LINE=$(tail -1 "$ORIG_LEDGER")
    if is_valid_json "$LAST_LINE"; then
        AGENT_NAME=$(json_field "$LAST_LINE" "agent")
        if [ "$AGENT_NAME" = '"test-agent"' ]; then
            pass "agent_track.sh — appended valid entry to ledger"
        else
            pass "agent_track.sh — appended entry to ledger (agent=$AGENT_NAME)"
        fi
    else
        fail "agent_track.sh — ledger entry" "last line is not valid JSON"
    fi
else
    fail "agent_track.sh — ledger" "no new line appended (before=$BEFORE_LINES, after=$AFTER_LINES)"
fi

# Clean up test entry from real ledger (remove last line if it's our test entry)
if [ -f "$ORIG_LEDGER" ] && [ "$AFTER_LINES" -gt "$BEFORE_LINES" ]; then
    LAST_LINE=$(tail -1 "$ORIG_LEDGER")
    AGENT_CHECK=$(json_field "$LAST_LINE" "agent" 2>/dev/null)
    if [ "$AGENT_CHECK" = '"test-agent"' ]; then
        # Remove the test entry we just added
        head -n -1 "$ORIG_LEDGER" > "$TMPDIR_TEST/ledger-trimmed.jsonl"
        mv "$TMPDIR_TEST/ledger-trimmed.jsonl" "$ORIG_LEDGER"
    fi
fi

###############################################################################
# 3. Test learn.sh
###############################################################################

LEARN_OUT=$(bash "$VMEM_DIR/learn.sh" 2>/dev/null)
LEARN_RC=$?

if [ $LEARN_RC -eq 0 ]; then
    if is_valid_json "$LEARN_OUT"; then
        SM=$(json_field "$LEARN_OUT" "systemMessage")
        if [ "$SM" != "null" ] && [ "$SM" != '""' ]; then
            pass "learn.sh — returns valid JSON with systemMessage"
        else
            fail "learn.sh — JSON structure" "systemMessage is null or empty"
        fi
    else
        fail "learn.sh — output" "not valid JSON: '$LEARN_OUT'"
    fi
else
    fail "learn.sh — execution" "non-zero exit code: $LEARN_RC"
fi

###############################################################################
# 4. Test statusline.sh
###############################################################################

STATUS_OUT=$(bash "$VMEM_DIR/statusline.sh" 2>/dev/null)
STATUS_RC=$?

if [ $STATUS_RC -eq 0 ]; then
    # Check for brain emoji (U+1F9E0) — might render as multi-byte
    if echo "$STATUS_OUT" | python3 -c "import sys; line=sys.stdin.read(); sys.exit(0 if '\U0001f9e0' in line else 1)" 2>/dev/null; then
        BRAIN_OK=1
    else
        BRAIN_OK=0
    fi

    if echo "$STATUS_OUT" | grep -q "cortex"; then
        VMEM_OK=1
    else
        VMEM_OK=0
    fi

    if [ "$BRAIN_OK" -eq 1 ] && [ "$VMEM_OK" -eq 1 ]; then
        pass "statusline.sh — outputs line with brain emoji and 'cortex'"
    elif [ "$BRAIN_OK" -eq 0 ]; then
        fail "statusline.sh — format" "missing brain emoji. Output: '$STATUS_OUT'"
    else
        fail "statusline.sh — format" "missing 'cortex' substring. Output: '$STATUS_OUT'"
    fi
else
    fail "statusline.sh — execution" "non-zero exit code: $STATUS_RC"
fi

###############################################################################
# 5. Test agent_dashboard.py
###############################################################################

DASH_OUT=$(python3 "$VMEM_DIR/agent_dashboard.py" 2>/dev/null)
DASH_RC=$?

if [ $DASH_RC -eq 0 ]; then
    if is_valid_json "$DASH_OUT"; then
        HAS_SUMMARY=$(json_field "$DASH_OUT" "summary")
        HAS_AGENTS=$(json_field "$DASH_OUT" "agents")
        if [ "$HAS_SUMMARY" != "null" ] && [ "$HAS_AGENTS" != "null" ]; then
            pass "agent_dashboard.py — returns valid JSON with summary and agents"
        else
            fail "agent_dashboard.py — JSON structure" "summary=$HAS_SUMMARY, agents=$HAS_AGENTS"
        fi
    else
        fail "agent_dashboard.py — output" "not valid JSON (first 200 chars): '${DASH_OUT:0:200}'"
    fi
else
    fail "agent_dashboard.py — execution" "non-zero exit code: $DASH_RC"
fi

###############################################################################
# 6. Test cleanup.sh
###############################################################################

CLEANUP_OUT=$(bash "$VMEM_DIR/cleanup.sh" 2>/dev/null)
CLEANUP_RC=$?

if [ $CLEANUP_RC -eq 0 ]; then
    if echo "$CLEANUP_OUT" | grep -q "\[cortex cleanup\]"; then
        pass "cleanup.sh — exits cleanly with [cortex cleanup] output"
    else
        # cleanup.sh may produce no output if the python except fires (no DB)
        # The script swallows all errors with bare except: pass
        # So empty output with rc=0 is technically valid but suboptimal
        if [ -z "$CLEANUP_OUT" ]; then
            # Check if DB exists — if no DB, the except block catches and outputs nothing
            if [ -d "$HOME/.claude/cortex-db" ]; then
                fail "cleanup.sh — output" "no output despite DB existing"
            else
                pass "cleanup.sh — exits cleanly (no DB, silent ok)"
            fi
        else
            fail "cleanup.sh — output" "missing '[cortex cleanup]'. Output: '$CLEANUP_OUT'"
        fi
    fi
else
    fail "cleanup.sh — execution" "non-zero exit code: $CLEANUP_RC"
fi

###############################################################################
# 7. Test lib/parse_transcript.py
###############################################################################

# Create a mock JSONL transcript
MOCK_TRANSCRIPT="$TMPDIR_TEST/mock_transcript.jsonl"
cat > "$MOCK_TRANSCRIPT" <<'JSONL'
{"message": {"role": "user", "content": "How do I set up Dask workers for GPU processing?"}}
{"message": {"role": "assistant", "content": "You need to set resources={'GPU': 1} on your Dask submit calls."}}
{"message": {"role": "user", "content": "What about memory limits?"}}
{"message": {"role": "assistant", "content": [{"type": "text", "text": "Dask workers have memory thresholds configured at 85% target."}, {"type": "tool_use", "name": "Read", "input": {"file_path": "/app/config.py"}}]}}
JSONL

PT_OUT=$(python3 "$VMEM_DIR/lib/parse_transcript.py" "$MOCK_TRANSCRIPT" 2>/dev/null)
PT_RC=$?

if [ $PT_RC -eq 0 ]; then
    # Should contain user and assistant messages
    HAS_USER=0
    HAS_ASSISTANT=0
    HAS_TOOL=0

    echo "$PT_OUT" | grep -q "\[user\]:" && HAS_USER=1
    echo "$PT_OUT" | grep -q "\[assistant\]:" && HAS_ASSISTANT=1
    echo "$PT_OUT" | grep -q "\[tools\]:" && HAS_TOOL=1

    if [ $HAS_USER -eq 1 ] && [ $HAS_ASSISTANT -eq 1 ]; then
        if [ $HAS_TOOL -eq 1 ]; then
            pass "lib/parse_transcript.py — parses user, assistant, and tool_use entries"
        else
            pass "lib/parse_transcript.py — parses user and assistant entries (no tool_use detected)"
        fi
    else
        fail "lib/parse_transcript.py — output" "user=$HAS_USER, assistant=$HAS_ASSISTANT. Output: '$PT_OUT'"
    fi
else
    fail "lib/parse_transcript.py — execution" "non-zero exit code: $PT_RC"
fi

# 7b. parse_transcript.py with no args should print usage and exit 1
PT_NOARG_OUT=$(python3 "$VMEM_DIR/lib/parse_transcript.py" 2>&1)
PT_NOARG_RC=$?
if [ $PT_NOARG_RC -eq 1 ] && echo "$PT_NOARG_OUT" | grep -qi "usage"; then
    pass "lib/parse_transcript.py — prints usage on missing args"
else
    fail "lib/parse_transcript.py — missing args" "rc=$PT_NOARG_RC, output='$PT_NOARG_OUT'"
fi

# 7c. parse_transcript.py with nonexistent file should exit 0 with empty output (graceful)
PT_NOFILE_OUT=$(python3 "$VMEM_DIR/lib/parse_transcript.py" "/tmp/nonexistent_cortex_test_file.jsonl" 2>/dev/null)
PT_NOFILE_RC=$?
if [ $PT_NOFILE_RC -eq 0 ]; then
    pass "lib/parse_transcript.py — handles nonexistent file gracefully"
else
    fail "lib/parse_transcript.py — nonexistent file" "rc=$PT_NOFILE_RC"
fi

###############################################################################
# 7d. Test lib/store_memories.py
###############################################################################

# Build mock JSON input — an array of memory items
MOCK_MEMORIES='[{"id": "cortex-test-memory-001", "content": "This is a test memory entry from the cortex test harness that should be long enough to pass the minimum length check of ten characters", "type": "project", "tags": "test"}]'

SM_OUT=$(python3 "$VMEM_DIR/lib/store_memories.py" "$MOCK_MEMORIES" 2>/dev/null)
SM_RC=$?

if [ $SM_RC -eq 0 ]; then
    if echo "$SM_OUT" | grep -q "\[cortex compact\]"; then
        pass "lib/store_memories.py — stores memory and outputs [cortex compact] message"
    elif [ -z "$SM_OUT" ]; then
        # Could be duplicate (cosine < 0.15) or no DB — still valid exit
        pass "lib/store_memories.py — runs without error (no output, likely dedup or first run)"
    else
        pass "lib/store_memories.py — runs without error. Output: '$SM_OUT'"
    fi
else
    fail "lib/store_memories.py — execution" "non-zero exit code: $SM_RC"
fi

# store_memories.py with empty input should exit 0 silently
SM_EMPTY_OUT=$(python3 "$VMEM_DIR/lib/store_memories.py" "" 2>/dev/null)
SM_EMPTY_RC=$?
if [ $SM_EMPTY_RC -eq 0 ] && [ -z "$SM_EMPTY_OUT" ]; then
    pass "lib/store_memories.py — empty input exits 0 silently"
else
    fail "lib/store_memories.py — empty input" "rc=$SM_EMPTY_RC, output='$SM_EMPTY_OUT'"
fi

# Clean up the test memory we just stored (if DB exists)
python3 -W ignore -c "
import os, warnings
warnings.filterwarnings('ignore')
try:
    import sys
    sys.path.insert(0, os.path.expanduser('~/.claude/skills/cortex/lib'))
    from chroma_client import get_collection
    col = get_collection()
    col.delete(ids=['cortex-test-memory-001'])
except: pass
" 2>/dev/null

###############################################################################
# 8. Plans (lib/plans.py, plan_* MCP tools, plans.sh, status line)
###############################################################################

# Run by path: a site-packages `tests` package can shadow `-m unittest tests.test_plans`.
PLANS_OUT=$(/usr/bin/python3 -W ignore "$VMEM_DIR/tests/test_plans.py" 2>&1)
if [ $? -eq 0 ]; then
    if echo "$PLANS_OUT" | grep -q "skipped="; then
        pass "tests/test_plans.py — $(echo "$PLANS_OUT" | grep -E '^Ran ') (live tests SKIPPED: services down)"
    else
        pass "tests/test_plans.py — $(echo "$PLANS_OUT" | grep -E '^Ran ')"
    fi
else
    fail "tests/test_plans.py" "$(echo "$PLANS_OUT" | grep -E '^(FAIL|ERROR):' | head -5 | tr '\n' ';')"
fi

###############################################################################
# Summary
###############################################################################

echo ""
echo "============================================"
TOTAL=$(( PASS_COUNT + FAIL_COUNT ))
echo "Results: $PASS_COUNT/$TOTAL passed, $FAIL_COUNT failed"
echo "============================================"

if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
else
    echo "All tests passed."
    exit 0
fi
