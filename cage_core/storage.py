"""Docker storage inventory, capacity guardrails, and narrow cleanup policy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .models import StoragePolicy


MANAGED_LABEL = "io.cage.managed"
ROLE_LABEL = "io.cage.role"
VERSION_LABEL = "io.cage.version"
GIB = 1024 ** 3
KNOWN_REPOSITORIES = {
    "cage-base": "base",
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "token-monitor": "monitor",
    "cage-token-monitor": "monitor",
    "ghcr.io/sindycate/cage/base": "base",
    "ghcr.io/sindycate/cage/claude-code": "claude",
    "ghcr.io/sindycate/cage/codex": "codex",
    "ghcr.io/sindycate/cage/opencode": "opencode",
    "ghcr.io/sindycate/cage/token-monitor": "monitor",
}
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapacityProbe:
    free_bytes: int | None
    total_bytes: int | None
    source: str
    error: str = ""


@dataclass(frozen=True)
class ImageRecord:
    image_id: str
    tags: tuple[str, ...]
    created: datetime
    size_bytes: int
    labels: dict[str, str]
    terminal_managed: bool = False

    @property
    def managed(self) -> bool:
        return (
            self.labels.get(MANAGED_LABEL) == "true"
            and self.labels.get(ROLE_LABEL) in {"base", "claude", "codex", "opencode", "monitor"}
            and bool(SEMVER_RE.fullmatch(self.labels.get(VERSION_LABEL, "")))
        )


@dataclass(frozen=True)
class CleanupCandidate:
    reference: str
    image_id: str
    reason: str
    size_bytes: int


@dataclass(frozen=True)
class StorageSnapshot:
    capacity: CapacityProbe
    images: tuple[ImageRecord, ...]
    referenced_image_ids: frozenset[str]
    candidates: tuple[CleanupCandidate, ...]
    retained_versions: dict[str, tuple[str, ...]]
    legacy_cage_images: int


def _run(docker: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [docker, *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise StorageError(f"cannot run Docker: {exc}") from exc


def _require(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Docker error"
        raise StorageError(f"{action}: {detail}")
    return result.stdout


def _parse_created(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        return datetime.fromtimestamp(0, timezone.utc)
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d\d:\d\d$)", r"\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _image_ids(docker: str) -> list[str]:
    output = _require(
        _run(docker, ["image", "ls", "-aq", "--no-trunc"]),
        "cannot list Docker images",
    )
    return sorted({line.strip() for line in output.splitlines() if line.strip()})


def _referenced_image_ids(docker: str) -> frozenset[str]:
    output = _require(
        _run(docker, ["container", "ls", "-aq", "--no-trunc"]),
        "cannot list Docker containers",
    )
    container_ids = [line.strip() for line in output.splitlines() if line.strip()]
    if not container_ids:
        return frozenset()
    inspected = _require(
        _run(docker, ["container", "inspect", *container_ids]),
        "cannot inspect Docker containers",
    )
    try:
        records = json.loads(inspected)
    except json.JSONDecodeError as exc:
        raise StorageError(f"Docker returned invalid container inventory: {exc}") from exc
    references: set[str] = set()
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and isinstance(record.get("Image"), str):
            references.add(record["Image"])
    return frozenset(references)


def _tag_parts(tag: str) -> tuple[str, str, str] | None:
    if tag in {"<none>:<none>", ""} or "@" in tag:
        return None
    slash = tag.rfind("/")
    colon = tag.rfind(":")
    if colon <= slash:
        return None
    repository, version = tag[:colon], tag[colon + 1 :]
    role = KNOWN_REPOSITORIES.get(repository, "")
    return (repository, version, role) if role else None


def _terminal_managed_history(docker: str, image_id: str) -> bool:
    result = _run(
        docker,
        ["image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image_id],
    )
    if result.returncode != 0:
        return False
    first = next((line for line in result.stdout.splitlines() if line.strip()), "")
    return (
        "LABEL" in first.upper()
        and MANAGED_LABEL in first
        and ROLE_LABEL in first
        and VERSION_LABEL in first
    )


def inventory_images(docker: str) -> tuple[ImageRecord, ...]:
    ids = _image_ids(docker)
    if not ids:
        return ()
    output = _require(
        _run(docker, ["image", "inspect", *ids]),
        "cannot inspect Docker images",
    )
    try:
        inspected = json.loads(output)
    except json.JSONDecodeError as exc:
        raise StorageError(f"Docker returned invalid image inventory: {exc}") from exc
    if not isinstance(inspected, list):
        raise StorageError("Docker image inventory was not a list")
    records: list[ImageRecord] = []
    for item in inspected:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), str):
            raise StorageError("Docker image inventory contained an invalid entry")
        raw_tags = item.get("RepoTags") or []
        tags = tuple(
            sorted(
                tag for tag in raw_tags
                if isinstance(tag, str) and tag != "<none>:<none>"
            )
        )
        config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
        raw_labels = config.get("Labels") if isinstance(config, dict) else {}
        labels = {
            str(name): str(value)
            for name, value in (raw_labels or {}).items()
            if isinstance(name, str) and isinstance(value, str)
        } if isinstance(raw_labels, dict) else {}
        managed = labels.get(MANAGED_LABEL) == "true"
        records.append(
            ImageRecord(
                image_id=item["Id"],
                tags=tags,
                created=_parse_created(item.get("Created")),
                size_bytes=int(item.get("Size") or 0),
                labels=labels,
                terminal_managed=(
                    managed and _terminal_managed_history(docker, item["Id"])
                ),
            )
        )
    return tuple(records)


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.fullmatch(value)
    return tuple(int(part) for part in match.groups()) if match else None


def cleanup_candidates(
    images: Iterable[ImageRecord],
    referenced: frozenset[str],
    policy: StoragePolicy,
    *,
    now: datetime | None = None,
) -> tuple[tuple[CleanupCandidate, ...], dict[str, tuple[str, ...]], int]:
    current_time = now or datetime.now(timezone.utc)
    versions: dict[str, set[str]] = {
        "base": set(),
        "claude": set(),
        "codex": set(),
        "opencode": set(),
        "monitor": set(),
    }
    legacy_ids: set[str] = set()
    for image in images:
        for tag in image.tags:
            parts = _tag_parts(tag)
            if not parts:
                continue
            _, version, role = parts
            if not image.managed:
                legacy_ids.add(image.image_id)
                continue
            if (
                image.terminal_managed
                and image.labels.get(ROLE_LABEL) == role
                and image.labels.get(VERSION_LABEL) == version
            ):
                if _semantic_version(version) is not None:
                    versions[role].add(version)
    retained: dict[str, tuple[str, ...]] = {}
    for role, available in versions.items():
        retained[role] = tuple(
            sorted(available, key=lambda item: _semantic_version(item) or (0, 0, 0), reverse=True)[
                : policy.keep_versions
            ]
        )

    candidates: list[CleanupCandidate] = []
    for image in images:
        if image.image_id in referenced:
            continue
        for tag in image.tags:
            parts = _tag_parts(tag)
            if not parts:
                continue
            _, version, role = parts
            if (
                image.managed
                and image.terminal_managed
                and image.labels.get(ROLE_LABEL) == role
                and image.labels.get(VERSION_LABEL) == version
                and _semantic_version(version) is not None
                and version not in retained[role]
            ):
                candidates.append(
                    CleanupCandidate(
                        reference=tag,
                        image_id=image.image_id,
                        reason=f"managed {role} version {version} is outside the newest {policy.keep_versions}",
                        size_bytes=image.size_bytes,
                    )
                )
        age_hours = (current_time - image.created).total_seconds() / 3600
        if (
            not image.tags
            and image.managed
            and image.terminal_managed
            and age_hours >= policy.dangling_min_age_hours
        ):
            candidates.append(
                CleanupCandidate(
                    reference=image.image_id,
                    image_id=image.image_id,
                    reason=f"terminal Cage build is dangling and at least {policy.dangling_min_age_hours}h old",
                    size_bytes=image.size_bytes,
                )
            )
    return tuple(sorted(candidates, key=lambda item: (item.reason, item.reference))), retained, len(legacy_ids)


def probe_capacity(
    docker: str,
    images: Iterable[ImageRecord] = (),
    *,
    preferred_image: str = "",
) -> CapacityProbe:
    info = _run(docker, ["info", "--format", "{{json .DockerRootDir}}"])
    errors: list[str] = []
    if info.returncode == 0:
        try:
            docker_root = json.loads(info.stdout)
        except json.JSONDecodeError:
            docker_root = ""
        if isinstance(docker_root, str) and docker_root:
            try:
                stats = os.statvfs(docker_root)
                return CapacityProbe(
                    free_bytes=stats.f_bavail * stats.f_frsize,
                    total_bytes=stats.f_blocks * stats.f_frsize,
                    source=f"host filesystem {docker_root}",
                )
            except OSError as exc:
                errors.append(str(exc))
    else:
        errors.append(info.stderr.strip())

    probes: list[str] = []
    if preferred_image:
        probes.append(preferred_image)
    for image in images:
        if image.managed or any(_tag_parts(tag) for tag in image.tags):
            probes.extend(image.tags[:1] or (image.image_id,))
    for image in dict.fromkeys(probes):
        result = _run(
            docker,
            [
                "run", "--rm", "--network", "none", "--read-only",
                "--entrypoint", "df", image, "-Pk", "/",
            ],
        )
        if result.returncode != 0:
            errors.append(result.stderr.strip())
            continue
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2 and len(lines[-1]) >= 4:
            try:
                return CapacityProbe(
                    free_bytes=int(lines[-1][3]) * 1024,
                    total_bytes=int(lines[-1][1]) * 1024,
                    source=f"Docker overlay via {image}",
                )
            except ValueError:
                errors.append(f"invalid df output from {image}")
    detail = next((item for item in errors if item), "no managed probe image is available")
    return CapacityProbe(None, None, "unavailable", detail)


def snapshot(
    docker: str,
    policy: StoragePolicy,
    *,
    preferred_image: str = "",
    now: datetime | None = None,
) -> StorageSnapshot:
    images = inventory_images(docker)
    referenced = _referenced_image_ids(docker)
    candidates, retained, legacy = cleanup_candidates(
        images, referenced, policy, now=now
    )
    return StorageSnapshot(
        capacity=probe_capacity(docker, images, preferred_image=preferred_image),
        images=images,
        referenced_image_ids=referenced,
        candidates=candidates,
        retained_versions=retained,
        legacy_cage_images=legacy,
    )


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def print_status(state: StorageSnapshot, policy: StoragePolicy) -> None:
    if state.capacity.free_bytes is None:
        print(f"Docker free space: unavailable ({state.capacity.error})")
    else:
        print(
            f"Docker free space: {_gib(state.capacity.free_bytes)} of "
            f"{_gib(state.capacity.total_bytes or 0)} ({state.capacity.source})"
        )
    print(
        "Policy: warning < "
        f"{policy.warn_free_gib} GiB; critical < {policy.critical_free_gib} GiB; "
        f"build floor {policy.min_build_free_gib} GiB"
    )
    retained = "; ".join(
        f"{role}={','.join(versions) or 'none'}"
        for role, versions in sorted(state.retained_versions.items())
    )
    print(f"Retained semantic versions: {retained}")
    print(
        f"Protected container image IDs: {len(state.referenced_image_ids)} "
        "(running and stopped)"
    )
    if state.legacy_cage_images:
        print(
            f"Legacy unlabeled Cage images: {state.legacy_cage_images} "
            "(reported only; never auto-cleaned)"
        )
    if not state.candidates:
        print("Cleanup candidates: none")
        return
    print(f"Cleanup candidates: {len(state.candidates)} exact reference(s)")
    for candidate in state.candidates:
        print(f"  {candidate.reference}  {_gib(candidate.size_bytes)}  {candidate.reason}")


def delete_candidates(
    docker: str,
    candidates: Iterable[CleanupCandidate],
) -> tuple[int, tuple[str, ...]]:
    removed = 0
    failures: list[str] = []
    for candidate in candidates:
        current_references = _referenced_image_ids(docker)
        inspected = _run(docker, ["image", "inspect", candidate.reference])
        if inspected.returncode != 0:
            continue
        try:
            records = json.loads(inspected.stdout)
            current_id = records[0].get("Id") if isinstance(records, list) and records else ""
        except (json.JSONDecodeError, AttributeError):
            failures.append(f"{candidate.reference}: Docker returned invalid inspection data")
            continue
        if current_id != candidate.image_id:
            failures.append(f"{candidate.reference}: changed since preview; skipped")
            continue
        if current_id in current_references:
            failures.append(f"{candidate.reference}: now referenced by a container; skipped")
            continue
        result = _run(docker, ["image", "rm", candidate.reference])
        if result.returncode == 0:
            removed += 1
        else:
            detail = result.stderr.strip() or result.stdout.strip() or "Docker refused removal"
            failures.append(f"{candidate.reference}: {detail}")
    return removed, tuple(failures)


def docker_command() -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise StorageError("docker command not found in PATH")
    return docker


def run_storage_command(
    action: str,
    policy: StoragePolicy,
    *,
    docker: str | None = None,
    input_stream=None,
) -> int:
    executable = docker or docker_command()
    state = snapshot(executable, policy)
    print_status(state, policy)
    if action == "status":
        return 0
    if action != "clean":
        raise StorageError("storage accepts 'status' or 'clean'")
    if not state.candidates:
        return 0
    stream = input_stream or sys.stdin
    if not stream.isatty():
        raise StorageError("storage clean requires an interactive TTY for confirmation")
    print("No volumes, containers, referenced images, unrelated images, or custom tags will be removed.")
    print("Type CLEAN to remove exactly the candidates above: ", end="", flush=True)
    confirmation = stream.readline().strip()
    if confirmation != "CLEAN":
        raise StorageError("cleanup aborted; confirmation did not match CLEAN")
    removed, failures = delete_candidates(executable, state.candidates)
    print(f"Removed {removed} exact image reference(s).")
    if failures:
        raise StorageError("cleanup incomplete: " + "; ".join(failures))
    return 0


def preflight(
    docker: str,
    policy: StoragePolicy,
    *,
    preferred_image: str,
    requires_build: bool,
    input_stream=None,
) -> None:
    state = snapshot(docker, policy, preferred_image=preferred_image)
    free = state.capacity.free_bytes
    if free is None:
        print(
            f"WARNING: Docker free space could not be measured: {state.capacity.error}",
            file=sys.stderr,
        )
        return
    threshold = policy.min_build_free_gib if requires_build else policy.warn_free_gib
    critical = free < policy.critical_free_gib * GIB
    below = free < threshold * GIB
    if not below:
        return
    kind = "build floor" if requires_build else ("critical floor" if critical else "warning floor")
    print(
        f"WARNING: Docker has {_gib(free)} free, below the {kind} of {threshold if requires_build else (policy.critical_free_gib if critical else policy.warn_free_gib)} GiB.",
        file=sys.stderr,
    )
    stream = input_stream or sys.stdin
    must_recover = requires_build or critical
    if not stream.isatty():
        if must_recover:
            raise StorageError(
                "noninteractive launch blocked by Docker storage policy; run 'cage storage status' and interactive 'cage storage clean'"
            )
        print("WARNING: noninteractive launch is proceeding above the critical floor.", file=sys.stderr)
        return
    print_status(state, policy)
    choices = "[c]lean/[a]bort" if must_recover else "[c]lean/[p]roceed/[a]bort"
    print(f"Storage action {choices}: ", end="", flush=True)
    choice = stream.readline().strip().lower()
    if choice == "p" and not must_recover:
        return
    if choice != "c":
        raise StorageError("launch aborted by storage policy")
    removed, failures = delete_candidates(docker, state.candidates)
    print(f"Removed {removed} exact image reference(s).")
    if failures:
        raise StorageError("cleanup incomplete: " + "; ".join(failures))
    refreshed = snapshot(docker, policy, preferred_image=preferred_image)
    refreshed_free = refreshed.capacity.free_bytes
    if refreshed_free is None:
        raise StorageError("Docker free space could not be re-measured after cleanup")
    required = policy.min_build_free_gib if requires_build else policy.critical_free_gib
    if refreshed_free < required * GIB:
        raise StorageError(
            f"Docker still has only {_gib(refreshed_free)} free after cleanup; {required} GiB is required"
        )
