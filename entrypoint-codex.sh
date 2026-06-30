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

# Copy host Codex config into the writable volume. When copy_auth is disabled,
# only auth.json is skipped; Codex MCP OAuth has its own credential store and is
# kept separate from the main Codex login cache.
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
    # cp ran as root and preserved host mode bits; re-own so the codex user can read them
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$CODEX_DIR" 2>/dev/null || true
fi

# Add MCP servers selected by cage's central config into the writable container
# config only. Host ~/.codex/config.toml remains untouched.
if [ -n "${CAGE_MCP_SERVERS:-}" ] || [ -n "${CAGE_REMOTE_MCP_SERVERS:-}" ]; then
    CODEX_CONFIG_PATH="$CODEX_DIR/config.toml" python3 - <<'PY'
import json
import os
import re
import sys
from pathlib import Path

path = Path(os.environ["CODEX_CONFIG_PATH"])
text = path.read_text() if path.exists() else ""

def q(value):
    return json.dumps(str(value))

def table_name(name):
    return '[mcp_servers.%s]' % q(name)

def oauth_table_name(name):
    return '[mcp_servers.%s.oauth]' % q(name)

def env_table_name(name):
    return '[mcp_servers.%s.env]' % q(name)

def existing_mcp_names(src):
    names = set()
    for match in re.finditer(r'(?m)^\s*\[mcp_servers\.([A-Za-z0-9_-]+)\]\s*(?:#.*)?$', src):
        names.add(match.group(1))
    for match in re.finditer(r'(?m)^\s*\[mcp_servers\."([^"]+)"\]\s*(?:#.*)?$', src):
        names.add(match.group(1))
    return names

def ensure_rmcp_feature(src):
    if 'experimental_use_rmcp_client' in src:
        return src
    feature_match = re.search(r'(?m)^\s*\[features\]\s*$', src)
    if not feature_match:
        suffix = '' if not src or src.endswith('\n') else '\n'
        return src + suffix + '\n[features]\nexperimental_use_rmcp_client = true\n'
    insert_at = feature_match.end()
    return src[:insert_at] + '\nexperimental_use_rmcp_client = true' + src[insert_at:]

def set_top_level_key(src, key, value):
    first_table = re.search(r'(?m)^\s*\[', src)
    split_at = first_table.start() if first_table else len(src)
    preamble = src[:split_at]
    rest = src[split_at:]
    line = '%s = %s\n' % (key, q(value))
    pattern = re.compile(r'(?m)^\s*%s\s*=.*(?:\n|$)' % re.escape(key))
    if pattern.search(preamble):
        return pattern.sub(line, preamble, count=1) + rest
    suffix = '' if not preamble or preamble.endswith('\n') else '\n'
    return preamble + suffix + line + rest

new_servers = []
if os.environ.get('CAGE_MCP_SERVERS'):
    try:
        bridged = json.loads(os.environ['CAGE_MCP_SERVERS'])
    except Exception as exc:
        sys.stderr.write('cage: invalid CAGE_MCP_SERVERS: %s\n' % exc)
        sys.exit(1)
    for name in bridged:
        port_env = 'MCP_BRIDGE_PORT_%s' % name.upper().replace('-', '_')
        bridge_host = os.environ.get('MCP_BRIDGE_HOST')
        bridge_port = os.environ.get(port_env)
        if not bridge_host or not bridge_port:
            sys.stderr.write(
                'cage: bridged MCP server %r is missing bridge env var(s): %s\n'
                % (name, ', '.join(
                    key for key, value in (
                        ('MCP_BRIDGE_HOST', bridge_host),
                        (port_env, bridge_port),
                    )
                    if not value
                ))
            )
            sys.exit(1)
        new_servers.append({
            'name': name,
            'kind': 'stdio',
            'command': 'mcp-relay',
            'args': [name],
            'env': {
                'MCP_BRIDGE_HOST': bridge_host,
                port_env: bridge_port,
            },
        })

if os.environ.get('CAGE_REMOTE_MCP_SERVERS'):
    try:
        remote = json.loads(os.environ['CAGE_REMOTE_MCP_SERVERS'])
    except Exception as exc:
        sys.stderr.write('cage: invalid CAGE_REMOTE_MCP_SERVERS: %s\n' % exc)
        sys.exit(1)
    for srv in remote:
        client_id = srv.get('oauth_client_id')
        client_env = srv.get('oauth_client_id_env_var')
        if client_env:
            client_id = os.environ.get(client_env)
            if not client_id:
                sys.stderr.write(
                    'cage: OAuth MCP server %r requires env var to be set: %s\n'
                    % (srv.get('name'), client_env))
                sys.exit(1)
        new_servers.append({
            'name': srv.get('name'),
            'kind': 'http',
            'url': srv.get('url'),
            'bearer_token_env_var': srv.get('bearer_token_env_var'),
            'auth': srv.get('auth'),
            'oauth_resource': srv.get('oauth_resource'),
            'oauth_client_id': client_id,
        })

existing = existing_mcp_names(text)
seen = set()
for srv in new_servers:
    name = srv.get('name')
    if not name:
        continue
    if name in seen:
        sys.stderr.write('cage: duplicate generated Codex MCP server: %s\n' % name)
        sys.exit(1)
    seen.add(name)
    if name in existing:
        sys.stderr.write('cage: Codex config already defines MCP server %r; remove it or choose a preset without that server\n' % name)
        sys.exit(1)

if new_servers:
    if any(srv.get('auth') == 'oauth' for srv in new_servers):
        text = set_top_level_key(text, 'mcp_oauth_credentials_store', 'file')
    text = ensure_rmcp_feature(text)
    if text and not text.endswith('\n'):
        text += '\n'
    for srv in new_servers:
        name = srv.get('name')
        if not name:
            continue
        text += '\n' + table_name(name) + '\n'
        if srv['kind'] == 'stdio':
            text += 'command = %s\n' % q(srv['command'])
            text += 'args = [%s]\n' % ', '.join(q(arg) for arg in srv.get('args', []))
            env = srv.get('env') or {}
            if env:
                text += '\n' + env_table_name(name) + '\n'
                for key in sorted(env):
                    text += '%s = %s\n' % (key, q(env[key]))
        else:
            text += 'url = %s\n' % q(srv.get('url') or '')
            if srv.get('auth') == 'oauth':
                if srv.get('oauth_resource'):
                    text += 'oauth_resource = %s\n' % q(srv['oauth_resource'])
                if srv.get('oauth_client_id'):
                    text += '\n' + oauth_table_name(name) + '\n'
                    text += 'client_id = %s\n' % q(srv['oauth_client_id'])
            elif srv.get('bearer_token_env_var'):
                text += 'bearer_token_env_var = %s\n' % q(srv['bearer_token_env_var'])

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text)
PY
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$CODEX_DIR" 2>/dev/null || true
fi

# Copy selected skill packs when cage mounted explicit skill directories. This
# keeps skill availability preset-scoped without copying the entire host
# registry.
if [ -n "${CAGE_SKILL_NAMES:-}" ]; then
    AGENTS_DIR="$HOME/.agents"
    SKILLS_DIR="$AGENTS_DIR/skills"
    mkdir -p "$SKILLS_DIR"
    for _skill_name in $CAGE_SKILL_NAMES; do
        _src="/host-agent-skills/$_skill_name"
        _dst="$SKILLS_DIR/$_skill_name"
        if [ ! -d "$_src" ] || [ ! -f "$_src/SKILL.md" ]; then
            echo "cage: selected skill mount is missing or invalid: $_skill_name" >&2
            exit 1
        fi
        rm -rf "$_dst"
        mkdir -p "$_dst"
        cp -rf "$_src/." "$_dst/"
    done
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$AGENTS_DIR" 2>/dev/null || true
# Copy host ~/.agents/ (npm `skills` CLI registry) into the writable home so
# globally-installed skills (e.g. via `npx skills add ... -g`) are visible.
elif [ -d /host-agents ]; then
    AGENTS_DIR="$HOME/.agents"
    mkdir -p "$AGENTS_DIR"
    cp -rf /host-agents/. "$AGENTS_DIR/"
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$AGENTS_DIR" 2>/dev/null || true
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

# Git identity from resolved cage config
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
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$GH_CONFIG_DIR" 2>/dev/null || true
fi

# Host command bridge shims: write /usr/local/bin/<name> for each entry in
# CAGE_HOST_COMMANDS (space-separated). The shim delegates to host-cmd-relay,
# which tunnels stdio over TCP to host-cmd-bridge.py on the host.
if [ -n "${CAGE_HOST_COMMANDS:-}" ]; then
    for _name in $CAGE_HOST_COMMANDS; do
        _shim="/usr/local/bin/$_name"
        cat > "$_shim" <<SHIM_EOF
#!/bin/sh
exec host-cmd-relay $_name "\$@"
SHIM_EOF
        chmod 755 "$_shim"
    done
fi

cd "$WORK_DIR"
exec gosu "$TARGET_USER" codex "$@"
