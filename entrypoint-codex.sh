#!/bin/bash
set -euo pipefail

CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"

# Copy host Codex config (read-only mount → writable volume)
# Only copy config.toml — avoid overwriting state DB and session data
if [ -f /host-codex/config.toml ]; then
    cp -f /host-codex/config.toml "$CODEX_DIR/config.toml"
fi

# Prevent git "dubious ownership" errors from UID mismatch
git config --global --add safe.directory /workspace

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

cd /workspace
exec codex "$@"
