#!/bin/bash
# Cortex reminders — UserPromptSubmit hook.
#
# Deliberately SEPARATE from recall.sh rather than folded into it. recall.sh is
# load-bearing for every prompt in every session; a reminder bug must not be able to
# cost the user their memory recall. Two hooks on the same event both inject.
#
# Surfaces two things:
#   FIRST PROMPT OF A SESSION — everything now due or overdue (the catch-up list)
#   ANY PROMPT               — reminders semantically close to what was just asked
#
# Nothing fires outside a session: by decision there is no cron and no email.

INPUT=$(cat)

/usr/bin/python3 -W ignore - "$INPUT" 2>/dev/null <<'PYEOF'
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
    sys.path.insert(0, os.path.expanduser("~/.claude/skills/cortex"))
    sys.path.insert(0, os.path.expanduser("~/.claude/skills/cortex/lib"))
    import reminders as R
finally:
    os.dup2(_fd, 2); os.close(_fd)

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    d = json.loads(raw)
except Exception:
    d = {}
prompt = d.get("prompt", "") or d.get("content", "") or raw
cwd = d.get("cwd", "") or os.getcwd()
transcript = d.get("transcript_path", "")

# Subprocess prompts (claude -p) get no reminders: nobody is reading them.
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
                if msg.get("role") != "assistant":
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
    """Project scope is derived from cwd, matching how recall.sh does it, so a
    reminder for one repo cannot interrupt unrelated work in another repo."""
    if not cwd:
        return set()
    try:
        rows = R._rows()
    except Exception:
        return set()
    known = {r.get("project", "") for r in rows if r.get("project")}
    low = cwd.lower()
    return {p for p in known if p.lower() in low}


try:
    first = is_first_message(transcript)
    due, relevant, expired = R.collect(prompt, detect_projects(cwd), first)
except R.ReminderStoreUnavailable:
    raise SystemExit(0)          # never block a prompt over the reminder store
except Exception:
    raise SystemExit(0)

if not due and not relevant:
    if expired:
        try:
            R.record_surfaced([], expired)
        except Exception:
            pass
    raise SystemExit(0)

blocks = []
if due:
    blocks.append(R.render(
        due, "[cortex] REMINDERS NOW DUE — raise these with the user before starting "
             "work, not afterwards. Mark done with reminder_done once handled."))
if relevant:
    blocks.append(R.render(
        relevant, "[cortex] Open reminders related to what was just asked — mention "
                  "the relevant one in your reply; do not silently act on it."))

try:
    R.record_surfaced(due + relevant, expired)
except Exception:
    pass

print(json.dumps({
    "suppressOutput": True,
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n\n".join(blocks),
    },
}))
PYEOF
