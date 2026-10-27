<p align="center">
  <img src="https://img.shields.io/badge/Claude_Code-Plugin-blueviolet?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0id2hpdGUiPjxwYXRoIGQ9Ik0xMiAyQzYuNDggMiAyIDYuNDggMiAxMnM0LjQ4IDEwIDEwIDEwIDEwLTQuNDggMTAtMTBTMTcuNTIgMiAxMiAyem0wIDE4Yy00LjQyIDAtOC0zLjU4LTgtOHMzLjU4LTggOC04IDggMy41OCA4IDgtMy41OCA0LTggOHoiLz48L3N2Zz4=" alt="Claude Code Plugin">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT License">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/ChromaDB-Vector_Store-orange?style=for-the-badge" alt="ChromaDB">
  <img src="https://img.shields.io/badge/Hooks-11_events-blueviolet?style=for-the-badge" alt="11 Hook Events">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey?style=for-the-badge" alt="Platform">
</p>

<p align="center">
  <img src="docs/logo.svg" alt="cortex" width="360">
</p>

<p align="center">
  <strong>Self-evolving vector memory + agent fleet management for Claude Code</strong>
  <br>
  <em>The first system where Claude Code agents write, evaluate, and reconcile their own agents.</em>
</p>

<p align="center">
  <a href="#-installation">Installation</a> &bull;
  <a href="#-how-it-works">How It Works</a> &bull;
  <a href="#-auto-skill-discovery">Skill Discovery</a> &bull;
  <a href="#-hook-reference">Hook Reference</a> &bull;
  <a href="#-agent-fleet-management">Fleet Management</a> &bull;
  <a href="#-memory-hygiene">Memory Hygiene</a> &bull;
  <a href="#-mcp-resources">MCP Resources</a> &bull;
  <a href="#-architecture">Architecture</a> &bull;
  <a href="#-safety-guardrails">Safety</a>
</p>


https://github.com/user-attachments/assets/82750aee-283c-4ce5-92df-cd54b434bf05


<!-- Demo video: drag the MP4 into this file in the github.com editor and put the
     https://github.com/user-attachments/assets/... line GitHub inserts here, on its own line.
     GitHub then shows it as a player; the screenshot below can stay or go. -->

<p align="center">
  <img src="docs/demo.jpg" alt="A new Claude Code session: cortex recalls 'Use pnpm, never npm. VPN up before staging deploys.' before the agent acts, and the deploy works first try" width="820">
</p>

---

## What It Does

cortex gives Claude Code **persistent memory across sessions** and **self-managing agents** that improve over time.

- **Memories** are stored as vector embeddings in ChromaDB and silently injected into Claude's context
- **Agents** are automatically created from accumulated knowledge, evaluated on usage, and retired when obsolete
- **Skills** are auto-discovered from your project's tech stack and generated as slash commands with best practices baked in
- **Research learner** — on-demand agent that web-searches, distills, and stores knowledge as cortex memories when Claude hits a gap
- **Reminders** — obligations rather than facts, in their own collection: a due list on
  the first prompt of a session, and subject-matched nudges on any prompt
- **Plans** — a goal plus a checklist, progressed item by item across sessions; parked
  future work lives here too, and the status line shows progress at a glance
- **Config system** — toggle features on/off via `~/.claude/.cortex_config` JSON file
- **12 hook events** cover the entire session lifecycle — from startup to shutdown
- **Multi-project** aware — memories are scoped per project, agents and skills exist at project and global levels

```
🧠 44 memories (7 project, 3 feedback, 6 prefs, 26 reference, 2 user) across 2 projects
⏰ 3 pending | next 2026-03-14T08:00
📋 2 active (5/12 resolved) | 3 future | latest: Migrate auth to OIDC 3/7
🤖 16 agents (11 project + 5 global) | 17 spawns today
📚 13 skills (10 project + 3 global)
⚙ chromadb:✓ | learn:✓ skills:✓ agents:✓
```

---

## Installation

### Prerequisites

| Requirement | Version | Check |
|---|---|---|
| Claude Code | v2.1.9+ | `claude --version` |
| Python | 3.10+ | `python3 --version` |
| pip | any | `pip --version` |

### Step 1: Clone

```bash
git clone https://github.com/digin1/cortex.git ~/.claude/skills/cortex
```

### Step 2: Install Dependencies

```bash
pip install chromadb "mcp>=1.2,<2"
```

`mcp` is the MCP SDK the cortex server is built on; keep it below 2.0 (2.x renamed `FastMCP`). ChromaDB will also install `onnxruntime` (for embeddings) and `numpy`. No GPU required — CPU embeddings are fast enough.

**Recommended: Run ChromaDB as a systemd user service** on `localhost:8100` using the v2 API. See `config/chromadb-service.md` for setup instructions. The v1 API is deprecated.

**Embeddings: gte-modernbert-base via a warm sidecar.** Cortex embeds with
`Alibaba-NLP/gte-modernbert-base` (768-dim, 8192-token context) served by a small
always-on sidecar on `localhost:8110`, instead of ChromaDB's default MiniLM (which
truncated memories at 256 tokens). Memories live in the `claude_memories_v2`
collection. Requires `pip install sentence-transformers`. See
`config/cortex-embed-service.md` for setup, migration, and fallback behaviour.

### Step 3: Initialize Database

```bash
python3 -c "
import chromadb
client = chromadb.PersistentClient(path='$HOME/.claude/cortex-db')
col = client.get_or_create_collection('claude_memories')
print(f'Database initialized: {col.count()} memories')
"
```

### Step 4: Configure MCP Server

Register cortex for every project (Claude Code keeps user-scope servers in `~/.claude.json`):

```bash
claude mcp add --scope user cortex -- python3 -W ignore ~/.claude/skills/cortex/mcp_server.py
```

Check it with `claude mcp get cortex`.

### Step 5: Configure Hooks

Add the following to your `~/.claude/settings.json` (merge with any existing settings):

<details>
<summary><strong>Click to expand full settings.json configuration</strong></summary>

```json
{
  "permissions": {
    "allow": [
      "mcp__cortex__memory_store",
      "mcp__cortex__memory_search",
      "mcp__cortex__memory_get",
      "mcp__cortex__memory_list",
      "mcp__cortex__memory_delete",
      "mcp__cortex__memory_update",
      "mcp__cortex__memory_merge",
      "mcp__cortex__memory_stats"
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/skills/cortex/statusline.sh 2>/dev/null",
    "padding": 0
  },
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/recall.sh 2>/dev/null",
            "statusMessage": "Recalling relevant memories..."
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__cortex",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/cortex_pretool_enrich.sh 2>/dev/null",
            "statusMessage": "Enriching cortex operation..."
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Agent",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/agent_track.sh 2>/dev/null",
            "statusMessage": "Tracking agent usage..."
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/compact_save.sh 2>/dev/null",
            "statusMessage": "Extracting learnings + managing agent fleet..."
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/agent_context_inject.sh 2>/dev/null",
            "statusMessage": "Injecting cortex context into agent..."
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/cleanup.sh 2>/dev/null",
            "statusMessage": "Cleaning stale memory snapshots...",
            "async": true
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/agent_bootstrap.sh 2>/dev/null",
            "statusMessage": "Bootstrapping agents from cortex...",
            "async": true
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/memory_hygiene.sh 2>/dev/null",
            "statusMessage": "Memory hygiene check...",
            "async": true
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/skill_discover.sh 2>/dev/null",
            "statusMessage": "Discovering project skills...",
            "async": true
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/session_end_cleanup.sh 2>/dev/null",
            "statusMessage": "Saving session summary..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/learn.sh 2>/dev/null",
            "statusMessage": "Saving session learnings..."
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/skills/cortex/fleet_eval_stop.sh 2>/dev/null",
            "statusMessage": "Evaluating agent fleet health..."
          }
        ]
      }
    ]
  }
}
```

</details>

### Step 6: Install Global Behavioral Rules

These files teach Claude to **always search cortex before saying "I don't remember"** and to proactively store useful memories:

```bash
# Global CLAUDE.md — loaded into every session, every project
cp ~/.claude/skills/cortex/config/CLAUDE.md ~/.claude/CLAUDE.md

# Global rules — loaded on-demand for cortex-related behavior
mkdir -p ~/.claude/rules
cp ~/.claude/skills/cortex/config/cortex-memory.md ~/.claude/rules/cortex-memory.md
cp ~/.claude/skills/cortex/config/skill-discovery.md ~/.claude/rules/skill-discovery.md
cp ~/.claude/skills/cortex/config/cortex-plans.md ~/.claude/rules/cortex-plans.md
```

> **Why?** Without these, Claude may not search cortex when you ask "do you remember X" — the recall hook injects context automatically, but if it misses something, Claude needs to know it should search manually. These files close that gap.

### Step 7: Verify Installation

```bash
# Run the test suite
bash ~/.claude/skills/cortex/test.sh

# Check status line
bash ~/.claude/skills/cortex/statusline.sh
```

Then restart Claude Code. You should see the status line at the bottom and memories will start accumulating automatically.

### Quick Install (Alternative)

```bash
bash ~/.claude/skills/cortex/install.sh
```

### Other agents: OpenCode, Codex, any MCP client

`install.sh` sets up Claude Code. The same memory store also works from any agent that
speaks MCP, because cortex is a local stdio MCP server (`mcp_server.py`, 19 tools for
memories, reminders and plans). What differs is how much happens on its own:

| Agent | Connects through | Recall | Saving |
|---|---|---|---|
| Claude Code | hooks + MCP | automatic, before each prompt | the agent stores as it works, plus a learning turn when it stops |
| OpenCode | MCP + the same rules file | the agent searches when the rules tell it to | the agent stores when the rules tell it to |
| Codex CLI | MCP | the agent calls the tools | the agent calls the tools |
| Any MCP client | MCP | the agent calls the tools | the agent calls the tools |

Run `install.sh` first (it installs the dependencies and the embedding sidecar), then point
the other agent at the server. Use the absolute path to your home directory.

**OpenCode** (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "cortex": {
      "type": "local",
      "command": ["python3", "/home/YOUR_USERNAME/.claude/skills/cortex/mcp_server.py"],
      "enabled": true
    }
  },
  "instructions": ["~/.claude/CLAUDE.md"]
}
```

The `instructions` line loads the same cortex rules Claude Code follows (search before
saying "I don't remember", store corrections, and so on).

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.cortex]
command = "python3"
args = ["/home/YOUR_USERNAME/.claude/skills/cortex/mcp_server.py"]
```

**Any other MCP client:** run `python3 ~/.claude/skills/cortex/mcp_server.py` as a stdio server.

---

## How It Works

### Session Lifecycle

Every session follows this automatic flow:

```
Session Start
  ├── cleanup.sh          → prune stale data (async)
  ├── agent_bootstrap.sh  → create agents from cortex knowledge (async, daily)
  ├── memory_hygiene.sh   → dedup, validate paths, consolidate (async, daily)
  ├── skill_discover.sh   → detect tech stack + generate skill commands (async, weekly)
Every Message
  └── recall.sh           → inject relevant memories into Claude's context
                            First message: comprehensive project load (all memories)
                            Subsequent: targeted semantic search

Agent Spawned
  ├── agent_context_inject.sh → inject domain memories into the agent
  └── agent_track.sh          → log spawn to usage ledger

Context Compressed
  └── compact_save.sh     → extract memories from transcript + create/evaluate agents

Session Paused
  ├── learn.sh            → block stop + give Claude a turn to save learnings via MCP
  └── fleet_eval_stop.sh  → check agent fleet health (if 5+ spawns today)

Session Ended
  └── session_end_cleanup.sh → save session summary, clean temp files

Every cortex Tool Call
  └── cortex_pretool_enrich.sh → auto-tag project from cwd, log to audit trail
```

### Project-Aware First-Message Recall

On the first message of every session, cortex detects your project from `cwd` and loads **all relevant memories** — not just semantic matches:

| Category | What's loaded | Why |
|---|---|---|
| User profile | Always | Who you are, how you work |
| Feedback | Always (all projects) | Cross-project rules and preferences |
| Project context | Matching project + global | Architecture, decisions, gotchas |
| References | All (always included) | File paths, commands, endpoints |
| Semantic matches | From your first prompt | Cross-project hits |

Subsequent messages use targeted semantic search with relaxed thresholds. "Remember" queries (containing keywords like "recall", "do you know", "last time") get **boosted thresholds** (0.75-0.85 vs 0.6-0.7) and more results (12 vs 8).

First-message content uses **progressive disclosure** — summaries are truncated to 250 chars to save tokens. Claude can fetch full content via `mcp__cortex__memory_search` when needed.

### Silent Context Injection

Memories are injected via Claude Code's `additionalContext` API — Claude sees them, you don't:

```json
{
  "suppressOutput": true,
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "[cortex] Session context loaded for project: my-project\n..."
  }
}
```

### Agent Context Injection

When any subagent spawns, the `SubagentStart` hook queries cortex for memories relevant to that agent type and injects them. A `dask-optimizer` agent automatically gets Dask-related memories; a `swarm-deployer` gets deployment knowledge.

---

## Auto-Skill Discovery

cortex automatically detects your project's tech stack and generates **slash-command skill files** (`.md`) with framework-specific best practices. Two paths:

### Automatic (SessionStart, background)

On every session start, `skill_discover.sh` runs asynchronously:

1. **Detect** — `skill_detect.py` scans for project markers (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, etc.) and identifies 50+ frameworks across Node.js, Python, Go, Rust, Ruby, Java, and infrastructure tools
2. **Check** — skips if skills already exist for detected frameworks, or if cooldown hasn't expired (weekly per project)
3. **Generate** — calls `claude -p --model sonnet` to produce skill definitions with framework best practices
4. **Write** — `skill_create.py` writes `.md` command files to `.claude/commands/` (project) or `~/.claude/commands/` (global)
5. **Track** — stores discovery record in cortex to avoid regeneration

```
Session starts in a FastAPI + SQLAlchemy project
  → skill_detect.py: "fastapi, sqlalchemy detected"
  → cooldown check: first time this week ✓
  → existing skills check: no fastapi-* commands found ✓
  → claude -p generates: fastapi-endpoint.md, fastapi-test.md, fastapi-model.md
  → skill_create.py writes to .claude/commands/
  → /fastapi-endpoint, /fastapi-test, /fastapi-model now available
```

### Manual (`/cortex discover`)

For deeper, web-researched skills:

```
/cortex discover
```

Unlike the automatic path (which uses LLM knowledge only), the manual command uses **WebSearch** to research current best practices online and reads your actual project code for project-specific conventions. Use this for:
- Newer or less common frameworks
- More detailed, up-to-date skills
- Bypassing the weekly cooldown

### Supported Frameworks

| Ecosystem | Frameworks |
|---|---|
| **Node.js** | Next.js, React, Vue, Angular, Express, Svelte, Nuxt, Astro, Remix, NestJS, Gatsby, Prisma, Drizzle, tRPC, Playwright, Jest, Vitest, Cypress |
| **Python** | FastAPI, Django, Flask, Celery, SQLAlchemy, Pydantic, Pandas, PyTorch, TensorFlow, Streamlit, Gradio, LangChain, Dask, Scrapy, Pytest |
| **Go** | Gin, Fiber, Echo, Gorilla Mux, GORM, pgx, Cobra, gRPC |
| **Rust** | Actix, Axum, Rocket, Tokio, Serde, Diesel, SQLx, Clap, Tonic |
| **Ruby** | Rails, Sinatra, RSpec, Sidekiq |
| **Java** | Spring Boot, Quarkus, Micronaut |
| **Infra** | Docker, Docker Compose, Kubernetes, Helm, Terraform, GitHub Actions, GitLab CI, Ansible, Serverless, Vercel, Netlify, Fly.io |

### Safety Limits

| Limit | Value |
|---|---|
| Max project skills | 10 |
| Max global skills | 10 |
| Skills per discovery run | 5 |
| Cooldown | Weekly per project (automatic), none (manual) |
| Overwrite protection | Never overwrites existing skill files |

---

## Hook Reference

| Hook Event | Script | Sync/Async | What It Does |
|---|---|---|---|
| `UserPromptSubmit` | `recall.sh` | Sync | Inject memories into Claude's context |
| `UserPromptSubmit` | `reminders.sh` | Sync | Surface due + subject-matched reminders |
| `UserPromptSubmit` | `plans.sh` | Sync | Surface active + subject-matched plans |
| `PreToolUse` | `cortex_pretool_enrich.sh` | Sync | Auto-tag project + audit log for cortex ops |
| `PostToolUse(Agent)` | `agent_track.sh` | Sync | Track agent spawns in usage ledger |
| `SubagentStart` | `agent_context_inject.sh` | Sync | Inject domain memories into spawned agents |
| `PreCompact` | `compact_save.sh` | Sync | Extract memories + create/evaluate agents |
| `SessionStart` | `cleanup.sh` | Async | Prune stale data |
| `SessionStart` | `agent_bootstrap.sh` | Async | Bootstrap agents from cortex (daily) |
| `SessionStart` | `memory_hygiene.sh` | Async | Dedup, path validation, consolidation (daily) |
| `SessionStart` | `skill_discover.sh` | Async | Auto-detect tech stack + generate skill commands (weekly) |
| `SessionEnd` | `session_end_cleanup.sh` | Sync | Save session summary + cleanup |
| `Stop` | `learn.sh` | Sync | Block stop + give Claude a turn to save learnings via MCP |
| `Stop` | `fleet_eval_stop.sh` | Sync | Lightweight fleet health check |

### Optional Hooks (Not Enabled by Default)

These scripts are included but not configured in the default `settings.json`. Add them if you want the extra functionality:

| Hook Event | Script | What It Does |
|---|---|---|
| `PreToolUse(Bash)` | `bash_guard.sh` | Block dangerous/destructive shell commands (rm -rf /, dd, etc.) |
| `PreCompact` | `compact_guide.sh` | Inject compaction guidance so Claude preserves modified files and user decisions |
| `PostToolUse(Edit,Write)` | `edit_track.sh` | Track which files were modified during the session |

---

## Agent Fleet Management

### Automatic Agent Creation

Agents are created from two sources:

1. **Bootstrap** (`SessionStart`): Queries cortex for accumulated knowledge. If a project has 3+ memories but fewer than 2 agents, it uses `claude -p --model haiku` to propose new agents. Runs once per project per day.

2. **Compact** (`PreCompact`): Analyzes the conversation transcript for recurring patterns. Creates 0-5 agents per compaction event with semantic dedup (cosine < 0.55).

### Agent Evaluation & Reconciliation

On every PreCompact, the system:
- **Scores** each agent 1-5 based on relevance, quality, and usage data
- **Updates** agents with stale instructions (creates `.bak` backup first)
- **Retires** low-scoring agents (extracts knowledge to cortex, moves to `~/.claude/.retired-agents/`)

### Persistent Agent Memory

All agents have the `memory:` field in their frontmatter:

```yaml
---
name: dask-optimizer
memory: project    # project agents use project scope
---

---
name: data-safety-ops
memory: user       # global agents use user scope
---
```

This means agents **accumulate knowledge across sessions** in their own `MEMORY.md` files.

### Fleet Health

| Metric | How it's tracked |
|---|---|
| Usage count | JSONL ledger (`~/.claude/agent-usage.jsonl`) |
| Eval scores | Stored in cortex as `agent_eval` type memories |
| Health status | Based on score + 7-day usage |
| Fleet dashboard | Run `/cortex agents` for full report |

### Limits

| Limit | Value | Purpose |
|---|---|---|
| Max project agents | unlimited (`MAX_PROJECT_AGENTS` in `lib/fleet_create.py`) | Set a number to cap the fleet |
| Max global agents | unlimited (`MAX_USER_AGENTS`) | Set a number to cap the fleet |
| Semantic dedup threshold | 0.55 | Prevent duplicate agents |
| Bootstrap per day | 1 per project | Prevent repeated creation |
| Agents per batch | 3 (bootstrap), 5 (compact) | Conservative creation |

---

## Memory Hygiene

The hygiene system runs daily on `SessionStart` (async) with 4 phases:

### Phase 1: Duplicate Detection
Finds memory pairs with cosine distance < 0.35 within the same type. Keeps the longer/more detailed version, merges tags, records `merged_from` metadata.

### Phase 2: Path Validation
Extracts file paths from memory content and checks if they still exist on the filesystem. Flags broken paths with `stale_path` tag. Skips NAS mounts (`/remote/`) and container paths (`/app/`). **Never deletes** — only flags.

### Phase 3: Recall Tracking
Reads the recall log (`~/.claude/.cortex_recall_log`) and updates each memory's `last_recalled` timestamp and `recall_count`. This data informs future hygiene decisions.

### Phase 4: Consolidation
For groups of 3+ related memories in the same project, uses `claude -p --model haiku` to merge them into 1 comprehensive memory. Only runs when 15+ total memories exist. Originals are deleted only after successful consolidation.

---

## MCP Resources

The MCP server exposes resources that can be referenced with `@` in the Claude Code prompt:

| Resource URI | What It Returns |
|---|---|
| `memory://all` | All memories with type, project, and content preview |
| `memory://{memory_id}` | Full content and metadata for a specific memory |
| `memory://project/{name}` | All memories scoped to a project |
| `memory://type/{type}` | All memories of a specific type (feedback, project, etc.) |

### MCP Tools

| Command | Description |
|---|---|
| `/cortex store <content>` | Store a new memory |
| `/cortex search <query>` | Semantic search across all memories |
| `/cortex list` | List all memories |
| `/cortex stats` | Database statistics |
| `/cortex delete <id>` | Delete a memory (archived to audit log) |
| `/cortex update <id>` | Update a memory |
| `/cortex merge <id1,id2,...>` | Merge related memories into one |
| `/cortex agents` | Fleet health dashboard |
| `/cortex discover` | Auto-detect tech stack + generate skill commands with web research |
| `/cortex learn` | Review session and extract learnings to memory |
| `/cortex config [key] [value]` | Toggle feature flags (auto_learn, auto_skills, auto_agents) |

---

## Memory Types

| Type | What | Example |
|---|---|---|
| `user` | User profile and role | *"Senior engineer, prefers terse responses"* |
| `feedback` | User corrections and preferences | *"Always use step=any on auto-calculated number inputs"* |
| `project` | Technical decisions and architecture | *"Docker Swarm --force reuses cached image, must use --image"* |
| `reference` | File paths, URLs, commands | *"GitLab API at git.example.com, token in config"* |

---

## Reminders

A **memory** is a timeless fact, injected as background. A **reminder** is an obligation
that is not yet discharged — *"read the run summary after tomorrow's cron"*. It has a
subject, sometimes a time, and always an end.

They live in a **separate ChromaDB collection**, deliberately. If reminders sat in the
memory store, a search for a topic would return an outstanding task as though it were
knowledge, and memory hygiene would eventually *merge* two obligations into one — which
is never correct.

| Tool | What |
|---|---|
| `reminder_add` | Create one. `due` takes ISO, `+2h`/`+3d`, `tomorrow`, `tomorrow 03:05` |
| `reminder_list` | `pending` (default), `done`, `dropped`, `expired`, or all |
| `reminder_done` | Close it — the thing actually happened |
| `reminder_snooze` | Right reminder, wrong time |
| `reminder_drop` | Delete it outright |

### Delivery

Reminders surface **only inside a session** — there is no cron and no email, by design.
A reminder due at 03:05 appears the next time you talk to Claude, which may be hours
later. It is a catch-up list and a context-sensitive nudge, **not an alarm clock**.

- **First prompt of a session** — everything now due or overdue
- **Any prompt** — reminders semantically close to what was just asked

### Noise discipline

The failure mode of every reminder system is that the block stops being read. Once that
happens the *next* reminder — the one that mattered — is skimmed past too. So:

- **Tighter threshold than memory recall** (0.35 vs 0.40–0.55). Memories inform;
  reminders interrupt, so they must clear a higher bar.
- **45-minute cooldown** — the same reminder is not raised twice in quick succession.
- **Self-demotion** — one raised three times without being acted on drops back to the
  session-opening list rather than continuing to interrupt. A reminder you keep passing
  over is a reminder that is wrong, and it should get out of the way.
- **Five per prompt, maximum**; `expires` so stale ones die on their own.
- **Project-scoped by default**, so one repo's reminders don't follow you into another.

### Writing a good one

```
reminder_add(
    text="Verify the migration under real load once the backfill finishes",
    due="+2d",
    project="my-service",
    verify="pytest tests/test_migration.py -q",   # surfacing it becomes actionable
    links="project_migration_plan",              # context travels with it
    expires="+30d",                              # a reminder with no end is a leak
)
```

An unparseable time is **not guessed** — it becomes a relevance-only reminder, surfacing
when the subject comes up rather than on a date invented for it.

---

## Plans

A **plan** is a goal plus the steps toward it — a checklist ticked off item by item, often
over several sessions. Parked **future work** ("later", "phase 2", the backlog) is a plan
too, with status `future`.

It is neither a memory nor a reminder. A memory is a fact, and memory hygiene merges
near-duplicates — merging two plans would fuse two bodies of work. A reminder is one
obligation, closed once; a plan is progressed a step at a time, and the progress is the
point. So plans get a third collection, `cortex_plans`.

| Tool | What |
|---|---|
| `plan_create` | Title, goal, and a checklist — one item per line. If the text has Markdown checkboxes, only checkbox lines become items (`[ ]`/`[~]` open, `[x]` done, `[-]` skipped) and `## Section` lines group them; prose and detail lines are counted and reported, not imported. Empty `project` = the current repo (filled in by the PreToolUse hook); `"global"` = everywhere |
| `plan_list` | `open` (active + future, default), a single status, or `all`; `query=` ranks by similarity |
| `plan_get` | The whole plan as a Markdown checklist with item numbers and notes |
| `plan_check` | Tick items: `"3"`, `"3,5"`, `"2-4"` → `done` / `skipped` / `open` — all-or-nothing |
| `plan_update` | Status (`active` / `future` / `done` / `abandoned`), wording (`"-"` clears goal, tags, links), add / remove / reword items, timestamped progress notes (capped at 12,000 chars: older notes move to `~/.claude/cortex-plan-notes/<plan_id>.log`) |
| `plan_delete` | Remove outright (archived to the audit log first; a notes archive file is left in place and named in the reply); prefer `status="abandoned"` |

### Guarantees

- **Item numbers are stable for life.** Removing `#3` never renumbers `#4`, and numbers
  are never reused, so a reference written in one session means the same item in the next.
- **All-or-nothing edits.** One unknown item number, or one invalid field, and nothing is
  written.
- **Notes are moved, never dropped.** An appended note that would take the log past
  12,000 characters is not refused: the oldest notes (then, if needed, the text above the
  first dated note) move to an append-only, owner-only file per plan, flushed to disk with
  its folder before the plan changes, and the log keeps one line pointing at it. A single
  note too long to fit even alone, or a replacement log over the cap, is still refused. The
  file is never pruned (unlike the audit log, kept 90 days). If the plan update fails after
  that write, the moved notes are in both places, never in neither; a retry adds them to
  the file again.
- **Concurrent sessions don't lose ticks.** Each session runs its own MCP server, so
  read-modify-write is serialised with a file lock, and the prompt hook's bookkeeping
  writes only its own metadata keys.
- **No accidental twins.** Creating a plan nearly identical to an open one is refused
  with a pointer to the existing plan (`force=True` overrides).
- **Fails closed** when embeddings are unavailable, rather than writing plans into the
  memory store — the same rule as reminders.
- **Closed plans never crowd out open ones.** Done and abandoned plans are kept for the
  record, but relevance matching only ever searches open plans.

Behaviour rules for Claude — create only when asked, tick items as they finish, keep
surfaced plans in the background unless the request continues one — ship in
`config/cortex-plans.md` and are installed to `~/.claude/rules/`.

### Delivery

- **Status line** — `📋 2 active (5/12 resolved) | 3 future | latest: <title> 3/7`, across
  all projects (done or skipped counts as resolved), shown only when open plans exist.
- **First prompt of a session** — up to three active plans in scope, each with its next
  open items, so a new session picks up where the last one stopped.
- **Any prompt** — up to two open plans (active or future) close to what was just asked,
  with the same noise discipline as reminders: a 45-minute cooldown, and self-demotion
  after three raises without the plan being touched (any edit revives it). The bar is
  0.40 cosine distance — looser than reminders' 0.35, because a plan informs rather
  than interrupts.

```
plan_create(
    title="Migrate auth to OIDC",
    goal="Every service authenticates via the IdP; local password tables are gone.",
    items="""## Services
- [x] api gateway
- [ ] billing
- [ ] reporting
## Cleanup
- [ ] retire the legacy password login""",
    project="my-service",
)
plan_check(plan_id="plan_migrate_auth_to_oidc", items="2", note="commit 4f1c2aa")
plan_update(plan_id="plan_migrate_auth_to_oidc", note="reporting blocked on IdP scopes")
```

---

## Architecture

```
~/.claude/
├── skills/cortex/
│   ├── recall.sh                  # UserPromptSubmit: project-aware context injection
│   ├── reminders.sh               # UserPromptSubmit: due + subject-matched reminders
│   ├── plans.sh                   # UserPromptSubmit: active + subject-matched plans
│   ├── cortex_pretool_enrich.sh     # PreToolUse: auto-tag project + audit
│   ├── agent_context_inject.sh    # SubagentStart: inject domain memories into agents
│   ├── compact_save.sh            # PreCompact: extract memories + fleet management
│   ├── agent_bootstrap.sh         # SessionStart: create agents from cortex knowledge
│   ├── memory_hygiene.sh          # SessionStart: dedup, validate, consolidate
│   ├── cleanup.sh                 # SessionStart: prune stale data
│   ├── skill_discover.sh          # SessionStart: auto-detect tech stack + generate skills
│   ├── learn.sh                   # Stop: block stop + save learnings via decision:block
│   ├── fleet_eval_stop.sh         # Stop: lightweight fleet health check
│   ├── session_end_cleanup.sh     # SessionEnd: save summary + cleanup
│   ├── agent_track.sh             # PostToolUse(Agent): log spawns
│   ├── bash_guard.sh              # PreToolUse(Bash): block dangerous commands (optional)
│   ├── compact_guide.sh           # PreCompact: inject compaction guidance (optional)
│   ├── edit_track.sh              # PostToolUse(Edit,Write): track file modifications (optional)
│   ├── agent_dashboard.py         # /cortex agents command
│   ├── statusline.sh              # Multi-line status bar
│   ├── mcp_server.py              # MCP server: 19 tools (memory, reminder, plan) + 4 resources
│   ├── memory_db.py               # ChromaDB CLI wrapper
│   ├── SKILL.md                   # Skill definition for /cortex
│   ├── test.sh                    # Test suite
│   ├── tests/test_plans.py        # Plans: library, tools, hook, status line
│   ├── config/
│   │   ├── CLAUDE.md              # Global behavioral rules → ~/.claude/CLAUDE.md
│   │   ├── cortex-memory.md       # Memory rules → ~/.claude/rules/cortex-memory.md
│   │   ├── skill-discovery.md     # Skill discovery rules → ~/.claude/rules/skill-discovery.md
│   │   ├── cortex-plans.md        # Plan rules → ~/.claude/rules/cortex-plans.md
│   │   └── chromadb-service.md    # ChromaDB systemd service setup guide
│   └── lib/
│       ├── parse_transcript.py    # Transcript JSONL parser
│       ├── store_memories.py      # Memory storage with dedup
│       ├── memory_hygiene.py      # Dedup, path validation, consolidation
│       ├── collect_memories_full.py # Full memory collector (for bootstrap)
│       ├── fleet_create.py        # Agent creation with semantic dedup + hard caps
│       ├── fleet_eval.py          # Agent evaluation, update, retire
│       ├── collect_agents.py      # Agent inventory collector
│       ├── collect_usage.py       # Usage ledger reader
│       ├── collect_memories.py    # ChromaDB memory reader
│       ├── skill_detect.py        # Tech stack detector (50+ frameworks)
│       ├── skill_create.py        # Skill .md file writer with safety caps
│       ├── chroma_client.py       # ChromaDB client (v2 API, localhost:8100)
│       ├── reminders.py           # Reminders collection: add, surface, snooze, close
│       ├── plans.py               # Plans collection: checklists, surfacing, status summary
├── cortex-db/              # ChromaDB persistent storage
├── agent-usage.jsonl              # Agent spawn ledger
├── .cortex_activity                 # Live activity indicator
├── .cortex_audit.jsonl              # Audit trail for all memory operations
├── .cortex_recall_log               # Recall tracking for hygiene
├── .cortex_ops_log.jsonl            # PreToolUse operation log
├── .cortex_sessions.jsonl           # Session start/end markers
├── .cortex_config                   # Feature toggles JSON (auto_learn, auto_skills, etc.)
├── .cortex_plans.lock               # Serialises plan edits across concurrent sessions
├── cortex-plan-notes/               # Older plan notes moved out of a full log, one file per plan
├── .retired-agents/               # Retired agents (outside git dirs to avoid discovery)
└── agents/
    └── *.md                       # Active global agents (memory: user)
```

Project-level agents live at `<project>/.claude/agents/*.md` with `memory: project`.

---

## Safety Guardrails

| Protection | Implementation |
|---|---|
| **Content size limit** | Max 5000 chars per memory |
| **Audit trail** | All store/update/delete operations logged to `.cortex_audit.jsonl` |
| **Soft delete** | Deleted memory content archived to audit log before removal |
| **Merge tracking** | Merged memories tagged with `merged_from` ID |
| **Consolidation tracking** | Consolidated memories tagged with `consolidated_from` IDs |
| **No age-based deletion** | Memories never removed for being old (designed for long-running projects) |
| **Path validation** | Broken file paths flagged, never auto-deleted |
| **Agent path traversal** | `os.path.realpath()` + directory whitelisting |
| **Agent caps** | Unlimited by default; set `MAX_PROJECT_AGENTS` / `MAX_USER_AGENTS` in `lib/fleet_create.py` to cap |
| **Agent filename sanitization** | Only `[a-z0-9\-_.]` allowed |
| **Semantic dedup** | 0.55 threshold for agents, 0.15 for memories, 0.35 for hygiene merges |
| **Backup before update** | Timestamped `.bak` files before agent overwrites |
| **Soft retire** | Agents moved to `~/.claude/.retired-agents/` (outside git dirs to avoid Claude Code discovery) |
| **Skill hard caps** | Max 10 project + 10 global skills, 5 per discovery run |
| **Skill overwrite protection** | Never overwrites existing skill files |
| **Daily cooldowns** | Bootstrap + hygiene run max once per project per day |
| **Weekly cooldowns** | Skill discovery runs max once per project per week |
| **Operation logging** | Every cortex tool call logged via PreToolUse hook |
| **Auto project tagging** | PreToolUse enriches memory_store with project from cwd |
| **Process locks** | learn.sh and cleanup.sh use file locks to prevent concurrent runs |
| **Ops log rotation** | `.cortex_ops_log.jsonl` rotated at 500KB |
| **Safe JSON encoding** | Session end cleanup escapes content to prevent injection |

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Claude Code | v2.1.9+ | Needs `additionalContext` + `SubagentStart` hook support |
| Python | 3.10+ | For ChromaDB, the MCP SDK and hook scripts |
| chromadb | Latest | `pip install chromadb` |
| mcp | 1.x | `pip install "mcp>=1.2,<2"` (the MCP SDK the server runs on) |
| claude CLI | Latest | For `claude -p` in compact/bootstrap/hygiene |

### Platform Support

| Platform | Status | Notes |
|---|---|---|
| Linux | Fully supported | Primary development platform |
| macOS | Fully supported | `stat` differences handled |
| Windows | Via Git Bash / WSL | Requires bash environment |

---

## Contributing

Contributions welcome! Areas of interest:

- **Recall caching** — daemon process to avoid ChromaDB cold-start on every prompt
- **Cross-project federation** — share agents between projects intelligently
- **Fleet analytics** — usage trends, score degradation alerts, visualizations
- **Plugin packaging** — convert to official Claude Code plugin format

Recently shipped: **hybrid search** (dense + BM25 + cross-encoder rerank, see
`config/cortex-embed-service.md`).

---

## License

[MIT](LICENSE)

---

<p align="center">
  <em>Built with care by <a href="https://github.com/digin1">@digin1</a></em>
  <br>
  <sub>If this saves you context, give it a star</sub>
</p>
