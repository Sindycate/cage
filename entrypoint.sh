#!/bin/bash
set -euo pipefail

# Match container user to host UID/GID for correct file ownership
TARGET_USER="claude"
if [ -n "${HOST_UID:-}" ] && [ "$(id -u "$TARGET_USER")" != "$HOST_UID" ]; then
    usermod -u "$HOST_UID" "$TARGET_USER" 2>/dev/null || true
fi
if [ -n "${HOST_GID:-}" ] && [ "$(id -g "$TARGET_USER")" != "$HOST_GID" ]; then
    groupmod -g "$HOST_GID" "$TARGET_USER" 2>/dev/null || true
fi

# Ensure home dir and volume are owned by the (possibly remapped) user
chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME" 2>/dev/null || true

CLAUDE_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_DIR"

# Persist ~/.claude.json (onboarding/preferences) inside the volume
# so it survives container restarts
PREFS_STORE="$CLAUDE_DIR/.claude.json"
[ ! -f "$PREFS_STORE" ] && echo '{}' > "$PREFS_STORE"
ln -sfn "$PREFS_STORE" "$HOME/.claude.json"

# Copy host settings (read-only mount → writable volume)
[ -f /host-claude/settings.json ] && { rm -f "$CLAUDE_DIR/settings.json" 2>/dev/null; cp /host-claude/settings.json "$CLAUDE_DIR/settings.json"; }

# statusLine.command may reference a script in ~/.claude/ which is read-only in-container
_sl_cmd=$(jq -r '.statusLine.command // empty' "$CLAUDE_DIR/settings.json" 2>/dev/null)
_sl_file="${_sl_cmd/#\~\/\.claude\//}"
if [ "$_sl_file" != "$_sl_cmd" ] && [[ "$_sl_file" != *..* ]] && [ -f "/host-claude/$_sl_file" ]; then
    ln -sfn "/host-claude/$_sl_file" "$CLAUDE_DIR/$_sl_file"
fi

# Merge MCP bridge servers into settings.json
if [ -n "${CAGE_MCP_SERVERS:-}" ]; then
    python3 -c "
import json, os
servers = json.loads(os.environ['CAGE_MCP_SERVERS'])
p = os.path.expanduser('~/.claude/settings.json')
try:
    s = json.load(open(p))
except (FileNotFoundError, json.JSONDecodeError):
    s = {}
mcp = s.setdefault('mcpServers', {})
for name in servers:
    mcp[name] = {'command': 'mcp-relay', 'args': [name]}
with open(p, 'w') as f:
    json.dump(s, f, indent=2)
"
fi

# Inject cage container context into CLAUDE.md, append host's CLAUDE.md if present
cat > "$CLAUDE_DIR/CLAUDE.md" <<'CAGE_EOF'
# Container Environment (cage)
You are running inside a Docker container managed by cage.
- You have passwordless `sudo` access — use `sudo apt-get install -y <package>` to install any system packages you need (e.g., playwright, build tools, native libraries)
- Python 3, Node.js (LTS), and npm are pre-installed
- Only the workspace directory is writable on the host filesystem
- `pip install` and `npm install` work without sudo
CAGE_EOF
if [ -f /host-claude/CLAUDE.md ]; then
    printf '\n' >> "$CLAUDE_DIR/CLAUDE.md"
    cat /host-claude/CLAUDE.md >> "$CLAUDE_DIR/CLAUDE.md"
fi

[ -d /host-claude/agents ]     && ln -sfn /host-claude/agents "$CLAUDE_DIR/agents"

# Use WORKSPACE_DIR so each project gets a unique identity in Claude Code
WORK_DIR="${WORKSPACE_DIR:-/workspace}"

# Prevent git "dubious ownership" errors from UID mismatch
git config --global --add safe.directory "$WORK_DIR"

# Git identity (from cage.conf via env vars)
[ -n "${GIT_USER_NAME:-}" ]  && git config --global user.name "$GIT_USER_NAME"
[ -n "${GIT_USER_EMAIL:-}" ] && git config --global user.email "$GIT_USER_EMAIL"

# SSH alias resolution (e.g. SSH_HOST="github-zse=github.com")
if [ -d "$HOME/.ssh" ]; then
    chmod 700 "$HOME/.ssh" 2>/dev/null || true
    if [ -n "${SSH_HOST:-}" ]; then
        alias_name="${SSH_HOST%%=*}"
        real_host="${SSH_HOST#*=}"
        printf 'Host %s\n    Hostname %s\n' "$alias_name" "$real_host" > "$HOME/.ssh/config"
        chmod 600 "$HOME/.ssh/config"
    fi
fi

# GitHub CLI: copy host config (non-auth settings like git_protocol, username)
if [ -d /host-gh ]; then
    GH_CONFIG_DIR="${HOME}/.config/gh"
    mkdir -p "$GH_CONFIG_DIR"
    cp -rf /host-gh/* "$GH_CONFIG_DIR/" 2>/dev/null || true
fi

cd "$WORK_DIR"
exec gosu "$TARGET_USER" claude "$@"
