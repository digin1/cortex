"""Cortex reminders — pending obligations, as distinct from remembered facts.

A MEMORY is a timeless fact ("the adrenal gland has a cortex"), injected as
background context. A REMINDER is something still owed ("read off_topic_by_profile
after the 03:00 run"), and it interrupts. They are different things and they live in
different collections, deliberately:

  * memory recall returns facts; if reminders shared that collection, a search for
    "librarian ingest" would hand back an outstanding task as though it were knowledge.
  * memory hygiene dedups and MERGES near-identical entries. Merging two reminders is
    never right -- they are two obligations, not one fact stated twice.

Reminders surface only inside a session (no cron, no email, by decision): the due list
rides the first prompt of a session, and relevance-matched ones ride any prompt whose
subject is close enough. Nothing fires while nobody is here.
"""
import os
import re
import time
import datetime

try:
    from lib.chroma_client import get_collection as _chroma_get_collection
    from lib.chroma_client import LEGACY_COLLECTION
except ImportError:                      # hooks put lib/ itself on the path
    from chroma_client import get_collection as _chroma_get_collection
    from chroma_client import LEGACY_COLLECTION

COLLECTION = os.environ.get("CORTEX_REMINDERS_COLLECTION", "cortex_reminders")

# Reminders INTERRUPT, so they must clear a higher bar than memories, which merely
# inform. Memory recall admits 0.40-0.55; anything that loose here would put an
# unrelated task in front of the user several times an hour.
RELEVANCE_THRESHOLD = 0.35
MAX_SURFACED = 5                 # per prompt, due + relevant combined
COOLDOWN_SECONDS = 45 * 60       # same reminder not re-surfaced within this window
DEMOTE_AFTER = 3                 # see _is_demoted


class ReminderStoreUnavailable(RuntimeError):
    """Raised rather than writing reminders into the wrong collection."""


def get_reminders_collection():
    """The reminders collection, or refuse.

    chroma_client.get_collection degrades to the LEGACY MEMORIES collection when
    embeddings are unavailable. For memory reads that is a sensible downgrade; here it
    would silently write reminders into the memory store, where they would come back
    as recalled 'facts' and be eligible for merging. Better to fail closed.
    """
    col = _chroma_get_collection(COLLECTION)
    if getattr(col, "name", None) != COLLECTION:
        raise ReminderStoreUnavailable(
            f"embeddings unavailable: got '{getattr(col, 'name', '?')}' instead of "
            f"'{COLLECTION}'. Refusing to write reminders into the memory store.")
    return col


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def parse_when(text):
    """Accept the shapes a person actually types. Returns an ISO string, or "".

    "" means no due date -- a relevance-only reminder, which is the common case:
    "whenever we touch the librarian again, the three papers are still in there."
    """
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    low = s.lower()
    now = datetime.datetime.now()

    m = re.fullmatch(r"\+\s*(\d+)\s*([mhdw])", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": datetime.timedelta(minutes=n), "h": datetime.timedelta(hours=n),
                 "d": datetime.timedelta(days=n), "w": datetime.timedelta(weeks=n)}[unit]
        return (now + delta).strftime("%Y-%m-%dT%H:%M:%S")

    if low in ("now", "today"):
        return now.strftime("%Y-%m-%dT%H:%M:%S")
    if low == "tomorrow":
        return (now + datetime.timedelta(days=1)).strftime("%Y-%m-%dT09:00:00")

    # "tomorrow 03:05" / "tomorrow 3am"
    m = re.fullmatch(r"tomorrow\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", low)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = m.group(3)
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        d = now + datetime.timedelta(days=1)
        return d.strftime(f"%Y-%m-%dT{hh:02d}:{mm:02d}:00")

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return ""                        # unparseable -> treat as relevance-only, never guess


def _meta(text_id, **kw):
    base = {"status": "pending", "created": now_iso(), "due": "", "project": "",
            "verify": "", "links": "", "tags": "", "expires_at": "", "snooze_until": "",
            "last_surfaced": "", "done_at": "", "surfaced_count": 0}
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


def add(text, due="", project="", verify="", links="", tags="", expires="",
        reminder_id=""):
    col = get_reminders_collection()
    rid = reminder_id or f"rem_{int(time.time() * 1000)}"
    meta = _meta(rid, due=parse_when(due), project=project or "", verify=verify or "",
                 links=links or "", tags=tags or "", expires_at=parse_when(expires))
    col.upsert(ids=[rid], documents=[text], metadatas=[meta])
    return {"id": rid, "text": text, **meta}


def _rows(status=None, project=None):
    col = get_reminders_collection()
    try:
        data = col.get(include=["documents", "metadatas"])
    except Exception:
        return []
    out = []
    for i, rid in enumerate(data.get("ids", [])):
        m = dict(data["metadatas"][i] or {})
        if status and m.get("status") != status:
            continue
        if project is not None and m.get("project", "") != project:
            continue
        out.append({"id": rid, "text": data["documents"][i], **m})
    return out


def list_all(status="pending", project=None):
    rows = _rows(status=status or None, project=project)
    rows.sort(key=lambda r: (r.get("due") or "9999", r.get("created") or ""))
    return rows


def set_status(reminder_id, status):
    col = get_reminders_collection()
    got = col.get(ids=[reminder_id], include=["metadatas"])
    if not got.get("ids"):
        return None
    m = dict(got["metadatas"][0] or {})
    m["status"] = status
    m["done_at"] = now_iso()
    col.update(ids=[reminder_id], metadatas=[m])
    return {"id": reminder_id, **m}


def snooze(reminder_id, until):
    col = get_reminders_collection()
    got = col.get(ids=[reminder_id], include=["metadatas"])
    if not got.get("ids"):
        return None
    m = dict(got["metadatas"][0] or {})
    m["snooze_until"] = parse_when(until) or ""
    col.update(ids=[reminder_id], metadatas=[m])
    return {"id": reminder_id, **m}


def drop(reminder_id):
    col = get_reminders_collection()
    if not col.get(ids=[reminder_id]).get("ids"):
        return None
    col.delete(ids=[reminder_id])
    return {"id": reminder_id, "status": "deleted"}


def _is_demoted(row):
    """A reminder raised repeatedly and never acted on has stopped being useful.

    Rather than keep interrupting -- which teaches the reader to skim past the whole
    reminder block, and so costs the NEXT reminder its audience too -- it drops back to
    appearing only in the session-opening list until something changes.
    """
    return int(row.get("surfaced_count") or 0) >= DEMOTE_AFTER


def _due_now(row, now):
    d = row.get("due") or ""
    return bool(d) and d <= now


def _snoozed(row, now):
    s = row.get("snooze_until") or ""
    return bool(s) and s > now


def _expired(row, now):
    e = row.get("expires_at") or ""
    return bool(e) and e <= now


def collect(prompt, projects, first_message, cooldown_seconds=COOLDOWN_SECONDS):
    """What should be put in front of the user for this prompt.

    Returns (due, relevant, expired_ids). Pure selection -- the caller records the
    bookkeeping, so a hook that fails to render surfaces the same reminder next time
    instead of silently burning it.
    """
    now = now_iso()
    pending = [r for r in _rows(status="pending")]
    expired = [r["id"] for r in pending if _expired(r, now)]
    live = [r for r in pending if r["id"] not in expired and not _snoozed(r, now)]

    def in_scope(r):
        p = r.get("project") or ""
        return (not p) or (p in projects)      # global reminders follow you everywhere

    live = [r for r in live if in_scope(r)]

    due = [r for r in live if _due_now(r, now)]
    due.sort(key=lambda r: r.get("due") or "")

    relevant = []
    if prompt and len(prompt.strip()) >= 8:
        candidates = [r for r in live if r not in due]
        if candidates and not first_message:
            candidates = [r for r in candidates if not _is_demoted(r)]
        if candidates:
            try:
                col = get_reminders_collection()
                # n_results must cover the WHOLE collection's top ranks, not just the
                # candidate count: the query searches everything, so reminders already
                # filtered out here (demoted, snoozed, out-of-project, or in the due
                # list) occupy the top slots and would crowd out the very candidates
                # this is trying to find. Sizing it to len(candidates) meant one
                # demoted reminder could silence a live one entirely.
                total = max(1, col.count())
                res = col.query(query_texts=[prompt[:400]],
                                n_results=min(total, max(20, len(candidates) * 4)))
                allowed = {r["id"]: r for r in candidates}
                cutoff = time.time() - cooldown_seconds
                for i, rid in enumerate(res["ids"][0]):
                    if rid not in allowed:
                        continue
                    dist = res["distances"][0][i] if res.get("distances") else 1.0
                    if dist >= RELEVANCE_THRESHOLD:
                        continue
                    last = allowed[rid].get("last_surfaced") or ""
                    if last:
                        try:
                            t = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
                            if t.timestamp() > cutoff:
                                continue      # already raised recently; don't nag
                        except ValueError:
                            pass
                    row = dict(allowed[rid]); row["distance"] = round(dist, 3)
                    relevant.append(row)
            except Exception:
                pass

    if first_message:
        due = due[:MAX_SURFACED]
        relevant = relevant[:max(0, MAX_SURFACED - len(due))]
    else:
        due = due[:MAX_SURFACED]
        relevant = relevant[:max(0, MAX_SURFACED - len(due))]
    return due, relevant, expired


def record_surfaced(rows, expired_ids=()):
    """Bookkeeping, run only after the caller has actually rendered the reminders."""
    if not rows and not expired_ids:
        return
    try:
        col = get_reminders_collection()
    except ReminderStoreUnavailable:
        return
    for rid in expired_ids:
        got = col.get(ids=[rid], include=["metadatas"])
        if got.get("ids"):
            m = dict(got["metadatas"][0] or {})
            m["status"] = "expired"; m["done_at"] = now_iso()
            col.update(ids=[rid], metadatas=[m])
    for r in rows:
        got = col.get(ids=[r["id"]], include=["metadatas"])
        if not got.get("ids"):
            continue
        m = dict(got["metadatas"][0] or {})
        m["surfaced_count"] = int(m.get("surfaced_count") or 0) + 1
        m["last_surfaced"] = now_iso()
        col.update(ids=[r["id"]], metadatas=[m])


def render(rows, header):
    lines = [header]
    for r in rows:
        bits = []
        if r.get("due"):
            bits.append(f"due {r['due'][:16]}")
        if r.get("project"):
            bits.append(r["project"])
        tag = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"  [{r['id']}]{tag} {r['text']}")
        if r.get("verify"):
            lines.append(f"      check: {r['verify']}")
        if r.get("links"):
            lines.append(f"      context: {r['links']}")
    return "\n".join(lines)
