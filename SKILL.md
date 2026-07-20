---
name: cortex
description: Cortex — self-evolving memory and agent fleet — store, search, and manage memories with semantic search across all projects
argument-hint: <store|search|list|delete|update|stats|agents|discover|learn|addskill|config|merge> [args...]
allowed-tools: "mcp__cortex__memory_store, mcp__cortex__memory_search, mcp__cortex__memory_get, mcp__cortex__memory_list, mcp__cortex__memory_delete, mcp__cortex__memory_update, mcp__cortex__memory_merge, mcp__cortex__memory_stats, Bash, Read, Glob, Write, WebSearch, WebFetch, Agent"
---

# Cortex Memory System

You have access to a persistent vector memory database (ChromaDB) via MCP tools. This memory is **global** — shared across all projects.

**IMPORTANT: Always use the MCP tools (mcp__cortex__memory_store, etc.) — NEVER call memory_db.py via Bash.** The MCP tools provide clean CLI output with spinners and status messages.

**Embeddings:** memories are embedded with `gte-modernbert-base` (768-dim, 8192-token
context — full memories, no 256-token truncation) via a warm sidecar on `localhost:8110`
and stored in the `claude_memories_v2` collection. See `config/cortex-embed-service.md`.

## MCP Tools Available

- `memory_store` — Store a memory with embedding (dedup check built-in)
- `memory_search` — Semantic similarity search (shows % similarity; pass `full=True` to print top hits untruncated)
- `memory_get` — Fetch ONE memory in full (untruncated content + metadata) by its exact ID — use after search/list surfaces the right ID
- `memory_list` — List all memories (with optional filters)
- `memory_delete` — Delete by ID (archived to audit log)
- `memory_update` — Update content or metadata (supports append/prepend mode, +tag syntax)
- `memory_merge` — Merge 2+ related memories into one consolidated memory
- `memory_stats` — Show DB statistics

## Interpreting the user's request

Parse `$ARGUMENTS` to determine the command:

- `/cortex store <content>` → call `memory_store`
- `/cortex search <query>` → call `memory_search`
- `/cortex list` → call `memory_list`
- `/cortex delete <id>` → call `memory_delete`
- `/cortex update <id> <content>` → call `memory_update`
- `/cortex stats` → call `memory_stats`
- `/cortex agents` → run agent fleet dashboard (see below)
- `/cortex agents discover` → auto-discover and create agents for the current project (see below)
- `/cortex discover` → auto-discover project skills with web research (see below)
- `/cortex learn` → session review (see below)
- `/cortex addskill <description>` → create a new skill from context (see below)
- `/cortex merge <id1,id2,...>` → merge related memories into one (see memory_merge tool)
- `/cortex config [key] [value]` → view or set cortex config (see below)
- `/cortex` with no args → call `memory_stats`

When storing, always:
1. Choose an appropriate `memory_type` based on content (user, feedback, project, reference, general)
2. Generate a meaningful `memory_id` (e.g., `feedback_no_emojis`, `user_role_datasci`)
3. Add relevant `tags`

When searching, show results in a readable format with ID, content, type, and similarity score.

## Agent Fleet Dashboard (`/cortex agents`)

When the user runs `/cortex agents`, provide a comprehensive fleet health report by running:

```bash
python3 -W ignore ~/.claude/skills/cortex/agent_dashboard.py 2>/dev/null
```

Then format the JSON output into a readable table showing:
- Agent name, scope (user/project), model
- Usage count and last used date (from `~/.claude/agent-usage.jsonl`)
- Latest eval score and notes (from cortex `agent_eval` type memories)
- Health indicator based on score + usage

If the user says `/cortex agents <name>`, show detailed info for that specific agent (read its .md file, full eval history, usage timeline).

## Agent Discovery (`/cortex agents discover`)

When the user runs `/cortex agents discover`, analyze the current project and automatically create specialized agents.

### Steps:

1. **Detect tech stack** — run the detector:
```bash
/usr/bin/python3 ~/.claude/skills/cortex/lib/skill_detect.py "$(pwd)" 2>/dev/null
```

2. **Read project structure** — understand the codebase:
   - Check for backend frameworks (Flask, FastAPI, Django, Express)
   - Check for frontend frameworks (React, Next.js, Vue)
   - Check for infrastructure (Docker, Docker Compose, CI/CD)
   - Check for test frameworks (pytest, jest, playwright)
   - Check for databases (PostgreSQL, MySQL, SQLite, Redis)
   - Read key config files (package.json, requirements.txt, docker-compose.yml)

3. **Check existing agents** — run the dashboard to see what already exists:
```bash
python3 -W ignore ~/.claude/skills/cortex/agent_dashboard.py 2>/dev/null
```
Do NOT create duplicates of existing agents.

4. **Determine scope** for each agent:

   **LOCAL** (`.claude/agents/`) when the agent:
   - References project-specific file paths, services, or architecture
   - Is only useful in this project's context
   - Uses project-specific conventions or domain knowledge

   **GLOBAL** (`~/.claude/agents/`) when the agent:
   - Works across multiple projects
   - Is a general-purpose specialist (testing, docs, security)
   - Has no project-specific content

5. **Generate agent files** — create `.md` files with this frontmatter:
```yaml
---
name: <Agent Name>
description: <When to use — one line, shown in agent selection>
model: sonnet
---

<System prompt with detailed instructions>
```

6. **Agent ideas to consider** (pick only what's relevant to this project):

   | Agent | Scope | When to create |
   |-------|-------|----------------|
   | **Test Writer** | Global | Any project with tests |
   | **Code Reviewer** | Global | Any codebase |
   | **DB Migration** | Local | Projects with databases + migration tools |
   | **API Designer** | Local | Projects with REST/GraphQL APIs |
   | **Docker Debugger** | Local | Projects with Docker/Compose/Swarm |
   | **CI/CD Fixer** | Local | Projects with GitHub Actions/GitLab CI |
   | **Performance Auditor** | Local | Frontend projects |
   | **Security Scanner** | Global | Any codebase |
   | **Content Writer** | Local | CMS/content-driven sites |
   | **Deployment Agent** | Local | Projects with specific deploy workflows |

7. **Write files** — use `Write` to create each agent `.md` file

8. **Store in cortex** — record what was created:
   - `memory_id`: `agent_discovery_<project_name>`
   - `memory_type`: `project`
   - Content: list of agents created with scope and purpose

9. **Report** — show the user what agents were created, their scope, and when to use them

### Quality rules:
- Max 3-4 agents per project — too many creates noise
- Each agent must have a clear, distinct purpose
- Don't create agents for things slash commands handle better (simple workflows)
- Agents are for complex, multi-step tasks that benefit from specialized context
- Use `model: sonnet` for most agents (fast, capable), `model: opus` only for deep analysis tasks
- The system prompt should be specific and opinionated, not generic

## Skill Discovery (`/cortex discover`)

When the user runs `/cortex discover`, perform a deep skill discovery for the current project. Unlike the automatic SessionStart hook (which uses LLM knowledge only), this manual command uses **web research** for more detailed, current results.

### Steps:

1. **Detect tech stack** — run the detector:
```bash
/usr/bin/python3 ~/.claude/skills/cortex/lib/skill_detect.py "$(pwd)" 2>/dev/null
```

2. **Show detected frameworks** — display what was found and ask the user if they want skills for all or specific ones.

3. **Research online** — for each framework the user wants skills for:
   - Use `WebSearch` to find current best practices, patterns, and conventions (e.g., "FastAPI best practices 2026", "Next.js app router patterns")
   - Use `WebFetch` to read authoritative docs if needed
   - Also `Read` the actual project code to understand the user's specific patterns and conventions

4. **Generate skill files** — create `.md` command files that encode:
   - Framework-specific best practices from web research
   - Project-specific patterns from code analysis
   - Testing approaches appropriate for the framework
   - Common scaffolding patterns
   - Each skill should have YAML frontmatter with `description:` and a detailed prompt body

5. **Write files** — use `Write` to create `.md` files in:
   - `.claude/commands/` (project-level, framework-specific skills)
   - `~/.claude/commands/` (global, cross-project utility skills)
   - Use kebab-case filenames matching the command name (e.g., `fastapi-endpoint.md` → `/fastapi-endpoint`)

6. **Store in cortex** — record what was discovered using `memory_store`:
   - `memory_id`: `skill_discovery_<project_name>`
   - `memory_type`: `project`
   - Content: list of created skills and frameworks covered

7. **Report** — show the user what skills were created and how to use them (e.g., "Run `/fastapi-endpoint create user endpoint` to scaffold a new endpoint").

### Quality guidelines for generated skills:
- `description:` must be clear and actionable (shown in command palette)
- Body must contain SPECIFIC instructions, not generic advice
- Include framework conventions, file structure patterns, testing idioms
- Use `$ARGUMENTS` to accept user input
- Keep each skill focused on ONE task (scaffold, test, debug, deploy)
- Max 5-7 skills per framework — pick the most useful ones

### Example skill file (for reference):
```markdown
---
description: Scaffold a new FastAPI endpoint with Pydantic models and tests
---

Create a new FastAPI endpoint based on the user's description: $ARGUMENTS

Follow these conventions:
1. Use async def for route handlers
2. Define Pydantic models for request/response in a models file
3. Use Depends() for shared dependencies (db, auth)
4. Add proper status codes (201 create, 404 not found, 422 validation)
5. Include OpenAPI metadata: summary, description, response_model
6. Add the route to the appropriate router
7. Write tests using TestClient covering success + error paths
8. Follow the existing project structure and naming conventions
```

## Skill Creation (`/cortex addskill`)

When the user runs `/cortex addskill <description>`, create a new slash-command skill file.

### Steps:

1. **Parse the description** — understand what the skill should do (scaffold, debug, automate, query, etc.)

2. **Determine scope (global vs local)** — apply these rules:

   **LOCAL** (`.claude/commands/`) when the skill references ANY of:
   - Project-specific file paths (e.g., `backend/api/`, `frontend/src/lib/api.ts`)
   - Project-specific container names, table names, Redis keys
   - Project-specific conventions, architecture, or domain logic
   - A framework used primarily in this project's specific way

   **GLOBAL** (`~/.claude/commands/`) when the skill is:
   - A generic wrapper around a CLI tool (docker, git, pytest, curl)
   - Useful across multiple projects with no project-specific content
   - A utility pattern (log viewing, DB querying, test running) with no hardcoded paths

   **When in doubt, prefer local.** A local skill can always be copied to global later, but a global skill with project-specific content breaks in other projects.

3. **Read existing skills** — check both `.claude/commands/` and `~/.claude/commands/` for similar skills to avoid duplication. If a similar skill exists, ask the user if they want to update it instead.

4. **Read project code** — if the skill is local, read relevant source files to understand conventions, patterns, and file structure. Encode these specifics into the skill.

5. **Generate the skill file** with:
   - `description:` in YAML frontmatter (clear, actionable, shown in command palette)
   - `$ARGUMENTS` placeholder for user input
   - Specific, actionable instructions (not generic advice)
   - Project conventions if local
   - Example commands or code patterns

6. **Write the file** — kebab-case filename matching the command name

7. **Store in cortex** — record the skill creation:
   - `memory_id`: `skill_<name>_<project_or_global>`
   - `memory_type`: `project` (local) or `reference` (global)
   - Content: skill name, description, scope, what it does

8. **Report** — show the user the skill name and how to use it

### Example:
```
/cortex addskill debug hyperliquid order execution failures
```
→ Creates `.claude/commands/debug-hl-orders.md` (local, references project-specific tables and containers)

```
/cortex addskill generic git PR review checklist
```
→ Creates `~/.claude/commands/pr-review.md` (global, no project-specific content)

## Global vs Local Placement Rules

These rules apply to ALL skill creation — `/cortex discover`, `/cortex addskill`, and auto-generated skills from session learning.

| Signal | Scope | Reasoning |
|--------|-------|-----------|
| References specific file paths | Local | Paths differ across projects |
| References container/service names | Local | Docker setup is project-specific |
| References database tables/columns | Local | Schema is project-specific |
| References project architecture | Local | Each project is different |
| Generic CLI tool wrapper | Global | Works everywhere |
| Language/framework pattern (no project paths) | Global | Reusable knowledge |
| Cross-project utility (log viewer, test runner) | Global | Universal tool |

## Auto Skill Improvement (Session End)

During the **Stop hook** (when `learn.sh` blocks to extract learnings), Claude should ALSO check if any skills should be created or improved based on the session. This happens automatically — no user action needed.

### What to look for:
1. **Repeated patterns** — Did you perform the same multi-step workflow multiple times? → Create a skill for it
2. **Debugging sessions** — Did you debug something complex with specific diagnostic steps? → Create or improve a debug skill
3. **New project knowledge** — Did you discover project-specific commands, table structures, or conventions not yet in any skill? → Update the relevant skill
4. **Skill gaps** — Did you do something that an existing skill should cover but doesn't? → Improve that skill

### How to auto-improve:
1. Check existing skills in `.claude/commands/` and `~/.claude/commands/`
2. If a skill exists but is missing info learned this session → use `Edit` to update it
3. If no skill covers a repeated pattern → create a new one via `Write`
4. Apply the global vs local placement rules above
5. Store a cortex memory noting the skill change: `memory_id: skill_update_<name>`, `memory_type: project`

### Constraints:
- Only create/update skills for patterns that occurred 2+ times OR were explicitly complex
- Don't create trivial skills (one-liner commands)
- Don't duplicate existing skills — improve them instead
- Keep skill files under 200 lines
- Max 2 new skills per session to avoid noise

## Configuration (`/cortex config`)

When the user runs `/cortex config`, read and manage `~/.claude/.cortex_config` (JSON file).

**Available settings:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auto_learn` | bool | `true` | Auto-extract learnings on session stop |
| `auto_skills` | bool | `true` | Auto-suggest skills on session stop |
| `auto_agents` | bool | `true` | Auto-suggest agents on session stop |

**Usage:**
- `/cortex config` — show current config
- `/cortex config auto_skills false` — disable auto skill suggestions

**Implementation:** Read/write `~/.claude/.cortex_config` as JSON:
```bash
# Read
cat ~/.claude/.cortex_config 2>/dev/null || echo '{}'
# Write (use python for safe JSON merge)
python3 -c "
import json, sys
f='$HOME/.claude/.cortex_config'
try: cfg=json.load(open(f))
except: cfg={}
cfg['KEY']=VALUE
json.dump(cfg, open(f,'w'), indent=2)
"
```

## Session Review (`/cortex learn`)

When the user runs `/cortex learn`, review the current conversation and extract any learnings worth persisting. Look for:

1. **Feedback** — corrections the user made, approaches they approved/rejected, preferences expressed
2. **Project** — decisions, constraints, or context about ongoing work that isn't in the code
3. **Reference** — external resources, deployment patterns, or lookup info discovered
4. **User** — new info about the user's role, expertise, or working style

For each finding:
- Check if a similar memory already exists (use `memory_search`) — update rather than duplicate
- Store with a descriptive `memory_id`, appropriate `memory_type`, and relevant `tags`
- Include the project name if the learning is project-specific

Report what you stored in a brief summary. If nothing new was learned, say so.
