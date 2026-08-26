"""Pure launch normalization and validation."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .models import (
    LaunchPlan,
    LaunchRequest,
    MountSpec,
    ResolvedConfig,
    RuntimeConfig,
)


CAGE_REGISTRY = "ghcr.io/sindycate/cage"
RESERVED_MOUNT_ROOTS = (
    "/etc",
    "/usr",
    "/var",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/home/claude",
    "/home/codex",
    "/home/opencode",
    "/workspace",
)


class PlanError(ValueError):
    """Raised before launch side effects when a request cannot be planned."""


@dataclass(frozen=True)
class PreparedLaunch:
    request: LaunchRequest
    plan: LaunchPlan


def path_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def is_reserved_mount_path(path: str | Path) -> bool:
    candidate = Path(path)
    if candidate == Path("/"):
        return True
    return any(path_within(candidate, Path(root)) for root in RESERVED_MOUNT_ROOTS)


def _configured_mount(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if value.startswith("rw="):
        return "rw", value[3:]
    if value.startswith("ro="):
        return "ro", value[3:]
    return "ro", value


def normalize_extra_mounts(
    request: LaunchRequest,
    resolved: ResolvedConfig,
    *,
    repository: Path,
    config_root: Path,
    install_root: Path,
) -> tuple[tuple[MountSpec, ...], tuple[str, ...]]:
    """Normalize user mounts with the launcher's historical first-wins rules."""

    warnings: list[str] = []
    mounts: list[MountSpec] = [
        MountSpec(path=str(repository), mode="rw", source="repo")
    ]
    seen: dict[Path, str] = {}
    raw_mounts = [
        (*_configured_mount(value), "config")
        for value in resolved.extra_mounts
    ]
    raw_mounts.extend(
        (mode, path, "cli") for mode, path in request.extra_mounts
    )
    for mode, raw, source in raw_mounts:
        expanded = Path(raw).expanduser()
        if not expanded.is_dir():
            warnings.append(
                f"extra mount skipped - {raw!r} is not an existing directory"
            )
            continue
        try:
            normalized = expanded.resolve(strict=True)
        except OSError:
            warnings.append(f"extra mount skipped - cannot resolve {raw!r}")
            continue
        if is_reserved_mount_path(normalized):
            raise PlanError(
                f"refusing extra mount {str(normalized)!r} - path conflicts "
                "with container filesystem"
            )
        if path_within(normalized, repository) or path_within(repository, normalized):
            warnings.append(
                f"extra mount skipped - {str(normalized)!r} overlaps the repo "
                f"mount ({repository})"
            )
            continue
        prior_mode = seen.get(normalized)
        if prior_mode is not None:
            if prior_mode != mode:
                warnings.append(
                    f"{str(normalized)!r} requested both ro and rw - "
                    f"keeping first ({prior_mode})"
                )
            continue
        seen[normalized] = mode
        mounts.append(MountSpec(path=str(normalized), mode=mode, source=source))

    if path_within(config_root, repository):
        raise PlanError(
            f"Cage config directory {str(config_root)!r} is inside the writable "
            f"repo mount {str(repository)!r}"
        )
    for mount in mounts:
        if mount.mode != "rw" or mount.source == "repo":
            continue
        root = Path(mount.path)
        if path_within(config_root, root):
            raise PlanError(
                f"Cage config directory {str(config_root)!r} is inside "
                f"read-write extra mount {str(root)!r}"
            )
        if request.target == "desktop" and path_within(install_root, root):
            raise PlanError(
                f"installed Cage launcher is inside read-write extra mount "
                f"{str(root)!r}"
            )
    return tuple(mounts), tuple(warnings)


def _target_error_checks(
    request: LaunchRequest,
    resolved: ResolvedConfig,
    *,
    target: str,
    network: str,
) -> None:
    if request.no_open and target != "desktop":
        raise PlanError("--no-open is only valid for desktop execution")
    if target in {"host", "desktop"} and resolved.tool != "codex":
        raise PlanError(
            f"{target} execution is only supported for Codex, not {resolved.tool}"
        )
    if target == "host":
        errors: list[str] = []
        if resolved.aws_access:
            errors.append(
                'aws_access = "host-cli" requires container execution'
            )
        if resolved.host_commands:
            errors.append(
                "host command bridges require container execution "
                '(target = "container")'
            )
        if resolved.extra_mounts or request.extra_mounts:
            errors.append(
                'extra mounts require container execution (target = "container")'
            )
        if network in {"gate", "off"}:
            errors.append(
                f"Cage cannot enforce network mode {network!r} without a container.\n"
                "  Host execution has no Cage network restriction.\n"
                "  Use --net open explicitly to acknowledge unrestricted host "
                "networking,\n"
                '  or use container execution (target = "container") for Cage '
                "network policy."
            )
        if resolved.host_agents_dir:
            selected = Path(resolved.host_agents_dir).expanduser()
            if selected != Path.home() / ".agents":
                errors.append(
                    f"custom host_agents_dir {str(selected)!r} is not supported "
                    "in host mode"
                )
        if errors:
            raise PlanError("\n".join(errors))
    if resolved.aws_access and network == "off":
        raise PlanError(
            'aws_access = "host-cli" cannot be combined with --net off; '
            "the host AWS CLI must make outbound connections"
        )
    if target == "desktop":
        if resolved.preset_source == "tui:launch-once" or not resolved.preset_name:
            raise PlanError("desktop targets require a saved or project-owned preset")
        if request.tool_arguments:
            raise PlanError(
                "desktop launches do not accept one-shot Codex arguments"
            )


def build_launch_plan(
    request: LaunchRequest,
    resolved: ResolvedConfig,
    *,
    cage_version: str,
    config_root: Path,
    install_root: Path,
) -> PreparedLaunch:
    """Build the complete immutable plan without starting launch resources."""

    try:
        repository = Path(resolved.repo_path).resolve(strict=True)
        normalized_config_root = config_root.resolve(strict=True)
        normalized_install_root = install_root.resolve(strict=True)
    except OSError as exc:
        raise PlanError(f"cannot resolve launch path: {exc}") from exc
    if not repository.is_dir():
        raise PlanError(f"repository is not a directory: {repository}")
    if is_reserved_mount_path(repository):
        raise PlanError(
            f"refusing to mount repo at {str(repository)!r} - path conflicts "
            "with container filesystem"
        )

    target = request.target or resolved.target or "container"
    yolo = request.yolo if request.yolo is not None else resolved.yolo == "1"
    network = request.network or resolved.net or ("gate" if yolo else "open")
    if network not in {"open", "gate", "off"}:
        raise PlanError(f"invalid network mode: {network!r}")
    _target_error_checks(
        request,
        resolved,
        target=target,
        network=network,
    )

    mounts, mount_warnings = normalize_extra_mounts(
        replace(request, target=target),
        resolved,
        repository=repository,
        config_root=normalized_config_root,
        install_root=normalized_install_root,
    )
    basename = repository.name
    digest = hashlib.md5(
        str(repository).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]
    if resolved.tool == "claude":
        image = f"claude-code:{cage_version}"
        registry_image = f"{CAGE_REGISTRY}/claude-code:{cage_version}"
        dockerfile = "Dockerfile"
        container_prefix = "claude"
        volume_prefix = "claude-state"
        container_home = "/home/claude"
    elif resolved.tool == "codex":
        image = f"codex:{cage_version}"
        registry_image = f"{CAGE_REGISTRY}/codex:{cage_version}"
        dockerfile = "Dockerfile.codex"
        container_prefix = "codex"
        volume_prefix = "codex-state"
        container_home = "/home/codex"
    else:
        image = f"opencode:{cage_version}"
        registry_image = f"{CAGE_REGISTRY}/opencode:{cage_version}"
        dockerfile = "Dockerfile.opencode"
        container_prefix = "opencode"
        volume_prefix = "opencode-state"
        container_home = "/home/opencode"
    container_name = f"{container_prefix}-{basename}-{digest}"
    volume_name = f"{volume_prefix}-{basename}-{digest}"
    if target == "desktop":
        target_id = hashlib.sha256(
            (str(repository) + "\0" + resolved.preset_name).encode("utf-8")
        ).hexdigest()[:16]
        container_name = f"cage-desktop-{target_id}"
        volume_name = f"cage-codex-desktop-{target_id}"
        if os.environ.get("CAGE_DESKTOP_INTERNAL") == "1":
            if os.environ.get("CAGE_DESKTOP_TARGET_ID") != target_id:
                raise PlanError(
                    "desktop target identity does not match the repository and preset"
                )
            if (
                os.environ.get("CAGE_DESKTOP_CONTAINER_NAME") != container_name
                or os.environ.get("CAGE_DESKTOP_VOLUME_NAME") != volume_name
            ):
                raise PlanError(
                    "invalid internal desktop container or volume name"
                )
            fingerprint = os.environ.get("CAGE_DESKTOP_FINGERPRINT", "")
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise PlanError(
                    "invalid internal desktop configuration fingerprint"
                )
    capabilities = ["repo-write", "persistent-tool-state"]
    if network == "gate":
        capabilities.append("netgate")
    if resolved.stdio_mcp:
        capabilities.append("mcp-bridge")
    if resolved.remote_mcp:
        capabilities.append("remote-mcp")
    if resolved.host_commands:
        capabilities.append("host-command-bridge")
    if resolved.aws_access:
        capabilities.append("aws-host-cli")
    if resolved.tool == "opencode":
        capabilities.append("opencode-frozen-config")
        if resolved.opencode_plugins == "1":
            capabilities.append("opencode-plugins")
    if target == "desktop":
        capabilities.extend(("desktop-ssh", "desktop-heartbeat"))
    if target == "host":
        capabilities = ["host-native", "unrestricted-network"]

    warnings = tuple(resolved.warnings) + mount_warnings
    plan = LaunchPlan(
        cage_version=cage_version,
        repository=str(repository),
        preset_name=resolved.preset_name,
        preset_source=resolved.preset_source,
        tool=resolved.tool,
        target=target,
        network=network,
        yolo=yolo,
        rebuild=request.rebuild,
        no_open=request.no_open,
        image=image,
        registry_image=registry_image,
        dockerfile=dockerfile,
        container_name=container_name,
        volume_name=volume_name,
        container_home=container_home,
        mounts=mounts,
        environment_names=tuple(resolved.extra_env),
        mcp_names=tuple(
            item["name"] for item in (*resolved.stdio_mcp, *resolved.remote_mcp)
        ),
        suppressed_mcp_names=tuple(resolved.mcp_suppressed),
        skill_names=tuple(item["name"] for item in resolved.skill_mounts),
        host_command_names=tuple(item["name"] for item in resolved.host_commands),
        capabilities=tuple(capabilities),
        warnings=warnings,
        passthrough_argument_count=len(request.tool_arguments),
        runtime_config=RuntimeConfig.from_resolved(resolved),
        storage_policy=resolved.storage_policy,
        codex_profile=resolved.codex_profile,
        session_sync=resolved.session_sync == "1",
    )
    return PreparedLaunch(request=request, plan=plan)
