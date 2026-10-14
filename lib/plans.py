"""Cortex plans — a body of intended work, with a checklist, that outlives a session.

Three kinds of thing, three collections:

  * MEMORY   — something KNOWN ("backups are kept for 30 days"). Background context.
  * REMINDER — ONE thing OWED, usually at a time ("check the 03:00 run tomorrow").
  * PLAN     — a GOAL plus the STEPS toward it, ticked off item by item, possibly over
               weeks ("Migrate auth to OIDC: gateway, billing, reporting, cleanup").

A plan is not a memory: memory hygiene dedups and merges, and merging two plans would
silently fuse two bodies of work. It is not a reminder either: a reminder is closed
once, a plan is progressed a step at a time, and the progress is the point.

Statuses: `active` (being worked), `future` (parked backlog — "later", "phase 2",
"future work"), `done`, `abandoned`. Items are `open`, `done` or `skipped`, and keep a
stable number for life — removing #3 never renumbers #4 — so a reference written in one
session still means the same item in the next.

Plans surface only inside a session (see plans.sh): active in-scope plans on the first
prompt, subject-matched open plans on any prompt.
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import time

try:
    from lib.chroma_client import get_collection as _chroma_get_collection
except ImportError:                      # hooks put lib/ itself on the path
    from chroma_client import get_collection as _chroma_get_collection

COLLECTION = os.environ.get("CORTEX_PLANS_COLLECTION", "cortex_plans")
LOCK_PATH = os.environ.get("CORTEX_PLANS_LOCK",
                           os.path.expanduser("~/.claude/.cortex_plans.lock"))

PLAN_STATUSES = ("active", "future", "done", "abandoned")
OPEN_STATUSES = ("active", "future")
ITEM_STATES = ("open", "done", "skipped")

MAX_TITLE = 200
MAX_GOAL = 4000
MAX_ITEM_TEXT = 500
MAX_ITEMS = 150
MAX_NOTES = 12000
# Where a plan's older notes go when an appended note would take the log past MAX_NOTES: one
# append-only file per plan, owner-only. NOT the audit log, which keeps 90 days.
NOTES_ARCHIVE_DIR = (os.environ.get("CORTEX_PLANS_NOTES_ARCHIVE")
                     or os.path.expanduser("~/.claude/cortex-plan-notes"))
NOTES_ARCHIVE_MARKER = "(Older notes moved to "
_NOTE_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] ", re.M)
MAX_DOCUMENT = 8000              # embedded text; the checklist beyond this still exists

# Measured against gte-modernbert on neutral example plans (2026-09-16): prompts about
# a plan's subject sat at 0.17-0.33 from the plan's document, unrelated prompts at
# 0.51-0.66. A plan informs rather than interrupts, so it gets a slightly looser bar
# than reminders (0.35).
RELEVANCE_THRESHOLD = 0.40
# Near-identical only: the same plan created twice from two sessions. A re-create
# measured 0.00; a sibling plan on the same subject area 0.35.
DUPLICATE_THRESHOLD = 0.08
MAX_SESSION_ACTIVE = 3           # active plans listed on a session's first prompt
MAX_RELEVANT = 2                 # subject-matched plans per prompt
COOLDOWN_SECONDS = 45 * 60
DEMOTE_AFTER = 3                 # relevance raises without the plan being touched
STALE_DAYS = 14

_PLAN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")

# Tests replace this with a factory returning an isolated collection.
_collection_factory = None


class PlanError(ValueError):
    """The request was wrong (unknown item, bad status). Nothing was written."""


class PlanStoreUnavailable(RuntimeError):
    """Raised rather than writing plans into the wrong collection."""


def get_plans_collection():
    """The plans collection, or refuse.

    chroma_client.get_collection degrades to the LEGACY MEMORIES collection when
    embeddings are unavailable. Plans written there would come back as recalled facts
    and be eligible for merging, so fail closed instead — same as reminders.
    """
    col = _collection_factory() if _collection_factory else _chroma_get_collection(COLLECTION)
    if getattr(col, "name", None) != COLLECTION:
        raise PlanStoreUnavailable(
            f"embeddings unavailable: got '{getattr(col, 'name', '?')}' instead of "
            f"'{COLLECTION}'. Refusing to write plans into the memory store.")
    return col


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextlib.contextmanager
def _locked(timeout=10.0):
    """Serialise read-modify-write across processes.

    Every Claude session runs its own MCP server, so two sessions ticking items on the
    same plan are two processes each rewriting the whole checklist. Without this the
    second write silently discards the first one's ticks.
    """
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PlanStoreUnavailable("plan store is busy (lock held); try again")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)                     # closing the descriptor releases the lock


# ── Parsing ───────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"\s*#{1,6}\s+(?P<title>.*\S)\s*")
_CHECKBOX_RE = re.compile(
    r"\s*(?:[-*+\u2022]\s+|\d+[.)]\s+)?"         # optional bullet or "1." / "1)"
    r"\[(?P<box>[ xX~-])\]\s+(?P<body>.*\S)\s*")
_BULLET_RE = re.compile(r"\s*(?:[-*+\u2022]|\d+[.)])\s+(?P<body>.*\S)\s*")
_FENCE_RE = re.compile(r"\s*(?:```|~~~)")
_NOT_AN_ITEM_RE = re.compile(r"\s*(?:\||>|[-*_=]{3,}\s*$)")  # table row, quote, rule
_BOX_TOKEN_RE = re.compile(r"\[[ xX~-]\]")
_MARKER_ONLY_RE = re.compile(r"[-*+\u2022]|\d+[.)]|\[[ xX~-]\]")  # "-", "1.", "[ ]" alone
# "[~]" conventionally means "partly done / in progress", which is still open work;
# counting it as resolved would overstate progress on the status line.
_BOX_STATE = {" ": "open", "x": "done", "X": "done", "-": "skipped", "~": "open"}


def parse_items(text, section=""):
    """Checklist text -> ([(text, state, section)], ignored_line_count).

    The text picks the mode:

      * CHECKLIST — if any line is a Markdown checkbox, ONLY checkbox lines become items,
        keeping their state ([x] done, [-] skipped, [ ] and [~] open), and "## Heading"
        lines set the section. Prose, indented detail under an item, code fences,
        tables and rules are not items. They are counted rather than silently kept or
        dropped, so the caller can say that detail was left behind.
      * LIST — no checkboxes: each bullet, numbered or plain line is an open item.
        Headings still set sections; fenced code, tables, quotes and rules are skipped.
    """
    current = (section or "").strip()
    ignored, in_fence, lines = 0, False, []
    for line in str(text or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            ignored += 1
        elif in_fence:
            ignored += 1 if line.strip() else 0
        elif line.strip():
            lines.append(line)
    checklist = any(_CHECKBOX_RE.fullmatch(line) for line in lines)

    out = []
    for line in lines:
        h = _HEADING_RE.fullmatch(line)
        if h:
            current = h.group("title")[:MAX_TITLE]
            continue
        if checklist:
            m = _CHECKBOX_RE.fullmatch(line)
            if m and _BOX_TOKEN_RE.search(m.group("body")):
                m = None                 # "[ ] open · [x] done" is a legend, not an item
            if m:
                out.append((m.group("body").strip(), _BOX_STATE[m.group("box")], current))
            else:
                ignored += 1
            continue
        if _NOT_AN_ITEM_RE.match(line):
            ignored += 1
            continue
        m = _BULLET_RE.fullmatch(line)
        body = (m.group("body") if m else line).strip()
        if _MARKER_ONLY_RE.fullmatch(body):
            ignored += 1
            continue
        out.append((body, "open", current))
    return out, ignored


def parse_refs(spec, valid_ids):
    """"3" / "#3" / "3,5" / "2-4" -> [3] / [3] / [3, 5] / [2, 3, 4].

    All-or-nothing: one unknown number rejects the whole request, so a typo can never
    tick half a list. A range covers the items that still exist inside it.
    """
    valid = set(valid_ids)
    ids = []
    for tok in re.split(r"[,\s]+", str(spec or "").strip()):
        tok = tok.lstrip("#")
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-#?(\d+)", tok)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            for end in (a, b):
                if end not in valid:
                    raise PlanError(f"no item #{end}. Items: {_id_summary(valid)}")
            ids.extend(i for i in range(a, b + 1) if i in valid)
        elif tok.isdigit():
            if int(tok) not in valid:
                raise PlanError(f"no item #{tok}. Items: {_id_summary(valid)}")
            ids.append(int(tok))
        else:
            raise PlanError(f"can't read item reference {tok!r}; "
                            "use item numbers like 3, 3,5 or 2-4")
    if not ids:
        raise PlanError("no item numbers given")
    return list(dict.fromkeys(ids))


def _id_summary(valid):
    s = sorted(valid)
    if not s:
        return "none"
    return ", ".join(f"#{i}" for i in s) if len(s) <= 12 else f"#{s[0]}..#{s[-1]}"


# ── Rows ──────────────────────────────────────────────────────────────────────────

def _row(pid, document, meta):
    m = dict(meta or {})
    try:
        items = json.loads(m.get("items") or "[]")
    except (TypeError, ValueError):
        items = []
    m["items"] = items if isinstance(items, list) else []
    return {"id": pid, "document": document or "", **m}


def progress(row):
    items = row.get("items") or []
    done = sum(1 for i in items if i.get("state") == "done")
    skipped = sum(1 for i in items if i.get("state") == "skipped")
    return {"done": done, "skipped": skipped, "open": len(items) - done - skipped,
            "total": len(items)}


def open_items(row):
    return [i for i in row.get("items") or [] if i.get("state") == "open"]


def _document(title, goal, items):
    parts = [title]
    if goal:
        parts.append(goal)
    parts.extend(f"- {i['text']}" for i in items)
    return "\n".join(parts)[:MAX_DOCUMENT]


def _validate_title(title):
    t = str(title or "").strip()
    if any(not ch.isprintable() for ch in t):
        raise PlanError("plan title must be a single line of printable text")
    if len(t) < 3:
        raise PlanError("plan title too short (min 3 chars)")
    if len(t) > MAX_TITLE:
        raise PlanError(f"plan title too long ({len(t)} chars, max {MAX_TITLE})")
    return t


def _validate_goal(goal):
    g = str(goal or "").strip()
    if len(g) > MAX_GOAL:
        raise PlanError(f"goal too long ({len(g)} chars, max {MAX_GOAL}); "
                        "put detail in a memory and link it")
    return g


def _new_items(parsed, next_id):
    items = []
    for text, state, section in parsed:
        if len(text) > MAX_ITEM_TEXT:
            raise PlanError(f"item too long ({len(text)} chars, max {MAX_ITEM_TEXT}): "
                            f"{text[:60]}...")
        items.append({"id": next_id, "text": text, "state": state, "section": section,
                      "note": "", "closed_at": now_iso() if state != "open" else ""})
        next_id += 1
    return items, next_id


def _normalize_project(project):
    """"global" is how a caller asks for everywhere; it is stored as "" (no scope)."""
    p = str(project or "").strip()
    return "" if p.lower() == "global" else p


def _slug_id(title):
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48].strip("_")
    return f"plan_{slug}" if slug else f"plan_{int(time.time() * 1000)}"


def _get_row(col, plan_id):
    got = col.get(ids=[plan_id], include=["documents", "metadatas"])
    if not got.get("ids"):
        return None
    return _row(got["ids"][0], got["documents"][0], got["metadatas"][0])


def _require(col, plan_id):
    row = _get_row(col, plan_id)
    if row is None:
        raise PlanError(f"no plan {plan_id!r}")
    return row


def _status_where(statuses):
    return {"status": {"$in": list(statuses)}}


# ── Writes ────────────────────────────────────────────────────────────────────────

def notes_archive_path(plan_id):
    """The file a plan's older notes are moved to (see `_fit_notes`)."""
    if not NOTES_ARCHIVE_DIR or any(not ch.isprintable() for ch in NOTES_ARCHIVE_DIR):
        raise PlanError("CORTEX_PLANS_NOTES_ARCHIVE must be a single-line path")
    return os.path.join(NOTES_ARCHIVE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", plan_id) + ".log")


def notes_archive_marker(plan_id):
    """The one line a log opens with once some of its notes live in the archive file."""
    return f"{NOTES_ARCHIVE_MARKER}{notes_archive_path(plan_id)}; read it for the earlier history.)"


def _fit_notes(plan_id, old, entry):
    """An append that would take the log past MAX_NOTES, made to fit: (kept, moved).

    `old` is the log before the append: a preamble (unstamped text first, e.g. a condensed
    summary) and then the stamped notes, oldest first. `entry` is the new stamped note and is
    never split, whatever its body looks like. The oldest stamped notes are moved out first;
    the preamble goes only if the new note still does not fit with it, and it is then put in
    front of them, since it is older than every stamped note. The kept log opens with the
    marker line, so the earlier history reads as moved, not lost. A note too long to fit
    alone is refused. (A body line that looks like a stamp inside an OLDER note splits that
    note at the line; both parts are kept, in order, possibly one in each place.)"""
    marker = notes_archive_marker(plan_id)
    starts = [m.start() for m in _NOTE_STAMP.finditer(old)]
    pre = old[:starts[0]] if starts else old
    entries = [old[a:b] for a, b in zip(starts, starts[1:] + [len(old)])]
    if pre.split("\n", 1)[0].rstrip("\r") == marker:     # only this plan's own marker line
        pre = pre.split("\n", 1)[1] if "\n" in pre else ""
    if not pre.strip():
        pre = ""
    moved = []

    def kept():
        body = (pre + "".join(entries)).rstrip("\n")
        return marker + "\n" + (body + "\n" if body else "") + entry

    while len(kept()) > MAX_NOTES and entries:
        moved.append(entries.pop(0))
    if len(kept()) > MAX_NOTES and pre:
        moved.insert(0, pre)
        pre = ""
    if len(kept()) > MAX_NOTES:
        room = MAX_NOTES - len(marker) - 1
        raise PlanError(f"this note is {len(entry)} chars with its timestamp; at most {room} "
                        "fit in the log after the archive line. Shorten it.")
    return kept(), "".join(moved)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_notes(plan_id, text):
    """Append moved notes to the plan's archive file before the plan changes. The file, its
    folder and that folder's parent are flushed to disk EVERY time, so a retry after a failed
    flush skips none of them; so is the parent of any further folder made this time. (Folders
    more than one level deep that an earlier, failed attempt made are not flushed again; the
    default folder is one level inside ~/.claude.)"""
    # real folders, so a symlink followed by ".." flushes the folder actually used
    made, d = [], os.path.realpath(NOTES_ARCHIVE_DIR)
    while not os.path.isdir(d):
        made.append(d)
        d = os.path.dirname(d)
    os.makedirs(NOTES_ARCHIVE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(notes_archive_path(plan_id), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(f"===== {now_iso()} moved out of plan {plan_id} =====\n{text.rstrip()}\n")
        fh.flush()
        os.fsync(fh.fileno())
    folder = os.path.realpath(NOTES_ARCHIVE_DIR)
    for d in dict.fromkeys([folder, os.path.dirname(folder)]
                           + [os.path.dirname(m) for m in made]):
        _fsync_dir(d)


def create(title, items="", goal="", project="", status="active", tags="", links="",
           plan_id="", force=False):
    title = _validate_title(title)
    goal = _validate_goal(goal)
    status = (status or "active").strip().lower()
    if status not in OPEN_STATUSES:
        raise PlanError(f"a new plan must be 'active' or 'future', not {status!r}")
    plan_id = (plan_id or "").strip()
    if plan_id and not _PLAN_ID_RE.fullmatch(plan_id):
        raise PlanError("plan_id may use letters, digits, '_', '.', '-' (max 100 chars)")
    parsed, ignored = parse_items(items)
    if len(parsed) > MAX_ITEMS:
        raise PlanError(f"too many items ({len(parsed)}, max {MAX_ITEMS}); split the plan")
    new_items, next_id = _new_items(parsed, 1)

    col = get_plans_collection()
    with _locked():
        if plan_id:
            if _get_row(col, plan_id) is not None:
                raise PlanError(f"plan {plan_id!r} already exists; use plan_update")
        else:
            base = _slug_id(title)
            plan_id, n = base, 2
            while _get_row(col, plan_id) is not None:
                plan_id, n = f"{base}_{n}", n + 1

        document = _document(title, goal, new_items)
        if not force:
            dupe = _find_duplicate(col, document)
            if dupe:
                raise PlanError(
                    f"an open plan already covers this: {dupe['id']} \"{dupe['title']}\" "
                    f"({round((1 - dupe['distance']) * 100, 1)}% similar). Update that one "
                    "with plan_update, or pass force=True if this really is separate work.")

        now = now_iso()
        meta = {"title": title, "goal": goal, "status": status,
                "project": _normalize_project(project), "tags": (tags or "").strip(),
                "links": (links or "").strip(), "items": json.dumps(new_items),
                "next_item_id": next_id, "notes": "", "created": now, "updated": now,
                "closed_at": "", "last_surfaced": "", "surfaced_count": 0}
        col.add(ids=[plan_id], documents=[document], metadatas=[meta])
        return {**_row(plan_id, document, meta), "ignored_lines": ignored}


def _find_duplicate(col, document):
    try:
        if col.count() == 0:
            return None
        res = col.query(query_texts=[document], n_results=min(5, col.count()),
                        where=_status_where(OPEN_STATUSES),
                        include=["metadatas", "distances"])
    except Exception:
        return None                      # dedup is best-effort; never block a create
    for i, pid in enumerate(res.get("ids", [[]])[0]):
        dist = res["distances"][0][i]
        if dist < DUPLICATE_THRESHOLD:
            return {"id": pid, "distance": dist,
                    "title": (res["metadatas"][0][i] or {}).get("title", "")}
    return None


def check(plan_id, refs, state="done", note=""):
    """Set items' state. Returns (row, changed_ids)."""
    state = (state or "done").strip().lower()
    if state not in ITEM_STATES:
        raise PlanError(f"item state must be one of {', '.join(ITEM_STATES)}")
    note = str(note or "").strip()
    if len(note) > MAX_ITEM_TEXT:
        raise PlanError(f"note too long ({len(note)} chars, max {MAX_ITEM_TEXT})")
    col = get_plans_collection()
    with _locked():
        row = _require(col, plan_id)
        items = row["items"]
        ids = parse_refs(refs, [i["id"] for i in items])
        now = now_iso()
        for it in items:
            if it["id"] in ids and note:
                joined = f"{it['note']} · {note}" if it.get("note") else note
                if len(joined) > MAX_ITEM_TEXT:
                    raise PlanError(f"item #{it['id']} note would exceed {MAX_ITEM_TEXT} "
                                    "chars; put the longer story in plan_update(note=...)")
        for it in items:
            if it["id"] in ids:
                it["state"] = state
                it["closed_at"] = now if state != "open" else ""
                if note:
                    it["note"] = f"{it['note']} · {note}" if it.get("note") else note
        meta = {"items": json.dumps(items), "updated": now, "surfaced_count": 0}
        col.update(ids=[plan_id], metadatas=[meta])
        row.update(meta)
        row["items"] = items
        return row, ids


def update(plan_id, title=None, goal=None, status=None, project=None, tags=None,
           links=None, note="", notes_mode="append", add_items="", section="",
           remove_items="", edit_item="", item_text=""):
    """Change anything but item states. None / "" = leave unchanged; for goal, tags and
    links, "-" clears the value.

    Everything is validated before anything is written, so a bad reference in
    remove_items cannot leave the title changed and the items not.
    """
    col = get_plans_collection()
    with _locked():
        row = _require(col, plan_id)
        meta, reembed = {}, False
        items = row["items"]
        next_id = int(row.get("next_item_id") or (max([i["id"] for i in items] or [0]) + 1))

        if title:
            t = _validate_title(title)
            if t != row.get("title"):
                meta["title"], reembed = t, True
        if goal is not None and goal != "":
            g = "" if goal.strip() == "-" else _validate_goal(goal)
            if g != row.get("goal"):
                meta["goal"], reembed = g, True
        if status:
            s = status.strip().lower()
            if s not in PLAN_STATUSES:
                raise PlanError(f"status must be one of {', '.join(PLAN_STATUSES)}")
            if s != row.get("status"):
                meta["status"] = s
                meta["closed_at"] = now_iso() if s not in OPEN_STATUSES else ""
        if project is not None and project != "":
            p = _normalize_project(project)
            if p != row.get("project", ""):
                meta["project"] = p
        for key, value in (("tags", tags), ("links", links)):
            if value is not None and value != "":
                v = "" if value.strip() == "-" else value.strip()
                if v != row.get(key, ""):
                    meta[key] = v

        if remove_items:
            gone = set(parse_refs(remove_items, [i["id"] for i in items]))
            items = [i for i in items if i["id"] not in gone]
            meta["items"], reembed = True, True
        if edit_item:
            ids = parse_refs(edit_item, [i["id"] for i in items])
            if len(ids) != 1:
                raise PlanError("edit_item takes exactly one item number")
            text = str(item_text or "").strip()
            if not text:
                raise PlanError("edit_item needs item_text")
            if any(not ch.isprintable() for ch in text):
                raise PlanError("item_text must be a single line of printable text")
            if len(text) > MAX_ITEM_TEXT:
                raise PlanError(f"item too long ({len(text)} chars, max {MAX_ITEM_TEXT})")
            for it in items:
                if it["id"] == ids[0]:
                    it["text"] = text
            meta["items"], reembed = True, True
        ignored = 0
        if add_items:
            parsed, ignored = parse_items(add_items, section=section)
            if not parsed:
                raise PlanError("add_items contained no items")
            added, next_id = _new_items(parsed, next_id)
            items = items + added
            meta["items"], meta["next_item_id"], reembed = True, next_id, True
        if len(items) > MAX_ITEMS:
            raise PlanError(f"too many items ({len(items)}, max {MAX_ITEMS}); split the plan")

        note = str(note or "").strip()
        moved_notes = ""
        mode = (notes_mode or "append").strip().lower()
        if mode not in ("append", "replace"):
            raise PlanError("notes_mode must be 'append' or 'replace'")
        if mode == "replace" and not note:
            raise PlanError("notes_mode='replace' needs the condensed notes as note=; "
                            "an empty replace would erase the log")
        if note:
            if mode == "replace":
                notes = note
                # a condensed log keeps pointing at the archive, if the plan has one
                if (os.path.exists(notes_archive_path(plan_id))
                        and notes.split("\n", 1)[0].rstrip("\r") != notes_archive_marker(plan_id)):
                    notes = notes_archive_marker(plan_id) + "\n" + notes
                if len(notes) > MAX_NOTES:
                    raise PlanError(f"condensed notes too long ({len(notes)} chars with any "
                                    f"archive line, max {MAX_NOTES}); shorten them")
            else:
                # the stamp is the shape _NOTE_STAMP finds
                entry = f"[{now_iso()[:16].replace('T', ' ')}] {note}"
                old = row.get("notes") or ""
                notes = f"{old}\n{entry}" if old else entry
                if len(notes) > MAX_NOTES:
                    # An append over the cap moves the oldest notes to the archive file
                    # instead of being refused, which had left only two choices: lose the new
                    # note or replace the whole log.
                    notes, moved_notes = _fit_notes(plan_id, old, entry)
            if notes != row.get("notes", ""):
                meta["notes"] = notes

        if not meta:
            raise PlanError("nothing to update")
        if moved_notes:
            # Written before the plan changes, so a failed write changes nothing. A failure
            # after it leaves the moved notes in both places, never in neither, and a retry
            # appends the same block to the archive again (a duplicate, never a loss).
            try:
                _archive_notes(plan_id, moved_notes)
            except OSError as e:
                raise PlanError(f"could not move older notes to {notes_archive_path(plan_id)} "
                                f"({e}); nothing was changed")

        if "items" in meta:
            meta["items"] = json.dumps(items)
        meta["updated"] = now_iso()
        meta["surfaced_count"] = 0
        merged = {**row, **meta, "previous_status": row.get("status"),
                  "ignored_lines": ignored}
        if reembed:
            document = _document(merged["title"], merged.get("goal", ""), items)
            col.update(ids=[plan_id], documents=[document], metadatas=[meta])
            merged["document"] = document
        else:
            col.update(ids=[plan_id], metadatas=[meta])
        merged["items"] = items
        merged["notes_moved"] = len(_NOTE_STAMP.findall(moved_notes))
        merged["notes_moved_preamble"] = bool(moved_notes) and not _NOTE_STAMP.match(moved_notes)
        merged["notes_archive"] = notes_archive_path(plan_id) if moved_notes else ""
        return merged


def delete(plan_id, archive=None):
    """Remove a plan, returning the row that was removed (None if there was none).

    `archive(row)` runs first, under the lock; if it raises, nothing is deleted — a
    plan is never gone without its backup having been written.
    """
    col = get_plans_collection()
    with _locked():
        row = _get_row(col, plan_id)
        if row is None:
            return None
        if archive is not None:
            archive(row)
        col.delete(ids=[plan_id])
        return row


# ── Reads ─────────────────────────────────────────────────────────────────────────

def get(plan_id):
    return _get_row(get_plans_collection(), plan_id)


def _statuses_for(status):
    s = (status or "").strip().lower()
    if s in ("", "all"):
        return None
    if s == "open":
        return OPEN_STATUSES
    if s not in PLAN_STATUSES:
        raise PlanError(f"status must be open, all, or one of {', '.join(PLAN_STATUSES)}")
    return (s,)


def list_plans(status="open", project=None, query="", limit=50):
    statuses = _statuses_for(status)
    col = get_plans_collection()
    where = _status_where(statuses) if statuses else None
    if query and query.strip():
        total = col.count()
        if total == 0:
            return []
        res = col.query(query_texts=[query.strip()[:400]], n_results=min(total, 100),
                        where=where, include=["documents", "metadatas", "distances"])
        rows = []
        for i, pid in enumerate(res["ids"][0]):
            r = _row(pid, res["documents"][0][i], res["metadatas"][0][i])
            r["distance"] = round(res["distances"][0][i], 3)
            rows.append(r)
    else:
        got = col.get(where=where, include=["documents", "metadatas"]) if where \
            else col.get(include=["documents", "metadatas"])
        rows = [_row(pid, got["documents"][i], got["metadatas"][i])
                for i, pid in enumerate(got.get("ids", []))]
        order = {s: n for n, s in enumerate(PLAN_STATUSES)}
        rows.sort(key=lambda r: r.get("updated") or "", reverse=True)
        rows.sort(key=lambda r: order.get(r.get("status"), 99))
    if project is not None:
        want = _normalize_project(project)
        rows = [r for r in rows if (r.get("project") or "") == want]
    return rows[:limit]


def summary():
    """Counts for the status line: one pass over the open plans."""
    rows = list_plans(status="open")
    active = [r for r in rows if r.get("status") == "active"]
    done = total = 0
    for r in active:
        p = progress(r)
        done += p["done"] + p["skipped"]
        total += p["total"]
    latest = active[0] if active else None      # list_plans sorts newest-updated first
    return {"active": len(active), "future": len(rows) - len(active),
            "active_done": done, "active_total": total, "latest": latest}


def _age_days(ts):
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return 0
    return (datetime.datetime.now() - t).days


def _is_demoted(row):
    """Raised by subject match repeatedly and never touched: stop interrupting.

    Any edit resets the count, so a plan that is actually being worked keeps surfacing.
    """
    return int(row.get("surfaced_count") or 0) >= DEMOTE_AFTER


def collect(prompt, projects, first_message, cooldown_seconds=COOLDOWN_SECONDS):
    """What to put in front of the user for this prompt -> (active, relevant).

    Pure selection; record_surfaced does the bookkeeping after rendering, so a hook
    that fails to print raises the same plans next time instead of burning them.
    """
    col = get_plans_collection()
    got = col.get(where=_status_where(OPEN_STATUSES), include=["documents", "metadatas"])
    rows = [_row(pid, got["documents"][i], got["metadatas"][i])
            for i, pid in enumerate(got.get("ids", []))]
    rows = [r for r in rows if not r.get("project") or r["project"] in projects]

    active = []
    if first_message:
        active = sorted((r for r in rows if r.get("status") == "active"),
                        key=lambda r: r.get("updated") or "", reverse=True)
        active = active[:MAX_SESSION_ACTIVE]
    shown = {r["id"] for r in active}

    relevant = []
    if prompt and len(prompt.strip()) >= 8:
        candidates = {r["id"]: r for r in rows if r["id"] not in shown
                      and (first_message or not _is_demoted(r))}
        if candidates:
            try:
                # Closed plans are kept forever, so the query is filtered to open ones;
                # otherwise enough done plans on a subject would take every returned
                # rank and hide the open plan. Sized well past the candidate count
                # because demoted and out-of-scope open plans still occupy ranks (the
                # defect found in reminders).
                total = max(1, col.count())
                res = col.query(query_texts=[prompt[:400]],
                                n_results=min(total, max(20, len(candidates) * 4)),
                                where=_status_where(OPEN_STATUSES),
                                include=["distances"])
                cutoff = time.time() - cooldown_seconds
                for i, pid in enumerate(res["ids"][0]):
                    r = candidates.get(pid)
                    if r is None:
                        continue
                    dist = res["distances"][0][i]
                    if dist >= RELEVANCE_THRESHOLD:
                        continue
                    last = r.get("last_surfaced") or ""
                    if last:
                        try:
                            t = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
                            if t.timestamp() > cutoff:
                                continue
                        except ValueError:
                            pass
                    r = dict(r)
                    r["distance"] = round(dist, 3)
                    relevant.append(r)
                    if len(relevant) >= MAX_RELEVANT:
                        break
            except Exception:
                pass
    return active, relevant


def record_surfaced(active, relevant):
    """Bookkeeping after rendering. Writes ONLY its own keys.

    Chroma's update() merges metadata keys, so writing just last_surfaced /
    surfaced_count can never overwrite a checklist another session ticked in between.
    A busy lock skips the bookkeeping rather than delaying the user's prompt.
    """
    if not active and not relevant:
        return
    try:
        col = get_plans_collection()
        with _locked(timeout=1.0):
            now = now_iso()
            for r in active:
                col.update(ids=[r["id"]], metadatas=[{"last_surfaced": now}])
            for r in relevant:
                got = col.get(ids=[r["id"]], include=["metadatas"])
                if not got.get("ids"):
                    continue
                n = int((got["metadatas"][0] or {}).get("surfaced_count") or 0) + 1
                col.update(ids=[r["id"]],
                           metadatas=[{"surfaced_count": n, "last_surfaced": now}])
    except Exception:
        return


# ── Rendering ─────────────────────────────────────────────────────────────────────

def _progress_text(row):
    p = progress(row)
    if not p["total"]:
        return "no items"
    skipped = f", {p['skipped']} skipped" if p["skipped"] else ""
    return f"{p['done']}/{p['total']} done{skipped}"


def render_brief(rows, header, next_n=2):
    lines = [header]
    for r in rows:
        bits = [r.get("status", "?"), _progress_text(r)]
        if r.get("project"):
            bits.append(r["project"])
        age = _age_days(r.get("updated"))
        if age >= STALE_DAYS:
            bits.append(f"untouched {age}d")
        lines.append(f"  [{r['id']}] {r.get('title', '')} ({', '.join(bits)})")
        nxt = open_items(r)[:next_n]
        if nxt:
            lines.append("      next: " + " ; ".join(f"#{i['id']} {i['text']}" for i in nxt))
    return "\n".join(lines)


_BOX = {"open": "[ ]", "done": "[x]", "skipped": "[-]"}


def render_full(row):
    """The whole plan as a Markdown checklist, item numbers included."""
    head = [f"# {row.get('title', '')}  [{row['id']}]"]
    meta = [f"status: {row.get('status', '?')}", _progress_text(row),
            f"project: {row.get('project') or 'all projects'}",
            f"updated {str(row.get('updated', ''))[:16]}"]
    if row.get("closed_at"):
        meta.append(f"closed {row['closed_at'][:16]}")
    head.append(" · ".join(meta))
    if row.get("tags"):
        head.append(f"tags: {row['tags']}")
    if row.get("links"):
        head.append(f"links: {row['links']}")
    out = head
    if row.get("goal"):
        out += ["", "Goal:", row["goal"]]
    items = row.get("items") or []
    if items:
        out.append("")
        section = None
        for it in items:
            if it.get("section", "") != section:
                section = it.get("section", "")
                if section:
                    out += ([] if out[-1] == "" else [""]) + [f"## {section}"]
            line = f"- {_BOX.get(it.get('state'), '[?]')} #{it['id']} {it['text']}"
            if it.get("note"):
                line += f"  — {it['note']}"
            out.append(line)
    else:
        out += ["", "(no items yet — add them with plan_update(add_items=...))"]
    if row.get("notes"):
        out += ["", "Notes:", row["notes"]]
    return "\n".join(out)
