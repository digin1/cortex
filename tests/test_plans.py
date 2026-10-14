"""Tests for cortex plans (lib/plans.py, the plan_* MCP tools, plans.sh, status line).

Run from the cortex root:  /usr/bin/python3 tests/test_plans.py -v
(not `-m unittest tests.test_plans`: a site-packages `tests` package can shadow this dir)

PlanLogicTests and PlanToolTests run against an in-memory Chroma client with a
bag-of-words embedder, so they never touch the real memory database. PlanLiveTests use
the real embed sidecar and Chroma server, but only a throwaway
`cortex_plans_test_<pid>` collection that is deleted afterwards; they skip (and say so)
when either service is down.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chromadb                                   # noqa: E402
import numpy as np                                # noqa: E402
from chromadb.api.types import EmbeddingFunction  # noqa: E402

from lib import plans as P                        # noqa: E402

TEST_COLLECTION = "cortex_plans_unittest"


class WordsEF(EmbeddingFunction):
    """Hashed bag of words: shared words -> small cosine distance, none -> ~1.0."""

    def __init__(self):
        pass

    def __call__(self, input):
        out = []
        for text in input:
            v = np.zeros(256, dtype=np.float32)
            for w in re.findall(r"[a-z0-9]+", text.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
            if not v.any():
                v[0] = 1.0
            out.append(v / np.linalg.norm(v))
        return out

    @staticmethod
    def name():
        return "test-words"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return WordsEF()


def _iso(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds_ago))


# A checklist shaped like a real running future-work file: legend, rule, numbered
# sections, checkbox items with indented detail, a table, and a fenced example.
FUTURE_WORK_MD = """# Service — Future Work

Running checklist of known-but-not-yet-done work. Each item records what, why and where.

Status legend: `[ ]` open · `[x]` done · `[~]` partially done / in progress

---

## 1. Backup hygiene (HIGH)

**Evidence:** the backup volume reached 91% on the night of the incident.

- [x] **Add a retention job** — done.
      Prunes backups older than 30 days, hourly.
      Two review findings worth remembering: (a) ... (b) ...
- [~] **Alert before the volume fills** — threshold agreed, probe not written.
- [ ] **Restore drill** into a scratch database.

| check | owner |
|---|---|
| restore | ops |

```
- [ ] this checkbox is inside a code fence and is not an item
```

## 2. Docs

- [-] **Wiki page** — decided against; the README covers it.
1. [ ] **Runbook** for the restore drill
"""


class _IsolatedStore(unittest.TestCase):
    """In-memory Chroma + temp lock file, swapped into lib.plans for each test."""

    @classmethod
    def setUpClass(cls):
        cls.client = chromadb.EphemeralClient()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with contextlib.suppress(Exception):
            self.client.delete_collection(TEST_COLLECTION)
        self.col = self.client.get_or_create_collection(
            TEST_COLLECTION, embedding_function=WordsEF(), metadata={"hnsw:space": "cosine"})
        self._saved = (P.COLLECTION, P.LOCK_PATH, P._collection_factory,
                       P.NOTES_ARCHIVE_DIR, P.MAX_NOTES)
        P.COLLECTION = TEST_COLLECTION
        P.LOCK_PATH = os.path.join(self.tmp.name, "plans.lock")
        P._collection_factory = lambda: self.col
        P.NOTES_ARCHIVE_DIR = tempfile.mkdtemp(dir=self.tmp.name)

    def tearDown(self):
        (P.COLLECTION, P.LOCK_PATH, P._collection_factory, P.NOTES_ARCHIVE_DIR,
         P.MAX_NOTES) = self._saved

    def _age(self, plan_id, **meta):
        """Backdate bookkeeping fields directly (the API never sets them to the past)."""
        self.col.update(ids=[plan_id], metadatas=[meta])


class PlanLogicTests(_IsolatedStore):

    # ── parsing ──────────────────────────────────────────────────────────────────

    def test_checklist_mode_imports_only_checkbox_lines(self):
        items, ignored = P.parse_items(FUTURE_WORK_MD)
        self.assertEqual(items, [
            ("**Add a retention job** — done.", "done", "1. Backup hygiene (HIGH)"),
            ("**Alert before the volume fills** — threshold agreed, probe not written.",
             "open", "1. Backup hygiene (HIGH)"),                     # [~] is still open
            ("**Restore drill** into a scratch database.", "open", "1. Backup hygiene (HIGH)"),
            ("**Wiki page** — decided against; the README covers it.", "skipped", "2. Docs"),
            ("**Runbook** for the restore drill", "open", "2. Docs"),
        ])
        # Counted by hand: intro, legend, rule, evidence, 2 indented detail lines,
        # 3 table rows, and the fence (2 fence markers + 1 line inside).
        self.assertEqual(ignored, 12)

    def test_list_mode_without_checkboxes(self):
        text = """
        ## Services
        - api gateway
        1. billing service
        2) reporting
        plain line counts too
        ---
        | a | table |
        > a quote
        ```
        code
        ```
        -
        """
        items, ignored = P.parse_items(text, section="Default")
        self.assertEqual(items, [("api gateway", "open", "Services"),
                                 ("billing service", "open", "Services"),
                                 ("reporting", "open", "Services"),
                                 ("plain line counts too", "open", "Services")])
        self.assertEqual(ignored, 7)          # rule, table, quote, 2 fences + code, bare "-"
        self.assertEqual(P.parse_items("one\ntwo", section="Later"),
                         ([("one", "open", "Later"), ("two", "open", "Later")], 0))
        self.assertEqual(P.parse_items(""), ([], 0))

    def test_legend_lines_and_multi_box_lines_are_not_items(self):
        items, ignored = P.parse_items("[ ] open · [x] done · [~] partial\n- [ ] real item")
        self.assertEqual((items, ignored), ([("real item", "open", "")], 1))

    def test_parse_refs(self):
        valid = [1, 2, 3, 5, 6]
        self.assertEqual(P.parse_refs("3", valid), [3])
        self.assertEqual(P.parse_refs("#3, 1", valid), [3, 1])
        self.assertEqual(P.parse_refs("2-6", valid), [2, 3, 5, 6])     # 4 was removed
        self.assertEqual(P.parse_refs("6-5", valid), [5, 6])
        self.assertEqual(P.parse_refs("3,3 #3", valid), [3])
        for bad in ("4", "1,99", "2-4", "three", "", "   "):
            with self.assertRaises(P.PlanError, msg=bad):
                P.parse_refs(bad, valid)

    # ── create ───────────────────────────────────────────────────────────────────

    def test_create_assigns_slug_ids_and_numbers_items(self):
        r = P.create("Migrate auth to OIDC", items="- [ ] billing\n- [x] gateway", project="proj")
        self.assertEqual(r["id"], "plan_migrate_auth_to_oidc")
        self.assertEqual([(i["id"], i["state"]) for i in r["items"]], [(1, "open"), (2, "done")])
        self.assertTrue(r["items"][1]["closed_at"])
        self.assertEqual((r["next_item_id"], r["ignored_lines"]), (3, 0))
        again = P.create("Migrate auth to OIDC", items="totally different words here", force=True)
        self.assertEqual(again["id"], "plan_migrate_auth_to_oidc_2")

    def test_create_imports_a_future_work_file(self):
        r = P.create("Service future work", items=FUTURE_WORK_MD, status="future")
        self.assertEqual(P.progress(r), {"done": 1, "skipped": 1, "open": 3, "total": 5})
        self.assertEqual(r["ignored_lines"], 12)

    def test_create_rejects_bad_input_without_writing(self):
        with self.assertRaises(P.PlanError):
            P.create("ab")
        with self.assertRaises(P.PlanError):
            P.create("valid title", status="done")
        with self.assertRaises(P.PlanError):
            P.create("valid title", plan_id="has spaces")
        with self.assertRaises(P.PlanError):
            P.create("valid title", items="\n".join(f"item {n}" for n in range(P.MAX_ITEMS + 1)))
        with self.assertRaises(P.PlanError):
            P.create("valid title", items="x" * (P.MAX_ITEM_TEXT + 1))
        self.assertEqual(self.col.count(), 0)
        P.create("first", plan_id="plan_fixed")
        with self.assertRaises(P.PlanError):
            P.create("second one entirely", plan_id="plan_fixed")

    def test_titles_and_item_text_must_be_single_line(self):
        with self.assertRaises(P.PlanError):
            P.create("line one\nline two")
        P.create("Fix C:\\config loader", plan_id="p", items="a")    # a backslash is fine
        with self.assertRaises(P.PlanError):
            P.update("p", edit_item="1", item_text="split\nacross lines")

    def test_create_with_project_global_is_unscoped(self):
        P.create("Everywhere plan", plan_id="g", project="global")
        self.assertEqual(P.get("g")["project"], "")
        active, _ = P.collect("hi", {"some-repo"}, first_message=True)
        self.assertEqual([r["id"] for r in active], ["g"])

    def test_create_refuses_a_near_duplicate_open_plan_unless_forced(self):
        P.create("Cortex plan storage", items="collection\nmcp tools\nstatus line")
        with self.assertRaises(P.PlanError) as cm:
            P.create("Cortex plan storage", items="collection\nmcp tools\nstatus line")
        self.assertIn("plan_cortex_plan_storage", str(cm.exception))
        self.assertEqual(self.col.count(), 1)
        P.create("Cortex plan storage", items="collection\nmcp tools\nstatus line", force=True)
        self.assertEqual(self.col.count(), 2)

    def test_closed_plans_do_not_block_a_new_one(self):
        P.create("Cortex plan storage", items="collection")
        P.update("plan_cortex_plan_storage", status="done")
        P.create("Cortex plan storage", items="collection")          # no PlanError

    def test_fails_closed_when_given_the_wrong_collection(self):
        other = self.client.get_or_create_collection("claude_memories_standin",
                                                     embedding_function=WordsEF())
        P._collection_factory = lambda: other
        with self.assertRaises(P.PlanStoreUnavailable):
            P.create("should not land in the memory store")
        self.assertEqual(other.count(), 0)

    # ── check ────────────────────────────────────────────────────────────────────

    def test_check_sets_states_notes_and_resets_surfacing(self):
        P.create("Plan", plan_id="p", items="a\nb\nc")
        self._age("p", surfaced_count=2)
        r, ids = P.check("p", "1-2", note="commit abc123")
        self.assertEqual(ids, [1, 2])
        self.assertEqual([i["state"] for i in r["items"]], ["done", "done", "open"])
        stored = P.get("p")
        self.assertEqual(stored["surfaced_count"], 0)
        self.assertEqual([i["state"] for i in stored["items"]], ["done", "done", "open"])
        P.check("p", "3", state="skipped")
        P.check("p", "1", state="open", note="reopened: regression found")
        stored = P.get("p")
        self.assertEqual([i["state"] for i in stored["items"]], ["open", "done", "skipped"])
        self.assertEqual(stored["items"][0]["closed_at"], "")
        self.assertEqual(stored["items"][0]["note"], "commit abc123 · reopened: regression found")
        self.assertEqual(P.progress(stored), {"done": 1, "skipped": 1, "open": 1, "total": 3})

    def test_check_is_all_or_nothing(self):
        P.create("Plan", plan_id="p", items="a\nb")
        with self.assertRaises(P.PlanError):
            P.check("p", "1,99")
        self.assertEqual([i["state"] for i in P.get("p")["items"]], ["open", "open"])
        with self.assertRaises(P.PlanError):
            P.check("p", "1", state="finished")
        with self.assertRaises(P.PlanError):
            P.check("missing", "1")
        P.check("p", "2", note="n" * (P.MAX_ITEM_TEXT - 10))
        with self.assertRaises(P.PlanError):                        # note would overflow
            P.check("p", "1,2", note="another long note")
        self.assertEqual([i["state"] for i in P.get("p")["items"]], ["open", "done"])

    # ── update ───────────────────────────────────────────────────────────────────

    def test_item_numbers_are_never_reused(self):
        P.create("Plan", plan_id="p", items="a\nb\nc")
        P.update("p", remove_items="3")
        r = P.update("p", add_items="d\n## Later\ne", section="Now")
        self.assertEqual([(i["id"], i["text"], i["section"]) for i in r["items"]],
                         [(1, "a", ""), (2, "b", ""), (4, "d", "Now"), (5, "e", "Later")])
        self.assertEqual(P.get("p")["next_item_id"], 6)

    def test_add_items_reports_ignored_lines(self):
        P.create("Plan", plan_id="p", items="- [ ] a")
        r = P.update("p", add_items="- [ ] b\n    detail under b")
        self.assertEqual((len(r["items"]), r["ignored_lines"]), (2, 1))

    def test_edit_item_keeps_number_and_state(self):
        P.create("Plan", plan_id="p", items="- [x] old wording\n- [ ] b")
        r = P.update("p", edit_item="#1", item_text="new wording")
        self.assertEqual((r["items"][0]["id"], r["items"][0]["text"], r["items"][0]["state"]),
                         (1, "new wording", "done"))
        self.assertIn("new wording", P.get("p")["document"])        # re-embedded
        with self.assertRaises(P.PlanError):
            P.update("p", edit_item="1,2", item_text="x")
        with self.assertRaises(P.PlanError):
            P.update("p", edit_item="1")

    def test_status_transitions_stamp_and_clear_closed_at(self):
        P.create("Plan", plan_id="p")
        r = P.update("p", status="done")
        self.assertEqual((r["previous_status"], r["status"]), ("active", "done"))
        self.assertTrue(P.get("p")["closed_at"])
        P.update("p", status="future")
        self.assertEqual(P.get("p")["closed_at"], "")
        with self.assertRaises(P.PlanError):
            P.update("p", status="paused")

    def test_project_global_and_dash_clear_values(self):
        P.create("Plan", plan_id="p", project="repo", goal="a goal", tags="t1", links="mem_1")
        P.update("p", project="global")
        P.update("p", goal="-", tags="-", links="-")
        row = P.get("p")
        self.assertEqual((row["project"], row["goal"], row["tags"], row["links"]), ("", "", "", ""))

    def test_notes_append_replace_and_cap(self):
        P.create("Plan", plan_id="p")
        P.update("p", note="stopped at the billing service")
        P.update("p", note="billing migrated")
        notes = P.get("p")["notes"].splitlines()
        self.assertEqual(len(notes), 2)
        self.assertRegex(notes[0],
                         r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] stopped at the billing service$")
        with self.assertRaisesRegex(P.PlanError, "erase the log"):
            P.update("p", notes_mode="replace")
        self.assertEqual(len(P.get("p")["notes"].splitlines()), 2)
        P.update("p", note="condensed", notes_mode="replace")
        self.assertEqual(P.get("p")["notes"], "condensed")
        with self.assertRaises(P.PlanError):
            P.update("p", note="x", notes_mode="overwrite")

    # ── notes over the cap move to the plan's archive file (2026-10-14) ──────────────

    def _fill(self, n, width=60):
        """Append n stamped notes "note 01 ...", each `width` characters of text."""
        for i in range(1, n + 1):
            P.update("p", note=f"note {i:02d} " + "x" * (width - 8))

    def _archived(self):
        path = P.notes_archive_path("p")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_an_append_over_the_cap_moves_the_oldest_notes_out_and_loses_none(self):
        P.MAX_NOTES = 600
        P.create("Plan", plan_id="p")
        self._fill(12)                     # 12 notes of 19 + 60 chars, a newline apart: 959 > 600
        notes = P.get("p")["notes"]
        self.assertLessEqual(len(notes), 600)
        first, *rest = notes.splitlines()
        self.assertEqual(first, f"(Older notes moved to {P.notes_archive_path('p')}; "
                                "read it for the earlier history.)")
        kept = [int(re.search(r"note (\d\d)", line).group(1)) for line in rest]
        archived = [int(n) for n in re.findall(r"\] note (\d\d) ", self._archived())]
        self.assertEqual(kept, list(range(kept[0], 13)), "the newest notes are kept, in order")
        self.assertEqual(archived + kept, list(range(1, 13)), "every note is in exactly one place")
        self.assertEqual(oct(os.stat(P.notes_archive_path("p")).st_mode & 0o777), "0o600")

    def test_the_archive_line_is_one_line_however_often_notes_move(self):
        P.MAX_NOTES = 400
        P.create("Plan", plan_id="p")
        self._fill(30)
        notes = P.get("p")["notes"]
        self.assertEqual(notes.count("(Older notes moved to"), 1)
        archived = [int(n) for n in re.findall(r"\] note (\d\d) ", self._archived())]
        kept = [int(n) for n in re.findall(r"\] note (\d\d) ", notes)]
        self.assertEqual(archived + kept, list(range(1, 31)))
        self.assertGreater(self._archived().count("moved out of plan p"), 1)

    def test_a_condensed_summary_stays_while_it_fits_and_goes_first_when_it_must(self):
        P.MAX_NOTES = 600
        P.create("Plan", plan_id="p")
        P.update("p", note="SUMMARY: the state of things.", notes_mode="replace")
        self._fill(12)
        notes = P.get("p")["notes"]
        self.assertIn("SUMMARY: the state of things.", notes.splitlines()[1],
                      "kept, under the archive line")
        self.assertNotIn("SUMMARY", self._archived())
        marker = P.notes_archive_marker("p") + "\n"
        # fits alone after the archive line, but not with the summary as well
        big = "BIG " + "y" * (600 - len(marker) - len("[2026-10-14 10:31] BIG ") - 10)
        P.update("p", note=big)
        notes = P.get("p")["notes"]
        self.assertNotIn("SUMMARY", notes)
        archive = self._archived()
        self.assertLess(archive.rindex("SUMMARY"), archive.rindex("] note 12 "),
                        "the summary is older than every stamped note, so it is archived first")
        self.assertTrue(notes.splitlines()[-1].endswith(big))

    def test_a_note_too_long_to_fit_alone_is_refused_and_nothing_moves(self):
        P.MAX_NOTES = 300
        P.create("Plan", plan_id="p")
        self._fill(3)
        before = P.get("p")["notes"]
        with self.assertRaisesRegex(P.PlanError,
                                    r"this note is 309 chars with its timestamp; at most \d+ fit"):
            P.update("p", note="z" * 290)
        self.assertEqual(P.get("p")["notes"], before)
        self.assertEqual(self._archived(), "")

    def test_a_condensed_log_over_the_cap_is_still_refused(self):
        P.MAX_NOTES = 300
        P.create("Plan", plan_id="p")
        with self.assertRaisesRegex(P.PlanError, "condensed notes too long"):
            P.update("p", note="c" * 301, notes_mode="replace")
        self.assertEqual(self._archived(), "")

    def test_a_failed_archive_write_changes_nothing(self):
        P.MAX_NOTES = 400
        P.create("Plan", plan_id="p")
        self._fill(5)
        before = P.get("p")["notes"]
        blocker = os.path.join(self.tmp.name, "a-file")
        open(blocker, "w").close()
        P.NOTES_ARCHIVE_DIR = os.path.join(blocker, "sub")    # no folder can be made under a file
        self._age("p", updated="2000-01-01T00:00:00")     # a 1-second clock could hide a bump
        updated = P.get("p")["updated"]
        with self.assertRaisesRegex(P.PlanError, "could not move older notes"):
            self._fill(6)
        self.assertEqual((P.get("p")["notes"], P.get("p")["updated"]), (before, updated))

    def test_the_archive_file_name_cannot_leave_its_folder(self):
        self.assertEqual(os.path.dirname(P.notes_archive_path("../../etc/x")), P.NOTES_ARCHIVE_DIR)

    def test_the_file_and_every_folder_made_for_it_are_flushed_every_time(self):
        P.MAX_NOTES = 400
        P.NOTES_ARCHIVE_DIR = os.path.join(self.tmp.name, "fresh", "notes")
        flushed = []
        real_fsync, real_open = os.fsync, os.open
        opened = {}

        def fake_open(path, flags, *a):
            fd = real_open(path, flags, *a)
            opened[fd] = path
            return fd

        def fake_fsync(fd):
            flushed.append(opened.get(fd, "the archive file"))
            real_fsync(fd)
        P.create("Plan", plan_id="p")
        P.os.open, P.os.fsync = fake_open, fake_fsync
        try:
            self._fill(8)
        finally:
            P.os.open, P.os.fsync = real_open, real_fsync
        notes_dir = P.NOTES_ARCHIVE_DIR
        self.assertIn(notes_dir, flushed)
        self.assertIn(os.path.dirname(notes_dir), flushed, "the folder made for it")
        self.assertIn(os.path.dirname(os.path.dirname(notes_dir)), flushed, "and the one above")
        before = flushed.count(notes_dir)
        P.os.open, P.os.fsync = fake_open, fake_fsync
        try:
            self._fill(8)
        finally:
            P.os.open, P.os.fsync = real_open, real_fsync
        self.assertGreater(flushed.count(notes_dir), before, "flushed again on a later move")
        self.assertGreater(flushed.count(os.path.dirname(notes_dir)), 1,
                           "and its parent again too, so a retry after a failed flush skips none")

    def test_the_folders_flushed_are_the_real_ones_behind_a_symlink(self):
        P.MAX_NOTES = 400
        real = os.path.join(self.tmp.name, "real", "deep")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(real, link)
        P.NOTES_ARCHIVE_DIR = os.path.join(link, "..", "notes")   # really <tmp>/real/notes
        flushed, real_fsync, real_open, opened = [], os.fsync, os.open, {}

        def fake_open(path, flags, *a):
            fd = real_open(path, flags, *a)
            opened[fd] = path
            return fd
        P.create("Plan", plan_id="p")
        P.os.open = fake_open
        P.os.fsync = lambda fd: (flushed.append(opened.get(fd, "file")), real_fsync(fd))
        try:
            self._fill(8)
        finally:
            P.os.open, P.os.fsync = real_open, real_fsync
        self.assertIn(os.path.join(os.path.realpath(self.tmp.name), "real", "notes"), flushed)
        self.assertIn(os.path.join(os.path.realpath(self.tmp.name), "real"), flushed)

    def test_a_condensed_log_starting_with_the_marker_and_crlf_gets_no_second_marker(self):
        P.MAX_NOTES = 400
        P.create("Plan", plan_id="p")
        self._fill(8)
        P.update("p", note=P.notes_archive_marker("p") + "\r\nkept", notes_mode="replace")
        self.assertEqual(P.get("p")["notes"].count("(Older notes moved to"), 1)

    def test_a_folder_setting_with_a_line_break_is_refused(self):
        P.NOTES_ARCHIVE_DIR = os.path.join(self.tmp.name, "a\nb")
        with self.assertRaisesRegex(P.PlanError, "single-line path"):
            P.notes_archive_path("p")

    def test_the_archive_folder_is_made_owner_only_when_it_does_not_exist(self):
        P.MAX_NOTES = 400
        P.NOTES_ARCHIVE_DIR = os.path.join(self.tmp.name, "not-yet", "notes")
        P.create("Plan", plan_id="p")
        self._fill(8)
        self.assertEqual(oct(os.stat(P.NOTES_ARCHIVE_DIR).st_mode & 0o777), "0o700")
        self.assertIn("] note 01 ", self._archived())

    def test_the_new_note_is_never_split_by_a_line_in_it_that_looks_like_a_stamp(self):
        P.MAX_NOTES = len(P.notes_archive_marker("p")) + 300
        P.create("Plan", plan_id="p")
        self._fill(4)
        body = "pasted log:\n[2026-01-01 00:00] an old line\n" + "w" * 150
        P.update("p", note=body)
        notes = P.get("p")["notes"]
        self.assertTrue(notes.endswith(body), "the whole new note is kept")
        self.assertNotIn("an old line", self._archived())

    def test_only_this_plans_own_marker_line_is_dropped(self):
        # A summary whose first line merely starts like the marker is text, not the marker (Codex).
        P.MAX_NOTES = 500
        P.create("Plan", plan_id="p")
        lookalike = "(Older notes moved to the shelf; ask Ana.)\nSUMMARY"
        P.update("p", note=lookalike, notes_mode="replace")
        self._fill(8)
        notes, archive = P.get("p")["notes"], self._archived()
        self.assertEqual(notes.count(P.notes_archive_marker("p")), 1)
        self.assertEqual((notes + archive).count(lookalike), 1,
                         "kept or archived whole, never dropped")

    def test_a_blank_preamble_does_not_block_a_note_that_fits(self):
        marker = P.notes_archive_marker("p")
        P.MAX_NOTES = len(marker) + 200
        P.create("Plan", plan_id="p")
        # a replace refuses blank text, so the blank log is set in the store directly
        self._age("p", notes=" \n" * (len(marker) // 2 + 20))
        note = "q" * (200 - 1 - len("[2026-10-14 10:31] ") - 5)    # fits after the marker alone
        calls = []
        real = P._fit_notes
        P._fit_notes = lambda *a: calls.append(a) or real(*a)
        try:
            P.update("p", note=note)
        finally:
            P._fit_notes = real
        self.assertEqual(len(calls), 1, "the append overflowed, so the fitting ran")
        self.assertEqual(P.get("p")["notes"].split("\n")[0], marker)
        self.assertTrue(P.get("p")["notes"].endswith(note))

    def test_a_condensed_log_keeps_pointing_at_the_archive(self):
        P.MAX_NOTES = 400
        P.create("Plan", plan_id="p")
        P.update("p", note="no archive yet", notes_mode="replace")
        self.assertEqual(P.get("p")["notes"], "no archive yet")
        self._fill(8)
        P.update("p", note="CONDENSED: the state now", notes_mode="replace")
        self.assertEqual(P.get("p")["notes"],
                         P.notes_archive_marker("p") + "\nCONDENSED: the state now")
        P.update("p", note=P.notes_archive_marker("p") + "\nagain", notes_mode="replace")
        self.assertEqual(P.get("p")["notes"].count("(Older notes moved to"), 1)

    def test_update_is_atomic_when_a_later_field_is_invalid(self):
        P.create("Original title", plan_id="p", items="a")
        with self.assertRaises(P.PlanError):
            P.update("p", title="Changed title", remove_items="99")
        with self.assertRaises(P.PlanError):
            P.update("p", title="Changed title", note="y" * (P.MAX_NOTES + 1))
        self.assertEqual(P.get("p")["title"], "Original title")
        with self.assertRaises(P.PlanError):
            P.update("p")                                           # nothing to update

    # ── delete ───────────────────────────────────────────────────────────────────

    def test_delete_archives_first_and_keeps_the_plan_if_archiving_fails(self):
        P.create("Plan", plan_id="p", items="a")

        def broken(row):
            raise OSError("disk full")

        with self.assertRaises(OSError):
            P.delete("p", archive=broken)
        self.assertIsNotNone(P.get("p"))
        seen = []
        self.assertEqual(P.delete("p", archive=seen.append)["id"], "p")
        self.assertEqual(seen[0]["items"][0]["text"], "a")
        self.assertIsNone(P.get("p"))
        self.assertIsNone(P.delete("p"))

    # ── list / summary ───────────────────────────────────────────────────────────

    def test_list_orders_active_first_then_newest_and_filters(self):
        P.create("old active work", plan_id="a_old", project="x")
        P.create("parked future work", plan_id="f", status="future")
        P.create("new active work", plan_id="a_new")
        P.create("finished work", plan_id="d")
        P.update("d", status="done")
        self._age("a_old", updated=_iso(3600))
        self.assertEqual([r["id"] for r in P.list_plans()], ["a_new", "a_old", "f"])
        self.assertEqual([r["id"] for r in P.list_plans(status="done")], ["d"])
        self.assertEqual(len(P.list_plans(status="all")), 4)
        self.assertEqual([r["id"] for r in P.list_plans(project="x")], ["a_old"])
        self.assertEqual({r["id"] for r in P.list_plans(project="global")}, {"a_new", "f"})
        ranked = P.list_plans(query="parked future")
        self.assertEqual(ranked[0]["id"], "f")
        self.assertIn("distance", ranked[0])
        with self.assertRaises(P.PlanError):
            P.list_plans(status="someday")

    def test_summary_counts_active_progress(self):
        P.create("alpha", plan_id="a", items="- [x] one\n- [-] two\n- [ ] three")
        P.create("beta", plan_id="b", items="one")
        P.create("gamma", plan_id="g", status="future", items="one\ntwo")
        self._age("a", updated=_iso(60))
        s = P.summary()
        self.assertEqual((s["active"], s["future"], s["active_done"], s["active_total"]),
                         (2, 1, 2, 4))
        self.assertEqual(s["latest"]["id"], "b")

    # ── surfacing ────────────────────────────────────────────────────────────────

    def test_first_prompt_lists_active_plans_in_scope_only(self):
        P.create("repo work", plan_id="mine", project="shop-api")
        P.create("everywhere work", plan_id="global")
        P.create("other repo work", plan_id="theirs", project="elsewhere")
        P.create("parked work", plan_id="parked", status="future", project="shop-api")
        P.create("closed work", plan_id="closed", project="shop-api")
        P.update("closed", status="done")
        active, relevant = P.collect("hi", {"shop-api"}, first_message=True)
        self.assertEqual({r["id"] for r in active}, {"mine", "global"})
        self.assertEqual(relevant, [])
        active, _ = P.collect("hi", {"shop-api"}, first_message=False)
        self.assertEqual(active, [])

    def test_first_prompt_caps_active_list(self):
        for n in range(P.MAX_SESSION_ACTIVE + 2):
            P.create(f"work stream {n} unique{n}", plan_id=f"p{n}", force=True)
        active, _ = P.collect("hi", set(), first_message=True)
        self.assertEqual(len(active), P.MAX_SESSION_ACTIVE)

    def test_subject_match_surfaces_future_plan_with_cooldown_and_demotion(self):
        P.create("prune old backups", plan_id="backups", status="future",
                 items="retention job prune old backups")
        P.create("dashboard chart fonts", plan_id="fonts", items="chart label fonts")
        prompt = "add a retention job to prune the old backups"
        _, rel = P.collect(prompt, set(), first_message=False)
        self.assertEqual([r["id"] for r in rel], ["backups"])

        P.record_surfaced([], rel)
        self.assertEqual(P.get("backups")["surfaced_count"], 1)
        _, rel = P.collect(prompt, set(), first_message=False)
        self.assertEqual(rel, [], "cooldown should hold it back")

        self._age("backups", last_surfaced=_iso(2 * P.COOLDOWN_SECONDS),
                  surfaced_count=P.DEMOTE_AFTER)
        _, rel = P.collect(prompt, set(), first_message=False)
        self.assertEqual(rel, [], "demoted after repeated unacted raises")
        _, rel = P.collect(prompt, set(), first_message=True)
        self.assertEqual([r["id"] for r in rel], ["backups"], "still offered at session open")

        P.check("backups", "1")                                      # touching it revives it
        self._age("backups", last_surfaced=_iso(2 * P.COOLDOWN_SECONDS))
        _, rel = P.collect(prompt, set(), first_message=False)
        self.assertEqual([r["id"] for r in rel], ["backups"])

    def test_closed_plans_on_the_same_subject_cannot_hide_an_open_one(self):
        """Done plans are kept forever. If the relevance query were not filtered to open
        plans, enough closed ones would take every returned rank."""
        words = "prune old backups retention job nightly"
        for n in range(25):
            P.create(words, plan_id=f"done{n}", items=words, force=True)
            P.update(f"done{n}", status="done")
        P.create("backup retention follow-up", plan_id="live", status="future",
                 items="prune old backups")
        _, rel = P.collect(words, set(), first_message=False)
        self.assertEqual([r["id"] for r in rel], ["live"])

    def test_active_plan_shown_at_session_open_is_not_repeated_as_relevant(self):
        P.create("prune old backups", plan_id="backups")
        active, rel = P.collect("prune old backups", set(), first_message=True)
        self.assertEqual([r["id"] for r in active], ["backups"])
        self.assertEqual(rel, [])

    def test_bookkeeping_never_overwrites_a_concurrent_checklist_edit(self):
        """The hook reads a plan, another session ticks an item, then the hook records
        that it surfaced the plan. The tick must survive."""
        P.create("prune old backups", plan_id="backups", items="a\nb")
        _, stale = P.collect("prune old backups", set(), first_message=False)
        self.assertEqual(len(stale), 1)
        P.check("backups", "1")                                      # concurrent session
        P.record_surfaced([], stale)
        row = P.get("backups")
        self.assertEqual([i["state"] for i in row["items"]], ["done", "open"])
        self.assertEqual(row["surfaced_count"], 1)

    def test_bookkeeping_skips_quietly_when_the_lock_is_held(self):
        P.create("backups", plan_id="backups")
        fd = os.open(P.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(P.PlanStoreUnavailable):
                with P._locked(timeout=0.2):
                    pass
            P.record_surfaced([P.get("backups")], [])               # must not raise
        finally:
            os.close(fd)
        self.assertEqual(P.get("backups")["last_surfaced"], "")

    # ── rendering ────────────────────────────────────────────────────────────────

    def test_render_full_and_brief(self):
        P.create("Backups", plan_id="p", goal="The disk never fills.", project="repo",
                 items="## Jobs\n- [x] retention\n- [ ] alert")
        P.update("p", note="alert threshold agreed")
        full = P.render_full(P.get("p"))
        for expected in ("# Backups  [p]", "1/2 done", "Goal:", "\n\n## Jobs\n",
                         "- [x] #1 retention", "- [ ] #2 alert", "alert threshold agreed"):
            self.assertIn(expected, full)
        self.assertNotIn("\n\n\n", full)
        self._age("p", updated=_iso((P.STALE_DAYS + 1) * 86400))
        brief = P.render_brief([P.get("p")], "HEADER")
        self.assertIn("untouched", brief)
        self.assertIn("next: #2 alert", brief)


class PlanToolTests(_IsolatedStore):
    """The MCP tool layer: answers are one-liners, refusals never raise."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import mcp_server
        cls.M = mcp_server

    def setUp(self):
        super().setUp()
        self._audit = self.M.AUDIT_LOG
        # one audit log per test, so an entry from one test cannot satisfy another
        self.M.AUDIT_LOG = os.path.join(tempfile.mkdtemp(dir=self.tmp.name), "audit.jsonl")

    def tearDown(self):
        self.M.AUDIT_LOG = self._audit
        super().tearDown()

    def test_tool_round_trip(self):
        M = self.M
        out = M.plan_create("Tool plan", items="- [ ] one\n- [ ] two", project="repo")
        self.assertIn("Plan plan_tool_plan created (active, 2 item(s), scope: repo)", out)
        self.assertIn("next: #1 one", out)
        self.assertNotIn("NOT imported", out)
        self.assertIn("Plan unchanged", M.plan_check("plan_tool_plan", "7"))
        out = M.plan_check("plan_tool_plan", "1,2")
        self.assertIn("2/2 done", out)
        self.assertIn('status="done"', out)                          # prompt to close
        out = M.plan_update("plan_tool_plan", status="done")
        self.assertIn("status active → done", out)
        self.assertIn("No open plans", M.plan_list())
        self.assertIn("Tool plan", M.plan_list(status="done"))
        self.assertIn("1 plan(s):", M.plan_list(status="all"))
        self.assertIn("- [x] #2 two", M.plan_get("plan_tool_plan"))
        self.assertIn("No plan nope", M.plan_get("nope"))
        self.assertIn("archived", M.plan_delete("plan_tool_plan"))
        with open(M.AUDIT_LOG) as f:
            entries = [json.loads(line) for line in f]
        backup = [e for e in entries if e["action"] == "plan_delete"][0]["plan_backup"]
        self.assertEqual(backup["items"][1]["text"], "two")

    def test_the_tool_says_where_older_notes_went(self):
        P.MAX_NOTES = len(P.notes_archive_marker("p")) + 300
        P.create("Plan", plan_id="p")
        i = 0
        while len(P.get("p")["notes"] or "") <= P.MAX_NOTES - 79:   # until the next note overflows
            i += 1
            P.update("p", note=f"note {i:02d} " + "x" * 52)
        out = self.M.plan_update("p", note="the overflowing note " + "x" * 40)
        self.assertIn(f"older note(s) moved to {P.notes_archive_path('p')} to stay under "
                      f"{P.MAX_NOTES} chars", out)
        out = self.M.plan_update("p", note="z" * P.MAX_NOTES)
        self.assertIn("at most", out)
        P.create("Other", plan_id="q")
        self.assertNotIn("moved to", self.M.plan_update("q", note="short"))
        with open(self.M.AUDIT_LOG) as f:
            actions = [json.loads(line)["action"] for line in f]
        self.assertIn("plan_notes_archived", actions)
        self.assertIn(f"Its older notes remain at {P.notes_archive_path('p')}",
                      self.M.plan_delete("p"))

    def test_delete_says_deleted_even_when_the_archive_setting_is_broken(self):
        P.create("Plan", plan_id="p")
        P.NOTES_ARCHIVE_DIR = "bad\npath"
        out = self.M.plan_delete("p")
        self.assertIn("Plan p deleted", out)
        self.assertIsNone(P.get("p"))

    def test_import_reports_lines_left_behind(self):
        out = self.M.plan_create("Service future work", items=FUTURE_WORK_MD, status="future")
        self.assertIn("5 item(s)", out)
        self.assertIn("12 line(s) were not checklist items and were NOT imported", out)

    def test_query_listing_shows_match(self):
        self.M.plan_create("prune old backups", items="retention job")
        self.assertRegex(self.M.plan_list(query="prune old backups"), r"\d+% match")

    def test_delete_refuses_when_the_archive_cannot_be_written(self):
        self.M.plan_create("Keep me", plan_id="k", items="a")
        self.M.AUDIT_LOG = os.path.join(self.tmp.name, "no-such-dir", "audit.jsonl")
        self.assertIn("NOT deleted", self.M.plan_delete("k"))
        self.assertIn("Keep me", self.M.plan_get("k"))

    def test_closing_with_open_items_is_called_out(self):
        self.M.plan_create("Half done", plan_id="h", items="a\nb")
        self.M.plan_check("h", "1")
        self.assertIn("1 item(s) still open", self.M.plan_update("h", status="done"))

    def test_store_unavailable_is_reported_not_raised(self):
        other = self.client.get_or_create_collection("claude_memories_standin2",
                                                     embedding_function=WordsEF())
        P._collection_factory = lambda: other
        self.assertIn("Plan store unavailable", self.M.plan_create("anything at all"))


class EnrichHookTests(unittest.TestCase):
    """cortex_pretool_enrich.sh fills in plan_create's project from the repo."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = cls.tmp.name
        cls.repo = os.path.join(base, "shop-api")
        git = lambda *a: subprocess.run(["git", *a], capture_output=True, check=True)
        git("init", "-q", cls.repo)
        git("-C", cls.repo, "-c", "user.email=t@example.org", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "init")
        os.makedirs(os.path.join(cls.repo, "src", "billing"))
        cls.worktree = os.path.join(base, "shop-api-feature-x")
        git("-C", cls.repo, "worktree", "add", "-q", cls.worktree)
        cls.plain = os.path.join(base, "notes-dir", "deeper")
        os.makedirs(cls.plain)
        cls.log = os.path.join(base, "ops.jsonl")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, tool, tool_input, cwd):
        payload = json.dumps({"tool_name": f"mcp__cortex__{tool}", "tool_input": tool_input,
                              "cwd": cwd})
        p = subprocess.run(["bash", os.path.join(ROOT, "cortex_pretool_enrich.sh")],
                           input=payload, capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, CORTEX_OPS_LOG=self.log))
        out = json.loads(p.stdout)["hookSpecificOutput"]
        return out.get("updatedInput", {}).get("project")

    def test_plan_project_is_the_repo_name_from_any_subdirectory(self):
        self.assertEqual(self._run("plan_create", {"title": "t"},
                                   os.path.join(self.repo, "src", "billing")), "shop-api")

    def test_plan_made_in_a_worktree_belongs_to_the_main_repo(self):
        self.assertEqual(self._run("plan_create", {"title": "t"}, self.worktree), "shop-api")

    def test_outside_git_falls_back_to_the_directory_heuristic(self):
        self.assertEqual(self._run("plan_create", {"title": "t"}, self.plain), "deeper")

    def test_explicit_project_and_global_are_left_alone(self):
        self.assertIsNone(self._run("plan_create", {"title": "t", "project": "global"}, self.repo))
        self.assertIsNone(self._run("plan_create", {"title": "t", "project": "x"}, self.repo))

    def test_memory_store_keeps_its_existing_behaviour(self):
        self.assertEqual(self._run("memory_store", {"content": "c"},
                                   os.path.join(self.repo, "src", "billing")), "billing")

    def test_plan_id_is_logged(self):
        self._run("plan_check", {"plan_id": "plan_x", "items": "1"}, self.repo)
        with open(self.log) as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual((last["tool"], last["plan_id"]), ("plan_check", "plan_x"))


def _live_services_up():
    try:
        from lib import embedding
        from lib.chroma_client import get_client
        return embedding.embedding_available() and bool(get_client().heartbeat())
    except Exception:
        return False


@unittest.skipUnless(_live_services_up(), "embed sidecar or Chroma server not reachable")
class PlanLiveTests(unittest.TestCase):
    """Real embedder + real server, isolated collection, removed afterwards."""

    NAME = f"cortex_plans_test_{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        assert cls.NAME.startswith("cortex_plans_test_")             # never anything else
        from lib.chroma_client import get_client
        cls.client = get_client()
        cls.tmp = tempfile.TemporaryDirectory()
        cls._saved = (P.COLLECTION, P.LOCK_PATH, P._collection_factory)
        P.COLLECTION, P._collection_factory = cls.NAME, None
        P.LOCK_PATH = os.path.join(cls.tmp.name, "plans.lock")

    @classmethod
    def tearDownClass(cls):
        P.COLLECTION, P.LOCK_PATH, P._collection_factory = cls._saved
        assert cls.NAME.startswith("cortex_plans_test_")
        with contextlib.suppress(Exception):
            cls.client.delete_collection(cls.NAME)
        cls.tmp.cleanup()

    def setUp(self):
        assert P.COLLECTION.startswith("cortex_plans_test_")         # never the real store
        for r in P.list_plans(status="all"):
            P.delete(r["id"])
        self.env = dict(os.environ, CORTEX_PLANS_COLLECTION=self.NAME,
                        CORTEX_PLANS_LOCK=P.LOCK_PATH,
                        CORTEX_PLANS_NOTES_ARCHIVE=os.path.join(self.tmp.name, "notes-archive"))

    def _hook(self, payload, **env):
        p = subprocess.run(["bash", os.path.join(ROOT, "plans.sh")], input=payload,
                           env={**self.env, **env}, capture_output=True, text=True,
                           timeout=60)
        return p.returncode, p.stdout.strip()

    def _statusline(self, **env):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(["bash", os.path.join(ROOT, "statusline.sh")], input="{}",
                               env={**self.env, "CORTEX_STATUSLINE_CACHE":
                                    os.path.join(d, "cache"), **env},
                               capture_output=True, text=True, timeout=120)
        return p.stdout.splitlines()

    def test_threshold_separates_related_from_unrelated_prompts(self):
        P.create("Nightly backup hygiene", status="future",
                 goal="Stop old database backups filling the disk.",
                 items="- [ ] retention job that prunes backups older than 30 days\n"
                       "- [ ] alert when the backup volume passes 80% full")
        _, rel = P.collect("the backup disk is nearly full again, can we prune old backups?",
                           set(), first_message=False, cooldown_seconds=0)
        self.assertEqual(len(rel), 1)
        for unrelated in ("fix the chart font size on the dashboard",
                          "draft an email about the quarterly report",
                          "why is the CI pipeline red on main"):
            _, rel = P.collect(unrelated, set(), first_message=False, cooldown_seconds=0)
            self.assertEqual(rel, [], unrelated)

    def test_duplicate_threshold_blocks_a_recreate_but_not_a_sibling_plan(self):
        items = ("- [ ] retention job that prunes old backups\n"
                 "- [ ] alert when the backup volume fills")
        P.create("Nightly backup hygiene", items=items, project="repo")
        with self.assertRaises(P.PlanError):
            P.create("Nightly backup hygiene", items=items, project="repo")
        P.create("Backup restore drills", project="repo",
                 items="- [ ] monthly restore test into a scratch database\n"
                       "- [ ] document the restore runbook")

    def test_statusline_plan_line_and_isolation(self):
        from lib import reminders as R
        rem_name = self.NAME + "_rem"
        assert rem_name.startswith("cortex_plans_test_")
        saved = R.COLLECTION
        R.COLLECTION = rem_name
        try:
            R.add("a throwaway reminder so the reminder line is present", due="+30d")
            env = {"CORTEX_REMINDERS_COLLECTION": rem_name}
            plan_lines = lambda ls: [l for l in ls if l.startswith("\U0001f4cb")]
            reminder_lines = lambda ls: [l for l in ls if l.startswith("\u23f0")]

            self.assertEqual(plan_lines(self._statusline(**env)), [], "no plans -> no line")
            P.create("A deliberately very long plan title that must be shortened",
                     items="- [x] one\n- [-] two\n- [ ] three")
            P.create("Parked backlog", status="future", items="later")
            healthy = self._statusline(**env)
            self.assertEqual(plan_lines(healthy), [
                "\U0001f4cb 1 active (2/3 resolved) | 1 future | "
                "latest: A deliberately very long plan t\u2026 2/3"])
            self.assertEqual(len(reminder_lines(healthy)), 1)

            # A plan store that fails closed (invalid collection name) removes the plan
            # line and leaves the reminder line, computed in the same process, intact.
            broken = self._statusline(CORTEX_PLANS_COLLECTION="!", **env)
            self.assertEqual(plan_lines(broken), [])
            self.assertEqual(reminder_lines(broken), reminder_lines(healthy))
        finally:
            R.COLLECTION = saved
            with contextlib.suppress(Exception):
                self.client.delete_collection(rem_name)

    def test_statusline_survives_a_backslash_in_a_title(self):
        """The output goes through echo -e; an unescaped backslash-c would cut it off."""
        P.create("Fix C:\\config loader", items="- [ ] one")
        lines = self._statusline()
        self.assertIn("\U0001f4cb 1 active (0/1 resolved) | latest: Fix C:\\config loader 0/1",
                      lines)
        self.assertTrue(any(l.startswith("\u2699") for l in lines),
                        "lines after the plan line must still be printed")

    def test_hook_end_to_end(self):
        P.create("Cortex plan storage", project="cortexproj",
                 items="- [x] library\n- [ ] status line entry")

        self.assertEqual(self._hook("not json at all"), (0, ""))
        self.assertEqual(self._hook(json.dumps(["a", "list"])), (0, ""))

        # First prompt, in scope: the active plan is listed with its next item.
        rc, out = self._hook(json.dumps({"prompt": "hello", "cwd": "/somewhere/cortexproj"}))
        self.assertEqual(rc, 0)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Active plans in scope here", ctx)
        self.assertIn("Cortex plan storage", ctx)
        self.assertIn("next: #2 status line entry", ctx)

        # Out of scope (including a directory that merely CONTAINS the project name),
        # headless, and system-injected prompts: nothing.
        self.assertEqual(self._hook(json.dumps({"prompt": "hello", "cwd": "/somewhere/else"})),
                         (0, ""))
        self.assertEqual(self._hook(json.dumps({"prompt": "hello",
                                                "cwd": "/somewhere/old-cortexproj-copy"})),
                         (0, ""))
        self.assertEqual(self._hook(json.dumps({"prompt": "hello", "cwd": "/x/cortexproj"}),
                                    CLAUDE_HEADLESS="1"), (0, ""))
        self.assertEqual(self._hook(json.dumps({"prompt": "<system>hello</system>",
                                                "cwd": "/x/cortexproj"})), (0, ""))
        # A plan store that fails closed never blocks or breaks the prompt.
        self.assertEqual(self._hook(json.dumps({"prompt": "hello", "cwd": "/x/cortexproj"}),
                                    CORTEX_PLANS_COLLECTION="!"), (0, ""))

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as t:
            t.write(json.dumps({"message": {"role": "assistant",
                                            "content": "an earlier reply in this session"}}))
        try:
            later = {"cwd": "/somewhere/cortexproj", "transcript_path": t.name}
            # Later prompt, unrelated subject: nothing.
            self.assertEqual(self._hook(json.dumps(
                {**later, "prompt": "what's the weather like for the weekend"})), (0, ""))
            # Later prompt, related subject: the related-plan block, and it is recorded.
            # (It was just listed at session open, which starts its cooldown; age that.)
            plan_id = P.list_plans()[0]["id"]
            P.get_plans_collection().update(ids=[plan_id],
                                            metadatas=[{"last_surfaced": _iso(3 * 3600)}])
            rc, out = self._hook(json.dumps(
                {**later, "prompt": "add the plan storage entry to the cortex status line"}))
            self.assertEqual(rc, 0)
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Open plan related to what was just asked", ctx)
            row = P.list_plans()[0]
            self.assertEqual(row["surfaced_count"], 1)
            self.assertGreater(row["last_surfaced"], _iso(60))
        finally:
            os.unlink(t.name)


if __name__ == "__main__":
    unittest.main()
