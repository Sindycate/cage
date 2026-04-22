#!/bin/bash
set -euo pipefail

# cage installer — works as both `curl | bash` and local `./install.sh`
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Sindycate/cage/main/install.sh | bash
#   ./install.sh
#   ./install.sh --uninstall

REPO="Sindycate/cage"
INSTALL_DIR="${CAGE_INSTALL_DIR:-$HOME/.local/share/cage}"
BIN_DIR="${CAGE_BIN_DIR:-$HOME/.local/bin}"

# --- Helpers ---

info()  { echo "  $*"; }
error() { echo "ERROR: $*" >&2; exit 1; }

# --- Uninstall ---

if [ "${1:-}" = "--uninstall" ]; then
    echo "Uninstalling cage..."
    rm -f "$BIN_DIR/cage"
    rm -rf "$INSTALL_DIR"
    info "Removed $BIN_DIR/cage and $INSTALL_DIR"
    info "Config at ~/.config/cage/ preserved."
    exit 0
fi

# --- Prerequisites ---

echo "Installing cage..."
echo ""

for cmd in docker python3 curl; do
    if ! command -v "$cmd" &>/dev/null; then
        error "$cmd is required but not found. Please install it first."
    fi
done

# --- Determine version ---

if [ -n "${CAGE_VERSION:-}" ]; then
    VERSION="$CAGE_VERSION"
    info "Using pinned version: $VERSION"
else
    info "Fetching latest release..."
    GH_AUTH_HEADER=()
    if [ -n "${GH_TOKEN:-}" ]; then
        GH_AUTH_HEADER=(-H "Authorization: token $GH_TOKEN")
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        GH_AUTH_HEADER=(-H "Authorization: token $GITHUB_TOKEN")
    elif command -v gh &>/dev/null; then
        _token="$(gh auth token 2>/dev/null)" || true
        [ -n "${_token:-}" ] && GH_AUTH_HEADER=(-H "Authorization: token $_token")
    fi
    VERSION=$(curl -fsSL "${GH_AUTH_HEADER[@]}" "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4 | sed 's/^v//')
    if [ -z "$VERSION" ]; then
        error "Could not determine latest version. Set CAGE_VERSION to install a specific version."
    fi
    info "Latest version: $VERSION"
fi

# --- Download and verify ---

TARBALL="cage-${VERSION}.tar.gz"
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/v${VERSION}/${TARBALL}"
CHECKSUM_URL="${DOWNLOAD_URL}.sha256"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

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
    info "Warning: no sha256 tool found, skipping checksum verification"
fi

# --- Install ---

info "Installing to $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

tar xzf "$TARBALL" -C "$INSTALL_DIR" --strip-components=1

chmod +x "$INSTALL_DIR/cage" "$INSTALL_DIR/cage-setup.sh" "$INSTALL_DIR/cage-profiles.sh" "$INSTALL_DIR/cage-netgate.sh" "$INSTALL_DIR/netgate-proxy.py" "$INSTALL_DIR/mcp-bridge.py" "$INSTALL_DIR/mcp-relay"
ln -sf "$INSTALL_DIR/cage" "$BIN_DIR/cage"

mkdir -p "$HOME/.config/cage"

# --- Verify ---

echo ""
if command -v cage &>/dev/null; then
    info "Installed: $(cage --version)"
else
    info "Installed cage $VERSION to $BIN_DIR/cage"
    echo ""
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        info "WARNING: $BIN_DIR is not in your PATH."
        info "Add this to your shell profile:"
        info "  export PATH=\"$BIN_DIR:\$PATH\""
    fi
fi

echo ""
info "Next steps:"
info "  1. Run: cage setup"
info "  2. Start Docker (e.g., colima start --cpu 4 --memory 8 --disk 100)"
info "  3. Run: cage ~/path/to/repo"
info "  4. Docker images will be pulled automatically on first run."
echo ""
