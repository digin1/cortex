#!/bin/bash
# Cortex installer — sets up vector memory + agent fleet for Claude Code
# Usage: bash ~/.claude/skills/cortex/install.sh

set -e

CORTEX_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

echo "=== Cortex Installer ==="
echo "Cortex directory: $CORTEX_DIR"
echo ""

# Step 1: Check prerequisites
echo "[1/6] Checking prerequisites..."

if ! command -v claude &>/dev/null; then
    echo "  WARNING: claude CLI not found — cortex requires Claude Code v2.1.9+"
fi

if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Install Python 3.8+ first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python: $PYTHON_VERSION"

# Step 2: Install ChromaDB
echo ""
echo "[2/6] Installing ChromaDB..."

if python3 -c "import chromadb" 2>/dev/null; then
    echo "  ChromaDB already installed."
else
    if python3 -m pip install chromadb 2>/dev/null; then
        echo "  ChromaDB installed successfully."
    elif python3 -m pip install --break-system-packages chromadb 2>/dev/null; then
        echo "  ChromaDB installed successfully."
    elif pip3 install chromadb 2>/dev/null; then
        echo "  ChromaDB installed successfully."
    elif pip3 install --break-system-packages chromadb 2>/dev/null; then
        echo "  ChromaDB installed successfully."
    else
        echo "  ERROR: Could not install ChromaDB. Install manually: pip install chromadb"
        exit 1
    fi
fi

# Step 3: Initialize database
echo ""
echo "[3/6] Initializing ChromaDB database..."

python3 -W ignore -c "
import chromadb
client = chromadb.PersistentClient(path='$CLAUDE_DIR/cortex-db')
col = client.get_or_create_collection('claude_memories')
print(f'  Database ready: {col.count()} memories')
"

# Step 4: Configure MCP server
echo ""
echo "[4/6] Configuring MCP server..."

MCP_FILE="$CLAUDE_DIR/.mcp.json"

if [ -f "$MCP_FILE" ]; then
    # Check if cortex entry already exists
    if python3 -c "import json; d=json.load(open('$MCP_FILE')); exit(0 if 'cortex' in d.get('mcpServers',{}) else 1)" 2>/dev/null; then
        echo "  MCP server already configured."
    else
        # Merge cortex into existing config
        python3 -W ignore -c "
import json
with open('$MCP_FILE', 'r') as f:
    config = json.load(f)
config.setdefault('mcpServers', {})['cortex'] = {
    'type': 'stdio',
    'command': 'python3',
    'args': ['-W', 'ignore', '$CORTEX_DIR/mcp_server.py']
}
with open('$MCP_FILE', 'w') as f:
    json.dump(config, f, indent=2)
print('  MCP server added to existing config.')
"
    fi
else
    python3 -W ignore -c "
import json
config = {
    'mcpServers': {
        'cortex': {
            'type': 'stdio',
            'command': 'python3',
            'args': ['-W', 'ignore', '$CORTEX_DIR/mcp_server.py']
        }
    }
}
with open('$MCP_FILE', 'w') as f:
    json.dump(config, f, indent=2)
print('  MCP server config created.')
"
fi

# Step 5: Configure hooks and permissions in settings.json
echo ""
echo "[5/6] Configuring hooks and permissions..."

SETTINGS_FILE="$CLAUDE_DIR/settings.json"

python3 -W ignore -c "
import json, os

settings_path = '$SETTINGS_FILE'

if os.path.exists(settings_path):
    with open(settings_path, 'r') as f:
        settings = json.load(f)
else:
    settings = {}

# Add permissions (merge with existing)
perms = settings.setdefault('permissions', {})
allow = set(perms.get('allow', []))
allow.update([
    'mcp__cortex__memory_store',
    'mcp__cortex__memory_search',
    'mcp__cortex__memory_get',
    'mcp__cortex__memory_list',
    'mcp__cortex__memory_delete',
    'mcp__cortex__memory_update',
    'mcp__cortex__memory_merge',
    'mcp__cortex__memory_stats',
])
perms['allow'] = sorted(allow)

# Add status line
settings['statusLine'] = {
    'type': 'command',
    'command': 'bash $CORTEX_DIR/statusline.sh 2>/dev/null',
    'padding': 0,
}

# Define hooks
cortex_hooks = {
    'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/recall.sh 2>/dev/null', 'statusMessage': 'Recalling relevant memories...'}]}],
    'PreToolUse': [{'matcher': 'mcp__cortex', 'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/cortex_pretool_enrich.sh 2>/dev/null', 'statusMessage': 'Enriching cortex operation...'}]}],
    'PostToolUse': [{'matcher': 'Agent', 'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/agent_track.sh 2>/dev/null', 'statusMessage': 'Tracking agent usage...'}]}],
    'PreCompact': [{'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/compact_save.sh 2>/dev/null', 'statusMessage': 'Extracting learnings + managing agent fleet...'}]}],
    'PostCompact': [{'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/post_compact_save.sh 2>/dev/null', 'statusMessage': 'Extracting knowledge from compressed context...'}]}],
    'SubagentStart': [{'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/agent_context_inject.sh 2>/dev/null', 'statusMessage': 'Injecting cortex context into agent...'}]}],
    'SessionStart': [
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/cleanup.sh 2>/dev/null', 'statusMessage': 'Cleaning stale memory snapshots...', 'async': True}]},
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/agent_bootstrap.sh 2>/dev/null', 'statusMessage': 'Bootstrapping agents from cortex...', 'async': True}]},
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/memory_hygiene.sh 2>/dev/null', 'statusMessage': 'Memory hygiene check...', 'async': True}]},
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/skill_discover.sh 2>/dev/null', 'statusMessage': 'Discovering project skills...', 'async': True}]},
    ],
    'SessionEnd': [{'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/session_end_cleanup.sh 2>/dev/null', 'statusMessage': 'Saving session summary...'}]}],
    'Stop': [
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/learn.sh 2>/dev/null', 'statusMessage': 'Saving session learnings...'}]},
        {'hooks': [{'type': 'command', 'command': 'bash $CORTEX_DIR/fleet_eval_stop.sh 2>/dev/null', 'statusMessage': 'Evaluating agent fleet health...'}]},
    ],
}

# Merge hooks (don't overwrite non-cortex hooks)
existing_hooks = settings.get('hooks', {})
for event, hook_list in cortex_hooks.items():
    if event not in existing_hooks:
        existing_hooks[event] = hook_list
    else:
        # Check if cortex hooks already present (by checking command substring)
        existing_cmds = set()
        for h in existing_hooks[event]:
            for inner in h.get('hooks', []):
                existing_cmds.add(inner.get('command', ''))
        for new_hook in hook_list:
            for inner in new_hook.get('hooks', []):
                if inner.get('command', '') not in existing_cmds:
                    existing_hooks[event].append(new_hook)
                    break
settings['hooks'] = existing_hooks

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  Hooks and permissions configured.')
"

# Step 6: Install global behavioral rules
echo ""
echo "[6/6] Installing global behavioral rules..."

# CLAUDE.md
if [ -f "$CLAUDE_DIR/CLAUDE.md" ]; then
    if grep -q "Cortex Memory System" "$CLAUDE_DIR/CLAUDE.md" 2>/dev/null; then
        echo "  CLAUDE.md already contains cortex rules."
    else
        echo "" >> "$CLAUDE_DIR/CLAUDE.md"
        cat "$CORTEX_DIR/config/CLAUDE.md" >> "$CLAUDE_DIR/CLAUDE.md"
        echo "  Appended cortex rules to existing CLAUDE.md."
    fi
else
    cp "$CORTEX_DIR/config/CLAUDE.md" "$CLAUDE_DIR/CLAUDE.md"
    echo "  Created ~/.claude/CLAUDE.md."
fi

# Rules directory
mkdir -p "$CLAUDE_DIR/rules"
cp "$CORTEX_DIR/config/cortex-memory.md" "$CLAUDE_DIR/rules/cortex-memory.md"
cp "$CORTEX_DIR/config/skill-discovery.md" "$CLAUDE_DIR/rules/skill-discovery.md"
echo "  Installed rules to ~/.claude/rules/."

# Global agents
mkdir -p "$CLAUDE_DIR/agents"
if [ -d "$CORTEX_DIR/config/agents" ]; then
    for agent_file in "$CORTEX_DIR/config/agents"/*.md; do
        [ -f "$agent_file" ] || continue
        dest="$CLAUDE_DIR/agents/$(basename "$agent_file")"
        if [ -f "$dest" ]; then
            echo "  Agent $(basename "$agent_file") already exists, skipping."
        else
            cp "$agent_file" "$dest"
            echo "  Installed agent: $(basename "$agent_file")"
        fi
    done
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Restart Claude Code to activate cortex."
echo "Run 'bash $CORTEX_DIR/test.sh' to verify."
