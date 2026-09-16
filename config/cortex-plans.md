# Cortex Plans — Work That Outlives the Session

## What a plan is, and what it is not

A **plan** is a goal plus the steps toward it — a checklist ticked off item by item,
often across several sessions. Parked **future work** ("later", "phase 2", "the
backlog") is a plan too, with status `future`.

| Thing | Holds | Lives |
|---|---|---|
| **Memory** | something *known* — a fact, a decision, a rule | cortex `claude_memories_v2` |
| **Reminder** | *one* thing *owed*, usually at a time | cortex `cortex_reminders` |
| **Plan** | a *goal* and its *steps*, progressed item by item | cortex `cortex_plans` |
| Todo / task list | steps for *this session only* | the session; gone when it ends |
| Plan mode | a *proposal* awaiting the user's approval | the session |

Plan mode and the todo list are for the session at hand. A cortex plan is for work that
has to survive the session ending.

## When to CREATE one — only when asked

Create a plan when the user asks for one: *make this a plan / track this / park this
for later / add it to the future work / put it on the backlog / add it to the plan*.

Announce it in one line — `Noted a plan: <plan_id> — <title> (<n> items)` — so it can be
killed immediately if unwanted. Never create one silently.

## When to OFFER one — one line, then drop it

Offer, without creating, when:

- multi-step work clearly will not finish in this session;
- an approved plan-mode plan spans more than this session;
- a review produces findings deliberately deferred as out of scope.

One line — *"Want me to keep the remaining steps as a cortex plan?"* — and if the user
doesn't take it up, don't ask again for the same work.

## Before creating or offering

- **Search first:** `plan_list(query="<subject>")`. If an open plan already covers it,
  extend that one with `plan_update(add_items=...)`. (The store also refuses a
  near-identical open plan.)
- **Not a plan:** work finished in this session (todo list), a single timed follow-up
  (reminder), a fact or decision (memory), or anything already tracked by an issue, MR
  or pipeline — link to that instead.
- **One source of truth.** If a repo file is already the canonical list (a
  `future_work.md`, a `TODO.md`), don't create a second copy that will drift. Either
  leave the file as the source, or — with the user's OK — import it and say the file is
  superseded.

## Importing an existing checklist

`plan_create(items=...)` reads Markdown. If any line is a checkbox, **only checkbox
lines become items**, keeping their state: `[ ]` and `[~]` open, `[x]` done, `[-]`
skipped. `## Headings` become sections. Prose, indented detail under an item, code and
tables are **not** imported — the tool reports how many lines were left behind. That
detail stays in the source file or goes into a memory linked from the plan (`links=`);
tell the user it was not carried over.

## While working a plan

- **Tick items as each one finishes** (`plan_check`), with `note=` carrying the commit
  hash or the reason. Not in a batch at the end: sessions end and get compacted without
  warning, and an unticked item reads as not done.
- **"done" means verified.** An item implemented but not yet verified stays `open`,
  with a note saying what is still unproven.
- **A step deliberately not taken** → `state="skipped"` with the reason. Don't silently
  remove it — the record of what was decided against is part of the plan.
- **New steps discovered** → `plan_update(add_items=...)`.
- **Stopping mid-plan** → `plan_update(note="stopped at #4: <state>; next: <step>;
  blocked on: <what>")`, so the next session resumes instead of re-deriving.
- Item numbers are stable for life (removing #3 never renumbers #4), so a number quoted
  in a note or to the user stays correct.

## When one is SURFACED

Plans arrive in a `[cortex]` block, in two forms:

- **Active plans in scope here** — on the first prompt of a session. This is
  **background**: say nothing about them unless the user's request continues one.
- **Open plan related to what was just asked** — on a prompt whose subject matches.

Rules:

1. **If the request continues a plan, say so before acting** — *"this continues
   plan_x (3/7 done, next #4)"* — then work from it (`plan_get` for the full checklist).
2. **If it doesn't, don't mention the plan.** A block of unrelated plans at every
   session start is exactly the noise that teaches the reader to skip it.
3. **Do not act on a plan the user didn't ask about.** Don't start its next item because
   it was surfaced.
4. **Never invent one.** Only plans present in an injected block or returned by
   `plan_list` / `plan_get` exist.

## Closing

- Every item resolved **and the result verified** → `plan_update(status="done")`, and
  say so in the reply.
- Work dropped → `status="abandoned"` with a `note=` saying why. This keeps the history;
  `plan_delete` is only for a plan created by mistake.
- Paused deliberately → `status="future"`.
- A plan shown as **untouched Nd** that comes up because the user is working on its
  subject → ask once whether to park it as `future` or abandon it. Not at every session
  start.

An open plan that is actually finished is the same poison as an unclosed reminder: it
teaches the reader to skim past the block, and then the plan that matters is skimmed
past too.

## Writing a good one

- **Title**: short, the name you'd say out loud — "Migrate auth to OIDC".
- **Goal**: what *done* means and why, written for a stranger in three weeks.
- **Items**: concrete, checkable steps, one per line (≤500 chars). Detail belongs in a
  memory — put its id in `links=`.
- **Scope**: leave `project` empty and the cortex PreToolUse hook fills in the current
  repo's name, so the plan surfaces only in that repo. Pass `project="global"` only for
  work that genuinely belongs everywhere.
- **A timed step inside a plan** → a reminder with `links="<plan_id>"`. Plans have no
  due dates on purpose.

## Status line

`📋 2 active (5/12 resolved) | 3 future | latest: <title> 3/7` — across all projects:
active plans with their combined progress (done or skipped counts as resolved), parked
future plans, and the most recently touched active plan. Absent when there are no open
plans. The prompt hook, by contrast, only surfaces plans for the current repo.

## Tools

`plan_create` · `plan_list` (`status=open|active|future|done|abandoned|all`, `query=`) ·
`plan_get` · `plan_check` (`items="3"|"3,5"|"2-4"`, `state=done|skipped|open`) ·
`plan_update` (status, wording, `add_items` / `remove_items` / `edit_item`, `note`;
`"-"` clears goal, tags or links) · `plan_delete`

Edits are all-or-nothing: one unknown item number or invalid field and nothing is written.
