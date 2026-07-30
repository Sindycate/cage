"""Best-effort Claude project-session reconciliation."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


SYNC_IN_SCRIPT = r'''
DST="/vol/projects/$SLUG"
mkdir -p "$DST"
sync_one() {
    src="$1"; dst="$2"; rel="$3"
    mkdir -p "$(dirname "$dst")"
    if [ ! -e "$dst" ]; then
        cp -p "$src" "$dst"
        return
    fi
    case "$rel" in
        *.jsonl)
            ss=$(stat -c%s "$src"); ds=$(stat -c%s "$dst")
            if [ "$ss" -gt "$ds" ]; then
                cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
            elif [ "$ss" = "$ds" ]; then
                touch -r "$src" "$dst"
            fi
            ;;
        *)
            sm=$(stat -c%Y "$src"); dm=$(stat -c%Y "$dst")
            if [ "$sm" -gt "$dm" ]; then
                cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
            fi
            ;;
    esac
}
if [ -d /src ]; then
    cd /src && find . -type f | while read rel; do
        rel="${rel#./}"
        sync_one "/src/$rel" "$DST/$rel" "$rel"
    done
fi
for old in /vol/projects/-workspace-*; do
    [ -d "$old" ] || continue
    cd "$old" && find . -type f | while read rel; do
        rel="${rel#./}"
        src="$old/$rel"; dst="$DST/$rel"
        mkdir -p "$(dirname "$dst")"
        case "$rel" in
            *.jsonl)
                if [ ! -e "$dst" ]; then
                    sed "s|\"cwd\":\"/workspace/[^\"]*\"|\"cwd\":\"$REPO_PATH\"|g" "$src" > "$dst.tmp" \
                        && mv "$dst.tmp" "$dst" \
                        && touch -r "$src" "$dst"
                else
                    touch -r "$src" "$dst"
                fi
                ;;
            *)
                if [ ! -e "$dst" ]; then
                    cp -p "$src" "$dst"
                else
                    sm=$(stat -c%Y "$src"); dm=$(stat -c%Y "$dst")
                    if [ "$sm" -gt "$dm" ]; then
                        cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
                    fi
                fi
                ;;
        esac
    done
done
chown -R "$HOST_UID:$HOST_GID" "$DST"
'''


SYNC_OUT_SCRIPT = r'''
SRC="/vol/projects/$SLUG"
[ -d "$SRC" ] || exit 0
cd "$SRC" && find . -type f | while read rel; do
    rel="${rel#./}"
    src="$SRC/$rel"; dst="/dst/$rel"
    mkdir -p "$(dirname "$dst")"
    if [ ! -e "$dst" ]; then
        cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
        continue
    fi
    case "$rel" in
        *.jsonl)
            ss=$(stat -c%s "$src"); ds=$(stat -c%s "$dst")
            if [ "$ss" -gt "$ds" ]; then
                cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
            elif [ "$ss" = "$ds" ]; then
                touch -r "$src" "$dst"
            fi
            ;;
        *)
            sm=$(stat -c%Y "$src"); dm=$(stat -c%Y "$dst")
            if [ "$sm" -gt "$dm" ]; then
                cp -p "$src" "$dst.tmp" && mv "$dst.tmp" "$dst"
            fi
            ;;
    esac
done
'''


@dataclass
class ClaudeSessionSync:
    docker: str
    volume_name: str
    repository: str
    host_claude_dir: Path
    enabled: bool

    @property
    def slug(self) -> str:
        return self.repository.replace("/", "-")

    @property
    def host_project_dir(self) -> Path:
        return self.host_claude_dir / "projects" / self.slug

    def sync_in(self) -> None:
        """Migrate legacy volume state and best-effort merge host state."""

        subprocess.run(
            [self.docker, "volume", "create", self.volume_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        command = [
            self.docker,
            "run",
            "--rm",
            "-v",
            f"{self.volume_name}:/vol",
        ]
        if self.enabled and self.host_project_dir.is_dir():
            command.extend(("-v", f"{self.host_project_dir}:/src:ro"))
        command.extend(
            (
                "-e",
                f"SLUG={self.slug}",
                "-e",
                f"REPO_PATH={self.repository}",
                "-e",
                f"HOST_UID={os.getuid()}",
                "-e",
                f"HOST_GID={os.getgid()}",
                "alpine:3",
                "sh",
                "-c",
                SYNC_IN_SCRIPT,
            )
        )
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def sync_out(self) -> int:
        """Best-effort merge volume state back to the host."""

        if not self.enabled:
            return 0
        try:
            self.host_project_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return 0
        subprocess.run(
            [
                self.docker,
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{self.volume_name}:/vol:ro",
                "-v",
                f"{self.host_project_dir}:/dst",
                "-e",
                f"SLUG={self.slug}",
                "alpine:3",
                "sh",
                "-c",
                SYNC_OUT_SCRIPT,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return 0
