#!/bin/bash
set -euo pipefail
umask 077

TARGET_USER=opencode
if [ -n "${HOST_UID:-}" ] && [ "$(id -u "$TARGET_USER")" != "$HOST_UID" ]; then
    usermod -u "$HOST_UID" "$TARGET_USER" 2>/dev/null || true
fi
if [ -n "${HOST_GID:-}" ] && [ "$(id -g "$TARGET_USER")" != "$HOST_GID" ]; then
    groupmod -g "$HOST_GID" "$TARGET_USER" 2>/dev/null || true
fi
chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME" 2>/dev/null || true
gosu "$TARGET_USER" chmod 755 "$HOME"

WORK_DIR="${WORKSPACE_DIR:-/workspace}"
STATE_ROOT="$HOME/.cage-state"
XDG_DATA_HOME="$STATE_ROOT/data"
XDG_STATE_HOME="$STATE_ROOT/state"
XDG_CACHE_HOME="$STATE_ROOT/cache"
export XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME OPENCODE_DISABLE_AUTOUPDATE=1

CAGE_STATE_ROOT="$STATE_ROOT" CAGE_TARGET_UID="$(id -u "$TARGET_USER")" \
CAGE_TARGET_GID="$(id -g "$TARGET_USER")" python3 -I - <<'PY'
import os
import stat

root = os.environ["CAGE_STATE_ROOT"]
uid = int(os.environ["CAGE_TARGET_UID"])
gid = int(os.environ["CAGE_TARGET_GID"])
info = os.lstat(root)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit("cage: unsafe OpenCode state root")
for name in ("data", "state", "cache"):
    path = os.path.join(root, name)
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        current = os.lstat(path)
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise SystemExit("cage: unsafe OpenCode XDG state directory: " + name)
    child = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        opened = os.fstat(child)
        if stat.S_IMODE(opened.st_mode) != 0o700:
            if opened.st_uid != 0:
                raise SystemExit(
                    "cage: OpenCode XDG state directory has unsafe permissions: " + name
                )
            os.fchmod(child, 0o700)
        if (opened.st_uid, opened.st_gid) != (uid, gid):
            os.fchown(child, uid, gid)
    finally:
        os.close(child)
opencode_data = os.path.join(root, "data", "opencode")
try:
    current = os.lstat(opencode_data)
except FileNotFoundError:
    os.mkdir(opencode_data, 0o700)
    current = os.lstat(opencode_data)
if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
    raise SystemExit("cage: unsafe OpenCode provider data directory")
child = os.open(opencode_data, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(child)
    if stat.S_IMODE(opened.st_mode) != 0o700:
        if opened.st_uid != 0:
            raise SystemExit("cage: OpenCode provider data directory has unsafe permissions")
        os.fchmod(child, 0o700)
    if (opened.st_uid, opened.st_gid) != (uid, gid):
        os.fchown(child, uid, gid)
finally:
    os.close(child)
PY
chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$STATE_ROOT"
gosu "$TARGET_USER" chmod 700 "$STATE_ROOT"

if [ ! -f /cage-opencode-snapshot/manifest.json ] || \
   [ -L /cage-opencode-snapshot/manifest.json ]; then
    echo "cage: OpenCode configuration snapshot is missing or unsafe" >&2
    exit 1
fi

SNAPSHOT_ROOT=/run/cage-opencode
SOURCE_CONFIG_PARENT="$SNAPSHOT_ROOT/source-config"
SOURCE_PROJECT_DIR="$SNAPSHOT_ROOT/source-project"
FINAL_CONFIG_PARENT="$SNAPSHOT_ROOT/final-config"
FINAL_CONFIG_DIR="$FINAL_CONFIG_PARENT/opencode"
mkdir -p "$SOURCE_CONFIG_PARENT/opencode" "$SOURCE_PROJECT_DIR" \
         "$FINAL_CONFIG_DIR"
chmod 700 "$SNAPSHOT_ROOT" "$SOURCE_CONFIG_PARENT" "$SOURCE_PROJECT_DIR" \
          "$FINAL_CONFIG_PARENT" "$FINAL_CONFIG_DIR"

PRIVATE_ENV_FILE="$SNAPSHOT_ROOT/private-env"
if ! CAGE_OPENCODE_RUNTIME_ENV=/cage-opencode-snapshot/runtime-env.json \
    python3 -I - <<'PY' > "$PRIVATE_ENV_FILE"; then
import json
import os
import re
import stat
import sys

path = os.environ["CAGE_OPENCODE_RUNTIME_ENV"]
try:
    before = os.lstat(path)
except OSError as exc:
    raise SystemExit("cage: private OpenCode environment is unavailable: %s" % exc)
if (
    stat.S_ISLNK(before.st_mode)
    or not stat.S_ISREG(before.st_mode)
    or before.st_nlink != 1
    or before.st_size > 1024 * 1024
    or stat.S_IMODE(before.st_mode) & 0o077
):
    raise SystemExit("cage: private OpenCode environment is unsafe")
descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        raise SystemExit("cage: private OpenCode environment changed while opening")
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65536, 1024 * 1024 + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 1024 * 1024:
            raise SystemExit("cage: private OpenCode environment is oversized")
    after = os.fstat(descriptor)
    current = os.lstat(path)
    if (
        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
    ):
        raise SystemExit("cage: private OpenCode environment changed while reading")
finally:
    os.close(descriptor)

def duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result

try:
    environment = json.loads(
        b"".join(chunks).decode("utf-8"), object_pairs_hook=duplicate_object
    )
except (UnicodeError, ValueError, RecursionError) as exc:
    raise SystemExit("cage: private OpenCode environment is invalid: %s" % exc)
if not isinstance(environment, dict):
    raise SystemExit("cage: private OpenCode environment must be an object")
for name, value in environment.items():
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SystemExit("cage: private OpenCode environment contains an invalid name")
    if not isinstance(value, str) or "\x00" in value:
        raise SystemExit("cage: private OpenCode environment contains an invalid value")
    sys.stdout.buffer.write(name.encode("ascii") + b"=" + value.encode("utf-8") + b"\0")
PY
    echo "cage: could not load the private OpenCode environment" >&2
    exit 1
fi
mapfile -d '' -t PRIVATE_ENV < "$PRIVATE_ENV_FILE"
rm -f "$PRIVATE_ENV_FILE"
for _assignment in "${PRIVATE_ENV[@]}"; do
    export "$_assignment"
done
unset PRIVATE_ENV _assignment

if [ -d /cage-opencode-snapshot/global ]; then
    cp -R --no-dereference /cage-opencode-snapshot/global/. \
        "$SOURCE_CONFIG_PARENT/opencode/"
fi
if [ -d /cage-opencode-snapshot/project-dir ]; then
    cp -R --no-dereference /cage-opencode-snapshot/project-dir/. \
        "$SOURCE_PROJECT_DIR/"
fi

RESOLVE_ENV=(
    "XDG_CONFIG_HOME=$SOURCE_CONFIG_PARENT"
    "XDG_DATA_HOME=$XDG_DATA_HOME"
    "XDG_STATE_HOME=$XDG_STATE_HOME"
    "XDG_CACHE_HOME=$XDG_CACHE_HOME"
    "OPENCODE_DISABLE_AUTOUPDATE=1"
    "OPENCODE_DISABLE_PROJECT_CONFIG=1"
    "OPENCODE_DISABLE_EXTERNAL_SKILLS=1"
    "OPENCODE_DISABLE_CLAUDE_CODE=1"
    "OPENCODE_DISABLE_MODELS_FETCH=1"
    "OPENCODE_DISABLE_LSP_DOWNLOAD=1"
)
if [ -d /cage-opencode-snapshot/project-dir ]; then
    RESOLVE_ENV+=("OPENCODE_CONFIG_DIR=$SOURCE_PROJECT_DIR")
fi
for _config_name in opencode.jsonc opencode.json; do
    if [ -f "/cage-opencode-snapshot/$_config_name" ]; then
        _source_project_config="$SNAPSHOT_ROOT/source-$_config_name"
        cp "/cage-opencode-snapshot/$_config_name" "$_source_project_config"
        RESOLVE_ENV+=("OPENCODE_CONFIG=$_source_project_config")
        break
    fi
done
chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$SNAPSHOT_ROOT"

RESOLVE_ARGS=(debug config)
if [ "${CAGE_OPENCODE_PLUGINS:-0}" != "1" ]; then
    RESOLVE_ARGS=(--pure "${RESOLVE_ARGS[@]}")
fi
if ! cd "$WORK_DIR" || ! gosu "$TARGET_USER" env "${RESOLVE_ENV[@]}" \
    timeout 30s opencode "${RESOLVE_ARGS[@]}" > "$SNAPSHOT_ROOT/resolved.json"; then
    echo "cage: OpenCode could not resolve the frozen configuration inputs" >&2
    exit 1
fi

CAGE_OPENCODE_RESOLVED="$SNAPSHOT_ROOT/resolved.json" \
CAGE_OPENCODE_MANIFEST=/cage-opencode-snapshot/manifest.json \
CAGE_OPENCODE_FINAL="$SNAPSHOT_ROOT/opencode.json" \
CAGE_OPENCODE_PLUGINS="${CAGE_OPENCODE_PLUGINS:-0}" python3 -I - <<'PY'
import json
import os
import re
import stat

def duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result

def read(path, label):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("cage: unsafe " + label)
    if info.st_size > 4 * 1024 * 1024:
        raise SystemExit("cage: oversized " + label)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=duplicate_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit("cage: invalid %s: %s" % (label, exc))
    if not isinstance(value, dict):
        raise SystemExit("cage: %s must contain a JSON object" % label)
    return value

def expand(value, missing):
    if not isinstance(value, str):
        return value
    def replace(match):
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        missing.add(name)
        return ""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replace, value)

config = read(os.environ["CAGE_OPENCODE_RESOLVED"], "resolved OpenCode configuration")
manifest = read(os.environ["CAGE_OPENCODE_MANIFEST"], "OpenCode snapshot manifest")
if manifest.get("schema") != 1 or not isinstance(manifest.get("selected_mcp"), dict):
    raise SystemExit("cage: unsupported OpenCode snapshot manifest")
selected = manifest["selected_mcp"]
stdio = selected.get("stdio")
remote = selected.get("remote")
if not isinstance(stdio, list) or not isinstance(remote, list):
    raise SystemExit("cage: invalid selected OpenCode MCP manifest")
if config.get("instructions"):
    raise SystemExit(
        "cage: OpenCode config instructions cannot be frozen safely; use a project AGENTS.md"
    )
skills = config.get("skills")
if isinstance(skills, dict) and (skills.get("paths") or skills.get("urls")):
    raise SystemExit(
        "cage: OpenCode configured skill paths or URLs cannot be frozen safely; use Cage skill packs"
    )
inherited = config.get("mcp", {})
if inherited is None:
    inherited = {}
if not isinstance(inherited, dict):
    raise SystemExit("cage: resolved OpenCode mcp configuration must be an object")

generated = {}
for server in stdio:
    if not isinstance(server, dict) or not isinstance(server.get("name"), str):
        raise SystemExit("cage: invalid selected local OpenCode MCP server")
    name = server["name"]
    if name in generated:
        raise SystemExit("cage: duplicate selected OpenCode MCP server %r" % name)
    generated[name] = {
        "type": "local",
        "command": ["mcp-relay", name],
        "enabled": True,
    }
for server in remote:
    if not isinstance(server, dict) or not isinstance(server.get("name"), str):
        raise SystemExit("cage: invalid selected remote OpenCode MCP server")
    name = server["name"]
    if name in generated:
        raise SystemExit("cage: duplicate selected OpenCode MCP server %r" % name)
    missing = set()
    headers = {}
    raw_headers = server.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise SystemExit("cage: invalid headers for OpenCode MCP server %r" % name)
    for header_name, header_value in raw_headers.items():
        if not isinstance(header_name, str) or not isinstance(header_value, str):
            raise SystemExit("cage: invalid header for OpenCode MCP server %r" % name)
        headers[header_name] = expand(header_value, missing)
    bearer = server.get("bearer_token_env_var")
    if bearer:
        token = os.environ.get(str(bearer))
        if not token:
            missing.add(str(bearer))
        else:
            headers["Authorization"] = "Bearer " + token
    if missing:
        raise SystemExit(
            "cage: OpenCode MCP server %r requires unset env var(s): %s"
            % (name, ", ".join(sorted(missing)))
        )
    item = {
        "type": "remote",
        "url": server.get("url"),
        "enabled": True,
    }
    if headers:
        item["headers"] = headers
    if server.get("auth") == "oauth":
        client_id = server.get("oauth_client_id")
        client_env = server.get("oauth_client_id_env_var")
        if client_env:
            client_id = os.environ.get(str(client_env))
            if not client_id:
                raise SystemExit(
                    "cage: OpenCode OAuth MCP server %r requires env var %s"
                    % (name, client_env)
                )
        oauth = {}
        if client_id:
            oauth["clientId"] = client_id
        scopes = server.get("oauth_scopes") or []
        if scopes:
            oauth["scope"] = " ".join(str(scope) for scope in scopes)
        item["oauth"] = oauth
    else:
        item["oauth"] = False
    generated[name] = item

config["mcp"] = generated
if os.environ.get("CAGE_OPENCODE_PLUGINS") != "1":
    config.pop("plugin", None)
    config.pop("plugins", None)

encoded = (json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
destination = os.environ["CAGE_OPENCODE_FINAL"]
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
try:
    with os.fdopen(fd, "wb", closefd=False) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
finally:
    os.close(fd)
PY

if [ "${CAGE_OPENCODE_PLUGINS:-0}" = "1" ]; then
    for _plugin_source in \
        "$SOURCE_CONFIG_PARENT/opencode/plugins" \
        "$SOURCE_PROJECT_DIR/plugins"; do
        [ -d "$_plugin_source" ] || continue
        mkdir -p "$FINAL_CONFIG_DIR/plugins"
        for _plugin in "$_plugin_source"/*; do
            [ -e "$_plugin" ] || continue
            _destination="$FINAL_CONFIG_DIR/plugins/${_plugin##*/}"
            if [ -e "$_destination" ]; then
                echo "cage: duplicate global/project OpenCode plugin: ${_plugin##*/}" >&2
                exit 1
            fi
            cp -R --no-dereference "$_plugin" "$_destination"
        done
    done
fi

cat > "$FINAL_CONFIG_DIR/AGENTS.md" <<'EOF'
# Container Environment (Cage)

You are running inside a Docker container managed by Cage. The repository and
explicit read-write mounts can modify host files, selected bridges can execute
with host authority, and provider/MCP authentication may be reconciled to the
selected host OpenCode data directory after exit. Python, Node.js, npm, Git,
GitHub CLI, and passwordless sudo are available.
EOF

if [ -d /cage-opencode-snapshot/project-instruction ]; then
    for _instruction in /cage-opencode-snapshot/project-instruction/*; do
        [ -f "$_instruction" ] || continue
        printf '\n# Frozen project instructions (%s)\n\n' "${_instruction##*/}" \
            >> "$FINAL_CONFIG_DIR/AGENTS.md"
        cat "$_instruction" >> "$FINAL_CONFIG_DIR/AGENTS.md"
        printf '\n' >> "$FINAL_CONFIG_DIR/AGENTS.md"
        break
    done
fi

mkdir -p "$FINAL_CONFIG_DIR/skills"
for _skill_root in \
    "$SOURCE_PROJECT_DIR/skill" \
    "$SOURCE_PROJECT_DIR/skills" \
    /cage-opencode-snapshot/project-agents-skills \
    /cage-opencode-snapshot/project-claude-skills \
    /cage-opencode-snapshot/selected-skills \
    /cage-opencode-snapshot/fallback-skills; do
    [ -d "$_skill_root" ] || continue
    for _skill in "$_skill_root"/*; do
        [ -d "$_skill" ] || continue
        if [ ! -f "$_skill/SKILL.md" ] || [ -L "$_skill/SKILL.md" ]; then
            echo "cage: frozen OpenCode skill is missing a regular SKILL.md: $_skill" >&2
            exit 1
        fi
        _destination="$FINAL_CONFIG_DIR/skills/${_skill##*/}"
        if [ -e "$_destination" ]; then
            echo "cage: duplicate frozen OpenCode skill directory: ${_skill##*/}" >&2
            exit 1
        fi
        cp -R --no-dereference "$_skill" "$_destination"
    done
done

gosu "$TARGET_USER" git config --global --add safe.directory "$WORK_DIR"
[ -n "${GIT_USER_NAME:-}" ] && gosu "$TARGET_USER" git config --global user.name "$GIT_USER_NAME"
[ -n "${GIT_USER_EMAIL:-}" ] && gosu "$TARGET_USER" git config --global user.email "$GIT_USER_EMAIL"

if [ -d "$HOME/.ssh" ]; then
    gosu "$TARGET_USER" chmod 700 "$HOME/.ssh"
    if [ -n "${SSH_HOST:-}" ]; then
        _alias_name="${SSH_HOST%%=*}"
        _real_host="${SSH_HOST#*=}"
        printf 'Host %s\n    Hostname %s\n' "$_alias_name" "$_real_host" > "$HOME/.ssh/config"
        chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$HOME/.ssh/config"
        gosu "$TARGET_USER" chmod 600 "$HOME/.ssh/config"
    fi
fi
if [ -d /host-gh ]; then
    mkdir -p "$HOME/.config/gh"
    cp -R --no-dereference /host-gh/. "$HOME/.config/gh/"
fi
if [ -n "${CAGE_HOST_COMMANDS:-}" ]; then
    for _name in $CAGE_HOST_COMMANDS; do
        _shim="/usr/local/bin/$_name"
        printf '#!/bin/sh\nexec host-cmd-relay %s "$@"\n' "$_name" > "$_shim"
        chmod 755 "$_shim"
    done
fi

chown -hR "$TARGET_USER":"$(id -gn "$TARGET_USER")" \
    "$HOME/.config" "$SNAPSHOT_ROOT" 2>/dev/null || true

if [ -n "${CAGE_OPENCODE_CALLBACK_PORTS:-}" ]; then
    CALLBACK_READY="$SNAPSHOT_ROOT/callback-ready"
    CAGE_OPENCODE_CALLBACK_PORTS="$CAGE_OPENCODE_CALLBACK_PORTS" \
    CAGE_OPENCODE_CALLBACK_READY="$CALLBACK_READY" python3 -I - <<'PY' &
import os
import socket
import threading

ports = [int(value) for value in os.environ["CAGE_OPENCODE_CALLBACK_PORTS"].split()]
container_ip = socket.gethostbyname(socket.gethostname())
listeners = []
for port in ports:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((container_ip, port))
    listener.listen(8)
    listeners.append(listener)

ready = os.open(
    os.environ["CAGE_OPENCODE_CALLBACK_READY"],
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
os.close(ready)

def relay(listener):
    while True:
        client, _ = listener.accept()
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError:
            client.close()
            continue
        def pump(source, destination):
            try:
                while True:
                    data = source.recv(65536)
                    if not data: break
                    destination.sendall(data)
            finally:
                try: destination.shutdown(socket.SHUT_WR)
                except OSError: pass
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
for listener in listeners:
    threading.Thread(target=relay, args=(listener,), daemon=True).start()
threading.Event().wait()
PY
    CALLBACK_RELAY_PID=$!
    for _attempt in $(seq 1 50); do
        [ -f "$CALLBACK_READY" ] && break
        if ! kill -0 "$CALLBACK_RELAY_PID" 2>/dev/null; then
            wait "$CALLBACK_RELAY_PID" || true
            echo "cage: OpenCode callback relay failed to start" >&2
            exit 1
        fi
        sleep 0.1
    done
    if [ ! -f "$CALLBACK_READY" ]; then
        kill "$CALLBACK_RELAY_PID" 2>/dev/null || true
        wait "$CALLBACK_RELAY_PID" 2>/dev/null || true
        echo "cage: timed out starting the OpenCode callback relay" >&2
        exit 1
    fi
fi

FINAL_ENV=(
    "XDG_CONFIG_HOME=$FINAL_CONFIG_PARENT"
    "XDG_DATA_HOME=$XDG_DATA_HOME"
    "XDG_STATE_HOME=$XDG_STATE_HOME"
    "XDG_CACHE_HOME=$XDG_CACHE_HOME"
    "OPENCODE_CONFIG=$SNAPSHOT_ROOT/opencode.json"
    "OPENCODE_CONFIG_DIR=$FINAL_CONFIG_DIR"
    "OPENCODE_DISABLE_AUTOUPDATE=1"
    "OPENCODE_DISABLE_PROJECT_CONFIG=1"
    "OPENCODE_DISABLE_EXTERNAL_SKILLS=1"
    "OPENCODE_DISABLE_CLAUDE_CODE=1"
)
FINAL_DEBUG_ARGS=(debug config)
if [ "${CAGE_OPENCODE_PLUGINS:-0}" != "1" ]; then
    FINAL_DEBUG_ARGS=(--pure "${FINAL_DEBUG_ARGS[@]}")
fi
if ! cd "$WORK_DIR" || ! gosu "$TARGET_USER" env "${FINAL_ENV[@]}" \
    OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    timeout 30s opencode "${FINAL_DEBUG_ARGS[@]}" > "$SNAPSHOT_ROOT/final-resolved.json"; then
    echo "cage: OpenCode could not verify the final frozen configuration" >&2
    exit 1
fi
CAGE_FINAL="$SNAPSHOT_ROOT/final-resolved.json" \
CAGE_MANIFEST=/cage-opencode-snapshot/manifest.json python3 -I - <<'PY'
import json, os
with open(os.environ["CAGE_FINAL"], encoding="utf-8") as handle:
    final = json.load(handle)
with open(os.environ["CAGE_MANIFEST"], encoding="utf-8") as handle:
    manifest = json.load(handle)
selected = manifest["selected_mcp"]
expected = {
    str(item["name"]): {
        "type": "local",
        "command": ["mcp-relay", str(item["name"])],
    }
    for item in selected["stdio"]
}
expected.update(
    {
        str(item["name"]): {"type": "remote", "url": str(item["url"])}
        for item in selected["remote"]
    }
)
actual = final.get("mcp") or {}
if not isinstance(actual, dict) or set(actual) != set(expected):
    raise SystemExit(
        "cage: final OpenCode MCP inventory differs from the selected Cage set"
    )
for name, transport in expected.items():
    value = actual[name]
    if not isinstance(value, dict) or value.get("enabled", True) is False:
        raise SystemExit("cage: final OpenCode MCP transport mismatch for %r" % name)
    for key, expected_value in transport.items():
        if value.get(key) != expected_value:
            raise SystemExit("cage: final OpenCode MCP transport mismatch for %r" % name)
PY

FINAL_SKILL_ARGS=(debug skill)
if [ "${CAGE_OPENCODE_PLUGINS:-0}" != "1" ]; then
    FINAL_SKILL_ARGS=(--pure "${FINAL_SKILL_ARGS[@]}")
fi
if ! cd "$WORK_DIR" || ! gosu "$TARGET_USER" env "${FINAL_ENV[@]}" \
    OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    timeout 30s opencode "${FINAL_SKILL_ARGS[@]}" > "$SNAPSHOT_ROOT/final-skills.json"; then
    echo "cage: OpenCode could not inventory the frozen skill set" >&2
    exit 1
fi
CAGE_SKILLS="$SNAPSHOT_ROOT/final-skills.json" \
CAGE_SKILL_ROOT="$FINAL_CONFIG_DIR/skills" \
CAGE_MANIFEST=/cage-opencode-snapshot/manifest.json python3 -I - <<'PY'
import json
import os

with open(os.environ["CAGE_SKILLS"], encoding="utf-8") as handle:
    skills = json.load(handle)
with open(os.environ["CAGE_MANIFEST"], encoding="utf-8") as handle:
    manifest = json.load(handle)
if not isinstance(skills, list):
    raise SystemExit("cage: OpenCode returned an invalid final skill inventory")
root = os.path.realpath(os.environ["CAGE_SKILL_ROOT"])
locations = []
for item in skills:
    if not isinstance(item, dict) or not isinstance(item.get("location"), str):
        raise SystemExit("cage: OpenCode returned an invalid final skill entry")
    location = item["location"]
    if location in {"<built-in>", "builtin://customize-opencode"}:
        continue
    resolved = os.path.realpath(location)
    if os.path.commonpath((root, resolved)) != root:
        raise SystemExit("cage: OpenCode discovered a skill outside the frozen snapshot")
    locations.append(resolved)
for name in manifest.get("selected_skill_names", []):
    expected = os.path.join(root, str(name)) + os.sep
    if not any(location.startswith(expected) for location in locations):
        raise SystemExit("cage: selected OpenCode skill was not discovered: %r" % name)
PY

EXEC_ARGS=()
if [ "${CAGE_OPENCODE_PLUGINS:-0}" != "1" ]; then
    EXEC_ARGS+=(--pure)
fi
cd "$WORK_DIR"
exec gosu "$TARGET_USER" env "${FINAL_ENV[@]}" opencode "${EXEC_ARGS[@]}" "$@"
