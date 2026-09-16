#!/bin/bash
# PreToolUse hook: enrich cortex operations with project context + audit trail
# For memory_store/update: auto-detect project from cwd, inject via updatedInput
# For ALL cortex tools: log operation to audit trail

INPUT=$(cat)

/usr/bin/python3 -W ignore - "$INPUT" 2>/dev/null <<'PYEOF'
import sys, json, os, time

OPS_LOG = os.environ.get("CORTEX_OPS_LOG") or os.path.expanduser("~/.claude/.cortex_ops_log.jsonl")

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    d = json.loads(raw)
except Exception:
    sys.exit(0)

tool_name = d.get("tool_name", "")
tool_input = d.get("tool_input", {})
cwd = d.get("cwd", "") or os.getcwd()

if not tool_name.startswith("mcp__cortex__"):
    sys.exit(0)

# Extract the specific tool (memory_store, memory_search, etc.)
tool_short = tool_name.replace("mcp__cortex__", "")

# ================================================================
# Audit: log every cortex operation
# ================================================================
try:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": tool_short,
        "cwd": cwd,
    }
    # Include memory_id if available
    if "memory_id" in tool_input:
        entry["memory_id"] = tool_input["memory_id"]
    if "plan_id" in tool_input:
        entry["plan_id"] = tool_input["plan_id"]
    if "query" in tool_input:
        entry["query"] = tool_input["query"][:100]

    with open(OPS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Rotate if >500KB (keep last 7 days) — atomic write
    if os.path.getsize(OPS_LOG) > 500_000:
        import tempfile
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
        with open(OPS_LOG) as f:
            lines = [l for l in f if l[16:26] >= cutoff]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(OPS_LOG))
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.writelines(lines)
            os.replace(tmp_path, OPS_LOG)
        except Exception:
            try: os.unlink(tmp_path)
            except OSError: pass
except Exception:
    pass

# ================================================================
# Enrich: auto-detect project for store/update if not set
# ================================================================
if tool_short in ("memory_store", "memory_update", "plan_create"):
    current_project = tool_input.get("project", "")

    if not current_project:
        detected = ""
        skip = {"home", "Users", "projects", "src", "work", "dev", "repos", "code", ".claude", ""}
        if tool_short == "plan_create":
            # A plan is surfaced by matching its project against the cwd, so name it after
            # the repo, not whichever subdirectory the session happens to be in. The common
            # git dir's parent is the MAIN checkout, so a plan made in a worktree still
            # surfaces in the repo itself; submodules keep their own top level.
            try:
                import subprocess
                def git(*args):
                    return subprocess.run(["git", "-C", cwd, "rev-parse", *args],
                                          capture_output=True, text=True,
                                          timeout=2).stdout.strip()
                common = git("--path-format=absolute", "--git-common-dir")
                if os.path.basename(common) == ".git":
                    name = os.path.basename(os.path.dirname(common))
                else:
                    name = os.path.basename(git("--show-toplevel"))
                if name not in skip and len(name) > 2 and not name.startswith("."):
                    detected = name
            except Exception:
                detected = ""
        # Otherwise detect project dynamically from cwd path components
        parts = cwd.replace("\\", "/").split("/")
        for p in reversed(parts):
            if detected:
                break
            if p not in skip and len(p) > 2 and not p.startswith("."):
                detected = p
                break

        if detected:
            updated_input = dict(tool_input)
            updated_input["project"] = detected

            output = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": updated_input
                }
            })
            print(output)
            sys.exit(0)

# For non-enriched calls, just allow
output = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow"
    }
})
print(output)
PYEOF
