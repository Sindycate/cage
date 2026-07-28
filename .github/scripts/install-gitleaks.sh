#!/bin/bash
set -euo pipefail

GITLEAKS_VERSION="8.30.1"
DESTINATION="${1:?usage: install-gitleaks.sh DESTINATION}"

case "$(uname -s):$(uname -m)" in
    Darwin:arm64)
        PLATFORM="darwin_arm64"
        ARCHIVE_SHA256="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
        ;;
    Darwin:x86_64)
        PLATFORM="darwin_x64"
        ARCHIVE_SHA256="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
        ;;
    Linux:aarch64|Linux:arm64)
        PLATFORM="linux_arm64"
        ARCHIVE_SHA256="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
        ;;
    Linux:x86_64)
        PLATFORM="linux_x64"
        ARCHIVE_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
        ;;
    *)
        echo "unsupported Gitleaks platform: $(uname -s) $(uname -m)" >&2
        exit 2
        ;;
esac

ARCHIVE="gitleaks_${GITLEAKS_VERSION}_${PLATFORM}.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${ARCHIVE}"
TEMPORARY="$(mktemp -d "${TMPDIR:-/tmp}/cage-gitleaks.XXXXXX")"
trap 'rm -rf "$TEMPORARY"' EXIT

mkdir -p "$DESTINATION"
curl --fail --silent --show-error --location "$URL" --output "$TEMPORARY/$ARCHIVE"
if command -v shasum >/dev/null 2>&1; then
    printf '%s  %s\n' "$ARCHIVE_SHA256" "$TEMPORARY/$ARCHIVE" | shasum -a 256 --check
elif command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$ARCHIVE_SHA256" "$TEMPORARY/$ARCHIVE" | sha256sum --check
else
    echo "SHA-256 verification requires shasum or sha256sum" >&2
    exit 2
fi

tar -xzf "$TEMPORARY/$ARCHIVE" -C "$DESTINATION" gitleaks
chmod 0755 "$DESTINATION/gitleaks"
test "$("$DESTINATION/gitleaks" version)" = "$GITLEAKS_VERSION"
