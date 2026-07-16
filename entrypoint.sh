#!/bin/bash
set -euo pipefail
umask 077

# Match container user to host UID/GID for correct file ownership
TARGET_USER="claude"
if [ -n "${HOST_UID:-}" ] && [ "$(id -u "$TARGET_USER")" != "$HOST_UID" ]; then
    usermod -u "$HOST_UID" "$TARGET_USER" 2>/dev/null || true
fi
if [ -n "${HOST_GID:-}" ] && [ "$(id -g "$TARGET_USER")" != "$HOST_GID" ]; then
    groupmod -g "$HOST_GID" "$TARGET_USER" 2>/dev/null || true
fi

# Ensure home dir and volume are owned by the (possibly remapped) user
chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME" 2>/dev/null || true

CLAUDE_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_DIR"
chmod 700 "$CLAUDE_DIR" 2>/dev/null || true

# Persist ~/.claude.json (onboarding/preferences) inside the volume. Cage-owned
# MCP entries are tracked separately and rebuilt on every launch: user settings
# remain durable, while a removed preset, host entry, or token cannot leave a
# stale generated connector behind.
PREFS_STORE="$CLAUDE_DIR/.claude.json"
MCP_MANIFEST="$CLAUDE_DIR/.cage-managed-mcp.json"
CLAUDE_PREFS_STORE="$PREFS_STORE" CLAUDE_MCP_MANIFEST="$MCP_MANIFEST" python3 -I - <<'PY'
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

store = Path(os.environ["CLAUDE_PREFS_STORE"])
manifest_path = Path(os.environ["CLAUDE_MCP_MANIFEST"])
host_path = Path(os.environ.get("CLAUDE_HOST_CONFIG_PATH", "/host-claude-json"))


def read_json(path, default, state_label=None):
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("refusing non-regular or symlinked JSON state")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identity = lambda value: (value.st_dev, value.st_ino)
        if identity(before) != identity(opened) or identity(opened) != identity(current):
            os.close(descriptor)
            raise OSError("JSON state changed while it was being opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        if state_label:
            sys.stderr.write("cage: invalid %s at %s: %s\n" % (state_label, path, exc))
            sys.exit(1)
        return default


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(value, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def expand(value, missing):
    if isinstance(value, str):
        def sub(match):
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.append(name)
            return match.group(0)

        return re.sub(r'\${(\w+)(?::-([^}]*))?}', sub, value)
    if isinstance(value, dict):
        return {key: expand(item, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, missing) for item in value]
    return value


def warn_missing(name, missing):
    sys.stderr.write(
        "cage: skipping MCP server %r — unset env var(s): %s\n"
        % (name, ", ".join(sorted(set(missing))))
    )


prefs = read_json(store, {}, "Claude preferences")
if not isinstance(prefs, dict):
    sys.stderr.write("cage: Claude preferences must contain a JSON object: %s\n" % store)
    sys.exit(1)

manifest = read_json(manifest_path, {}, "Cage MCP manifest")
if not isinstance(manifest, dict):
    sys.stderr.write("cage: Cage MCP manifest must contain a JSON object: %s\n" % manifest_path)
    sys.exit(1)
previous_names = manifest.get("mcp_server_names", [])
previous_names = {name for name in previous_names if isinstance(name, str)}
shadowed_servers = manifest.get("shadowed_mcp_servers", {})
if not isinstance(shadowed_servers, dict):
    sys.stderr.write("cage: Cage MCP manifest shadowed_mcp_servers must be an object\n")
    sys.exit(1)

existing_mcp = prefs.get("mcpServers")
mcp = dict(existing_mcp) if isinstance(existing_mcp, dict) else {}
for name in previous_names:
    if name in shadowed_servers:
        mcp[name] = shadowed_servers[name]
    else:
        mcp.pop(name, None)

# Preserve the historical precedence: host user config, central HTTP config,
# then the stdio bridge. Only entries successfully materialized for this launch
# are recorded as managed.
managed = {}
host = read_json(host_path, {}) if host_path.is_file() else {}
host_servers = host.get("mcpServers", {}) if isinstance(host, dict) else {}
if isinstance(host_servers, dict):
    for name, conf in host_servers.items():
        missing = []
        expanded = expand(conf, missing)
        if missing:
            warn_missing(name, missing)
            continue
        managed[name] = expanded

remote_json = os.environ.get("CAGE_REMOTE_MCP_SERVERS")
if remote_json:
    try:
        remote_servers = json.loads(remote_json)
    except Exception as exc:
        sys.stderr.write("cage: invalid CAGE_REMOTE_MCP_SERVERS: %s\n" % exc)
        sys.exit(1)
    if not isinstance(remote_servers, list):
        sys.stderr.write("cage: CAGE_REMOTE_MCP_SERVERS must be a JSON list\n")
        sys.exit(1)
    for srv in remote_servers:
        if not isinstance(srv, dict):
            continue
        name = srv.get("name")
        if not name:
            continue
        conf = {"type": "http", "url": srv.get("url", "")}
        headers = dict(srv.get("headers") or {})
        bearer = srv.get("bearer_token_env_var")
        if bearer:
            headers.setdefault("Authorization", "Bearer ${%s}" % bearer)
        if headers:
            conf["headers"] = headers
        missing = []
        if srv.get("auth") == "oauth":
            client_id = srv.get("oauth_client_id")
            client_env = srv.get("oauth_client_id_env_var")
            if client_env:
                client_id = os.environ.get(client_env)
                if not client_id:
                    missing.append(client_env)
            if client_id:
                conf["oauth"] = {"clientId": client_id}
        conf = expand(conf, missing)
        if missing:
            warn_missing(name, missing)
            continue
        managed[name] = conf

bridged_json = os.environ.get("CAGE_MCP_SERVERS")
if bridged_json:
    try:
        bridged_servers = json.loads(bridged_json)
    except Exception as exc:
        sys.stderr.write("cage: invalid CAGE_MCP_SERVERS: %s\n" % exc)
        sys.exit(1)
    if not isinstance(bridged_servers, (dict, list)):
        sys.stderr.write("cage: CAGE_MCP_SERVERS must be a JSON object or list\n")
        sys.exit(1)
    for name in bridged_servers:
        managed[name] = {"type": "stdio", "command": "mcp-relay", "args": [name]}

next_shadowed = {}
for name in managed:
    if name in previous_names and name in shadowed_servers:
        next_shadowed[name] = shadowed_servers[name]
    elif name in mcp:
        next_shadowed[name] = mcp[name]

mcp.update(managed)
if mcp:
    prefs["mcpServers"] = mcp
else:
    prefs.pop("mcpServers", None)

write_json_atomic(store, prefs)
write_json_atomic(
    manifest_path,
    {
        "version": 2,
        "mcp_server_names": sorted(managed),
        "shadowed_mcp_servers": next_shadowed,
    },
)
PY

ln -sfn "$PREFS_STORE" "$HOME/.claude.json"

# Copy host settings (read-only mount → writable volume)
[ -f /host-claude/settings.json ] && { rm -f "$CLAUDE_DIR/settings.json" 2>/dev/null; cp /host-claude/settings.json "$CLAUDE_DIR/settings.json"; chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$CLAUDE_DIR/settings.json" 2>/dev/null || true; }

# statusLine.command may reference a script in ~/.claude/ which is read-only in-container
_sl_cmd=$(jq -r '.statusLine.command // empty' "$CLAUDE_DIR/settings.json" 2>/dev/null)
_sl_file="${_sl_cmd/#\~\/\.claude\//}"
if [ "$_sl_file" != "$_sl_cmd" ] && [[ "$_sl_file" != *..* ]] && [ -f "/host-claude/$_sl_file" ]; then
    ln -sfn "/host-claude/$_sl_file" "$CLAUDE_DIR/$_sl_file"
fi

# ccstatusline reads ~/.config/ccstatusline/settings.json, not ~/.claude/settings.json
if [ -d /host-ccstatusline ]; then
    mkdir -p "$HOME/.config/ccstatusline"
    cp -rf /host-ccstatusline/. "$HOME/.config/ccstatusline/"
    chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME/.config/ccstatusline" 2>/dev/null || true
fi

# MCP reconciliation runs as root; keep its generated state private while still
# allowing Claude to update normal preferences during the session.
chmod 600 "$PREFS_STORE" "$MCP_MANIFEST" 2>/dev/null || true
chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$PREFS_STORE" "$MCP_MANIFEST" 2>/dev/null || true

# Inject Cage context through a same-directory atomic replacement. Persistent
# state belongs to the previous model process, so never redirect through a
# destination it may have replaced with a symlink.
_claude_instructions_tmp="$(mktemp "$CLAUDE_DIR/.CLAUDE.md.cage.XXXXXX")"
cat > "$_claude_instructions_tmp" <<'CAGE_EOF'
# Container Environment (cage)
You are running inside a Docker container managed by cage.
- You have passwordless `sudo` access — use `sudo apt-get install -y <package>` to install any system packages you need (e.g., playwright, build tools, native libraries)
- Python 3, Node.js (LTS), and npm are pre-installed
- The workspace and any mounts explicitly marked read-write can modify host files; the workspace includes writable Git metadata
- Read-only credential mounts can still be read and used, and selected host MCP/command bridges execute with host authority
- Cage may reconcile selected session and OAuth state back to host-owned locations after the tool exits
- `pip install` and `npm install` work without sudo
CAGE_EOF
if [ -f /host-claude/CLAUDE.md ]; then
    printf '\n' >> "$_claude_instructions_tmp"
    cat /host-claude/CLAUDE.md >> "$_claude_instructions_tmp"
fi
chmod 600 "$_claude_instructions_tmp"
chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$_claude_instructions_tmp" 2>/dev/null || true
mv -fT -- "$_claude_instructions_tmp" "$CLAUDE_DIR/CLAUDE.md"

if [ -d /host-claude/agents ]; then
    rm -rf -- "$CLAUDE_DIR/agents"
    ln -s /host-claude/agents "$CLAUDE_DIR/agents"
fi

# Use WORKSPACE_DIR so each project gets a unique identity in Claude Code
WORK_DIR="${WORKSPACE_DIR:-/workspace}"

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
    chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$GH_CONFIG_DIR" 2>/dev/null || true
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
exec gosu "$TARGET_USER" claude "$@"
