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

# Prevent git "dubious ownership" errors from UID mismatch
git config --global --add safe.directory /workspace

cd /workspace
exec claude "$@"
