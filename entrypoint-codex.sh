#!/bin/bash
set -euo pipefail
umask 077

# Match container user to host UID/GID for correct file ownership
TARGET_USER="codex"
if [ -n "${HOST_UID:-}" ] && [ "$(id -u "$TARGET_USER")" != "$HOST_UID" ]; then
    usermod -u "$HOST_UID" "$TARGET_USER" 2>/dev/null || true
fi
if [ -n "${HOST_GID:-}" ] && [ "$(id -g "$TARGET_USER")" != "$HOST_GID" ]; then
    groupmod -g "$HOST_GID" "$TARGET_USER" 2>/dev/null || true
fi

# Ensure home dir and volume are owned by the (possibly remapped) user
chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME" 2>/dev/null || true

CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"
chmod 700 "$CODEX_DIR" 2>/dev/null || true

# Use WORKSPACE_DIR so each project gets a unique identity
WORK_DIR="${WORKSPACE_DIR:-/workspace}"

# Persistent state belongs to the previous model process. Remove an unsafe
# config destination before any root read or append; normal regular state is
# preserved until the host copy/reconciler replaces it.
if [ -L "$CODEX_DIR/config.toml" ] || { [ -e "$CODEX_DIR/config.toml" ] && [ ! -f "$CODEX_DIR/config.toml" ]; }; then
    rm -rf -- "$CODEX_DIR/config.toml"
fi

# Check if workspace was already trusted (from a previous run)
WAS_TRUSTED=0
if [ -f "$CODEX_DIR/config.toml" ] && grep -q "projects\\.\"${WORK_DIR}\"" "$CODEX_DIR/config.toml" 2>/dev/null; then
    WAS_TRUSTED=1
fi

# Import only host-owned Codex configuration and credentials into the writable
# per-repository volume. Runtime-owned state (sessions, history, SQLite indexes,
# logs, memories, caches, and similar entries) must remain volume-local: copying
# or replacing it from a shared host Codex directory can hide or destroy the
# repository's resumable history. When copy_auth is disabled, auth.json is
# explicitly removed. Codex MCP OAuth's .credentials.json is also reconciled by
# the host launcher before/after the container runs so rotated refresh tokens do
# not diverge between the host Codex dir and this volume.
reconcile_codex_auth() {
    local host_dir="$1"
    local codex_dir="$2"
    local copy_auth="$3"
    local destination="$codex_dir/auth.json"

    # Remove first so a disabled/missing source cannot fall back to credentials
    # retained by this repository volume from a different preset.
    rm -rf -- "$destination"
    if [ "$copy_auth" != "0" ] && [ -f "$host_dir/auth.json" ]; then
        install -m 600 "$host_dir/auth.json" "$destination"
    fi
}

copy_host_codex_entry() {
    local source="$1"
    local name="$2"
    local destination="$CODEX_DIR/$name"
    rm -rf -- "$destination"
    [ -f "$source" ] || {
        echo "cage: expected regular host Codex state: $name" >&2
        return 1
    }
    install -m 600 -- "$source" "$destination"
}

copy_host_codex_directory() {
    local source="$1"
    local name="$2"
    local destination="$CODEX_DIR/$name"
    [ -d "$source" ] && [ ! -L "$source" ] || {
        echo "cage: expected host Codex configuration directory: $name" >&2
        return 1
    }
    rm -rf -- "$destination"
    # Do not preserve host ownership. Cage deliberately omits CAP_FOWNER, so a
    # recursive archive copy can chown a destination inode to the host UID and
    # then fail while restoring its mode. The final recursive chown below makes
    # the copied configuration readable by the remapped Codex user.
    cp -R --no-dereference -- "$source" "$destination"
}

import_host_codex_state() {
    local host_dir="$1"
    local name source
    [ -d "$host_dir" ] || return 0
    for source in \
        "$host_dir"/config.toml \
        "$host_dir"/*.config.toml \
        "$host_dir"/AGENTS.md \
        "$host_dir"/AGENTS.override.md \
        "$host_dir"/hooks.json \
        "$host_dir"/.credentials.json; do
        [ -e "$source" ] || continue
        name="${source##*/}"
        copy_host_codex_entry "$source" "$name"
    done
    if [ -e "$host_dir/rules" ]; then
        copy_host_codex_directory "$host_dir/rules" rules
    fi
}

if [ -d /host-codex ]; then
    import_host_codex_state /host-codex
    chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$CODEX_DIR" 2>/dev/null || true
fi

reconcile_codex_auth /host-codex "$CODEX_DIR" "${CODEX_COPY_AUTH:-1}"

# Root deliberately lacks CAP_FOWNER. Secure each sensitive file through one
# no-follow descriptor, assign that inode to the remapped user, then fork and
# set its mode after dropping to that owner. Path swaps can make the launch fail
# but cannot redirect chmod onto a repository or other writable host mount.
CAGE_CODEX_STATE_DIR="$CODEX_DIR" \
CAGE_TARGET_UID="$(id -u "$TARGET_USER")" \
CAGE_TARGET_GID="$(id -g "$TARGET_USER")" \
python3 -I - <<'PY'
import os
import stat
import sys

state_dir = os.environ["CAGE_CODEX_STATE_DIR"]
target_uid = int(os.environ["CAGE_TARGET_UID"])
target_gid = int(os.environ["CAGE_TARGET_GID"])
names = ("auth.json", ".credentials.json", "config.toml")


def identity(value):
    return value.st_dev, value.st_ino


def refuse(name, detail):
    sys.stderr.write(
        "cage: refusing unsafe sensitive Codex state %s: %s\n" % (name, detail)
    )
    raise SystemExit(1)


directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
try:
    directory_fd = os.open(state_dir, directory_flags)
except OSError as exc:
    refuse("directory", exc)

try:
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        refuse("directory", "state path is not a directory")

    for name in names:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            refuse(name, exc)

        if not stat.S_ISREG(before.st_mode):
            refuse(name, "expected a regular non-symlink file")
        if before.st_nlink != 1:
            refuse(name, "hard-linked sensitive files are not allowed")

        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            refuse(name, exc)

        try:
            opened = os.fstat(descriptor)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if identity(before) != identity(opened) or identity(opened) != identity(current):
                refuse(name, "file changed while it was being opened")
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                refuse(name, "opened state is not a private regular file")

            os.fchown(descriptor, target_uid, target_gid)
            owned = os.fstat(descriptor)
            if (
                identity(opened) != identity(owned)
                or owned.st_uid != target_uid
                or owned.st_gid != target_gid
            ):
                refuse(name, "ownership normalization did not reach the opened file")

            child = os.fork()
            if child == 0:
                try:
                    os.setgroups([])
                    os.setgid(target_gid)
                    os.setuid(target_uid)
                    os.fchmod(descriptor, 0o600)
                    secured = os.fstat(descriptor)
                    if (
                        identity(owned) != identity(secured)
                        or secured.st_uid != target_uid
                        or secured.st_gid != target_gid
                        or stat.S_IMODE(secured.st_mode) != 0o600
                    ):
                        raise OSError("mode verification failed")
                    os._exit(0)
                except BaseException as exc:
                    message = "cage: could not secure Codex state %s: %s\n" % (name, exc)
                    os.write(2, message.encode("utf-8", "replace"))
                    os._exit(1)

            _, child_status = os.waitpid(child, 0)
            if not os.WIFEXITED(child_status) or os.WEXITSTATUS(child_status) != 0:
                refuse(name, "could not set mode as the remapped owner")

            secured = os.fstat(descriptor)
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                refuse(name, "file changed while its mode was being set: %s" % exc)
            if identity(secured) != identity(current):
                refuse(name, "file changed while its mode was being set")
            if (
                not stat.S_ISREG(secured.st_mode)
                or secured.st_nlink != 1
                or secured.st_uid != target_uid
                or secured.st_gid != target_gid
                or stat.S_IMODE(secured.st_mode) != 0o600
            ):
                refuse(name, "private owner or mode verification failed")
        finally:
            os.close(descriptor)
finally:
    os.close(directory_fd)
PY

# Reconcile MCP servers selected by cage's central config into a marked,
# generated block. This always runs so switching to a preset with no MCP pack
# removes Cage's previous block while leaving host/user TOML untouched.
CODEX_CONFIG_PATH="$CODEX_DIR/config.toml" python3 -I - <<'PY'
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

path = Path(os.environ["CODEX_CONFIG_PATH"])
original_text = path.read_text() if path.exists() else ""
managed_start = "# BEGIN CAGE MANAGED MCP SERVERS"
managed_end = "# END CAGE MANAGED MCP SERVERS"


def strip_managed_blocks(src):
    pattern = re.compile(
        r"(?ms)^[ \t]*%s[ \t]*\n.*?^[ \t]*%s[ \t]*(?:\n|$)"
        % (re.escape(managed_start), re.escape(managed_end))
    )
    return pattern.sub("", src)


text = strip_managed_blocks(original_text)

def q(value):
    return json.dumps(str(value))

def table_name(name):
    return '[mcp_servers.%s]' % q(name)

def oauth_table_name(name):
    return '[mcp_servers.%s.oauth]' % q(name)

def env_table_name(name):
    return '[mcp_servers.%s.env]' % q(name)


def existing_mcp_names(src):
    try:
        parsed = tomllib.loads(src)
    except tomllib.TOMLDecodeError as exc:
        sys.stderr.write("cage: cannot reconcile MCP servers in invalid Codex config: %s\n" % exc)
        sys.exit(1)
    servers = parsed.get("mcp_servers", {})
    return set(servers) if isinstance(servers, dict) else set()


def write_text_atomic(destination, value):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".%s." % destination.name,
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(value)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, destination)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


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
        bridge_token = os.environ.get('MCP_BRIDGE_TOKEN')
        if not bridge_host or not bridge_port or not bridge_token:
            sys.stderr.write(
                'cage: bridged MCP server %r is missing bridge env var(s): %s\n'
                % (name, ', '.join(
                    key for key, value in (
                        ('MCP_BRIDGE_HOST', bridge_host),
                        (port_env, bridge_port),
                        ('MCP_BRIDGE_TOKEN', bridge_token),
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
                'MCP_BRIDGE_TOKEN': bridge_token,
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
            'oauth_scopes': srv.get('oauth_scopes') or [],
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
    text += '\n' + managed_start + '\n'
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
                if srv.get('oauth_scopes'):
                    text += 'scopes = [%s]\n' % ', '.join(q(scope) for scope in srv['oauth_scopes'])
                if srv.get('oauth_client_id'):
                    text += '\n' + oauth_table_name(name) + '\n'
                    text += 'client_id = %s\n' % q(srv['oauth_client_id'])
            elif srv.get('bearer_token_env_var'):
                text += 'bearer_token_env_var = %s\n' % q(srv['bearer_token_env_var'])
    text += managed_end + '\n'

if path.exists() or text != original_text:
    write_text_atomic(path, text)
PY
chmod 600 "$CODEX_DIR/config.toml" 2>/dev/null || true
chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$CODEX_DIR" 2>/dev/null || true

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
    chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$AGENTS_DIR" 2>/dev/null || true
# Copy host ~/.agents/ (npm `skills` CLI registry) into the writable home so
# globally-installed skills (e.g. via `npx skills add ... -g`) are visible.
elif [ -d /host-agents ]; then
    AGENTS_DIR="$HOME/.agents"
    mkdir -p "$AGENTS_DIR"
    cp -rf /host-agents/. "$AGENTS_DIR/"
    chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$AGENTS_DIR" 2>/dev/null || true
fi

# Inject Cage context through a same-directory atomic replacement so a symlink
# planted in persistent model-owned state cannot redirect the root write.
_codex_instructions_tmp="$(mktemp "$CODEX_DIR/.instructions.md.cage.XXXXXX")"
cat > "$_codex_instructions_tmp" <<'CAGE_EOF'
# Container Environment (cage)
You are running inside a Docker container managed by cage.
- You have passwordless `sudo` access — use `sudo apt-get install -y <package>` to install any system packages you need (e.g., playwright, build tools, native libraries)
- Python 3, Node.js (LTS), and npm are pre-installed
- The workspace and any mounts explicitly marked read-write can modify host files; the workspace includes writable Git metadata
- Read-only credential mounts can still be read and used, and selected host MCP/command bridges execute with host authority
- Cage may reconcile selected session and OAuth state back to host-owned locations after the tool exits
- `pip install` and `npm install` work without sudo
CAGE_EOF
chmod 600 "$_codex_instructions_tmp"
chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$_codex_instructions_tmp" 2>/dev/null || true
mv -fT -- "$_codex_instructions_tmp" "$CODEX_DIR/instructions.md"

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
exec gosu "$TARGET_USER" codex "$@"
