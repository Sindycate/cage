#!/bin/bash
set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_DIR"

# Persist ~/.claude.json (onboarding/preferences) inside the volume
# so it survives container restarts
PREFS_STORE="$CLAUDE_DIR/.claude.json"
[ ! -f "$PREFS_STORE" ] && echo '{}' > "$PREFS_STORE"
ln -sfn "$PREFS_STORE" "$HOME/.claude.json"

# Copy host settings (read-only mount → writable volume)
[ -f /host-claude/settings.json ] && cp -f /host-claude/settings.json "$CLAUDE_DIR/settings.json"

# Symlink optional host files (read-only is fine, claude only reads these)
[ -f /host-claude/CLAUDE.md ]  && ln -sfn /host-claude/CLAUDE.md "$CLAUDE_DIR/CLAUDE.md"
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
exec claude "$@"
