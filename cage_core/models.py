"""Typed, side-effect-free models shared by Cage frontends and runtimes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


class ContractError(ValueError):
    """Raised when an internal versioned contract is malformed."""


@dataclass(frozen=True)
class MountSpec:
    """A normalized same-path host bind mount."""

    path: str
    mode: str
    source: str

    def __post_init__(self) -> None:
        if self.mode not in {"ro", "rw"}:
            raise ContractError(f"invalid mount mode: {self.mode!r}")
        if self.source not in {"repo", "config", "cli", "runtime"}:
            raise ContractError(f"invalid mount source: {self.source!r}")

    def public_dict(self) -> dict[str, str]:
        return {"path": self.path, "mode": self.mode, "source": self.source}


@dataclass(frozen=True)
class LaunchRequest:
    """Parsed command-line and TUI overrides.

    Tool arguments remain opaque. They are never included in the serialized
    contract; only their count is exposed for diagnostics.
    """

    repo_operand: str
    tool_arguments: tuple[str, ...] = ()
    explicit_tool: str = ""
    preset: str = ""
    interactive: bool = False
    yolo: bool | None = None
    network: str = ""
    target: str = ""
    rebuild: bool = False
    no_open: bool = False
    extra_mounts: tuple[tuple[str, str], ...] = ()
    tui_result: str = ""
    legacy_interactive: bool = False


@dataclass
class ResolvedConfig:
    """Validated central-configuration selection.

    Resolution builds this mutable value locally, then the launch planner
    copies it into an immutable :class:`LaunchPlan` before side effects.
    """

    config_path: Path
    repo_path: str
    preset_name: str
    preset_source: str
    tool: str
    codex_profile: str = ""
    auth_name: str = ""
    identity_name: str = ""
    mcp_pack_names: list[str] = field(default_factory=list)
    skill_pack_names: list[str] = field(default_factory=list)
    net: str = ""
    session_sync: str = ""
    yolo: str = ""
    target: str = "container"
    claude_auth: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    host_codex_dir: str = ""
    host_agents_dir: str = ""
    codex_copy_auth: str = ""
    git_user_name: str = ""
    git_user_email: str = ""
    ssh_key: str = ""
    ssh_host: str = ""
    gh_auth: str = ""
    gh_account: str = ""
    extra_env: list[str] = field(default_factory=list)
    stdio_mcp: list[dict[str, Any]] = field(default_factory=list)
    remote_mcp: list[dict[str, Any]] = field(default_factory=list)
    skill_mounts: list[dict[str, str]] = field(default_factory=list)
    host_commands: list[dict[str, str]] = field(default_factory=list)
    extra_mounts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mcp_inventory_enabled: list[str] = field(default_factory=list)
    mcp_suppressed: list[str] = field(default_factory=list)
    mcp_disable_overrides: list[str] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        """Return the non-secret, versioned resolver contract payload."""

        stdio_mcp = [
            {
                "name": str(item.get("name", "")),
                "type": "stdio",
                "environment_names": sorted(
                    name
                    for name in item.get("env", {})
                    if isinstance(name, str)
                )
                if isinstance(item.get("env"), dict)
                else [],
            }
            for item in self.stdio_mcp
        ]
        remote_mcp = [
            {
                "name": str(item.get("name", "")),
                "type": "http",
                "auth": str(item.get("auth", "")),
                "environment_names": sorted(
                    {
                        str(item.get("bearer_token_env_var", "")),
                        str(item.get("oauth_client_id_env_var", "")),
                        *(
                            str(name)
                            for name in item.get("env", [])
                            if isinstance(name, str)
                        ),
                    }
                    - {""}
                ),
                "header_names": sorted(
                    str(name)
                    for name in item.get("headers", {})
                    if isinstance(name, str)
                )
                if isinstance(item.get("headers"), dict)
                else [],
            }
            for item in self.remote_mcp
        ]
        return {
            "config_path": str(self.config_path),
            "repo_path": self.repo_path,
            "preset": {
                "name": self.preset_name,
                "source": self.preset_source,
            },
            "tool": self.tool,
            "target": self.target,
            "network": self.net,
            "yolo": self.yolo == "1",
            "session_sync": self.session_sync == "1",
            "codex_profile": self.codex_profile,
            "auth": {
                "name": self.auth_name,
                "claude_mode": self.claude_auth,
                "aws_profile": self.aws_profile,
                "aws_region": self.aws_region,
                "host_codex_dir": self.host_codex_dir,
                "host_agents_dir": self.host_agents_dir,
                "copy_auth": self.codex_copy_auth == "1",
            },
            "identity": {
                "name": self.identity_name,
                "git_user_name": self.git_user_name,
                "git_user_email": self.git_user_email,
                "ssh_key": self.ssh_key,
                "ssh_host": self.ssh_host,
                "gh_auth": self.gh_auth == "1",
                "gh_account": self.gh_account,
            },
            "environment_names": list(self.extra_env),
            "mcp": {
                "packs": list(self.mcp_pack_names),
                "stdio": stdio_mcp,
                "remote": remote_mcp,
                "suppressed": list(self.mcp_suppressed),
                "disable_overrides": list(self.mcp_disable_overrides),
            },
            "skills": {
                "packs": list(self.skill_pack_names),
                "mounts": [dict(item) for item in self.skill_mounts],
            },
            "host_commands": [
                {"name": str(item.get("name", ""))}
                for item in self.host_commands
            ],
            "extra_mounts": list(self.extra_mounts),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable, non-serialized target inputs captured by the launch plan."""

    claude_auth: str
    aws_profile: str
    aws_region: str
    host_codex_dir: str
    host_agents_dir: str
    codex_copy_auth: str
    codex_profile: str
    git_user_name: str
    git_user_email: str
    ssh_key: str
    ssh_host: str
    gh_auth: str
    gh_account: str
    session_sync: str
    extra_env: tuple[str, ...]
    stdio_mcp_json: str
    remote_mcp_json: str
    skill_mounts_json: str
    host_commands_json: str
    mcp_suppressed: tuple[str, ...]
    mcp_disable_overrides: tuple[str, ...]

    @staticmethod
    def _encode(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_resolved(cls, resolved: ResolvedConfig) -> "RuntimeConfig":
        return cls(
            claude_auth=resolved.claude_auth,
            aws_profile=resolved.aws_profile,
            aws_region=resolved.aws_region,
            host_codex_dir=resolved.host_codex_dir,
            host_agents_dir=resolved.host_agents_dir,
            codex_copy_auth=resolved.codex_copy_auth,
            codex_profile=resolved.codex_profile,
            git_user_name=resolved.git_user_name,
            git_user_email=resolved.git_user_email,
            ssh_key=resolved.ssh_key,
            ssh_host=resolved.ssh_host,
            gh_auth=resolved.gh_auth,
            gh_account=resolved.gh_account,
            session_sync=resolved.session_sync,
            extra_env=tuple(resolved.extra_env),
            stdio_mcp_json=cls._encode(resolved.stdio_mcp),
            remote_mcp_json=cls._encode(resolved.remote_mcp),
            skill_mounts_json=cls._encode(resolved.skill_mounts),
            host_commands_json=cls._encode(resolved.host_commands),
            mcp_suppressed=tuple(resolved.mcp_suppressed),
            mcp_disable_overrides=tuple(resolved.mcp_disable_overrides),
        )

    @staticmethod
    def _definitions(value: str) -> list[dict[str, Any]]:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or any(
            not isinstance(item, dict) for item in decoded
        ):
            raise ContractError("immutable runtime definitions are invalid")
        return decoded

    @property
    def stdio_mcp(self) -> list[dict[str, Any]]:
        return self._definitions(self.stdio_mcp_json)

    @property
    def remote_mcp(self) -> list[dict[str, Any]]:
        return self._definitions(self.remote_mcp_json)

    @property
    def skill_mounts(self) -> list[dict[str, Any]]:
        return self._definitions(self.skill_mounts_json)

    @property
    def host_commands(self) -> list[dict[str, Any]]:
        return self._definitions(self.host_commands_json)


@dataclass(frozen=True)
class LaunchPlan:
    """Complete, immutable launch description validated before side effects."""

    SCHEMA: ClassVar[str] = "cage.launch-plan"
    SCHEMA_VERSION: ClassVar[int] = 1

    cage_version: str
    repository: str
    preset_name: str
    preset_source: str
    tool: str
    target: str
    network: str
    yolo: bool
    rebuild: bool
    no_open: bool
    image: str
    registry_image: str
    dockerfile: str
    container_name: str
    volume_name: str
    container_home: str
    mounts: tuple[MountSpec, ...]
    environment_names: tuple[str, ...]
    mcp_names: tuple[str, ...]
    suppressed_mcp_names: tuple[str, ...]
    skill_names: tuple[str, ...]
    host_command_names: tuple[str, ...]
    capabilities: tuple[str, ...]
    warnings: tuple[str, ...]
    passthrough_argument_count: int
    runtime_config: RuntimeConfig
    codex_profile: str = ""
    session_sync: bool = False

    def __post_init__(self) -> None:
        if self.tool not in {"claude", "codex"}:
            raise ContractError(f"invalid tool: {self.tool!r}")
        if self.target not in {"container", "desktop", "host"}:
            raise ContractError(f"invalid execution target: {self.target!r}")
        if self.network not in {"open", "gate", "off"}:
            raise ContractError(f"invalid network mode: {self.network!r}")
        if self.passthrough_argument_count < 0:
            raise ContractError("passthrough argument count cannot be negative")

    def public_dict(self) -> dict[str, Any]:
        """Serialize only public planning evidence, never runtime secret data."""

        return {
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "cage_version": self.cage_version,
            "repository": self.repository,
            "preset": {
                "name": self.preset_name,
                "source": self.preset_source,
            },
            "execution": {
                "tool": self.tool,
                "target": self.target,
                "network": self.network,
                "yolo": self.yolo,
                "rebuild": self.rebuild,
                "no_open": self.no_open,
                "codex_profile": self.codex_profile,
                "passthrough_argument_count": self.passthrough_argument_count,
            },
            "image": {
                "local": self.image,
                "registry": self.registry_image,
                "dockerfile": self.dockerfile,
            },
            "state": {
                "container_name": self.container_name,
                "volume_name": self.volume_name,
                "container_home": self.container_home,
                "session_sync": self.session_sync,
            },
            "mounts": [item.public_dict() for item in self.mounts],
            "environment_names": list(self.environment_names),
            "selected_capabilities": {
                "mcp": list(self.mcp_names),
                "skills": list(self.skill_names),
                "host_commands": list(self.host_command_names),
                "runtime": list(self.capabilities),
            },
            "suppressed_mcp": list(self.suppressed_mcp_names),
            "warnings": list(self.warnings),
        }

    @classmethod
    def validate_public_dict(cls, value: object) -> dict[str, Any]:
        """Validate the exact public contract shape and reject unknown fields."""

        if not isinstance(value, dict):
            raise ContractError("launch plan must be a JSON object")
        expected = {
            "schema",
            "schema_version",
            "cage_version",
            "repository",
            "preset",
            "execution",
            "image",
            "state",
            "mounts",
            "environment_names",
            "selected_capabilities",
            "suppressed_mcp",
            "warnings",
        }
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown:
            raise ContractError(
                "unknown launch-plan fields: " + ", ".join(sorted(unknown))
            )
        if missing:
            raise ContractError(
                "missing launch-plan fields: " + ", ".join(sorted(missing))
            )
        if value["schema"] != cls.SCHEMA:
            raise ContractError(f"unsupported launch-plan schema: {value['schema']!r}")
        if value["schema_version"] != cls.SCHEMA_VERSION:
            raise ContractError(
                f"unsupported launch-plan schema version: {value['schema_version']!r}"
            )
        nested_shapes = {
            "preset": {"name", "source"},
            "execution": {
                "tool",
                "target",
                "network",
                "yolo",
                "rebuild",
                "no_open",
                "codex_profile",
                "passthrough_argument_count",
            },
            "image": {"local", "registry", "dockerfile"},
            "state": {
                "container_name",
                "volume_name",
                "container_home",
                "session_sync",
            },
            "selected_capabilities": {
                "mcp",
                "skills",
                "host_commands",
                "runtime",
            },
        }
        for name, allowed in nested_shapes.items():
            nested = value.get(name)
            if not isinstance(nested, dict):
                raise ContractError(f"launch-plan field {name!r} must be an object")
            nested_unknown = set(nested) - allowed
            nested_missing = allowed - set(nested)
            if nested_unknown or nested_missing:
                details = sorted(nested_unknown or nested_missing)
                kind = "unknown" if nested_unknown else "missing"
                raise ContractError(
                    f"{kind} launch-plan {name} fields: " + ", ".join(details)
                )
        mounts = value.get("mounts")
        if not isinstance(mounts, list):
            raise ContractError("launch-plan field 'mounts' must be a list")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise ContractError("launch-plan mount entries must be objects")
            allowed = {"path", "mode", "source"}
            unknown = set(mount) - allowed
            missing = allowed - set(mount)
            if unknown or missing:
                details = sorted(unknown or missing)
                kind = "unknown" if unknown else "missing"
                raise ContractError(
                    f"{kind} launch-plan mount fields: "
                    + ", ".join(details)
                )
            if not all(isinstance(mount[name], str) for name in allowed):
                raise ContractError(
                    "launch-plan mount fields must be strings"
                )
        for name in (
            "environment_names",
            "suppressed_mcp",
            "warnings",
        ):
            entries = value.get(name)
            if not isinstance(entries, list) or any(
                not isinstance(item, str) for item in entries
            ):
                raise ContractError(
                    f"launch-plan field {name!r} must be a string list"
                )
        capabilities = value["selected_capabilities"]
        for name in ("mcp", "skills", "host_commands", "runtime"):
            entries = capabilities[name]
            if not isinstance(entries, list) or any(
                not isinstance(item, str) for item in entries
            ):
                raise ContractError(
                    f"launch-plan capability {name!r} must be a string list"
                )
        return value
