#!/usr/bin/env python3
"""Curses configuration and launch UI for Cage.

The UI is intentionally an untrusted presentation layer. All reads, previews,
commits, and launch resolution go through cage-config.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from typing import Any


COLLECTION_LABELS = {
    "presets": "Reusable configurations",
    "auth": "Authentication profiles",
    "identities": "Git and GitHub identities",
    "mcp_packs": "MCP packs",
    "skill_packs": "Skill packs",
    "host_commands": "Host commands",
}

FIELD_SPECS: dict[str, list[tuple[str, str, str]]] = {
    "auth": [
        ("tool", "Tool", "tool"), ("mode", "Claude auth mode", "auth_mode"),
        ("env", "Environment variable names", "list"),
        ("aws_profile", "AWS profile", "text"),
        ("aws_access", "AWS access", "aws_access"),
        ("aws_region", "AWS region", "text"),
        ("host_codex_dir", "Host Codex directory", "text"),
        ("host_opencode_config_dir", "Host OpenCode config directory", "text"),
        ("host_opencode_data_dir", "Host OpenCode data directory", "text"),
        ("host_agents_dir", "Host agents directory", "text"),
        ("copy_auth", "Copy main tool login", "bool"),
    ],
    "identities": [
        ("git_user_name", "Git user name", "text"),
        ("git_user_email", "Git email", "text"), ("ssh_key", "SSH key path", "text"),
        ("ssh_host", "SSH host alias", "text"), ("gh_auth", "Forward GitHub auth", "bool"),
        ("gh_account", "GitHub account", "text"),
    ],
    "mcp_packs": [
        ("env", "Environment variable names", "list"),
    ],
    "skill_packs": [("source", "Agents registry", "text"), ("skills", "Skill names", "list")],
    "host_commands": [("command", "Host command", "text")],
}

CODEX_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DESKTOP_TARGET_ID_RE = re.compile(r"^[0-9a-f]{16}$")
DESKTOP_PRESET_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DESKTOP_STATUSES = {
    "starting", "ready", "stopping", "stopped", "stale", "failed", "unknown",
}
MAX_DESKTOP_TARGETS = 256
MAX_DESKTOP_LIST_CHARS = 2 * 1024 * 1024


def execution_target_label(target: str) -> str:
    if target == "host":
        return "Host CLI — no Docker boundary"
    if target == "desktop":
        return "Desktop via Cage container"
    return "Container"


class UiError(Exception):
    pass


class Controller:
    def __init__(
        self, backend: Path, config: Path, repo: Path, result: Path,
        net_override: str = "", yolo_override: str = "", tool_override: str = "",
        target_override: str = "",
    ):
        self.backend = backend
        self.config = config
        self.repo = repo.resolve()
        self.result = result
        self.net_override = net_override
        self.yolo_override = yolo_override
        self.tool_override = tool_override
        self.target_override = target_override
        self.snapshot = self._call("ui-export", "--repo", str(self.repo))

    def _call(self, command: str, *arguments: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request_path: Path | None = None
        try:
            command_line = [
                sys.executable, "-I", str(self.backend), "--config", str(self.config), command,
                *arguments,
            ]
            if request is not None:
                self.config.parent.mkdir(parents=True, exist_ok=True)
                descriptor, raw_path = tempfile.mkstemp(prefix=".cage-ui-request.", dir=self.config.parent)
                request_path = Path(raw_path)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(request, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(request_path, 0o600)
                command_line.extend(["--request", str(request_path)])
            completed = subprocess.run(command_line, text=True, capture_output=True, check=False)
            if completed.returncode:
                raise UiError(completed.stderr.strip() or completed.stdout.strip() or "configuration operation failed")
            try:
                output = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise UiError("configuration backend returned invalid data") from exc
            if not isinstance(output, dict):
                raise UiError("configuration backend returned an invalid object")
            return output
        finally:
            if request_path is not None:
                request_path.unlink(missing_ok=True)

    @property
    def data(self) -> dict[str, Any]:
        return self.snapshot["config"]

    def preview(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call("ui-preview", "--repo", str(self.repo), request={"operations": operations})

    def commit(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        updated = self._call(
            "ui-commit", "--repo", str(self.repo),
            request={
                "expected_sha256": self.snapshot["sha256"],
                "expected_destination": self.snapshot["destination"],
                "operations": operations,
            },
        )
        self.snapshot = updated
        return updated

    def write_result(self, value: dict[str, Any]) -> None:
        try:
            before = os.lstat(self.result)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                raise OSError("launch result must be a regular file owned by the current user")
            descriptor = os.open(self.result, os.O_WRONLY | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            current = os.lstat(self.result)
            identity = lambda item: (item.st_dev, item.st_ino)
            if identity(before) != identity(opened) or identity(opened) != identity(current):
                os.close(descriptor)
                raise OSError("launch result changed while it was being opened")
        except OSError as exc:
            raise UiError(f"cannot open private launch result: {exc}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 0)
            payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def oauth_targets(self) -> list[tuple[str, str, str]]:
        targets: list[tuple[str, str, str]] = []
        packs = self.data.get("mcp_packs", {})
        for preset_name, preset in self.data.get("presets", {}).items():
            if not isinstance(preset, dict):
                continue
            auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
            tool = preset.get("tool") or (auth.get("tool") if isinstance(auth, dict) else "")
            if tool not in {"codex", "opencode"}:
                continue
            for pack_name in preset.get("mcp_packs", []):
                pack = packs.get(pack_name, {})
                for server in pack.get("servers", []) if isinstance(pack, dict) else []:
                    if isinstance(server, dict) and server.get("type") == "http" and server.get("auth") == "oauth":
                        target = (str(preset_name), str(server.get("name", "")), str(server.get("url", "")))
                        if target not in targets:
                            targets.append(target)
        return sorted(targets)

    def run_oauth_action(self, action: str, preset: str, server: str) -> int:
        completed = subprocess.run([
            sys.executable, "-I", str(self.backend), "--config", str(self.config),
            "mcp", action, "--preset", preset, server, str(self.repo),
        ], check=False)
        return completed.returncode

    def desktop_targets(self) -> list[dict[str, Any]]:
        launcher = self.backend.with_name("cage")
        completed = subprocess.run(
            [str(launcher), "desktop", "list", "--json"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if len(completed.stdout) + len(completed.stderr) > MAX_DESKTOP_LIST_CHARS:
            raise UiError("Desktop target manager returned too much data")
        if completed.returncode:
            raise UiError(
                completed.stderr.strip()
                or completed.stdout.strip()
                or "could not list Desktop targets"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise UiError("Desktop target manager returned invalid data") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("targets"), list)
            or len(payload["targets"]) > MAX_DESKTOP_TARGETS
        ):
            raise UiError("Desktop target manager returned an unsupported result")
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in payload["targets"]:
            if not isinstance(raw, dict):
                raise UiError("Desktop target manager returned an invalid target")
            identifier = raw.get("target_id")
            alias = raw.get("alias")
            preset = raw.get("preset")
            repo = raw.get("repo")
            status = raw.get("status")
            volume = raw.get("volume_name")
            if (
                not isinstance(identifier, str)
                or not DESKTOP_TARGET_ID_RE.fullmatch(identifier)
                or identifier in seen
                or not isinstance(alias, str)
                or not alias.startswith("cage-")
                or len(alias) > 128
                or any(character in alias for character in "\0\r\n\t")
                or not isinstance(preset, str)
                or len(preset) > 128
                or not DESKTOP_PRESET_RE.fullmatch(preset)
                or not isinstance(repo, str)
                or len(repo) > 4096
                or not Path(repo).is_absolute()
                or any(character in repo for character in "\0\r\n")
                or status not in DESKTOP_STATUSES
                or volume != f"cage-codex-desktop-{identifier}"
            ):
                raise UiError("Desktop target manager returned an invalid target")
            target = {
                key: raw[key]
                for key in (
                    "target_id", "alias", "preset", "repo", "status", "volume_name",
                    "container_id", "started_at", "ready_at", "stopped_at", "exit_code",
                )
                if key in raw
            }
            targets.append(target)
            seen.add(identifier)
        return targets

    def run_desktop_action(
        self,
        action: str,
        target: dict[str, Any] | None = None,
        *,
        assume_yes: bool = False,
    ) -> tuple[int, str]:
        if action not in {"setup", "start", "restart", "stop", "logs", "remove"}:
            raise UiError(f"unsupported Desktop action: {action}")
        if action == "remove" and not assume_yes:
            raise UiError("Desktop removal requires an explicit TUI confirmation")
        launcher = self.backend.with_name("cage")
        command = [str(launcher), "desktop", action]
        if action != "setup":
            if target is None:
                raise UiError("Desktop action requires a registered target")
            preset = target.get("preset")
            repo = target.get("repo")
            identifier = target.get("target_id")
            if (
                not isinstance(preset, str)
                or len(preset) > 128
                or not DESKTOP_PRESET_RE.fullmatch(preset)
                or not isinstance(repo, str)
                or len(repo) > 4096
                or not Path(repo).is_absolute()
                or not isinstance(identifier, str)
                or not DESKTOP_TARGET_ID_RE.fullmatch(identifier)
            ):
                raise UiError("Desktop target selection is invalid")
            command.extend(["--preset", preset])
            if action == "remove" and assume_yes:
                command.append("--yes")
            command.append(repo)
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if len(output) > MAX_DESKTOP_LIST_CHARS:
            output = (
                f"(showing the newest {MAX_DESKTOP_LIST_CHARS} characters)\n"
                + output[-MAX_DESKTOP_LIST_CHARS:]
            )
        if action == "logs":
            lines = output.splitlines()
            if len(lines) > 250:
                output = "\n".join(
                    [f"(showing the newest 250 of {len(lines)} log lines)", *lines[-250:]]
                )
        return completed.returncode, output

    def effective_preset(self) -> tuple[str, dict[str, Any]]:
        effective = self.snapshot.get("effective", {})
        name = effective.get("preset", "") if isinstance(effective, dict) else ""
        preset = self.data.get("presets", {}).get(name, {})
        return (name, dict(preset)) if isinstance(preset, dict) else ("", {})

    def codex_profiles(self, preset: dict[str, Any]) -> tuple[Path, list[str]]:
        auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
        raw_home = (
            str(auth.get("host_codex_dir", "~/.codex"))
            if isinstance(auth, dict)
            else "~/.codex"
        )
        codex_home = Path(raw_home).expanduser()
        names: list[str] = []
        if codex_home.is_dir():
            for path in codex_home.glob("*.config.toml"):
                name = path.name.removesuffix(".config.toml")
                if path.is_file() and CODEX_PROFILE_RE.fullmatch(name):
                    names.append(name)
        return codex_home, sorted(set(names))

    def effective_exec_state(self, preset: dict[str, Any]) -> tuple[str, bool, str]:
        target = self.target_override or str(preset.get("target", "container"))
        yolo = self.yolo_override == "on" or (
            not self.yolo_override and preset.get("yolo") is True
        )
        net = (
            self.net_override
            or str(preset.get("net") or "")
            or str(self.data.get("defaults", {}).get("net") or "")
            or ("gate" if yolo else "open")
        )
        return target, yolo, net

    def risks(self, preset: dict[str, Any]) -> list[str]:
        risks: list[str] = []
        target, yolo, net = self.effective_exec_state(preset)
        auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
        auth = auth if isinstance(auth, dict) else {}
        aws_access = preset.get("aws_access") or auth.get("aws_access")
        aws_profile = preset.get("aws_profile") or auth.get("aws_profile")
        if aws_access == "host-cli":
            risks.append(
                "AWS commands run through the host AWS CLI with the fixed profile "
                f"{aws_profile or '(missing)'}; IAM permissions remain authoritative."
            )
            risks.append(
                "AWS host CLI traffic bypasses Cage's Netgate network policy and "
                "uses host browser/SSO state."
            )
        if target == "host":
            risks.append("Execution runs directly on the host — NO Docker isolation boundary.")
            risks.append("Host execution has unrestricted networking; Cage enforces no network policy.")
            if preset.get("skill_packs"):
                risks.append("Selected skills can direct actions performed by host-native Codex.")
            stdio_names: list[str] = []
            for pack_name in preset.get("mcp_packs", []):
                pack = self.data.get("mcp_packs", {}).get(pack_name, {})
                for server in pack.get("servers", []) if isinstance(pack, dict) else []:
                    if isinstance(server, dict) and server.get("type", "stdio") == "stdio":
                        stdio_names.append(str(server.get("name", pack_name)))
            if stdio_names:
                risks.append(
                    "Stdio MCP servers execute directly with host-user authority: "
                    + ", ".join(stdio_names)
                )
        elif target == "desktop":
            risks.append(
                "A repository- and preset-specific container remains running until explicitly stopped."
            )
            risks.append(
                "Cage registers a managed SSH alias and dedicated key for ChatGPT Desktop."
            )
        if yolo:
            risks.append("Coding-tool permission prompts are disabled (yolo).")
        if preset.get("tool") == "opencode" and preset.get("opencode_plugins") is True:
            risks.append(
                "OpenCode plugins are enabled and may execute global or project extension code."
            )
        if net == "open" and target != "host":
            risks.append("The container has unrestricted network access.")
        for mount in preset.get("extra_mounts", []):
            if isinstance(mount, dict) and mount.get("mode") == "rw":
                risks.append(f"Read-write host mount: {mount.get('path', '')}")
        commands = preset.get("host_commands", [])
        if commands:
            risks.append("Host commands execute outside the container: " + ", ".join(
                item if isinstance(item, str) else str(item.get("name", "unnamed")) for item in commands
            ))
        identity = self.data.get("identities", {}).get(preset.get("identity", ""), {})
        if isinstance(identity, dict) and identity.get("gh_auth") is True:
            if target == "host":
                risks.append("GitHub credentials are made available to the host Codex process.")
            else:
                risks.append("GitHub credentials are forwarded into the container.")
        for pack_name in preset.get("mcp_packs", []):
            pack = self.data.get("mcp_packs", {}).get(pack_name, {})
            for server in pack.get("servers", []) if isinstance(pack, dict) else []:
                if isinstance(server, dict) and (
                    server.get("auth") or server.get("bearer_token_env_var") or server.get("headers")
                ):
                    risks.append(f"Authenticated external MCP integration: {server.get('name', pack_name)}")
        return risks

    def object_risks(self, collection: str, value: dict[str, Any]) -> list[str]:
        if collection == "presets":
            return self.risks(value)
        if collection == "identities" and value.get("gh_auth") is True:
            return ["This identity forwards GitHub credentials into the container."]
        if collection == "host_commands" and value.get("command"):
            return ["This command can execute with full host-user authority."]
        if collection == "auth":
            risks: list[str] = []
            if value.get("copy_auth") is True:
                risks.append("This profile copies the main tool login into project containers.")
            if value.get("aws_access") == "host-cli":
                risks.append(
                    "This profile enables a profile-pinned host AWS CLI relay; "
                    "traffic bypasses Netgate and uses host browser/SSO state."
                )
            return risks
        if collection == "mcp_packs":
            authenticated = [
                str(server.get("name", "unnamed")) for server in value.get("servers", [])
                if isinstance(server, dict) and (
                    server.get("auth") or server.get("bearer_token_env_var") or server.get("headers")
                )
            ]
            if authenticated:
                return ["Authenticated external integrations: " + ", ".join(authenticated)]
        return []

    def preflight(self, preset: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        target, _yolo, effective_net = self.effective_exec_state(preset)
        auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
        auth = auth if isinstance(auth, dict) else {}
        aws_access = preset.get("aws_access") or auth.get("aws_access")
        aws_profile = preset.get("aws_profile") or auth.get("aws_profile")
        if aws_access == "host-cli":
            if not aws_profile:
                warnings.append("AWS host CLI access requires an AWS profile.")
            if target == "host":
                warnings.append(
                    "AWS host CLI access is for container execution; host-native Codex "
                    "already runs directly on the host."
                )
            if effective_net == "off":
                warnings.append(
                    "AWS host CLI access requires host outbound networking and cannot "
                    "be combined with --net off."
                )
            if shutil.which("aws") is None:
                warnings.append("aws command not found in PATH (required for AWS host CLI access).")
            else:
                warnings.append(
                    "AWS host CLI traffic bypasses Netgate and uses the host's "
                    "browser/SSO session."
                )
        if target == "host":
            if (self.tool_override or preset.get("tool", "codex")) != "codex":
                warnings.append("Host execution is only supported for Codex.")
            if shutil.which("codex") is None:
                warnings.append("codex command not found in PATH (required for host execution).")
            if preset.get("host_commands"):
                warnings.append("Host commands require container execution and will be rejected at launch.")
            if preset.get("extra_mounts"):
                warnings.append("Extra mounts require container execution and will be rejected at launch.")
            if effective_net in ("gate", "off"):
                warnings.append(
                    f"Network mode '{effective_net}' cannot be enforced without a container. "
                    "Use --net open explicitly or switch to container execution."
                )
            if auth.get("host_agents_dir"):
                agents_dir = str(auth["host_agents_dir"])
                default_agents = str(Path.home() / ".agents")
                resolved = str(Path(agents_dir).expanduser())
                if resolved != default_agents:
                    warnings.append(
                        f"Custom host_agents_dir '{agents_dir}' is not supported in host mode. "
                        "The default ~/.agents is naturally available."
                    )
            for pack_name in preset.get("skill_packs", []):
                pack = self.data.get("skill_packs", {}).get(pack_name, {})
                if not isinstance(pack, dict):
                    continue
                source = str(
                    pack.get("source")
                    or (auth.get("host_agents_dir") if isinstance(auth, dict) else "")
                    or "~/.agents"
                )
                if Path(source).expanduser() != Path.home() / ".agents":
                    warnings.append(
                        f"Skill pack '{pack_name}' uses '{source}'. Host-native skill packs "
                        "currently require the default ~/.agents registry."
                    )
            for pack_name in preset.get("mcp_packs", []):
                pack = self.data.get("mcp_packs", {}).get(pack_name, {})
                for server in pack.get("servers", []) if isinstance(pack, dict) else []:
                    if not isinstance(server, dict) or server.get("type", "stdio") != "stdio":
                        continue
                    command = str(server.get("command", ""))
                    try:
                        argv = shlex.split(command)
                    except ValueError:
                        warnings.append(
                            f"Stdio MCP server '{server.get('name', pack_name)}' has invalid quoting."
                        )
                        continue
                    executable = shutil.which(argv[0]) if argv else None
                    if executable is None:
                        warnings.append(
                            f"Stdio MCP executable is not available for "
                            f"'{server.get('name', pack_name)}': {argv[0] if argv else '(empty)'}"
                        )
                        continue
                    try:
                        resolved_executable = Path(executable).resolve(strict=True)
                    except OSError as exc:
                        warnings.append(
                            f"Cannot resolve stdio MCP executable for "
                            f"'{server.get('name', pack_name)}': {exc}"
                        )
                        continue
                    if (
                        resolved_executable == self.repo
                        or resolved_executable.is_relative_to(self.repo)
                    ):
                        warnings.append(
                            f"Stdio MCP executable for '{server.get('name', pack_name)}' "
                            "is inside the writable repository and will be rejected."
                        )
            identity = self.data.get("identities", {}).get(preset.get("identity", ""), {})
            if isinstance(identity, dict):
                if identity.get("ssh_host"):
                    warnings.append(
                        "ssh_host aliases are not supported in host mode and will be rejected at launch."
                    )
                if (
                    identity.get("gh_auth") is True
                    and not os.environ.get("GH_TOKEN")
                    and not os.environ.get("GITHUB_TOKEN")
                    and shutil.which("gh") is None
                ):
                    warnings.append(
                        "GitHub authentication is enabled but neither a token nor the gh command is available."
                    )
        elif target == "desktop":
            if (self.tool_override or preset.get("tool", "codex")) != "codex":
                warnings.append("Desktop execution is only supported for Codex.")
            if sys.platform != "darwin":
                warnings.append("Desktop execution is currently supported only on macOS.")
            if not Path("/Applications/ChatGPT.app").is_dir() and not Path(
                "/Applications/Codex.app"
            ).is_dir():
                warnings.append("ChatGPT Desktop was not found under /Applications.")
            if shutil.which("docker") is None:
                warnings.append("docker command not found (required for Desktop execution).")
        elif shutil.which("docker") is None:
            warnings.append("Docker is not available in PATH.")
        required: set[str] = set(preset.get("env", []))
        auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
        if isinstance(auth, dict):
            required.update(auth.get("env", []))
            for key in (
                "host_codex_dir",
                "host_opencode_config_dir",
                "host_opencode_data_dir",
                "host_agents_dir",
            ):
                raw_path = auth.get(key)
                if raw_path and not Path(str(raw_path)).expanduser().is_dir():
                    warnings.append(f"{key} does not exist: {raw_path}")
        if preset.get("codex_profile"):
            codex_home = Path(
                str(auth.get("host_codex_dir", "~/.codex"))
                if isinstance(auth, dict)
                else "~/.codex"
            ).expanduser()
            profile_path = codex_home / f"{preset['codex_profile']}.config.toml"
            if not profile_path.is_file():
                warnings.append(f"Selected Codex profile does not exist: {profile_path}")
        for pack_name in preset.get("mcp_packs", []):
            pack = self.data.get("mcp_packs", {}).get(pack_name, {})
            if not isinstance(pack, dict):
                continue
            required.update(pack.get("env", []))
            for server in pack.get("servers", []):
                if isinstance(server, dict):
                    for key in ("bearer_token_env_var", "oauth_client_id_env_var"):
                        if server.get(key):
                            required.add(server[key])
        warnings.extend(f"Environment variable is not set: {name}" for name in sorted(required) if name not in os.environ)
        for mount in preset.get("extra_mounts", []):
            raw = mount if isinstance(mount, str) else mount.get("path", "") if isinstance(mount, dict) else ""
            if raw and not Path(raw).expanduser().is_dir():
                warnings.append(f"Mount path does not exist: {raw}")
        identity = self.data.get("identities", {}).get(preset.get("identity", ""), {})
        if isinstance(identity, dict) and identity.get("ssh_key"):
            key_path = Path(str(identity["ssh_key"])).expanduser()
            if not key_path.is_file():
                warnings.append(f"SSH key does not exist: {identity['ssh_key']}")
        command_defs = self.data.get("host_commands", {})
        for item in preset.get("host_commands", []):
            definition = command_defs.get(item, {}) if isinstance(item, str) else item
            command = definition.get("command", "") if isinstance(definition, dict) else ""
            try:
                argv = shlex.split(command)
            except ValueError:
                warnings.append(f"Host command has invalid quoting: {command}")
                continue
            if argv and "/" not in argv[0] and shutil.which(argv[0]) is None:
                warnings.append(f"Host command executable is not available: {argv[0]}")
        return warnings


class CursesView:
    def __init__(self, screen, controller: Controller):
        import curses
        self.screen = screen
        self.controller = controller
        self.message = ""
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        self.screen.keypad(True)

    @staticmethod
    def _wrapped(lines: list[str], width: int) -> list[str]:
        wrapped: list[str] = []
        width = max(1, width)
        for line in lines:
            logical_lines = str(line).splitlines() or [""]
            for logical in logical_lines:
                wrapped.extend(textwrap.wrap(
                    logical,
                    width=width,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=True,
                    break_on_hyphens=False,
                ) or [""])
        return wrapped

    @staticmethod
    def _initial_index(options: list[tuple[str, str]], initial_key: str | None) -> int:
        if initial_key:
            for index, (key, _) in enumerate(options):
                if key == initial_key:
                    return index
        return 0

    def _draw(self, title: str, lines: list[str], footer: str = "") -> None:
        import curses
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 3 or width < 4:
            self.screen.refresh()
            return
        self.screen.addnstr(0, 0, f" Cage — {title} ", max(1, width - 1), curses.A_REVERSE)
        for row, line in enumerate(lines[: max(0, height - 2)], start=1):
            self.screen.addnstr(row, 1, line, max(1, width - 2))
        status = self.message or footer
        if status:
            self.screen.addnstr(height - 1, 0, status, max(1, width - 1), curses.A_REVERSE)
        self.screen.refresh()

    def menu(
        self,
        title: str,
        options: list[tuple[str, str]],
        details: list[str] | None = None,
        initial_key: str | None = None,
        space_select: bool = False,
        footer: str = "",
    ) -> str:
        import curses
        if not options:
            self.message = f"No entries are available for {title}."
            return ""
        index = self._initial_index(options, initial_key)
        scroll = 0
        while True:
            height, width = self.screen.getmaxyx()
            if height < 5 or width < 20:
                self._draw(title, ["Terminal too small; resize to continue."], "Esc/q back")
                key = self.screen.getch()
                if key in (27, ord("q")):
                    return ""
                continue
            available = max(1, height - 2)
            detail_lines = self._wrapped(list(details or []), max(1, width - 2))
            if detail_lines:
                detail_lines.append("")
            option_start = len(detail_lines)
            lines = detail_lines + [f"  {label}" for _, label in options]
            selected_line = option_start + index
            if selected_line < scroll:
                scroll = selected_line
            elif selected_line >= scroll + available:
                scroll = selected_line - available + 1
            scroll = max(0, min(scroll, max(0, len(lines) - available)))
            visible = lines[scroll:scroll + available]
            controls = footer or (
                "↑/↓ move • Space/Enter toggle • Esc/q back"
                if space_select else
                "↑/↓ move • Enter select • Esc/q back"
            )
            if len(lines) > available:
                controls += f" • rows {scroll + 1}-{min(len(lines), scroll + available)}/{len(lines)}"
            self._draw(title, visible, controls)
            option_row = selected_line - scroll + 1
            if 1 <= option_row < max(1, height - 1):
                self.screen.addnstr(
                    option_row,
                    1,
                    f"  {options[index][1]}",
                    max(1, width - 2),
                    curses.A_REVERSE,
                )
                self.screen.refresh()
            key = self.screen.getch()
            self.message = ""
            if key in (curses.KEY_UP, ord("k")):
                index = (index - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = (index + 1) % len(options)
            elif key == curses.KEY_PPAGE:
                index = max(0, index - max(1, available - 1))
            elif key == curses.KEY_NPAGE:
                index = min(len(options) - 1, index + max(1, available - 1))
            elif key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(options) - 1
            elif key in (10, 13, curses.KEY_ENTER):
                return options[index][0]
            elif space_select and key == ord(" "):
                return options[index][0]
            elif key in (27, ord("q")):
                return ""

    def show_text(self, title: str, lines: list[str]) -> None:
        import curses
        scroll = 0
        while True:
            height, width = self.screen.getmaxyx()
            if height < 4 or width < 20:
                self._draw(title, ["Terminal too small; resize to continue."], "Esc/q back")
                key = self.screen.getch()
                if key in (27, ord("q"), 10, 13, curses.KEY_ENTER):
                    return
                continue
            available = max(1, height - 2)
            wrapped = self._wrapped(lines or ["(no output)"], max(1, width - 2))
            maximum = max(0, len(wrapped) - available)
            scroll = max(0, min(scroll, maximum))
            footer = "↑/↓ scroll • PgUp/PgDn page • Esc/q/Enter back"
            if len(wrapped) > available:
                footer += (
                    f" • rows {scroll + 1}-"
                    f"{min(len(wrapped), scroll + available)}/{len(wrapped)}"
                )
            self._draw(title, wrapped[scroll:scroll + available], footer)
            key = self.screen.getch()
            if key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll = min(maximum, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - available)
            elif key == curses.KEY_NPAGE:
                scroll = min(maximum, scroll + available)
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = maximum
            elif key in (27, ord("q"), 10, 13, curses.KEY_ENTER):
                return

    def _line_input(
        self,
        title: str,
        label: str,
        initial: str = "",
        details: list[str] | None = None,
        footer: str = "Enter saves • Esc cancels • erase all text to clear",
    ) -> str | None:
        import curses
        characters = list(initial)
        cursor = len(characters)
        horizontal = 0
        detail_scroll = 0
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                height, width = self.screen.getmaxyx()
                if height < 5 or width < 20:
                    self._draw(title, ["Terminal too small; resize to continue."], "Esc cancels")
                    key = self.screen.get_wch()
                    if key == "\x1b":
                        return None
                    continue
                input_row = max(1, height - 2)
                detail_capacity = max(0, input_row - 1)
                detail_lines = self._wrapped(list(details or []) + [label], max(1, width - 2))
                detail_scroll = max(
                    0,
                    min(detail_scroll, max(0, len(detail_lines) - detail_capacity)),
                )
                self._draw(
                    title,
                    detail_lines[detail_scroll:detail_scroll + detail_capacity],
                    footer,
                )

                prefix = "> "
                input_width = max(1, width - len(prefix) - 2)
                if cursor < horizontal:
                    horizontal = cursor
                elif cursor > horizontal + input_width:
                    horizontal = cursor - input_width
                horizontal = max(0, min(horizontal, max(0, len(characters) - input_width)))
                visible = "".join(characters[horizontal:horizontal + input_width])
                if height >= 3 and width >= 4:
                    self.screen.move(input_row, 1)
                    self.screen.clrtoeol()
                    self.screen.addnstr(
                        input_row,
                        1,
                        prefix + visible,
                        max(1, width - 2),
                        curses.A_UNDERLINE,
                    )
                    self.screen.move(input_row, min(width - 2, len(prefix) + 1 + cursor - horizontal))
                    self.screen.refresh()

                key = self.screen.get_wch()
                if key in ("\n", "\r") or key == curses.KEY_ENTER:
                    return "".join(characters).strip()
                if key == "\x1b":
                    return None
                if key in (curses.KEY_LEFT,):
                    cursor = max(0, cursor - 1)
                elif key in (curses.KEY_RIGHT,):
                    cursor = min(len(characters), cursor + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = 0
                elif key in (curses.KEY_END, "\x05"):
                    cursor = len(characters)
                elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    if cursor:
                        del characters[cursor - 1]
                        cursor -= 1
                elif key == curses.KEY_DC:
                    if cursor < len(characters):
                        del characters[cursor]
                elif key == "\x15":
                    characters.clear()
                    cursor = 0
                    horizontal = 0
                elif key == curses.KEY_UP:
                    detail_scroll = max(0, detail_scroll - 1)
                elif key == curses.KEY_DOWN:
                    detail_scroll = min(max(0, len(detail_lines) - detail_capacity), detail_scroll + 1)
                elif key == curses.KEY_PPAGE:
                    detail_scroll = max(0, detail_scroll - max(1, detail_capacity))
                elif key == curses.KEY_NPAGE:
                    detail_scroll = min(
                        max(0, len(detail_lines) - detail_capacity),
                        detail_scroll + max(1, detail_capacity),
                    )
                elif key == curses.KEY_RESIZE:
                    continue
                elif isinstance(key, str) and key.isprintable():
                    characters.insert(cursor, key)
                    cursor += 1
        except (KeyboardInterrupt, curses.error):
            return None
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def prompt(self, title: str, label: str, default: str = "") -> str | None:
        return self._line_input(title, label, default)

    def confirm(
        self,
        title: str,
        lines: list[str],
        phrase: str = "yes",
        *,
        case_sensitive: bool = False,
    ) -> bool:
        answer = self._line_input(
            title,
            f"Type '{phrase}' and press Enter to continue:",
            "",
            details=lines,
            footer="↑/↓ review • Enter submits • Esc cancels",
        )
        if answer is None:
            return False
        return answer == phrase if case_sensitive else answer.casefold() == phrase.casefold()

    def choose_names(self, title: str, names: list[str], selected: list[str]) -> list[str] | None:
        chosen = set(selected)
        options = [(name, ("[x] " if name in chosen else "[ ] ") + name) for name in names]
        options.append(("__done", "Done"))
        cursor = next((name for name in names if name in chosen), names[0] if names else "__done")
        while True:
            choice = self.menu(
                title,
                options,
                initial_key=cursor,
                space_select=True,
                footer="↑/↓ move • Space/Enter toggle • choose Done to accept • Esc/q back",
            )
            if not choice:
                return None
            if choice == "__done":
                return [name for name in names if name in chosen]
            cursor = choice
            chosen.symmetric_difference_update({choice})
            options = [(name, ("[x] " if name in chosen else "[ ] ") + name) for name in names]
            options.append(("__done", "Done"))

    def select_value(self, title: str, values: list[str], current: str, allow_none: bool = True) -> str | None:
        options = []
        if allow_none:
            options.append(("__none", "none" + (" (current)" if not current else "")))
        options.extend((value, value + (" (current)" if value == current else "")) for value in values)
        choice = self.menu(
            title,
            options,
            initial_key=current if current in values else "__none" if allow_none else None,
        )
        if not choice:
            return None
        return "" if choice == "__none" else choice

    def edit_preset(self, preset: dict[str, Any]) -> dict[str, Any] | None:
        value = dict(preset)
        value["tool"] = self.controller.tool_override or value.get("tool", "codex")
        if value["tool"] == "claude":
            value.pop("skill_packs", None)
            value.pop("codex_profile", None)
            value.pop("opencode_plugins", None)
        elif value["tool"] == "opencode":
            value.pop("codex_profile", None)
            value.pop("session_sync", None)
        cursor = "tool"
        while True:
            yolo_value = "on" if value.get("yolo") is True else "off"
            if self.controller.yolo_override:
                yolo_value = f"{self.controller.yolo_override} (command override)"
            net_value = str(value.get("net", "default"))
            if self.controller.net_override:
                net_value = f"{self.controller.net_override} (command override)"
            sync_value = (
                "default" if "session_sync" not in value
                else "on" if value.get("session_sync") is True
                else "off"
            )
            target_value = self.controller.target_override or value.get("target", "container")
            target_label = execution_target_label(str(target_value))
            if self.controller.target_override:
                target_label += " (command override)"
            rows = [
                ("tool", f"Tool: {value.get('tool', 'codex')}"),
                ("target", f"Execution: {target_label}"),
                ("profile", f"Codex profile: {value.get('codex_profile', 'base config')}"),
                ("auth", f"Auth: {value.get('auth', 'none')}"),
                ("identity", f"Identity: {value.get('identity', 'none')}"),
                ("mcp", f"MCP packs (selected packs only): {', '.join(value.get('mcp_packs', [])) or 'none'}"),
                ("skills", f"Skill packs: {', '.join(value.get('skill_packs', [])) or 'none'}"),
                ("commands", f"Host commands: {', '.join(value.get('host_commands', [])) or 'none'}"),
                ("env", f"Extra environment names: {', '.join(value.get('env', [])) or 'none'}"),
                ("net", f"Network: {net_value}"),
                ("yolo", f"Yolo: {yolo_value}"),
                ("sync", f"Claude history sync: {sync_value}"),
                ("mounts", f"Extra mounts: {len(value.get('extra_mounts', []))}"),
                ("done", "Done"),
            ]
            if value.get("tool") == "opencode":
                rows.insert(
                    7,
                    (
                        "plugins",
                        "OpenCode plugins: "
                        + (
                            "enabled"
                            if value.get("opencode_plugins") is True
                            else "pure/disabled"
                        ),
                    ),
                )
            choice = self.menu("Customize launch", rows, initial_key=cursor)
            if not choice:
                return None
            cursor = choice
            if choice == "done":
                # target_override is launch-only; do NOT inject it into the
                # saved preset. The preview validates the preset as-is.
                try:
                    self.controller.preview([{"action": "upsert", "collection": "presets", "name": "__cage_preview", "value": value}])
                except UiError as exc:
                    self.message = str(exc)
                    continue
                return value
            if choice == "tool":
                if self.controller.tool_override:
                    self.message = f"Tool is fixed to {self.controller.tool_override} by the command."
                    continue
                selected = self.select_value(
                    "Tool", ["codex", "claude", "opencode"],
                    str(value.get("tool", "codex")), False,
                )
                if selected:
                    value["tool"] = selected
                    if selected == "claude":
                        value.pop("skill_packs", None)
                        value.pop("codex_profile", None)
                        value.pop("opencode_plugins", None)
                        value.pop("target", None)  # non-container targets are Codex-only
                    elif selected == "opencode":
                        value.pop("codex_profile", None)
                        value.pop("session_sync", None)
                        value.pop("target", None)
                    else:
                        value.pop("opencode_plugins", None)
            elif choice == "profile":
                if value.get("tool", "codex") != "codex":
                    self.message = "Named Codex profiles are only supported for Codex."
                    continue
                codex_home, names = self.controller.codex_profiles(value)
                selected = self.select_value(
                    f"Codex profile ({codex_home})",
                    names,
                    str(value.get("codex_profile", "")),
                )
                if selected is not None:
                    if selected:
                        value["codex_profile"] = selected
                    else:
                        value.pop("codex_profile", None)
                    if not names:
                        self.message = (
                            f"No named profiles found under {codex_home}; using base config."
                        )
            elif choice == "target":
                if self.controller.target_override:
                    self.message = f"Execution target is fixed to {self.controller.target_override} by the command."
                    continue
                if value.get("tool", "codex") != "codex":
                    self.message = "Host and Desktop execution are only supported for Codex."
                    continue
                selected = self.select_value(
                    "Execution target",
                    ["container", "desktop", "host"],
                    str(value.get("target", "container")),
                    False,
                )
                if selected:
                    if selected == "container":
                        value.pop("target", None)
                    else:
                        value["target"] = selected
            elif choice in ("auth", "identity"):
                collection = "auth" if choice == "auth" else "identities"
                names = sorted(self.controller.data.get(collection, {}))
                if choice == "auth":
                    names = [name for name in names if self.controller.data[collection][name].get("tool") in (None, value["tool"])]
                selected = self.select_value(choice.title(), names, str(value.get(choice, "")))
                if selected is not None:
                    value[choice] = selected
                    if not selected:
                        value.pop(choice, None)
            elif choice in ("mcp", "skills", "commands"):
                key, collection = {
                    "mcp": ("mcp_packs", "mcp_packs"),
                    "skills": ("skill_packs", "skill_packs"),
                    "commands": ("host_commands", "host_commands"),
                }[choice]
                selected = self.choose_names(choice.title(), sorted(self.controller.data.get(collection, {})), value.get(key, []))
                if selected is not None:
                    if selected:
                        value[key] = selected
                    else:
                        value.pop(key, None)
            elif choice == "plugins":
                if value.get("tool") != "opencode":
                    self.message = "Plugin isolation is an OpenCode-only setting."
                    continue
                selected = self.select_value(
                    "OpenCode plugins", ["pure", "enabled"],
                    "enabled" if value.get("opencode_plugins") is True else "pure",
                    False,
                )
                if selected:
                    value["opencode_plugins"] = selected == "enabled"
            elif choice == "net":
                if self.controller.net_override:
                    self.message = f"Network is fixed to {self.controller.net_override} by the command."
                    continue
                selected = self.select_value("Network", ["gate", "open", "off"], str(value.get("net", "")))
                if selected is not None:
                    if selected: value["net"] = selected
                    else: value.pop("net", None)
            elif choice == "yolo":
                if self.controller.yolo_override:
                    self.message = f"Yolo is fixed to {self.controller.yolo_override} by the command."
                    continue
                selected = self.select_value("Yolo", ["on", "off"], yolo_value, False)
                if selected:
                    value["yolo"] = selected == "on"
            elif choice == "sync":
                if value.get("tool") == "opencode":
                    self.message = "OpenCode state remains in its per-repository volume."
                    continue
                selected = self.select_value("Claude history sync", ["default", "on", "off"], sync_value, False)
                if selected == "default":
                    value.pop("session_sync", None)
                elif selected:
                    value["session_sync"] = selected == "on"
            elif choice == "mounts":
                raw = self.prompt("Extra mounts", "Comma-separated paths; prefix read-write paths with rw=. Erase all text to clear.", ", ".join(
                    ("rw=" + str(item.get("path"))) if isinstance(item, dict) and item.get("mode") == "rw" else str(item.get("path")) if isinstance(item, dict) else str(item)
                    for item in value.get("extra_mounts", [])
                ))
                if raw is not None:
                    if raw == "-":
                        raw = ""
                    mounts: list[Any] = []
                    for item in [part.strip() for part in raw.split(",") if part.strip()]:
                        mounts.append({"path": item[3:], "mode": "rw"} if item.startswith("rw=") else item)
                    if mounts: value["extra_mounts"] = mounts
                    else: value.pop("extra_mounts", None)
            elif choice == "env":
                raw = self.prompt(
                    "Extra environment",
                    "Comma-separated environment variable names. Erase all text to clear.",
                    ", ".join(value.get("env", [])),
                )
                if raw is not None:
                    names = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()]
                    if names: value["env"] = names
                    else: value.pop("env", None)

    def risk_review(
        self,
        preset: dict[str, Any],
        action: str = "",
        force_confirmation: bool = False,
        notices: list[str] | None = None,
    ) -> bool:
        risks = self.controller.risks(preset)
        warnings = self.controller.preflight(preset)
        if not risks and not warnings and not force_confirmation and not notices:
            return True
        lines = [f"Action: {action}"] if action else []
        lines += list(notices or [])
        if risks:
            lines += ["", "Risk review"] + [f"• {item}" for item in risks]
        if warnings:
            lines += ["", "Preflight warnings"] + [f"• {item}" for item in warnings]
        return self.confirm("Review before launch/save", lines)

    def edit_mcp_server(self, current: dict[str, Any]) -> dict[str, Any] | None:
        value = copy.deepcopy(current)
        value.setdefault("type", "stdio")
        cursor = "name"
        while True:
            server_type = value.get("type", "stdio")
            fields = [
                ("name", f"Name: {value.get('name', 'unset')}"),
                ("type", f"Transport: {server_type}"),
            ]
            if server_type == "stdio":
                fields.append(("command", f"Command: {value.get('command', 'unset')}"))
            else:
                fields.extend([
                    ("url", f"URL: {value.get('url', 'unset')}"),
                    ("auth", f"Auth: {value.get('auth', 'none')}"),
                    ("bearer_token_env_var", f"Bearer token env: {value.get('bearer_token_env_var', 'unset')}"),
                    ("oauth_resource", f"OAuth resource: {value.get('oauth_resource', 'unset')}"),
                    ("oauth_scopes", f"OAuth scopes: {', '.join(value.get('oauth_scopes', [])) or 'unset'}"),
                    ("oauth_client_id", f"OAuth client ID: {value.get('oauth_client_id', 'unset')}"),
                    ("oauth_client_id_env_var", f"OAuth client ID env: {value.get('oauth_client_id_env_var', 'unset')}"),
                    ("headers", f"Headers: {json.dumps(value.get('headers', {}), separators=(',', ':'))}"),
                ])
            fields.extend([
                ("env", f"Forwarded env names: {', '.join(value.get('env', [])) or 'none'}"),
                ("done", "Done"),
            ])
            choice = self.menu("MCP server", fields, initial_key=cursor)
            if not choice:
                return None
            cursor = choice
            if choice == "done":
                return value
            if choice == "type":
                selected = self.select_value("MCP transport", ["stdio", "http"], str(server_type), False)
                if selected:
                    value["type"] = selected
                    if selected == "stdio":
                        for key in ("url", "auth", "bearer_token_env_var", "oauth_resource", "oauth_scopes", "oauth_client_id", "oauth_client_id_env_var", "headers"):
                            value.pop(key, None)
                    else:
                        value.pop("command", None)
                continue
            if choice == "auth":
                selected = self.select_value("HTTP authentication", ["oauth"], str(value.get("auth", "")))
                if selected is not None:
                    if selected:
                        value["auth"] = selected
                        value.pop("bearer_token_env_var", None)
                    else:
                        value.pop("auth", None)
                continue
            if choice in ("oauth_scopes", "env"):
                raw = self.prompt(
                    choice.replace("_", " ").title(),
                    "Comma-separated values. Erase all text or enter '-' to clear.",
                    ", ".join(value.get(choice, [])),
                )
                if raw is not None:
                    items = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()]
                    if items: value[choice] = items
                    else: value.pop(choice, None)
                continue
            if choice == "headers":
                raw = self.prompt(
                    "HTTP headers",
                    "JSON object using ${ENV_VAR} for secrets. Erase all text or enter '-' to clear.",
                    json.dumps(value.get("headers", {})),
                )
                if raw is None:
                    continue
                if raw in ("", "-"):
                    value.pop("headers", None)
                else:
                    try:
                        headers = json.loads(raw)
                        if not isinstance(headers, dict):
                            raise ValueError
                        value["headers"] = headers
                    except (json.JSONDecodeError, ValueError):
                        self.message = "Headers must be a JSON object."
                continue
            raw = self.prompt(
                choice.replace("_", " ").title(),
                "Enter a value. Erase all text or enter '-' to clear.",
                str(value.get(choice, "")),
            )
            if raw is not None:
                if raw in ("", "-"): value.pop(choice, None)
                else: value[choice] = raw

    def edit_mcp_pack(self, current: dict[str, Any]) -> dict[str, Any] | None:
        value = copy.deepcopy(current)
        servers = [server for server in value.get("servers", []) if isinstance(server, dict)]
        cursor = "env"
        while True:
            options = [("env", f"Environment names: {', '.join(value.get('env', [])) or 'none'}")]
            options.extend((f"server:{index}", f"Server: {server.get('name', 'unnamed')} ({server.get('type', 'stdio')})") for index, server in enumerate(servers))
            options.extend([("new", "＋ Add server"), ("done", "Done")])
            choice = self.menu("Edit MCP pack", options, initial_key=cursor)
            if not choice:
                return None
            cursor = choice
            if choice == "done":
                if servers: value["servers"] = servers
                else: value.pop("servers", None)
                return value
            if choice == "env":
                raw = self.prompt(
                    "MCP pack environment",
                    "Comma-separated variable names. Erase all text or enter '-' to clear.",
                    ", ".join(value.get("env", [])),
                )
                if raw is not None:
                    names = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()]
                    if names: value["env"] = names
                    else: value.pop("env", None)
                continue
            if choice == "new":
                edited = self.edit_mcp_server({"type": "stdio"})
                if edited is not None:
                    servers.append(edited)
                    cursor = f"server:{len(servers) - 1}"
                continue
            index = int(choice.split(":", 1)[1])
            action = self.menu(str(servers[index].get("name", "unnamed")), [("edit", "Edit"), ("delete", "Delete")])
            if action == "edit":
                edited = self.edit_mcp_server(servers[index])
                if edited is not None:
                    servers[index] = edited
            elif action == "delete" and self.confirm("Delete MCP server", [str(servers[index].get("name", "unnamed"))]):
                del servers[index]
                if servers:
                    cursor = f"server:{min(index, len(servers) - 1)}"
                else:
                    cursor = "new"

    @staticmethod
    def _preset_summary(preset: dict[str, Any]) -> list[str]:
        sync = (
            "default" if "session_sync" not in preset
            else "on" if preset.get("session_sync") is True
            else "off"
        )
        target = preset.get("target", "container")
        target_label = execution_target_label(str(target))
        lines = [
            f"Tool: {preset.get('tool', 'codex')}  Execution: {target_label}",
            f"Codex profile: {preset.get('codex_profile', 'base config')}",
            f"Network: {preset.get('net', 'default')}",
            f"Yolo: {'on' if preset.get('yolo') is True else 'off'}  Claude history sync: {sync}",
            f"MCP packs: {', '.join(preset.get('mcp_packs', [])) or 'none'}",
            f"Skill packs: {', '.join(preset.get('skill_packs', [])) or 'none'}",
        ]
        if preset.get("aws_access") == "host-cli":
            lines.append(
                "AWS host CLI: enabled "
                f"(profile {preset.get('aws_profile', '(from auth)')}; bypasses Netgate)"
            )
        if preset.get("tool") == "opencode":
            lines.append(
                "OpenCode plugins: "
                + (
                    "enabled"
                    if preset.get("opencode_plugins") is True
                    else "pure/disabled"
                )
            )
        return lines

    def launch_actions(self, preset: dict[str, Any]) -> bool:
        choices = [
            ("once", "Launch once — discard these customizations after exit"),
            ("remember", "Remember for this exact project — save and launch"),
            ("save", "Save named reusable configuration — project mapping unchanged"),
        ]
        if hasattr(self.controller, "effective_exec_state"):
            target, _yolo, _net = self.controller.effective_exec_state(preset)
        else:
            target = str(preset.get("target", "container"))
        if target == "desktop":
            choices = [choice for choice in choices if choice[0] != "once"]
        choice = self.menu(
            "Use configuration",
            choices,
            details=self._preset_summary(preset),
            initial_key="remember",
        )
        if not choice:
            return False
        name = ""
        notices: list[str] = []
        operation: list[dict[str, Any]]
        if choice == "once":
            action = "Launch once. These customizations will not be saved."
            operation = [{"action": "upsert", "collection": "presets", "name": "__cage_preview", "value": preset}]
        elif choice == "remember":
            action = f"Save this configuration for the exact project {self.controller.repo} and launch it."
            operation = [{"action": "remember_project", "path": str(self.controller.repo), "value": preset}]
        else:
            name = self.prompt("Save configuration", "Reusable configuration name:")
            if not name:
                return False
            action = f"Save reusable configuration {name!r} and launch it. The project mapping will not change."
            if name in self.controller.data.get("presets", {}):
                notices.append(f"Existing reusable configuration {name!r} will be overwritten.")
            operation = [{"action": "upsert", "collection": "presets", "name": name, "value": preset}]
        try:
            self.controller.preview(operation)
            if not self.risk_review(
                preset,
                action=action,
                force_confirmation=True,
                notices=notices,
            ):
                self.message = "Review was not confirmed."
                return False
            if choice == "once":
                self.controller.write_result({"action": "launch_once", "preset": preset})
            elif choice == "remember":
                self.controller.commit(operation)
                name, _ = self.controller.effective_preset()
                self.controller.write_result({"action": "preset", "preset_name": name})
            else:
                self.controller.commit(operation)
                self.controller.write_result({"action": "preset", "preset_name": name})
            return True
        except UiError as exc:
            self.message = str(exc)
            return False

    def edit_generic(self, collection: str, current: dict[str, Any]) -> dict[str, Any] | None:
        value = dict(current)
        specs = FIELD_SPECS[collection]
        cursor = specs[0][0] if specs else "done"
        while True:
            options = [(key, f"{label}: {value.get(key, 'unset')}") for key, label, _ in specs]
            options.append(("done", "Done"))
            choice = self.menu(f"Edit {COLLECTION_LABELS[collection]}", options, initial_key=cursor)
            if not choice:
                return None
            cursor = choice
            if choice == "done":
                return value
            key, label, kind = next(spec for spec in specs if spec[0] == choice)
            if kind == "bool":
                value[key] = not bool(value.get(key, False))
                continue
            if kind in ("tool", "auth_mode", "aws_access"):
                values = (
                    ["codex", "claude", "opencode"]
                    if kind == "tool"
                    else ["bedrock", "api-key"]
                    if kind == "auth_mode"
                    else ["host-cli"]
                )
                selected = self.select_value(label, values, str(value.get(key, "")))
                if selected is not None:
                    if selected: value[key] = selected
                    else: value.pop(key, None)
                continue
            default = ", ".join(value.get(key, [])) if kind == "list" else str(value.get(key, ""))
            raw = self.prompt(label, label, default)
            if raw is None:
                continue
            if raw in ("", "-"):
                value.pop(key, None)
            elif kind == "list":
                value[key] = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                value[key] = raw

    def manage_collection(self, collection: str) -> None:
        cursor: str | None = None
        while True:
            table = self.controller.data.get(collection, {})
            names = sorted(name for name in table if not (collection == "presets" and name.startswith("__cage_project_")))
            options = [(name, name) for name in names] + [("__new", "＋ Create new")]
            choice = self.menu(COLLECTION_LABELS[collection], options, initial_key=cursor)
            if not choice:
                return
            cursor = choice
            existing = choice != "__new"
            name = choice if existing else self.prompt("Create", "Name:")
            if not name:
                continue
            if not existing and name in table:
                self.message = f"{collection}.{name} already exists; select it to edit."
                cursor = name
                continue
            action = "edit"
            if existing:
                action = self.menu(name, [("edit", "Edit"), ("rename", "Rename"), ("delete", "Delete")])
                if not action:
                    continue
            try:
                if action == "rename":
                    new_name = self.prompt("Rename", "New name:", name)
                    if new_name and new_name != name:
                        self.controller.commit([{"action": "rename", "collection": collection, "name": name, "new_name": new_name}])
                        cursor = new_name
                elif action == "delete":
                    refs = self.controller.snapshot.get("dependencies", {}).get(collection, {}).get(name, [])
                    if refs:
                        self.message = "Referenced by: " + ", ".join(refs)
                    elif self.confirm("Delete", [f"Delete {collection}.{name}?"]):
                        self.controller.commit([{"action": "delete", "collection": collection, "name": name}])
                        cursor = None
                else:
                    current = table.get(name, {}) if existing else {}
                    if collection == "presets":
                        edited = self.edit_preset(current)
                    elif collection == "mcp_packs":
                        edited = self.edit_mcp_pack(current)
                    else:
                        edited = self.edit_generic(collection, current)
                    if edited is None:
                        continue
                    risks = self.controller.object_risks(collection, edited)
                    if risks and not self.confirm("Review before save", [f"• {item}" for item in risks]):
                        self.message = "Risk review was not confirmed."
                        continue
                    self.controller.commit([{"action": "upsert", "collection": collection, "name": name, "value": edited}])
            except UiError as exc:
                self.message = str(exc)

    def manage(self) -> None:
        cursor: str | None = None
        while True:
            options = [(name, label) for name, label in COLLECTION_LABELS.items()]
            options += [
                ("defaults", "Launch defaults"),
                ("storage", "Docker storage guardrails"),
                ("project", "Project mappings"),
                ("oauth", "Codex MCP OAuth login/logout"),
            ]
            choice = self.menu("Manage configuration", options, initial_key=cursor)
            if not choice:
                return
            cursor = choice
            if choice in COLLECTION_LABELS:
                self.manage_collection(choice)
            elif choice == "defaults":
                current = dict(self.controller.data.get("defaults", {}))
                action = self.menu("Launch defaults", [
                    ("preset", f"Configuration: {self.controller.data.get('default_preset', 'unset')}"),
                    ("net", f"Network: {current.get('net', 'automatic')}"),
                    ("sync", f"Claude history sync: {current.get('session_sync', True)}"),
                ])
                try:
                    if action == "preset":
                        names = sorted(name for name in self.controller.data.get("presets", {}) if not name.startswith("__cage_project_"))
                        selected = self.select_value("Default configuration", names, str(self.controller.data.get("default_preset", "")), False)
                        if selected: self.controller.commit([{"action": "set_default", "name": selected}])
                    elif action == "net":
                        selected = self.select_value("Default network", ["gate", "open", "off"], str(current.get("net", "")))
                        if selected is not None:
                            if selected: current["net"] = selected
                            else: current.pop("net", None)
                            if selected == "open" and not self.confirm("Review before save", ["• New configurations default to unrestricted network access."]):
                                continue
                            self.controller.commit([{"action": "update_defaults", "value": current}])
                    elif action == "sync":
                        current["session_sync"] = not bool(current.get("session_sync", True))
                        self.controller.commit([{"action": "update_defaults", "value": current}])
                except UiError as exc: self.message = str(exc)
            elif choice == "storage":
                current = dict(self.controller.snapshot.get("storage", {}))
                fields = [
                    ("warn_free_gib", "Warning free space (GiB)"),
                    ("critical_free_gib", "Critical free space (GiB)"),
                    ("min_build_free_gib", "Build free-space floor (GiB)"),
                    ("keep_versions", "Retained versions per role"),
                    ("dangling_min_age_hours", "Dangling-image minimum age (hours)"),
                ]
                action = self.menu(
                    "Docker storage guardrails",
                    [
                        (name, f"{label}: {current.get(name)}")
                        for name, label in fields
                    ],
                    [
                        "Cleanup is always previewed and confirmation-gated.",
                        "Volumes, containers, referenced images, unrelated images,",
                        "and custom derived images are never cleanup candidates.",
                    ],
                )
                if action:
                    label = dict(fields)[action]
                    entered = self.prompt(
                        "Docker storage guardrails",
                        f"{label}:",
                        str(current.get(action, "")),
                    )
                    if entered is not None:
                        try:
                            current[action] = int(entered)
                            self.controller.commit([
                                {"action": "update_storage", "value": current}
                            ])
                        except ValueError:
                            self.message = f"{label} must be an integer."
                        except UiError as exc:
                            self.message = str(exc)
            elif choice == "project":
                projects = self.controller.data.get("projects", {})
                details = [f"{path} → {preset}" for path, preset in sorted(projects.items())] or ["No project mappings."]
                action = self.menu(
                    "Project mappings",
                    [("set", "Set mapping"), ("remove", "Remove mapping")],
                    details,
                    initial_key="set",
                )
                try:
                    if action == "set":
                        path = self.prompt("Project mapping", "Absolute project path:", str(self.controller.repo))
                        if not path:
                            continue
                        names = sorted(self.controller.data.get("presets", {}))
                        selected = self.select_value("Project configuration", names, str(projects.get(path, "")), False)
                        if selected: self.controller.commit([{"action": "set_project", "path": path, "name": selected}])
                    elif action == "remove":
                        paths = sorted(projects)
                        selected = self.select_value("Remove project mapping", paths, str(self.controller.repo) if str(self.controller.repo) in projects else "", False)
                        if selected and self.confirm("Remove mapping", [selected]):
                            self.controller.commit([{"action": "remove_project", "path": selected}])
                except UiError as exc: self.message = str(exc)
            else:
                targets = self.controller.oauth_targets()
                target_options = [
                    (str(index), f"{server} via {preset} ({url})")
                    for index, (preset, server, url) in enumerate(targets)
                ]
                selected = self.menu("Codex MCP OAuth", target_options)
                if not selected:
                    continue
                preset, server, url = targets[int(selected)]
                action = self.menu(server, [("login", "Log in or refresh"), ("logout", "Log out")])
                if not action:
                    continue
                if not self.confirm("OAuth action", [
                    f"{action.title()} {server} for preset {preset}",
                    f"Provider: {url}",
                    "This opens the provider flow on the host and updates the selected Codex auth directory.",
                ]):
                    continue
                import curses
                curses.endwin()
                status = self.controller.run_oauth_action(action, preset, server)
                self.screen.refresh()
                self.message = "OAuth action completed." if status == 0 else "OAuth action failed; see terminal output."

    @staticmethod
    def _desktop_target_details(target: dict[str, Any]) -> list[str]:
        details = [
            f"Status: {str(target['status']).upper()}",
            f"Host: {target['alias']}",
            f"Repository: {target['repo']}",
            f"Configuration: {target['preset']}",
            f"Persistent volume: {target['volume_name']}",
        ]
        if target.get("container_id"):
            details.append(f"Container: {str(target['container_id'])[:12]}")
        if "exit_code" in target and target.get("exit_code") is not None:
            details.append(f"Last exit code: {target['exit_code']}")
        details += [
            "",
            "Stop disconnects ChatGPT but preserves this target's history and SSH identity.",
            "Remove permanently deletes its Desktop history, keys, registration, and volume.",
        ]
        return details

    def _perform_desktop_action(
        self,
        action: str,
        target: dict[str, Any] | None,
        *,
        assume_yes: bool = False,
    ) -> tuple[int, str]:
        label = {
            "setup": "Setting up Desktop connections…",
            "start": "Starting or recovering the Desktop target…",
            "restart": "Restarting the Desktop target…",
            "stop": "Stopping the Desktop target…",
            "logs": "Reading Desktop target logs…",
            "remove": "Removing the Desktop target…",
        }[action]
        self.message = ""
        self._draw(
            "Desktop targets",
            [label, "This may take a moment."],
            "Please wait",
        )
        try:
            return self.controller.run_desktop_action(
                action,
                target,
                assume_yes=assume_yes,
            )
        except (UiError, OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)

    def manage_desktop_target(self, target: dict[str, Any]) -> None:
        status = str(target["status"])
        options: list[tuple[str, str]] = []
        if status in {"stopped", "stale", "failed", "unknown"}:
            options.append(("start", "Start / recover and open ChatGPT"))
        elif status == "ready":
            options.extend([
                ("start", "Open ChatGPT (reuse ready target)"),
                ("restart", "Restart target"),
                ("stop", "Stop target (preserve history)"),
            ])
        elif status == "starting":
            options.append(("stop", "Stop target (preserve history)"))
        options += [
            ("refresh", "Refresh status"),
            ("logs", "View recent logs"),
            ("remove", "Remove target and delete Desktop history"),
        ]
        action = self.menu(
            str(target["alias"]),
            options,
            self._desktop_target_details(target),
        )
        if not action or action == "refresh":
            return
        if action == "remove":
            alias = str(target["alias"])
            if not self.confirm(
                "Remove Desktop target",
                [
                    f"Target: {alias}",
                    f"Repository: {target['repo']}",
                    f"Configuration: {target['preset']}",
                    f"Volume: {target['volume_name']}",
                    "",
                    "This permanently deletes this target's Desktop history, SSH keys, "
                    "registration, and persistent volume. It cannot be undone by Cage.",
                ],
                phrase=alias,
                case_sensitive=True,
            ):
                self.message = "Desktop target removal was cancelled."
                return
            code, output = self._perform_desktop_action(
                "remove",
                target,
                assume_yes=True,
            )
        else:
            code, output = self._perform_desktop_action(action, target)
        if action == "logs" or code != 0 or action in {"start", "restart"}:
            title = (
                "Desktop target logs"
                if action == "logs"
                else "Desktop action completed"
                if code == 0
                else "Desktop action failed"
            )
            self.show_text(title, output.splitlines() or ["(no output)"])
        self.message = (
            f"Desktop {action} completed."
            if code == 0
            else f"Desktop {action} failed. Open the target again to inspect its status or logs."
        )

    def manage_desktop_targets(self) -> None:
        cursor: str | None = None
        while True:
            try:
                targets = self.controller.desktop_targets()
            except (UiError, OSError, subprocess.SubprocessError) as exc:
                choice = self.menu(
                    "Desktop targets",
                    [("refresh", "Try again")],
                    [
                        "Cage could not read the registered Desktop targets.",
                        str(exc),
                    ],
                )
                if choice != "refresh":
                    return
                continue
            options = [
                (
                    str(target["target_id"]),
                    f"{str(target['status']).upper():8}  {target['alias']}  "
                    f"— {target['preset']} — {Path(str(target['repo'])).name}",
                )
                for target in targets
            ]
            options += [
                ("__refresh", "Refresh all statuses"),
                ("__setup", "Set up ChatGPT Desktop connections"),
            ]
            details = [
                "Registered Desktop targets are shown independently of the configuration "
                "selected for this folder.",
                "Choose a target to start, recover, restart, inspect, stop, or remove it.",
            ]
            if not targets:
                details += [
                    "",
                    "No Desktop targets are registered yet. Save a Codex configuration, "
                    "choose Desktop via Cage container, and launch it once to create one.",
                ]
            selected = self.menu(
                "Desktop targets",
                options,
                details,
                initial_key=cursor,
            )
            if not selected:
                return
            cursor = selected
            if selected == "__refresh":
                self.message = "Desktop target statuses refreshed."
                continue
            if selected == "__setup":
                if not self.confirm(
                    "Set up Desktop connections",
                    [
                        "Cage will add one managed Include to ~/.ssh/config and preserve "
                        "the file's comments, permissions, and symlink target.",
                        "Cage owns only the included Desktop host blocks.",
                    ],
                ):
                    continue
                code, output = self._perform_desktop_action("setup", None)
                if code != 0:
                    self.show_text(
                        "Desktop setup failed",
                        output.splitlines() or ["(no output)"],
                    )
                self.message = (
                    "Desktop connection setup completed."
                    if code == 0
                    else "Desktop connection setup failed."
                )
                continue
            target = next(
                (item for item in targets if item["target_id"] == selected),
                None,
            )
            if target is None:
                self.message = "That Desktop target no longer exists."
                continue
            self.manage_desktop_target(target)

    def run(self) -> int:
        cursor: str | None = None
        while True:
            effective = self.controller.snapshot.get("effective", {})
            if "error" in effective:
                details = ["Configuration needs attention:", str(effective["error"])]
            else:
                _, effective_value = self.controller.effective_preset()
                # The backend supplies saved effective state. Apply command
                # overrides through the same local helper used by risk review
                # and preflight so the reviewed launch matches the shell.
                shown_target, shown_yolo, shown_net = self.controller.effective_exec_state(
                    effective_value
                )
                target_label = execution_target_label(shown_target)
                if self.controller.target_override:
                    target_label += " (command override)"
                details = [
                    f"Project: {self.controller.repo}",
                    f"Tool: {effective.get('tool')}  Execution: {target_label}",
                    f"Codex profile: {effective.get('codex_profile') or 'base config'}",
                    f"Auth: {effective.get('auth') or 'default'}  Identity: {effective.get('identity') or 'none'}",
                    f"Network: {shown_net}"
                    + (" (command override)" if self.controller.net_override else ""),
                    f"MCP: {', '.join(effective.get('mcp_packs', [])) or 'none'}",
                    f"Skills: {', '.join(effective.get('skill_packs', [])) or 'none'}",
                    f"History sync: {'on' if effective.get('session_sync') else 'off'}  "
                    f"Yolo: {'on' if shown_yolo else 'off'}"
                    + (" (command override)" if self.controller.yolo_override else ""),
                ]
                if self.controller.tool_override and effective.get("tool") != self.controller.tool_override:
                    details.append(
                        f"Command requires {self.controller.tool_override}; customize or choose a matching saved configuration."
                    )
            options = []
            if "error" not in effective and (
                not self.controller.tool_override or effective.get("tool") == self.controller.tool_override
            ):
                options.append(("launch", "Launch with this configuration"))
            options.append(("custom", "Customize launch"))
            if sys.platform == "darwin":
                options.append(("desktop-targets", "Manage Desktop targets"))
            options += [("manage", "Manage saved configuration"), ("quit", "Quit without launching")]
            choice = self.menu("Launch", options, details, initial_key=cursor)
            if choice in ("", "quit"):
                return 1
            cursor = choice
            if choice == "manage":
                self.manage()
            elif choice == "desktop-targets":
                self.manage_desktop_targets()
            elif choice == "custom":
                _, seed = self.controller.effective_preset()
                edited = self.edit_preset(seed or {"tool": "codex"})
                if edited is not None and self.launch_actions(edited):
                    return 0
            else:
                name, preset = self.controller.effective_preset()
                if self.controller.target_override:
                    preset = dict(preset)
                    if self.controller.target_override == "container":
                        preset.pop("target", None)
                    else:
                        preset["target"] = self.controller.target_override
                if self.risk_review(preset, action=f"Launch saved configuration {name!r}."):
                    if self.controller.target_override and self.controller.target_override != "desktop":
                        # Write a launch-once result with the overridden target
                        self.controller.write_result({"action": "launch_once", "preset": preset})
                    else:
                        self.controller.write_result({"action": "preset", "preset_name": name})
                    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cage")
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--net-override", choices=["open", "gate", "off"], default="")
    parser.add_argument("--yolo-override", choices=["on", "off"], default="")
    parser.add_argument(
        "--tool-override", choices=["codex", "claude", "opencode"], default=""
    )
    parser.add_argument("--target-override", choices=["container", "desktop", "host"], default="")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("curses UI requires a terminal", file=sys.stderr)
        return 70
    try:
        import curses
    except ImportError:
        return 70
    try:
        controller = Controller(
            args.backend, args.config, args.repo, args.result,
            net_override=args.net_override, yolo_override=args.yolo_override,
            tool_override=args.tool_override, target_override=args.target_override,
        )
        return curses.wrapper(lambda screen: CursesView(screen, controller).run())
    except UiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except curses.error:
        return 70
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
