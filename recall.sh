#!/bin/bash
# Auto-recall: searches vector memory for context relevant to user's prompt
# Called by UserPromptSubmit hook — reads JSON from stdin
# Outputs JSON with additionalContext for silent injection into Claude's context
#
# Two modes:
#   FIRST MESSAGE  — comprehensive project-aware context load
#                    (all user/feedback + project-specific + global memories)
#   SUBSEQUENT     — targeted semantic search with project boost

INPUT=$(cat)

/usr/bin/python3 -W ignore - "$INPUT" 2>/dev/null <<'PYEOF'
import sys, json, os, time, warnings
warnings.filterwarnings("ignore")
os.environ["ONNXRUNTIME_DISABLE_TELEMETRY"] = "1"
os.environ["ORT_LOG_LEVEL"] = "ERROR"
# Throttle onnxruntime threads — prevents each hook from saturating all cores
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["ONNXRUNTIME_SESSION_THREAD_POOL_SIZE"] = "2"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Suppress onnxruntime noise
_fd = os.dup(2)
_dn = os.open(os.devnull, os.O_WRONLY)
os.dup2(_dn, 2)
os.close(_dn)
try:
    import onnxruntime
    onnxruntime.set_default_logger_severity(3)
    import chromadb
finally:
    os.dup2(_fd, 2)
    os.close(_fd)

ACTIVITY_FILE = os.path.expanduser("~/.claude/.cortex_activity")

raw = sys.argv[1] if len(sys.argv) > 1 else ""

# Parse hook input
try:
    d = json.loads(raw)
except:
    d = {}

user_prompt = d.get("prompt", "") or d.get("content", "") or raw
transcript_path = d.get("transcript_path", "")
cwd = d.get("cwd", "") or os.getcwd()

# Skip recall for automated claude -p subprocess prompts
_skip_patterns = [
    "you are a memory extraction system",
    "you identify reusable workflow patterns",
    "you evaluate and reconcile an existing fleet",
    "you are an agent architect",
    "summarize this session in one sentence",
    "extract learnings from this coding session",
    "analyze this coding session for skill",
    "analyze this coding session for specialized agent",
    "output a json array of memories",
    "output a json array of agents",
    "output only the json array",
    "consolidate these related memories",
]
_prompt_lower = user_prompt[:500].lower()
for _pat in _skip_patterns:
    if _pat in _prompt_lower:
        sys.exit(0)

# Skip very short or empty prompts
if len(user_prompt.strip()) < 5:
    sys.exit(0)

# Connect to ChromaDB (HttpClient with PersistentClient fallback)
sys.path.insert(0, os.path.expanduser("~/.claude/skills/cortex/lib"))
from chroma_client import get_client, get_collection

try:
    col = get_collection()
    if col.count() == 0:
        sys.exit(0)
except Exception:
    sys.exit(0)


# ================================================================
# Detect first message (no assistant replies in transcript yet)
# ================================================================
def is_first_message(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path):
        return True
    try:
        with open(transcript_path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except:
                    continue
                msg = entry.get('message', entry)
                if msg.get('role') != 'assistant':
                    continue
                content = msg.get('content', '')
                if isinstance(content, str) and len(content) > 10:
                    return False
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get('type') == 'text':
                            if len(part.get('text', '')) > 10:
                                return False
    except:
        pass
    return True


# ================================================================
# Detect project(s) from working directory
# ================================================================
def detect_projects(cwd):
    """Match cwd path components against known cortex project names."""
    if not cwd:
        return set()

    # Get all unique project names from DB
    all_data = col.get(include=["metadatas"])
    known_projects = set()
    for m in all_data["metadatas"]:
        p = m.get("project", "")
        if p and p != "global":
            known_projects.add(p)

    # Match: any known project name that appears as a path component
    cwd_lower = cwd.lower()
    matched = set()
    for proj in known_projects:
        if proj.lower() in cwd_lower:
            matched.add(proj)

    return matched


matched_projects = detect_projects(cwd)
first_msg = is_first_message(transcript_path)

# Detect "remember" intent globally — applies to both first and subsequent messages
remember_keywords = ["remember", "recall", "do you know", "have you seen",
                     "did we", "last time", "previously", "earlier session"]
prompt_lower = user_prompt.lower()
is_remember_query = any(kw in prompt_lower for kw in remember_keywords)


# ================================================================
# FIRST MESSAGE: Relevance-ranked context load
# ================================================================
if first_msg:
    MAX_OUTPUT = 7000  # total output cap (memories + inventory + JSON)
    INVENTORY_RESERVE = 2500  # reserve for agent/skill/doc inventory
    SUMMARY_LIMIT = 150

    # ── Step 1: Collect all candidate memories with relevance scores ──
    all_data = col.get(include=["documents", "metadatas"])

    # Score every memory by relevance (lower = more relevant)
    # Tier 0 (always include): user, feedback — behavioral rules
    # Tier 1 (high priority): project memories matching current project
    # Tier 2 (medium): references matching current project
    # Tier 3 (lower): global/untagged project+reference memories
    # Tier 4 (lowest): cross-project memories
    # Within each tier, sort by semantic distance to prompt

    candidates = []  # [(score, mid, doc, meta, tier)]

    for i in range(len(all_data["ids"])):
        mid = all_data["ids"][i]
        doc = all_data["documents"][i]
        meta = all_data["metadatas"][i]
        mtype = meta.get("type", "general")
        mproject = meta.get("project", "")

        if mtype == "agent_eval":
            continue

        if mtype in ("user", "feedback", "preferences"):
            candidates.append((0, mid, doc, meta, 0))  # tier 0: always
        elif mtype == "project" and (mproject in matched_projects):
            candidates.append((1, mid, doc, meta, 1))  # tier 1: current project
        elif mtype == "reference" and (mproject in matched_projects):
            candidates.append((2, mid, doc, meta, 2))  # tier 2: project refs
        elif mtype in ("project", "reference") and (mproject in ("", "global")):
            candidates.append((3, mid, doc, meta, 3))  # tier 3: global
        elif mtype == "reference":
            candidates.append((4, mid, doc, meta, 4))  # tier 4: other refs

    # ── Step 2: Get semantic scores for prompt-based ranking within tiers ──
    sem_distances = {}
    if len(user_prompt) >= 3:
        try:
            n_query = min(col.count(), 30 if is_remember_query else 20)
            sem_results = col.query(query_texts=[user_prompt[:400]], n_results=n_query)
            for i in range(len(sem_results["ids"][0])):
                sem_distances[sem_results["ids"][0][i]] = sem_results["distances"][0][i]
        except:
            pass

    # ── Step 3: Sort by (tier, semantic_distance) ──
    def sort_key(item):
        score, mid, doc, meta, tier = item
        dist = sem_distances.get(mid, 0.99)  # default: low relevance
        return (tier, dist)

    candidates.sort(key=sort_key)

    # ── Step 4: Fill output budget greedily ──
    proj_label = ', '.join(sorted(matched_projects)) if matched_projects else "unknown"
    lines = [f"[cortex] Session context for: {proj_label}"]

    type_labels = {"user": "user", "feedback": "rule", "preferences": "pref",
                   "project": "proj", "reference": "ref"}
    budget = MAX_OUTPUT - INVENTORY_RESERVE - 200  # reserve for inventory + header
    included_ids = []
    included_count = 0
    current_tier = -1

    tier_names = {0: "Profile & Rules", 1: "Project Context",
                  2: "Project References", 3: "Global Context", 4: "Other References"}

    for score, mid, doc, meta, tier in candidates:
        mtype = meta.get("type", "general")
        mproject = meta.get("project", "")
        proj_tag = f" [{mproject}]" if mproject and mtype in ("project", "reference") else ""
        dist = sem_distances.get(mid, None)
        relevance = f" ({100-int(dist*100)}%)" if dist is not None else ""

        summary = doc[:SUMMARY_LIMIT] + ("..." if len(doc) > SUMMARY_LIMIT else "")
        entry = f"  [{type_labels.get(mtype, mtype)}] {mid}{proj_tag}: {summary}"

        # Add tier header
        if tier != current_tier:
            header = f"\n== {tier_names.get(tier, 'Other')} =="
            if budget - len(header) - len(entry) < 0:
                break
            lines.append(header)
            budget -= len(header) + 1
            current_tier = tier

        if budget - len(entry) < 0:
            break  # budget exhausted

        lines.append(entry)
        budget -= len(entry) + 1
        included_ids.append(mid)
        included_count += 1

    # ── Step 5: Agent, skill, and doc inventory ──
    import glob as _glob

    def _scan_inventory(dirs, prefix=""):
        items = []
        for scope, d in dirs:
            if not os.path.isdir(d):
                continue
            for f in sorted(_glob.glob(os.path.join(d, "*.md"))):
                name = os.path.splitext(os.path.basename(f))[0]
                desc = ""
                try:
                    with open(f) as fh:
                        in_front = False
                        for line in fh:
                            line = line.strip()
                            if line == "---" and not in_front:
                                in_front = True; continue
                            if line == "---" and in_front:
                                break
                            if in_front and line.lower().startswith("description:"):
                                desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
                items.append(f"  [{scope}] {prefix}{name}: {desc[:80]}")
        return items

    agent_lines = _scan_inventory([
        ("project", os.path.join(cwd, ".claude", "agents")),
        ("global", os.path.expanduser("~/.claude/agents")),
    ])
    skill_lines = _scan_inventory([
        ("project", os.path.join(cwd, ".claude", "commands")),
        ("global", os.path.expanduser("~/.claude/commands")),
    ], prefix="/")
    if agent_lines:
        lines.append("\n[cortex] Agents:")
        lines.extend(agent_lines)
    if skill_lines:
        lines.append("\n[cortex] Skills:")
        lines.extend(skill_lines)

    skipped = len(candidates) - included_count
    if skipped > 0:
        lines.append(f"\n[cortex] Showing {included_count}/{included_count + skipped} memories (ranked by relevance). Use mcp__cortex__memory_search for more.")

    if included_count > 0 or agent_lines:
        with open(ACTIVITY_FILE, "w") as af:
            af.write(f"loaded {included_count} (session start)")

        # Log recalled IDs
        try:
            recall_log = os.path.expanduser("~/.claude/.cortex_recall_log")
            with open(recall_log, "a") as rl:
                rl.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {','.join(included_ids)}\n")
        except Exception:
            pass

        context_text = '\n'.join(lines)
        output = json.dumps({
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context_text
            }
        })
        print(output)


# ================================================================
# SUBSEQUENT MESSAGES: Semantic search with project boost
# ================================================================
else:
    if len(user_prompt.strip()) < 20 and not is_remember_query:
        sys.exit(0)  # Skip trivial messages like "ok", "yes", "thanks", "do it"

    # Build richer query with assistant context
    assistant_context = ""
    if transcript_path and os.path.exists(transcript_path):
        try:
            last_assistant_msgs = []
            with open(transcript_path, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                    except:
                        continue
                    msg = entry.get('message', entry)
                    if msg.get('role') != 'assistant':
                        continue
                    raw_content = msg.get('content', '')
                    text = ''
                    if isinstance(raw_content, str):
                        text = raw_content.strip()
                    elif isinstance(raw_content, list):
                        for part in raw_content:
                            if isinstance(part, dict) and part.get('type') == 'text':
                                t = part.get('text', '').strip()
                                if t:
                                    text += t + ' '
                    text = text.strip()
                    if len(text) > 20:
                        last_assistant_msgs.append(text[:300])
            if last_assistant_msgs:
                assistant_context = ' '.join(last_assistant_msgs[-2:])[:500]
        except:
            pass

    search_query = user_prompt[:400]
    if assistant_context:
        search_query = f"{user_prompt[:300]} {assistant_context[:200]}"

    # ── LLM query expansion via claude -p (DISABLED) ───────────────
    # BUG: claude -p returns empty stdout on v2.1.83 despite generating
    # tokens (output_tokens > 0, result: ""). Filed as:
    #   https://github.com/anthropics/claude-code/issues/38774
    # TODO: Re-enable when the bug is fixed. Test with:
    #   echo "say hello" | claude -p --model haiku --max-turns 1
    # If that produces output, uncomment the block below.
    expanded_query = ""
    # try:
    #     import subprocess as _sp
    #     _expand_prompt = (
    #         "Extract 5-10 search keywords/phrases that would help find "
    #         "stored memories about tools, credentials, APIs, config, or "
    #         "project context needed to fulfil this request. "
    #         "Return ONLY the keywords, comma-separated, nothing else.\n\n"
    #         f"User message: {user_prompt[:300]}\n"
    #     )
    #     if assistant_context:
    #         _expand_prompt += f"Recent conversation context: {assistant_context[:200]}\n"
    #     _proc = _sp.run(
    #         ["claude", "-p", "--model", "haiku", "--max-turns", "1"],
    #         input=_expand_prompt, capture_output=True, text=True, timeout=4
    #     )
    #     if _proc.returncode == 0 and _proc.stdout.strip():
    #         expanded_query = _proc.stdout.strip()[:300]
    # except Exception:
    #     pass  # Timeout or missing claude — fall back to regular search

    # ── Hybrid retrieval (dense + BM25 + cross-encoder rerank) ─────
    # Adds keyword recall + reranking on top of dense search. Falls back to the
    # dense-only path on any error; disable with CORTEX_RECALL_HYBRID=0.
    n_results = min(10 if is_remember_query else 5, col.count())
    relevant = None

    if os.environ.get("CORTEX_RECALL_HYBRID", "1") != "0":
        try:
            from retrieval import hybrid_search, rerank_relevance_pct
            hits = hybrid_search(col, search_query, n_results=max(n_results, 8))
            relevant = []
            for h in hits:
                meta = h.get("metadata") or {}
                mem_type = meta.get("type", "general")
                if mem_type == "agent_eval":
                    continue
                mem_project = meta.get("project", "")
                pct = rerank_relevance_pct(h.get("rerank_score"))
                hdist = h.get("distance")
                # Rerank-relevance gate (relevant hits score ~90%+, noise <10%).
                # Floors map to logits >= ~0 so weak-negative rerank scores drop.
                if pct is not None:
                    if is_remember_query:
                        floor = 30 if mem_project in matched_projects else 40
                    else:
                        floor = 40 if mem_project in matched_projects else 50
                    if pct < floor:
                        continue
                elif hdist is not None:
                    # Reranker unavailable — gate on cosine distance like the dense path.
                    if is_remember_query:
                        thr = 0.55 if mem_project in matched_projects else 0.48
                    else:
                        thr = 0.45 if mem_project in matched_projects else 0.40
                    if hdist >= thr:
                        continue
                relevant.append({
                    "id": h["id"],
                    "content": (h.get("document") or "")[:400],
                    "type": mem_type,
                    "project": mem_project,
                    "distance": round(h["distance"], 3) if h.get("distance") is not None else None,
                })
                if len(relevant) >= n_results:
                    break
        except Exception:
            relevant = None  # fall back to dense-only below

    if relevant is None:
        # ── Fallback: dense-only ChromaDB search + distance gate ──────
        results = col.query(query_texts=[search_query], n_results=n_results)
        best = {}  # mid → (dist, index)
        for i in range(len(results["ids"][0])):
            mid = results["ids"][0][i]
            dist = results["distances"][0][i] if results.get("distances") else 1.0
            if mid not in best or dist < best[mid][0]:
                best[mid] = (dist, i)

        relevant = []
        for mid, (dist, i) in best.items():
            meta = results["metadatas"][0][i]
            mem_type = meta.get("type", "general")
            mem_project = meta.get("project", "")
            if mem_type == "agent_eval":
                continue
            # Recall boost: frequently recalled memories get a distance discount.
            recall_count = int(meta.get("recall_count", "0") or "0")
            if recall_count >= 5:
                dist *= 0.85
            elif recall_count >= 2:
                dist *= 0.92
            # gte-modernbert distance gates (tighter than the old MiniLM model).
            if is_remember_query:
                threshold = 0.55 if mem_project in matched_projects else 0.48
            else:
                threshold = 0.45 if mem_project in matched_projects else 0.40
            if dist < threshold:
                relevant.append({
                    "id": mid,
                    "content": results["documents"][0][i][:400],
                    "type": mem_type,
                    "project": mem_project,
                    "distance": round(dist, 3)
                })

    if relevant:
        with open(ACTIVITY_FILE, "w") as af:
            af.write(f"recalled {len(relevant)}")

        # Log recalled IDs for hygiene tracking
        try:
            recall_log = os.path.expanduser("~/.claude/.cortex_recall_log")
            recalled_ids = [r["id"] for r in relevant]
            with open(recall_log, "a") as rl:
                rl.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {','.join(recalled_ids)}\n")
        except Exception:
            pass

        context_lines = ["[cortex] Recalled memories relevant to this message:"]
        for r in relevant:
            proj_tag = f" [{r['project']}]" if r.get('project') else ""
            context_lines.append(
                f"  [{r['type']}] {r['id']}{proj_tag}: {r['content']}"
            )

        context_text = '\n'.join(context_lines)
        output = json.dumps({
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context_text
            }
        })
        print(output)

PYEOF
