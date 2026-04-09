#!/bin/bash
set -euo pipefail

CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"

# Use WORKSPACE_DIR so each project gets a unique identity
WORK_DIR="${WORKSPACE_DIR:-/workspace}"

# Check if workspace was already trusted (from a previous run)
WAS_TRUSTED=0
if [ -f "$CODEX_DIR/config.toml" ] && grep -q "projects\\.\"${WORK_DIR}\"" "$CODEX_DIR/config.toml" 2>/dev/null; then
    WAS_TRUSTED=1
fi

# Copy host Codex config into writable volume
# Skip auth.json — it holds provider-specific OAuth tokens that may be expired
# or irrelevant when using alternative providers (e.g. Azure OpenAI instead of OpenAI)
if [ -d /host-codex ]; then
    for f in /host-codex/*; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        [ "${CODEX_COPY_AUTH:-1}" = "0" ] && [ "$name" = "auth.json" ] && continue
        cp -rf "$f" "$CODEX_DIR/"
    done
    # Also copy dotfiles
    for f in /host-codex/.*; do
        [ -e "$f" ] || continue
        name="$(basename "$f")"
        [ "$name" = "." ] || [ "$name" = ".." ] && continue
        cp -rf "$f" "$CODEX_DIR/"
    done
fi

# Restore workspace trust if it was previously granted but lost by the copy
if [ "$WAS_TRUSTED" -eq 1 ] && ! grep -q "projects\\.\"${WORK_DIR}\"" "$CODEX_DIR/config.toml" 2>/dev/null; then
    printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$WORK_DIR" >> "$CODEX_DIR/config.toml"
fi

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
exec codex "$@"
