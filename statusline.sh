#!/bin/bash
# Status line for vector memory — multi-line, clear labels, visual indicators

# ── Read JSON input from stdin ──
INPUT=$(cat)

# ── Context window usage ──
USED_PCT=$(echo "$INPUT" | jq -r '.context_window.used_percentage // empty' 2>/dev/null)
CTX_LINE=""
if [ -n "$USED_PCT" ]; then
    USED_INT=$(printf '%.0f' "$USED_PCT")
    REMAINING_INT=$((100 - USED_INT))
    # Build a simple ASCII bar (20 chars wide)
    FILLED=$(( USED_INT / 5 ))
    EMPTY=$(( 20 - FILLED ))
    BAR=$(printf '%0.s#' $(seq 1 $FILLED) 2>/dev/null)$(printf '%0.s-' $(seq 1 $EMPTY) 2>/dev/null)
    CTX_LINE="Context [${BAR}] ${USED_INT}% used, ${REMAINING_INT}% remaining"
fi

# ── Line 0: PS1-style shell prompt header ──
PS1_LINE=$(printf "\033[01;32m%s@%s\033[00m:\033[01;34m%s\033[00m" "$(whoami)" "$(hostname -s)" "$(pwd)")

DB_PATH="$HOME/.claude/cortex-db"
ACTIVITY_FILE="$HOME/.claude/.cortex_activity"
OPS_LOG="$HOME/.claude/.cortex_ops_log.jsonl"
SESSIONS_LOG="$HOME/.claude/.cortex_sessions.jsonl"
CACHE_FILE="$HOME/.claude/.cortex_statusline_cache"
CACHE_TTL=15  # seconds

# Use cached output if fresh enough
if [ -f "$CACHE_FILE" ]; then
    CACHE_AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$CACHE_AGE" -lt "$CACHE_TTL" ]; then
        cat "$CACHE_FILE"
        exit 0
    fi
fi

# ── Line 1: Memory stats ──
MEMORY_LINE=$(/usr/bin/python3 -W ignore - "$DB_PATH" 2>/dev/null <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/cortex/lib"))
from chroma_client import get_client, get_collection

try:
    col = get_collection()
    c = col.count()
    if c == 0:
        print("empty")
    else:
        data = col.get()
        types = {}
        projects = set()
        for m in data["metadatas"]:
            t = m.get("type", "?")
            if t == "agent_eval":
                continue
            types[t] = types.get(t, 0) + 1
            p = m.get("project", "")
            if p and p != "global":
                projects.add(p)

        # Build readable breakdown
        type_names = {
            "project": "project",
            "feedback": "feedback",
            "preferences": "prefs",
            "reference": "reference",
            "user": "user",
            "general": "general"
        }
        parts = []
        for t in ["project", "feedback", "preferences", "reference", "user", "general"]:
            if t in types:
                parts.append(f"{types[t]} {type_names.get(t, t)}")

        breakdown = ", ".join(parts)
        proj_count = len(projects)
        proj_text = f" across {proj_count} project{'s' if proj_count != 1 else ''}" if proj_count > 0 else ""
        print(f"{c} memories ({breakdown}){proj_text}")
except Exception:
    print("offline")
PYEOF
)

# ── Line 1b: Reminders ──
# Shown whenever any are pending. The reminder system is deliberately silent -- it says
# nothing when nothing is due -- which makes "quiet" and "broken" look identical from the
# outside. One line here is what distinguishes them at a glance.
REMINDER_LINE=$(/usr/bin/python3 -W ignore -c "
import os, sys, time
sys.path.insert(0, os.path.expanduser('~/.claude/skills/cortex'))
try:
    from lib import reminders as R
    rows = R.list_all(status='pending')
except Exception:
    sys.exit(0)                      # store unavailable -> say nothing, never an error line
if not rows:
    sys.exit(0)
now = R.now_iso()
due = [r for r in rows if (r.get('due') or '') and r['due'] <= now
       and not ((r.get('snooze_until') or '') > now)]
overdue_worst = ''
if due:
    oldest = min(d['due'] for d in due)
    try:
        import datetime
        days = (datetime.datetime.now()
                - datetime.datetime.strptime(oldest, '%Y-%m-%dT%H:%M:%S')).days
        if days >= 1:
            overdue_worst = f', oldest {days}d overdue'
    except Exception:
        pass
if due:
    print(f'{len(due)} DUE{overdue_worst} | {len(rows)} pending')
else:
    nxt = sorted([r['due'] for r in rows if r.get('due')])
    tail = f' | next {nxt[0][:16]}' if nxt else ''
    print(f'{len(rows)} pending{tail}')
" 2>/dev/null)

# ── Line 2: Agent fleet ──
FLEET_LINE=$(/usr/bin/python3 -W ignore -c "
import os, sys, glob, json

user_dir = os.path.expanduser('~/.claude/agents')
proj_dir = '.claude/agents'
user_count = len(glob.glob(os.path.join(user_dir, '*.md'))) if os.path.isdir(user_dir) else 0
proj_count = len(glob.glob(os.path.join(proj_dir, '*.md'))) if os.path.isdir(proj_dir) else 0
total = user_count + proj_count

if total == 0:
    exit(0)

parts = []
if proj_count:
    parts.append(f'{proj_count} project')
if user_count:
    parts.append(f'{user_count} global')

# Usage stats from ledger
usage_total = 0
usage_today = 0
today = __import__('time').strftime('%Y-%m-%d')
ledger = os.path.expanduser('~/.claude/agent-usage.jsonl')
if os.path.exists(ledger):
    try:
        with open(ledger) as f:
            for line in f:
                usage_total += 1
                try:
                    e = json.loads(line.strip())
                    if e.get('timestamp', '').startswith(today):
                        usage_today += 1
                except: pass
    except: pass

# Eval scores
avg_score = ''
try:
    sys.path.insert(0, os.path.expanduser('~/.claude/skills/cortex/lib'))
    from chroma_client import get_client, get_collection
    col = get_collection()
    data = col.get(where={'type': 'agent_eval'})
    latest = {}
    for i in range(len(data['ids'])):
        m = data['metadatas'][i]
        name = m.get('agent_name', '?')
        ts = m.get('timestamp', '')
        if name not in latest or ts > latest[name]['ts']:
            latest[name] = {'ts': ts, 'score': m.get('score', '0')}
    if latest:
        scores = [int(v['score']) for v in latest.values() if v['score'].isdigit()]
        if scores:
            avg = sum(scores) / len(scores)
            avg_score = f' | health {avg:.1f}/5'
except: pass

scope_text = ' + '.join(parts)
usage_text = ''
if usage_today > 0:
    usage_text = f' | {usage_today} spawns today'
elif usage_total > 0:
    usage_text = f' | {usage_total} total spawns'

print(f'{total} agents ({scope_text}){usage_text}{avg_score}')
" 2>/dev/null)

# ── Line 3: Auto-discovered skills ──
SKILLS_LINE=$(/usr/bin/python3 -W ignore -c "
import os, glob
proj_dir = '.claude/commands'
global_dir = os.path.expanduser('~/.claude/commands')
proj_count = len(glob.glob(os.path.join(proj_dir, '*.md'))) if os.path.isdir(proj_dir) else 0
global_count = len(glob.glob(os.path.join(global_dir, '*.md'))) if os.path.isdir(global_dir) else 0
total = proj_count + global_count
if total == 0:
    exit(0)
parts = []
if proj_count:
    parts.append(f'{proj_count} project')
if global_count:
    parts.append(f'{global_count} global')
print(f'{total} skills ({\" + \".join(parts)})')
" 2>/dev/null)

# ── Line 5: Health & config ──
HEALTH_LINE=$(/usr/bin/python3 -W ignore -c "
import json, os, subprocess

config_file = os.path.expanduser('~/.claude/.cortex_config')
try:
    with open(config_file) as f:
        cfg = json.load(f)
except:
    cfg = {}

# Defaults
defaults = {
    'auto_learn': True,
    'auto_skills': True,
    'auto_agents': True,
}

# ChromaDB status
db_ok = False
try:
    import urllib.request
    r = urllib.request.urlopen('http://localhost:8100/api/v2/heartbeat', timeout=1)
    db_ok = r.status == 200
except:
    pass

# Build toggle display
toggles = []
for key in ['auto_learn', 'auto_skills', 'auto_agents']:
    val = cfg.get(key, defaults[key])
    short = key.replace('auto_', '')
    symbol = '\u2713' if val else '\u2717'
    toggles.append(f'{short}:{symbol}')

db_status = 'chromadb:\u2713' if db_ok else 'chromadb:\u2717'
print(f'{db_status} | {\" \".join(toggles)}')
" 2>/dev/null)

# ── Line 4: Today's operations ──
OPS_LINE=""
if [ -f "$OPS_LOG" ]; then
    TODAY=$(date +%Y-%m-%d)
    # `grep -c` EXITS 1 when the count is zero, so `|| echo 0` fired on top of grep's
    # own "0" and OPS_TODAY became the two-line string "0\n0" -- which then failed
    # `[ -gt ]` with "integer expression expected" on every render. Take the first line.
    OPS_TODAY=$(grep -c "\"$TODAY" "$OPS_LOG" 2>/dev/null | head -1)
    OPS_TODAY=${OPS_TODAY:-0}
    if [ "$OPS_TODAY" -gt 0 ]; then
        OPS_LINE="$OPS_TODAY ops today"
    fi
fi

# ── Line 4: Recent activity (last 30 seconds) ──
ACTIVITY=""
if [ -f "$ACTIVITY_FILE" ]; then
    LAST_MOD=$(stat -c %Y "$ACTIVITY_FILE" 2>/dev/null || stat -f %m "$ACTIVITY_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$(( NOW - LAST_MOD ))
    if [ "$AGE" -le 30 ]; then
        ACTIVITY=$(cat "$ACTIVITY_FILE" 2>/dev/null)
    fi
fi


# ── Assemble output ──
OUTPUT="${PS1_LINE}"

if [ -n "$CTX_LINE" ]; then
    OUTPUT="${OUTPUT}\n${CTX_LINE}"
fi

OUTPUT="${OUTPUT}\n\U0001f9e0 ${MEMORY_LINE}"

if [ -n "$REMINDER_LINE" ]; then
    OUTPUT="${OUTPUT}\n\U000023f0 ${REMINDER_LINE}"
fi

if [ -n "$FLEET_LINE" ]; then
    OUTPUT="${OUTPUT}\n\U0001f916 ${FLEET_LINE}"
fi

if [ -n "$SKILLS_LINE" ]; then
    OUTPUT="${OUTPUT}\n\U0001f4da ${SKILLS_LINE}"
fi


if [ -n "$HEALTH_LINE" ]; then
    OUTPUT="${OUTPUT}\n\U00002699 ${HEALTH_LINE}"
fi

# Combine ops + activity on one line if both exist
EXTRA=""
if [ -n "$OPS_LINE" ] && [ -n "$ACTIVITY" ]; then
    EXTRA="\U000026a1 ${OPS_LINE} | ${ACTIVITY}"
elif [ -n "$OPS_LINE" ]; then
    EXTRA="\U000026a1 ${OPS_LINE}"
elif [ -n "$ACTIVITY" ]; then
    EXTRA="\U000026a1 ${ACTIVITY}"
fi

if [ -n "$EXTRA" ]; then
    OUTPUT="${OUTPUT}\n${EXTRA}"
fi

# Cache output and display
echo -e "$OUTPUT" | tee "$CACHE_FILE"
