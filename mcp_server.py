#!/usr/bin/env python3
"""Cortex MCP server — always-on memory tools for Claude Code.

Exposes memory_store, memory_search, memory_get, memory_list, memory_delete,
memory_update, memory_merge, memory_stats as native Claude Code tools via the
MCP protocol (stdio transport).

Safety guardrails:
  - Content size limit: 5000 chars per memory
  - Soft-delete: deleted memories archived to audit log before removal
  - Audit trail: all store/update/delete operations logged to .cortex_audit.jsonl
  - Total DB cap: unlimited (set MAX_TOTAL_MEMORIES > 0 to enforce a limit)
"""

import hashlib
import json
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys_path = os.path.dirname(os.path.abspath(__file__))
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)
from lib.chroma_client import get_client, get_collection as _chroma_get_collection
from lib import retrieval

from mcp.server.fastmcp import FastMCP

AUDIT_LOG = str(Path.home() / ".claude" / ".cortex_audit.jsonl")
RECALL_LOG = str(Path.home() / ".claude" / ".cortex_recall_log")

MAX_CONTENT_LENGTH = 5000
MAX_TOTAL_MEMORIES = 0  # 0 = unlimited
def _prefix():
    return "❖ cortex ›"
RECALL_LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB — truncate to last 7 days when exceeded
AUDIT_ROTATION_INTERVAL = 86400  # Check at most once per day (seconds)
AUDIT_RETENTION_DAYS = 90

mcp = FastMCP("cortex", log_level="ERROR")

# Singleton ChromaDB client — reused across all tool calls (MCP server is long-lived)
_chroma_client = None
_chroma_collection = None
_last_audit_rotation_check = 0


def get_collection():
    global _chroma_client, _chroma_collection
    if _chroma_collection is not None:
        try:
            _chroma_collection.count()  # Lightweight liveness check
            return _chroma_collection
        except Exception:
            _chroma_client = None
            _chroma_collection = None

    _chroma_collection = _chroma_get_collection()
    return _chroma_collection


def _open_restricted_append(path):
    """Open file for append with 600 permissions (owner-only read/write)."""
    import stat
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
    return os.fdopen(fd, "a")


def audit_log_write(action, memory_id, content_hash="", metadata=None, reason=""):
    """Append an entry to the audit log."""
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
            "memory_id": memory_id,
            "content_hash": content_hash,
            "reason": reason,
        }
        if metadata:
            entry["metadata"] = {k: v for k, v in metadata.items()
                                 if k in ("type", "project", "tags")}
        with _open_restricted_append(AUDIT_LOG) as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _track_recalls(collection, result_ids):
    """Update recall_count and last_recalled in ChromaDB, and append to recall log."""
    if not result_ids:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 1. Update ChromaDB metadata inline (real-time recall tracking)
    try:
        existing = collection.get(ids=result_ids)
        for i, mid in enumerate(existing["ids"]):
            meta = dict(existing["metadatas"][i])
            meta["recall_count"] = str(int(meta.get("recall_count", "0") or "0") + 1)
            meta["last_recalled"] = now
            collection.update(ids=[mid], metadatas=[meta])
    except Exception:
        pass

    # 2. Append to recall log for recall.sh/memory_hygiene.py compatibility
    try:
        with open(RECALL_LOG, "a") as f:
            f.write(f"{now} {','.join(result_ids)}\n")
    except Exception:
        pass

    # 3. Truncate recall log if too large
    _maybe_truncate_recall_log()


def _maybe_truncate_recall_log():
    """If recall log exceeds 5MB, truncate to last 7 days."""
    try:
        if not os.path.exists(RECALL_LOG):
            return
        if os.path.getsize(RECALL_LOG) < RECALL_LOG_MAX_SIZE:
            return

        cutoff = time.strftime(
            "%Y-%m-%d",
            time.localtime(time.time() - 7 * 86400)
        )
        kept = []
        with open(RECALL_LOG, "r") as f:
            for line in f:
                if line[:10] >= cutoff:
                    kept.append(line)

        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(RECALL_LOG))
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.writelines(kept)
            os.replace(tmp_path, RECALL_LOG)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


def _maybe_rotate_audit_log():
    """Rotate audit log if not checked recently. Keeps last 90 days."""
    global _last_audit_rotation_check
    now = time.time()
    if now - _last_audit_rotation_check < AUDIT_ROTATION_INTERVAL:
        return
    _last_audit_rotation_check = now

    try:
        if not os.path.exists(AUDIT_LOG):
            return
        if os.path.getsize(AUDIT_LOG) < 50_000:  # Only bother if > 50KB
            return

        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(now - AUDIT_RETENTION_DAYS * 86400)
        )
        kept = []
        with open(AUDIT_LOG, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", "") >= cutoff:
                        kept.append(line)
                except Exception:
                    kept.append(line)  # Keep unparseable lines

        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(AUDIT_LOG))
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write("\n".join(kept) + "\n" if kept else "")
            os.replace(tmp_path, AUDIT_LOG)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


# Cosine distance below this = near-duplicate. Calibrated for the gte-modernbert
# embedder (2026-07-20): distinct-but-related memories sit as close as 0.075, and
# 16% of the DB has a distinct neighbour within 0.15 — so the old 0.15 would
# false-block. 0.06 blocks only near-identical re-stores (>94% similar).
DEDUP_DISTANCE_THRESHOLD = 0.06


@mcp.tool()
def memory_store(
    content: str,
    memory_type: str = "general",
    memory_id: str = "",
    project: str = "",
    tags: str = "",
) -> str:
    """Store a memory in the vector database with semantic embedding.

    Automatically checks for near-duplicates before storing. If a near-identical
    memory exists (>~94% similarity), returns a warning with the existing ID
    so you can update it instead.

    Args:
        content: The memory text to store (max 5000 chars)
        memory_type: Category — user, feedback, preferences, project, reference, or general
        memory_id: Custom ID (auto-generated if empty). Use descriptive IDs like 'pref_dark_mode'
        project: Project name for scoping (empty = global)
        tags: Comma-separated tags for organization
    """
    # Content size limit
    if len(content) > MAX_CONTENT_LENGTH:
        return f"{_prefix()} Error: Content too long ({len(content)} chars). Max {MAX_CONTENT_LENGTH}."

    if len(content) < 10:
        return f"{_prefix()} Error: Content too short (min 10 chars)."

    # Validate memory_id format
    if memory_id and (len(memory_id) > 200 or '\n' in memory_id or '\r' in memory_id):
        return f"{_prefix()} Error: Invalid memory_id (max 200 chars, no newlines)."

    collection = get_collection()

    # Total DB cap (only check on new memories, not updates)
    mem_id = memory_id or f"mem_{int(time.time() * 1000)}"
    existing = collection.get(ids=[mem_id])
    is_update = bool(existing["ids"])

    if MAX_TOTAL_MEMORIES and not is_update and collection.count() >= MAX_TOTAL_MEMORIES:
        return f"{_prefix()} Error: Database full ({MAX_TOTAL_MEMORIES}). Delete old memories first."

    # Dedup check — find near-duplicates before storing (skip if updating same ID)
    if not is_update and collection.count() > 0:
        try:
            dupes = collection.query(
                query_texts=[content[:1000]],
                n_results=min(3, collection.count()),
            )
            for i, dist in enumerate(dupes["distances"][0]):
                if dist < DEDUP_DISTANCE_THRESHOLD:
                    dupe_id = dupes["ids"][0][i]
                    if dupe_id == mem_id:
                        continue
                    similarity = round((1 - dist) * 100, 1)
                    dupe_preview = dupes["documents"][0][i][:150]
                    return (
                        f"{_prefix()} Near-duplicate found ({similarity}% similar):\n"
                        f"  Existing: {dupe_id}\n"
                        f"  Content: {dupe_preview}...\n"
                        f"  → Use memory_update(memory_id=\"{dupe_id}\", ...) to update it, "
                        f"or use a unique memory_id to force store."
                    )
        except Exception:
            pass  # Dedup is best-effort, don't block store on errors

    metadata = {"type": memory_type, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if project:
        metadata["project"] = project
    if tags:
        metadata["tags"] = tags

    collection.upsert(ids=[mem_id], documents=[content], metadatas=[metadata])

    audit_log_write("store" if not is_update else "update_via_store", mem_id,
                    content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
                    metadata=metadata)

    meta_parts = [f"type={memory_type}"]
    if project:
        meta_parts.append(f"project={project}")
    if tags:
        meta_parts.append(f"tags={tags}")
    return f"{_prefix()} Stored: {mem_id} ({', '.join(meta_parts)})"


@mcp.tool()
def memory_search(
    query: str,
    n: int = 5,
    memory_type: str = "",
    project: str = "",
    full: bool = False,
) -> str:
    """Semantic search across all stored memories. Returns the most similar matches.

    Each result shows its exact ID (the slug) — pass that ID to memory_get to
    pull the complete, untruncated memory in a single follow-up call.

    Args:
        query: Natural language search query
        n: Number of results to return (default 5, max 20)
        memory_type: Filter by type (empty = all)
        project: Filter by project (empty = all)
        full: If True, print each result's COMPLETE content instead of a 200-char
              preview (avoids a follow-up memory_get when you already know the top
              hit is the one you want). Keep the default False for broad scans.
    """
    collection = get_collection()
    if collection.count() == 0:
        return f"{_prefix()} No memories stored yet."

    # Cap results to prevent excessive output
    n = min(n, 20)

    conds = []
    if memory_type:
        conds.append({"type": memory_type})
    if project:
        conds.append({"project": project})
    where = conds[0] if len(conds) == 1 else ({"$and": conds} if conds else None)

    # Hybrid retrieval: dense (gte-modernbert) + BM25 keyword, fused via RRF and
    # reranked by a cross-encoder. Query truncated to prevent abuse.
    hits = retrieval.hybrid_search(collection, query[:1000], n_results=n, where=where)

    output = []
    for h in hits:
        output.append({
            "id": h["id"],
            "content": h["document"] or "",
            "metadata": h["metadata"] or {},
            "distance": h.get("distance"),
            "rerank_score": h.get("rerank_score"),
        })

    # Track recalls — update metadata + append to recall log
    if output:
        _track_recalls(collection, [r["id"] for r in output])

    lines = [f"{_prefix()} Found {len(output)} result(s) ({collection.count()} total in DB):\n"]
    # When full=True, bound total content so a wide search can't recreate the
    # token-overflow / file-spill failure this flag exists to avoid. Past the
    # budget, remaining hits fall back to preview + a pointer to memory_get.
    FULL_OUTPUT_BUDGET = 30000
    full_chars = 0
    full_truncated = 0
    for r in output:
        meta = r["metadata"]
        mtype = meta.get("type", "general")
        proj = meta.get("project", "")
        proj_tag = f" [{proj}]" if proj else ""
        rr = r.get("rerank_score")
        if rr is not None:
            sim_str = f" ({retrieval.rerank_relevance_pct(rr)}% relevant)"
        elif r.get("distance") is not None:
            similarity = round((1 - r["distance"]) * 100, 1)
            sim_str = f" ({similarity}% similar)"
        else:
            sim_str = ""
        lines.append(f"  [{mtype}] {r['id']}{proj_tag}{sim_str}")
        if full and full_chars < FULL_OUTPUT_BUDGET:
            full_chars += len(r["content"])
            lines.append(f"    {r['content']}")
        elif full:
            full_truncated += 1
            lines.append(f"    {r['content'][:200]}  … [truncated — memory_get(\"{r['id']}\") for full text]")
        else:
            lines.append(f"    {r['content'][:200]}")
        lines.append("")
    if full_truncated:
        lines.append(
            f"  ({full_truncated} result(s) shown as preview to stay within the output budget; "
            f"fetch any in full with memory_get)"
        )
    return "\n".join(lines)


@mcp.tool()
def memory_get(memory_id: str) -> str:
    """Fetch ONE memory in FULL — complete untruncated content plus metadata — by its exact ID.

    Use this immediately after memory_search or memory_list surfaces the right
    memory but shows only a truncated preview. It returns the whole stored text
    in a single call, so you never have to fall back to listing the DB or reading
    the ChromaDB file directly.

    Args:
        memory_id: The exact ID (the slug shown by memory_search / memory_list)
    """
    collection = get_collection()
    try:
        result = collection.get(ids=[memory_id])
    except Exception as e:
        return f"{_prefix()} Error: {e}"
    if not result["ids"]:
        return f"{_prefix()} Not found: {memory_id}"

    # Guard against ChromaDB rows stored with a None document/metadata element
    doc = (result["documents"][0] if result["documents"] else "") or ""
    meta = (result["metadatas"][0] if result["metadatas"] else {}) or {}

    # Intentionally a PURE READ — no _track_recalls here. A get-by-id is always a
    # follow-up to a search that already counted the recall, so tracking again
    # would double-count and add a read-modify-write clobber risk on the metadata.

    mtype = meta.get("type", "general")
    proj = meta.get("project", "")
    proj_tag = f" [{proj}]" if proj else ""
    tags = meta.get("tags", "")

    lines = [f"{_prefix()} [{mtype}] {memory_id}{proj_tag} ({len(doc)} chars)"]
    if tags:
        lines.append(f"    tags: {tags}")
    lines.append("")
    lines.append(doc)
    return "\n".join(lines)


@mcp.tool()
def memory_list(memory_type: str = "", project: str = "") -> str:
    """List all stored memories, optionally filtered by type or project.

    Args:
        memory_type: Filter by type (empty = all)
        project: Filter by project (empty = all)
    """
    collection = get_collection()
    if collection.count() == 0:
        return f"{_prefix()} No memories stored."

    where = {}
    if memory_type:
        where["type"] = memory_type
    if project:
        where["project"] = project

    data = collection.get(where=where if where else None)
    total = len(data["ids"])
    lines = [f"{_prefix()} {total} memor{'y' if total == 1 else 'ies'}:\n"]
    for i in range(total):
        meta = data["metadatas"][i]
        mtype = meta.get("type", "general")
        proj = meta.get("project", "")
        proj_tag = f" [{proj}]" if proj else ""
        doc = data["documents"][i]
        preview = doc[:200] + ("..." if len(doc) > 200 else "")
        lines.append(f"  [{mtype}] {data['ids'][i]}{proj_tag}")
        lines.append(f"    {preview}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def memory_delete(memory_id: str) -> str:
    """Delete a memory by its ID. The memory content is archived to the audit log before deletion.

    Args:
        memory_id: The ID of the memory to delete
    """
    collection = get_collection()
    try:
        existing = collection.get(ids=[memory_id])
        if not existing["ids"]:
            return f"{_prefix()} Not found: {memory_id}"

        # Archive to audit log before deletion
        content = existing["documents"][0] if existing["documents"] else ""
        metadata = existing["metadatas"][0] if existing["metadatas"] else {}
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # Write full content to audit log for recovery
        audit_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": "delete",
            "memory_id": memory_id,
            "content_hash": content_hash,
            "content_backup": content[:2000],  # Backup up to 2000 chars
            "metadata": metadata,
        }
        try:
            with open(AUDIT_LOG, "a") as f:
                f.write(json.dumps(audit_entry) + "\n")
        except Exception:
            pass

        collection.delete(ids=[memory_id])
        return f"{_prefix()} Deleted: {memory_id} (archived to audit log)"
    except Exception as e:
        return f"{_prefix()} Error: {e}"


@mcp.tool()
def memory_update(memory_id: str, content: str = "", memory_type: str = "", tags: str = "", project: str = "", mode: str = "replace") -> str:
    """Update an existing memory's content or metadata.

    Args:
        memory_id: The ID of the memory to update
        content: New content (empty = keep existing, max 5000 chars)
        memory_type: New type (empty = keep existing)
        tags: New tags (empty = keep existing). Prefix with '+' to append (e.g. '+newtag,other') instead of replacing all tags
        project: New project scope (empty = keep existing)
        mode: Content update mode — 'replace' (default, overwrites), 'append' (adds after existing), 'prepend' (adds before existing)
    """
    if content and mode == "replace" and len(content) > MAX_CONTENT_LENGTH:
        return f"{_prefix()} Error: Content too long ({len(content)} chars). Max {MAX_CONTENT_LENGTH}."

    collection = get_collection()
    existing = collection.get(ids=[memory_id])
    if not existing["ids"]:
        return f"{_prefix()} Not found: {memory_id}"

    old_content = existing["documents"][0] if existing["documents"] else ""

    # Audit log the update (old content hash for diffing)
    audit_log_write("update", memory_id,
                    content_hash=hashlib.sha256(old_content.encode()).hexdigest()[:16],
                    metadata=existing["metadatas"][0] if existing["metadatas"] else None,
                    reason=f"content_{mode}" if content else "metadata_only")

    metadata = existing["metadatas"][0]
    metadata["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if memory_type:
        metadata["type"] = memory_type

    # Tag handling: '+tag1,tag2' appends, otherwise replaces
    if tags:
        if tags.startswith("+"):
            existing_tags = set(t.strip() for t in (metadata.get("tags", "") or "").split(",") if t.strip())
            new_tags = set(t.strip() for t in tags[1:].split(",") if t.strip())
            metadata["tags"] = ",".join(sorted(existing_tags | new_tags))
        else:
            metadata["tags"] = tags

    if project:
        metadata["project"] = project

    # Content mode handling
    if content:
        if mode == "append":
            doc = old_content.rstrip() + "\n\n" + content
        elif mode == "prepend":
            doc = content + "\n\n" + old_content.lstrip()
        else:
            doc = content

        if len(doc) > MAX_CONTENT_LENGTH:
            return f"{_prefix()} Error: Combined content too long ({len(doc)} chars). Max {MAX_CONTENT_LENGTH}."
    else:
        doc = old_content

    collection.update(ids=[memory_id], documents=[doc], metadatas=[metadata])

    changed = []
    if content:
        changed.append(f"content ({mode})")
    if memory_type:
        changed.append(f"type={memory_type}")
    if tags:
        changed.append(f"tags={metadata.get('tags', '')}")
    if project:
        changed.append(f"project={project}")
    return f"{_prefix()} Updated: {memory_id} ({', '.join(changed) if changed else 'metadata timestamp'})"


@mcp.tool()
def memory_merge(memory_ids: str, new_id: str = "", new_content: str = "") -> str:
    """Merge multiple related memories into one consolidated memory.

    Combines content from 2+ memories, preserves tags from all sources,
    keeps the most specific project scope, and deletes the originals.

    Args:
        memory_ids: Comma-separated IDs of memories to merge (e.g. "mem_a,mem_b,mem_c")
        new_id: ID for the merged memory (default: first source ID)
        new_content: Merged content. If empty, concatenates all source contents with separators
    """
    ids = [mid.strip() for mid in memory_ids.split(",") if mid.strip()]
    if len(ids) < 2:
        return f"{_prefix()} Error: Need at least 2 memory IDs to merge."

    collection = get_collection()

    # Fetch all source memories
    sources = []
    for mid in ids:
        result = collection.get(ids=[mid])
        if not result["ids"]:
            return f"{_prefix()} Error: Memory '{mid}' not found."
        sources.append({
            "id": mid,
            "content": result["documents"][0],
            "metadata": result["metadatas"][0],
        })

    # Determine merged metadata
    all_tags = set()
    all_projects = set()
    memory_type = sources[0]["metadata"].get("type", "general")
    for s in sources:
        meta = s["metadata"]
        tags_str = meta.get("tags", "")
        if tags_str:
            all_tags.update(t.strip() for t in tags_str.split(",") if t.strip())
        proj = meta.get("project", "")
        if proj:
            all_projects.add(proj)
        # Use most specific type (preferences > feedback > project > reference > user > general)
        stype = meta.get("type", "general")
        if stype == memory_type:
            continue
        # Keep the type from the first source unless overridden

    # Build merged content
    if new_content:
        merged_content = new_content
    else:
        parts = []
        for s in sources:
            parts.append(s["content"].strip())
        merged_content = "\n\n".join(parts)

    if len(merged_content) > MAX_CONTENT_LENGTH:
        return (
            f"{_prefix()} Error: Merged content too long ({len(merged_content)} chars). "
            f"Max {MAX_CONTENT_LENGTH}. Provide condensed new_content."
        )

    merged_id = new_id or sources[0]["id"]
    merged_project = list(all_projects)[0] if len(all_projects) == 1 else (
        ",".join(sorted(all_projects)) if all_projects else ""
    )

    merged_metadata = {
        "type": memory_type,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "merged_from": ",".join(ids),
    }
    if all_tags:
        merged_metadata["tags"] = ",".join(sorted(all_tags))
    if merged_project:
        merged_metadata["project"] = merged_project

    # Audit log: record merge
    for s in sources:
        audit_log_write("merge_source", s["id"],
                        content_hash=hashlib.sha256(s["content"].encode()).hexdigest()[:16],
                        metadata=s["metadata"],
                        reason=f"merged_into:{merged_id}")

    # Delete originals (except the one being reused as merged_id)
    for s in sources:
        if s["id"] != merged_id:
            collection.delete(ids=[s["id"]])

    # Upsert the merged memory
    collection.upsert(ids=[merged_id], documents=[merged_content], metadatas=[merged_metadata])

    audit_log_write("merge_result", merged_id,
                    content_hash=hashlib.sha256(merged_content.encode()).hexdigest()[:16],
                    metadata=merged_metadata,
                    reason=f"merged_{len(ids)}_memories")

    return (
        f"{_prefix()} Merged {len(ids)} memories into: {merged_id}\n"
        f"  Sources: {', '.join(ids)}\n"
        f"  Tags: {merged_metadata.get('tags', 'none')}\n"
        f"  Content: {len(merged_content)} chars"
    )


@mcp.tool()
def memory_stats() -> str:
    """Show statistics about the memory database — total count, breakdown by type and project."""
    _maybe_rotate_audit_log()
    collection = get_collection()
    total = collection.count()
    if total == 0:
        return f"{_prefix()} No memories stored."

    data = collection.get()
    types = {}
    projects = {}
    for m in data["metadatas"]:
        t = m.get("type", "general")
        types[t] = types.get(t, 0) + 1
        p = m.get("project", "global")
        projects[p] = projects.get(p, 0) + 1

    lines = [f"{_prefix()} Total: {total} memories"]
    lines.append("By type: " + ", ".join(f"{t} ({c})" for t, c in sorted(types.items())))
    lines.append("By project: " + ", ".join(f"{p} ({c})" for p, c in sorted(projects.items())))
    return "\n".join(lines)


# ================================================================
# MCP Resources — @memory:// references
# ================================================================

@mcp.resource("memory://all", name="all-memories", description="All stored memories — use @memory://all to pull into context")
def resource_all_memories() -> str:
    """List all memories with type, project, and content preview."""
    collection = get_collection()
    if collection.count() == 0:
        return "No memories stored."
    data = collection.get(include=["documents", "metadatas"])
    lines = []
    for i in range(len(data["ids"])):
        meta = data["metadatas"][i]
        mtype = meta.get("type", "general")
        project = meta.get("project", "")
        proj_tag = f" [{project}]" if project else ""
        lines.append(f"[{mtype}] {data['ids'][i]}{proj_tag}: {data['documents'][i][:300]}")
    return "\n".join(lines)


@mcp.resource("memory://{memory_id}", name="memory-by-id", description="Fetch a specific memory by ID")
def resource_memory_by_id(memory_id: str) -> str:
    """Get full content and metadata for a specific memory."""
    collection = get_collection()
    result = collection.get(ids=[memory_id])
    if not result["ids"]:
        return f"Memory '{memory_id}' not found."
    doc = result["documents"][0]
    meta = result["metadatas"][0]
    return json.dumps({"id": memory_id, "content": doc, "metadata": meta}, indent=2)


@mcp.resource("memory://project/{project_name}", name="project-memories", description="All memories for a specific project")
def resource_project_memories(project_name: str) -> str:
    """Get all memories scoped to a project."""
    collection = get_collection()
    try:
        data = collection.get(where={"project": project_name}, include=["documents", "metadatas"])
    except Exception:
        return f"No memories for project '{project_name}'."
    if not data["ids"]:
        return f"No memories for project '{project_name}'."
    lines = [f"Memories for project: {project_name} ({len(data['ids'])} total)\n"]
    for i in range(len(data["ids"])):
        meta = data["metadatas"][i]
        mtype = meta.get("type", "?")
        lines.append(f"[{mtype}] {data['ids'][i]}: {data['documents'][i][:300]}")
    return "\n".join(lines)


@mcp.resource("memory://type/{memory_type}", name="typed-memories", description="All memories of a specific type (user, feedback, project, reference)")
def resource_typed_memories(memory_type: str) -> str:
    """Get all memories filtered by type."""
    collection = get_collection()
    try:
        data = collection.get(where={"type": memory_type}, include=["documents", "metadatas"])
    except Exception:
        return f"No memories of type '{memory_type}'."
    if not data["ids"]:
        return f"No memories of type '{memory_type}'."
    lines = [f"Memories of type: {memory_type} ({len(data['ids'])} total)\n"]
    for i in range(len(data["ids"])):
        meta = data["metadatas"][i]
        proj = meta.get("project", "")
        proj_tag = f" [{proj}]" if proj else ""
        lines.append(f"{data['ids'][i]}{proj_tag}: {data['documents'][i][:300]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
