#!/bin/bash
# Cortex plans — UserPromptSubmit hook.
#
# A THIRD hook on the event rather than folded into recall.sh or reminders.sh: each
# must be unable to cost the user the others. Claude Code runs an event's hooks in
# parallel, so the separation adds no latency.
#
# Surfaces two things:
#   FIRST PROMPT OF A SESSION — active plans in this project (pick up where you left off)
#   ANY PROMPT               — open plans (active or future) close to what was just asked
#
# Exits 0 with no output on any failure: a dead plan store never blocks a prompt.

INPUT=$(cat)
# Import the plans library that sits next to this script, not a fixed install path, so
# the code under test is the code that runs.
CORTEX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

/usr/bin/python3 -W ignore - "$INPUT" "$CORTEX_DIR" 2>/dev/null <<'PYEOF'
import sys, json, os, warnings
warnings.filterwarnings("ignore")
os.environ["ONNXRUNTIME_DISABLE_TELEMETRY"] = "1"
os.environ["ORT_LOG_LEVEL"] = "ERROR"
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["ONNXRUNTIME_SESSION_THREAD_POOL_SIZE"] = "2"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

_fd = os.dup(2); _dn = os.open(os.devnull, os.O_WRONLY); os.dup2(_dn, 2); os.close(_dn)
try:
    try:
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)
    except Exception:
        pass
    cortex_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/.claude/skills/cortex")
    sys.path.insert(0, cortex_dir)
    sys.path.insert(0, os.path.join(cortex_dir, "lib"))
    import plans as P
except Exception:
    raise SystemExit(0)
finally:
    os.dup2(_fd, 2); os.close(_fd)

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    d = json.loads(raw)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
prompt = d.get("prompt", "") or d.get("content", "") or ""
cwd = d.get("cwd", "") or os.getcwd()
transcript = d.get("transcript_path", "")

# Subprocess prompts (claude -p) and system-injected prompts: nobody is reading them.
if os.environ.get("CLAUDE_HEADLESS") or prompt.startswith("<"):
    raise SystemExit(0)


def is_first_message(path):
    if not path or not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except Exception:
                    continue
                msg = entry.get("message", entry)
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                c = msg.get("content", "")
                if isinstance(c, str) and len(c) > 10:
                    return False
                if isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text" \
                                and len(part.get("text", "")) > 10:
                            return False
    except Exception:
        pass
    return True


def detect_projects(cwd):
    """Scope from cwd: a plan for one repo must not appear while working in another.

    Matched on whole path components, not substrings (as reminders.sh does), so a
    project named "api" does not surface inside ".../rapid-api-docs"."""
    if not cwd:
        return set()
    try:
        known = {r.get("project", "") for r in P.list_plans(status="open") if r.get("project")}
    except Exception:
        return set()
    parts = {c.lower() for c in cwd.replace("\\", "/").split("/") if c}
    return {p for p in known if p.lower() in parts}


try:
    first = is_first_message(transcript)
    active, relevant = P.collect(prompt, detect_projects(cwd), first)
except Exception:
    raise SystemExit(0)

if not active and not relevant:
    raise SystemExit(0)

try:
    blocks = []
    if active:
        blocks.append(P.render_brief(
            active, "[cortex] Active plans in scope here — background only. Mention one "
                    "only if the user's request continues it; if it does, say so before "
                    "starting and work from it (plan_get for the full checklist), ticking "
                    "items with plan_check as they finish."))
    if relevant:
        blocks.append(P.render_brief(
            relevant, "[cortex] Open plan related to what was just asked — if the request "
                      "continues it, say so and extend it instead of starting a parallel "
                      "plan. Do not act on it unless the user's request is that work."))
    payload = json.dumps({
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(blocks),
        },
    })
except Exception:
    raise SystemExit(0)

try:
    P.record_surfaced(active, relevant)
except Exception:
    pass

print(payload)
PYEOF
