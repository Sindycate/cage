#!/bin/bash
set -euo pipefail

# cage installer — works as both `curl | bash` and local `./install.sh`
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Sindycate/cage/main/install.sh | bash
#   ./install.sh
#   ./install.sh --from-source
#   ./install.sh --uninstall

REPO="Sindycate/cage"
INSTALL_DIR="${CAGE_INSTALL_DIR:-$HOME/.local/share/cage}"
BIN_DIR="${CAGE_BIN_DIR:-$HOME/.local/bin}"
INSTALL_MARKER=".cage-install"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
FROM_SOURCE=0

# --- Helpers ---

info()  { echo "  $*"; }
error() { echo "ERROR: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || error "python3 3.11+ is required."
python3 -I -c 'import tomllib' 2>/dev/null || error "python3 must be 3.11+ with tomllib support."

# Validate the exact user input before command substitution can strip trailing
# newlines or realpath can hide a symlink at the selected directory itself.
if [[ "$INSTALL_DIR" == *$'\n'* || "$BIN_DIR" == *$'\n'* ]]; then
    error "Install paths must not contain newlines."
fi

canonical_path() {
    python3 -I -c 'import os, sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$1"
}

lexical_path() {
    python3 -I -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}

INSTALL_INPUT="$(lexical_path "$INSTALL_DIR")"
BIN_INPUT="$(lexical_path "$BIN_DIR")"
[ ! -L "$INSTALL_INPUT" ] || error "Refusing install-directory symlink: $INSTALL_INPUT"

HOME_DIR="$(canonical_path "$HOME")"
INSTALL_DIR="$(canonical_path "$INSTALL_INPUT")"
BIN_DIR="$(canonical_path "$BIN_INPUT")"

validate_install_paths() {
    if [ -z "$INSTALL_DIR" ] || [ "$INSTALL_DIR" = "/" ] || [ "$INSTALL_DIR" = "$HOME_DIR" ]; then
        error "Refusing unsafe install directory: $INSTALL_DIR"
    fi
    case "$HOME_DIR/" in
        "$INSTALL_DIR/"*) error "Refusing install directory that contains the home directory: $INSTALL_DIR" ;;
    esac
    if [ -z "$BIN_DIR" ] || [ "$BIN_DIR" = "/" ] || [ "$BIN_DIR" = "$HOME_DIR" ]; then
        error "Refusing unsafe binary directory: $BIN_DIR"
    fi
}

is_managed_install() {
    [ -f "$INSTALL_DIR/$INSTALL_MARKER" ] && [ ! -L "$INSTALL_DIR/$INSTALL_MARKER" ] && return 0

    # Compatibility for installations created before the marker existed. Only
    # recognize the historical default path automatically.
    [ "$INSTALL_DIR" = "$HOME_DIR/.local/share/cage" ] && \
        [ -f "$INSTALL_DIR/cage" ] && [ ! -L "$INSTALL_DIR/cage" ] && \
        [ -f "$INSTALL_DIR/cage-config.py" ] && [ ! -L "$INSTALL_DIR/cage-config.py" ]
}

is_managed_launcher() {
    local launcher="$BIN_DIR/cage"
    [ -L "$launcher" ] || return 1
    [ "$(canonical_path "$launcher")" = "$INSTALL_DIR/cage" ]
}

validate_launcher_path() {
    local launcher="$BIN_DIR/cage"
    if [ -e "$launcher" ] || [ -L "$launcher" ]; then
        is_managed_launcher || error "Refusing to replace or remove unrelated launcher: $launcher"
    fi
}

validate_install_paths

# --- Uninstall ---

if [ "${1:-}" = "--uninstall" ]; then
    echo "Uninstalling cage..."
    validate_launcher_path
    if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
        [ ! -L "$INSTALL_DIR" ] || error "Refusing to uninstall through symlink: $INSTALL_DIR"
        is_managed_install || error "Refusing to remove unrecognized install directory: $INSTALL_DIR"
    fi
    [ -L "$BIN_DIR/cage" ] && rm -f "$BIN_DIR/cage"
    rm -rf "$INSTALL_DIR"
    info "Removed $BIN_DIR/cage and $INSTALL_DIR"
    info "Config at ~/.config/cage/ preserved."
    exit 0
fi

if [ "${1:-}" = "--from-source" ]; then
    FROM_SOURCE=1
    shift
fi
[ "$#" -eq 0 ] || error "Unknown installer argument: $1"

# --- Prerequisites ---

echo "Installing cage..."
echo ""

REQUIRED_COMMANDS=(docker)
[ "$FROM_SOURCE" -eq 1 ] || REQUIRED_COMMANDS+=(curl)
for cmd in "${REQUIRED_COMMANDS[@]}"; do
    if ! command -v "$cmd" &>/dev/null; then
        error "$cmd is required but not found. Please install it first."
    fi
done

# --- Determine version ---

if [ "$FROM_SOURCE" -eq 1 ]; then
    [ -f "$SOURCE_DIR/cage" ] && [ ! -L "$SOURCE_DIR/cage" ] || \
        error "--from-source must run from a Cage source checkout"
    VERSION="$("$SOURCE_DIR/cage" --version | awk '{print $NF}')"
    info "Using source checkout version: $VERSION"
elif [ -n "${CAGE_VERSION:-}" ]; then
    VERSION="$CAGE_VERSION"
    info "Using pinned version: $VERSION"
else
    info "Fetching latest release..."
    _github_token=""
    if [ -n "${GH_TOKEN:-}" ]; then
        _github_token="$GH_TOKEN"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        _github_token="$GITHUB_TOKEN"
    elif command -v gh &>/dev/null; then
        _token="$(gh auth token 2>/dev/null)" || true
        [ -n "${_token:-}" ] && _github_token="$_token"
    fi

    fetch_latest_release() {
        local url="https://api.github.com/repos/${REPO}/releases/latest"
        if [ -n "$_github_token" ]; then
            curl -fsSL -H "Authorization: token $_github_token" "$url"
        else
            curl -fsSL "$url"
        fi
    }
    VERSION=$(fetch_latest_release \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4 | sed 's/^v//')
    unset _github_token _token
    if [ -z "$VERSION" ]; then
        error "Could not determine latest version. Set CAGE_VERSION to install a specific version."
    fi
    info "Latest version: $VERSION"
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z][0-9A-Za-z.-]*)?$ ]]; then
    error "Invalid release version: $VERSION"
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# --- Download and verify ---

if [ "$FROM_SOURCE" -eq 0 ]; then
    TARBALL="cage-${VERSION}.tar.gz"
    DOWNLOAD_URL="https://github.com/${REPO}/releases/download/v${VERSION}/${TARBALL}"
    CHECKSUM_URL="${DOWNLOAD_URL}.sha256"

    info "Downloading cage ${VERSION}..."
    curl -fsSL "$DOWNLOAD_URL" -o "$TMPDIR/$TARBALL"
    curl -fsSL "$CHECKSUM_URL" -o "$TMPDIR/${TARBALL}.sha256"

    info "Verifying checksum..."
    cd "$TMPDIR"
    if command -v shasum &>/dev/null; then
        shasum -a 256 -c "${TARBALL}.sha256" >/dev/null
    elif command -v sha256sum &>/dev/null; then
        sha256sum -c "${TARBALL}.sha256" >/dev/null
    else
        python3 -I -c '
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
checksum_file = pathlib.Path(sys.argv[2])
expected = checksum_file.read_text(encoding="utf-8").split()[0].lower()
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"checksum mismatch: expected {expected}, got {actual}")
' "$TARBALL" "${TARBALL}.sha256"
    fi
fi

# --- Install ---

info "Installing to $INSTALL_DIR..."
INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
mkdir -p "$INSTALL_PARENT" "$BIN_DIR"
STAGE_DIR="$(mktemp -d "$INSTALL_PARENT/.cage-stage.XXXXXX")"
BACKUP_DIR=""
BACKUP_ROOT=""
NEW_INSTALL_ACTIVE=0
LAUNCHER_PREEXISTED=0
[ -e "$BIN_DIR/cage" ] || [ -L "$BIN_DIR/cage" ] && LAUNCHER_PREEXISTED=1

cleanup_install() {
    status=$?
    if [ -n "${STAGE_DIR:-}" ] && [ -d "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR"
    fi
    if [ "$status" -ne 0 ] && [ "${NEW_INSTALL_ACTIVE:-0}" = "1" ] && \
       [ -e "$INSTALL_DIR" ] && is_managed_install; then
        rm -rf "$INSTALL_DIR"
    fi
    if [ "$status" -ne 0 ] && [ "${LAUNCHER_PREEXISTED:-0}" = "0" ] && \
       [ -L "$BIN_DIR/cage" ] && \
       [ "$(canonical_path "$BIN_DIR/cage")" = "$INSTALL_DIR/cage" ]; then
        rm -f "$BIN_DIR/cage"
    fi
    if [ "$status" -ne 0 ] && [ -n "${BACKUP_DIR:-}" ] && \
       [ -e "$BACKUP_DIR" ] && [ ! -e "$INSTALL_DIR" ]; then
        mv "$BACKUP_DIR" "$INSTALL_DIR" || true
    fi
    if [ -n "${BACKUP_ROOT:-}" ] && [ -d "$BACKUP_ROOT" ]; then
        rmdir "$BACKUP_ROOT" 2>/dev/null || true
    fi
    rm -rf "$TMPDIR"
}
trap cleanup_install EXIT

if [ "$FROM_SOURCE" -eq 1 ]; then
    SOURCE_FILES=(
        cage cage-config.py cage-netgate.sh netgate-proxy.py
        mcp-bridge.py mcp-relay host-cmd-bridge.py host-cmd-relay
        docker-compose.yml Dockerfile Dockerfile.codex
        entrypoint.sh entrypoint-codex.sh install.sh Makefile README.md SECURITY.md
        CHANGELOG.md
    )
    for source_file in "${SOURCE_FILES[@]}"; do
        [ -f "$SOURCE_DIR/$source_file" ] && [ ! -L "$SOURCE_DIR/$source_file" ] || \
            error "Source checkout is missing a safe regular file: $source_file"
        cp "$SOURCE_DIR/$source_file" "$STAGE_DIR/$source_file"
    done
    for source_dir in netgate docs; do
        [ -d "$SOURCE_DIR/$source_dir" ] && [ ! -L "$SOURCE_DIR/$source_dir" ] || \
            error "Source checkout is missing a safe directory: $source_dir"
        cp -R "$SOURCE_DIR/$source_dir" "$STAGE_DIR/$source_dir"
    done
else
    tar xzf "$TARBALL" -C "$STAGE_DIR" --strip-components=1
fi

for required in cage cage-config.py cage-netgate.sh netgate-proxy.py mcp-bridge.py mcp-relay host-cmd-bridge.py host-cmd-relay; do
    [ -f "$STAGE_DIR/$required" ] && [ ! -L "$STAGE_DIR/$required" ] || \
        error "Release archive is missing a safe regular file: $required"
done

printf '%s\n' "$VERSION" > "$STAGE_DIR/$INSTALL_MARKER"
chmod +x "$STAGE_DIR/cage" "$STAGE_DIR/cage-config.py" "$STAGE_DIR/cage-netgate.sh" "$STAGE_DIR/netgate-proxy.py" "$STAGE_DIR/mcp-bridge.py" "$STAGE_DIR/mcp-relay" "$STAGE_DIR/host-cmd-bridge.py" "$STAGE_DIR/host-cmd-relay"

validate_launcher_path
if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
    [ ! -L "$INSTALL_DIR" ] || error "Refusing to replace install symlink: $INSTALL_DIR"
    is_managed_install || error "Refusing to replace unrecognized install directory: $INSTALL_DIR"
    BACKUP_ROOT="$(mktemp -d "$INSTALL_PARENT/.cage-backup.XXXXXX")"
    BACKUP_DIR="$BACKUP_ROOT/install"
    mv "$INSTALL_DIR" "$BACKUP_DIR"
fi

mv "$STAGE_DIR" "$INSTALL_DIR"
STAGE_DIR=""
NEW_INSTALL_ACTIVE=1

validate_launcher_path
ln -sf "$INSTALL_DIR/cage" "$BIN_DIR/cage"

mkdir -p "$HOME/.config/cage"

# --- Verify ---

echo ""
INSTALLED_VERSION="$($INSTALL_DIR/cage --version)"
if [ "$INSTALLED_VERSION" != "cage $VERSION" ]; then
    error "Installed launcher reports unexpected version: $INSTALLED_VERSION"
fi
if command -v cage &>/dev/null; then
    info "Installed: $INSTALLED_VERSION"
else
    info "Installed cage $VERSION to $BIN_DIR/cage"
    echo ""
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        info "WARNING: $BIN_DIR is not in your PATH."
        info "Add this to your shell profile:"
        info "  export PATH=\"$BIN_DIR:\$PATH\""
    fi
fi

# Keep the previous installation until extraction, launcher replacement, and
# version verification have all succeeded. The EXIT trap restores it on error.
if [ -n "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_ROOT"
    BACKUP_DIR=""
    BACKUP_ROOT=""
fi
NEW_INSTALL_ACTIVE=0

echo ""
info "Next steps:"
info "  1. Run: cage setup"
info "  2. Start Docker (e.g., colima start --cpu 4 --memory 8 --disk 100)"
info "  3. Run: cage ~/path/to/repo"
info "  4. Docker images will be pulled automatically on first run."
echo ""
