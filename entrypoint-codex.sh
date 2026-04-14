#!/bin/bash
set -euo pipefail

# Match container user to host UID/GID for correct file ownership
TARGET_USER="codex"
if [ -n "${HOST_UID:-}" ] && [ "$(id -u "$TARGET_USER")" != "$HOST_UID" ]; then
    usermod -u "$HOST_UID" "$TARGET_USER" 2>/dev/null || true
fi
if [ -n "${HOST_GID:-}" ] && [ "$(id -g "$TARGET_USER")" != "$HOST_GID" ]; then
    groupmod -g "$HOST_GID" "$TARGET_USER" 2>/dev/null || true
fi

# Ensure home dir and volume are owned by the (possibly remapped) user
chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME" 2>/dev/null || true

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

# Inject cage container context into instructions.md
cat > "$CODEX_DIR/instructions.md" <<'CAGE_EOF'
# Container Environment (cage)
You are running inside a Docker container managed by cage.
- You have passwordless `sudo` access — use `sudo apt-get install -y <package>` to install any system packages you need (e.g., playwright, build tools, native libraries)
- Python 3, Node.js (LTS), and npm are pre-installed
- Only the workspace directory is writable on the host filesystem
- `pip install` and `npm install` work without sudo
CAGE_EOF

# Restore workspace trust if it was previously granted but lost by the copy
if [ "$WAS_TRUSTED" -eq 1 ] && ! grep -q "projects\\.\"${WORK_DIR}\"" "$CODEX_DIR/config.toml" 2>/dev/null; then
    printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$WORK_DIR" >> "$CODEX_DIR/config.toml"
fi

# In YOLO mode, auto-trust the workspace so Codex doesn't prompt
if [ "${CAGE_YOLO:-0}" = "1" ]; then
    if ! grep -q "projects\\.\"${WORK_DIR}\"" "$CODEX_DIR/config.toml" 2>/dev/null; then
        touch "$CODEX_DIR/config.toml"
        printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$WORK_DIR" >> "$CODEX_DIR/config.toml"
    fi
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
exec gosu "$TARGET_USER" codex "$@"
