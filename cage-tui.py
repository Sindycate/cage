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
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
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
        ("aws_profile", "AWS profile", "text"), ("aws_region", "AWS region", "text"),
        ("host_codex_dir", "Host Codex directory", "text"),
        ("host_agents_dir", "Host agents directory", "text"),
        ("copy_auth", "Copy main Codex login", "bool"),
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


class UiError(Exception):
    pass


class Controller:
    def __init__(
        self, backend: Path, config: Path, repo: Path, result: Path,
        net_override: str = "", yolo_override: str = "", tool_override: str = "",
    ):
        self.backend = backend
        self.config = config
        self.repo = repo.resolve()
        self.result = result
        self.net_override = net_override
        self.yolo_override = yolo_override
        self.tool_override = tool_override
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
            if tool != "codex":
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

    def effective_preset(self) -> tuple[str, dict[str, Any]]:
        effective = self.snapshot.get("effective", {})
        name = effective.get("preset", "") if isinstance(effective, dict) else ""
        preset = self.data.get("presets", {}).get(name, {})
        return (name, dict(preset)) if isinstance(preset, dict) else ("", {})

    def risks(self, preset: dict[str, Any]) -> list[str]:
        risks: list[str] = []
        yolo = self.yolo_override == "on" or (
            not self.yolo_override and preset.get("yolo") is True
        )
        if yolo:
            risks.append("Coding-tool permission prompts are disabled (yolo).")
        net = self.net_override or preset.get("net") or self.data.get("defaults", {}).get("net") or ("gate" if yolo else "open")
        if net == "open":
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
        if collection == "auth" and value.get("copy_auth") is True:
            return ["This profile copies the main Codex login into project containers."]
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
        if shutil.which("docker") is None:
            warnings.append("Docker is not available in PATH.")
        required: set[str] = set(preset.get("env", []))
        auth = self.data.get("auth", {}).get(preset.get("auth", ""), {})
        if isinstance(auth, dict):
            required.update(auth.get("env", []))
            for key in ("host_codex_dir", "host_agents_dir"):
                raw_path = auth.get(key)
                if raw_path and not Path(str(raw_path)).expanduser().is_dir():
                    warnings.append(f"{key} does not exist: {raw_path}")
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
        self.screen = screen
        self.controller = controller
        self.message = ""
        self.screen.keypad(True)

    def _draw(self, title: str, lines: list[str], footer: str = "") -> None:
        import curses
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.screen.addnstr(0, 0, f" Cage — {title} ", max(1, width - 1), curses.A_REVERSE)
        for row, line in enumerate(lines[: max(0, height - 3)], start=1):
            self.screen.addnstr(row, 1, line, max(1, width - 2))
        status = self.message or footer
        if status:
            self.screen.addnstr(height - 1, 0, status, max(1, width - 1), curses.A_REVERSE)
        self.screen.refresh()

    def menu(self, title: str, options: list[tuple[str, str]], details: list[str] | None = None) -> str:
        import curses
        if not options:
            self.message = f"No entries are available for {title}."
            return ""
        index = 0
        while True:
            lines = list(details or [])
            if lines:
                lines.append("")
            start = len(lines)
            lines.extend(f"  {label}" for _, label in options)
            self._draw(title, lines, "↑/↓ move • Enter select • q back")
            for offset in range(len(options)):
                attr = curses.A_REVERSE if offset == index else curses.A_NORMAL
                row = start + offset + 1
                if row < self.screen.getmaxyx()[0] - 1:
                    self.screen.addnstr(row, 1, f"  {options[offset][1]}", self.screen.getmaxyx()[1] - 2, attr)
            key = self.screen.getch()
            self.message = ""
            if key in (curses.KEY_UP, ord("k")):
                index = (index - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = (index + 1) % len(options)
            elif key in (10, 13, curses.KEY_ENTER):
                return options[index][0]
            elif key in (27, ord("q")):
                return ""

    def prompt(self, title: str, label: str, default: str = "") -> str | None:
        import curses
        self._draw(title, [label, "", f"Current: {default}"], "Enter saves • Esc cancels")
        height, width = self.screen.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        try:
            self.screen.move(min(4, height - 2), 1)
            self.screen.clrtoeol()
            raw = self.screen.getstr(min(4, height - 2), 1, max(1, width - 2))
        except (KeyboardInterrupt, curses.error):
            return None
        finally:
            curses.noecho()
            curses.curs_set(0)
        entered = raw.decode("utf-8").strip()
        return entered if entered else default

    def confirm(self, title: str, lines: list[str], phrase: str = "yes") -> bool:
        answer = self.prompt(title, "Type 'yes' to continue:", "\n".join(lines))
        return answer == phrase

    def choose_names(self, title: str, names: list[str], selected: list[str]) -> list[str] | None:
        chosen = set(selected)
        options = [(name, ("[x] " if name in chosen else "[ ] ") + name) for name in names]
        options.append(("__done", "Done"))
        while True:
            choice = self.menu(title, options)
            if not choice:
                return None
            if choice == "__done":
                return [name for name in names if name in chosen]
            chosen.symmetric_difference_update({choice})
            options = [(name, ("[x] " if name in chosen else "[ ] ") + name) for name in names]
            options.append(("__done", "Done"))

    def select_value(self, title: str, values: list[str], current: str, allow_none: bool = True) -> str | None:
        options = []
        if allow_none:
            options.append(("__none", "none" + (" (current)" if not current else "")))
        options.extend((value, value + (" (current)" if value == current else "")) for value in values)
        choice = self.menu(title, options)
        if not choice:
            return None
        return "" if choice == "__none" else choice

    def edit_preset(self, preset: dict[str, Any]) -> dict[str, Any] | None:
        value = dict(preset)
        value["tool"] = self.controller.tool_override or value.get("tool", "codex")
        if value["tool"] == "claude":
            value.pop("skill_packs", None)
        while True:
            rows = [
                ("tool", f"Tool: {value.get('tool', 'codex')}"),
                ("auth", f"Auth: {value.get('auth', 'none')}"),
                ("identity", f"Identity: {value.get('identity', 'none')}"),
                ("mcp", f"MCP packs: {', '.join(value.get('mcp_packs', [])) or 'none'}"),
                ("skills", f"Skill packs: {', '.join(value.get('skill_packs', [])) or 'none'}"),
                ("commands", f"Host commands: {', '.join(value.get('host_commands', [])) or 'none'}"),
                ("env", f"Extra environment names: {', '.join(value.get('env', [])) or 'none'}"),
                ("net", f"Network: {value.get('net', 'default')}"),
                ("yolo", f"Yolo: {'on' if value.get('yolo') else 'off'}"),
                ("sync", f"Claude history sync: {value.get('session_sync', 'default')}"),
                ("mounts", f"Extra mounts: {len(value.get('extra_mounts', []))}"),
                ("done", "Done"),
            ]
            choice = self.menu("Customize launch", rows)
            if not choice:
                return None
            if choice == "done":
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
                selected = self.select_value("Tool", ["codex", "claude"], str(value.get("tool", "codex")), False)
                if selected:
                    value["tool"] = selected
                    if selected == "claude":
                        value.pop("skill_packs", None)
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
            elif choice == "net":
                selected = self.select_value("Network", ["gate", "open", "off"], str(value.get("net", "")))
                if selected is not None:
                    if selected: value["net"] = selected
                    else: value.pop("net", None)
            elif choice in ("yolo", "sync"):
                key = "yolo" if choice == "yolo" else "session_sync"
                value[key] = not bool(value.get(key, False))
            elif choice == "mounts":
                raw = self.prompt("Extra mounts", "Comma-separated paths. Prefix a read-write path with rw=", ", ".join(
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
                raw = self.prompt("Extra environment", "Comma-separated environment variable names", ", ".join(value.get("env", [])))
                if raw is not None:
                    names = [part.strip() for part in raw.split(",") if part.strip()]
                    if names: value["env"] = names
                    else: value.pop("env", None)

    def risk_review(self, preset: dict[str, Any]) -> bool:
        risks = self.controller.risks(preset)
        warnings = self.controller.preflight(preset)
        if not risks and not warnings:
            return True
        lines = ["Risk review"] + [f"• {item}" for item in risks]
        if warnings:
            lines += ["", "Preflight warnings"] + [f"• {item}" for item in warnings]
        return self.confirm("Review before launch/save", lines)

    def edit_mcp_server(self, current: dict[str, Any]) -> dict[str, Any] | None:
        value = copy.deepcopy(current)
        value.setdefault("type", "stdio")
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
            choice = self.menu("MCP server", fields)
            if not choice:
                return None
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
                raw = self.prompt(choice.replace("_", " ").title(), "Comma-separated values; '-' clears", ", ".join(value.get(choice, [])))
                if raw is not None:
                    items = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()]
                    if items: value[choice] = items
                    else: value.pop(choice, None)
                continue
            if choice == "headers":
                raw = self.prompt("HTTP headers", "JSON object using ${ENV_VAR} for secrets; '-' clears", json.dumps(value.get("headers", {})))
                if raw is None:
                    continue
                if raw == "-":
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
            raw = self.prompt(choice.replace("_", " ").title(), "Enter a value; '-' clears", str(value.get(choice, "")))
            if raw is not None:
                if raw == "-": value.pop(choice, None)
                else: value[choice] = raw

    def edit_mcp_pack(self, current: dict[str, Any]) -> dict[str, Any] | None:
        value = copy.deepcopy(current)
        servers = [server for server in value.get("servers", []) if isinstance(server, dict)]
        while True:
            options = [("env", f"Environment names: {', '.join(value.get('env', [])) or 'none'}")]
            options.extend((f"server:{index}", f"Server: {server.get('name', 'unnamed')} ({server.get('type', 'stdio')})") for index, server in enumerate(servers))
            options.extend([("new", "＋ Add server"), ("done", "Done")])
            choice = self.menu("Edit MCP pack", options)
            if not choice:
                return None
            if choice == "done":
                if servers: value["servers"] = servers
                else: value.pop("servers", None)
                return value
            if choice == "env":
                raw = self.prompt("MCP pack environment", "Comma-separated variable names; '-' clears", ", ".join(value.get("env", [])))
                if raw is not None:
                    names = [] if raw == "-" else [part.strip() for part in raw.split(",") if part.strip()]
                    if names: value["env"] = names
                    else: value.pop("env", None)
                continue
            if choice == "new":
                edited = self.edit_mcp_server({"type": "stdio"})
                if edited is not None:
                    servers.append(edited)
                continue
            index = int(choice.split(":", 1)[1])
            action = self.menu(str(servers[index].get("name", "unnamed")), [("edit", "Edit"), ("delete", "Delete")])
            if action == "edit":
                edited = self.edit_mcp_server(servers[index])
                if edited is not None:
                    servers[index] = edited
            elif action == "delete" and self.confirm("Delete MCP server", [str(servers[index].get("name", "unnamed"))]):
                del servers[index]

    def launch_actions(self, preset: dict[str, Any]) -> bool:
        choice = self.menu("Use configuration", [
            ("once", "Launch once (do not save)"),
            ("remember", "Remember for this project"),
            ("save", "Save as reusable configuration"),
        ])
        if not choice:
            return False
        if not self.risk_review(preset):
            self.message = "Risk review was not confirmed."
            return False
        try:
            if choice == "once":
                self.controller.write_result({"action": "launch_once", "preset": preset})
            elif choice == "remember":
                self.controller.commit([{"action": "remember_project", "path": str(self.controller.repo), "value": preset}])
                name, _ = self.controller.effective_preset()
                self.controller.write_result({"action": "preset", "preset_name": name})
            else:
                name = self.prompt("Save configuration", "Reusable configuration name:")
                if not name:
                    return False
                self.controller.commit([{"action": "upsert", "collection": "presets", "name": name, "value": preset}])
                self.controller.write_result({"action": "preset", "preset_name": name})
            return True
        except UiError as exc:
            self.message = str(exc)
            return False

    def edit_generic(self, collection: str, current: dict[str, Any]) -> dict[str, Any] | None:
        value = dict(current)
        specs = FIELD_SPECS[collection]
        while True:
            options = [(key, f"{label}: {value.get(key, 'unset')}") for key, label, _ in specs]
            options.append(("done", "Done"))
            choice = self.menu(f"Edit {COLLECTION_LABELS[collection]}", options)
            if not choice:
                return None
            if choice == "done":
                return value
            key, label, kind = next(spec for spec in specs if spec[0] == choice)
            if kind == "bool":
                value[key] = not bool(value.get(key, False))
                continue
            if kind in ("tool", "auth_mode"):
                values = ["codex", "claude"] if kind == "tool" else ["bedrock", "api-key"]
                selected = self.select_value(label, values, str(value.get(key, "")))
                if selected is not None:
                    if selected: value[key] = selected
                    else: value.pop(key, None)
                continue
            default = ", ".join(value.get(key, [])) if kind == "list" else str(value.get(key, ""))
            raw = self.prompt(label, label, default)
            if raw is None:
                continue
            if raw == "-":
                value.pop(key, None)
            elif kind == "list":
                value[key] = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                value[key] = raw

    def manage_collection(self, collection: str) -> None:
        while True:
            table = self.controller.data.get(collection, {})
            names = sorted(name for name in table if not (collection == "presets" and name.startswith("__cage_project_")))
            options = [(name, name) for name in names] + [("__new", "＋ Create new")]
            choice = self.menu(COLLECTION_LABELS[collection], options)
            if not choice:
                return
            existing = choice != "__new"
            name = choice if existing else self.prompt("Create", "Name:")
            if not name:
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
                elif action == "delete":
                    refs = self.controller.snapshot.get("dependencies", {}).get(collection, {}).get(name, [])
                    if refs:
                        self.message = "Referenced by: " + ", ".join(refs)
                    elif self.confirm("Delete", [f"Delete {collection}.{name}?"]):
                        self.controller.commit([{"action": "delete", "collection": collection, "name": name}])
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
        while True:
            options = [(name, label) for name, label in COLLECTION_LABELS.items()]
            options += [
                ("defaults", "Launch defaults"),
                ("project", "Project mappings"),
                ("oauth", "Codex MCP OAuth login/logout"),
            ]
            choice = self.menu("Manage configuration", options)
            if not choice:
                return
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
            elif choice == "project":
                projects = self.controller.data.get("projects", {})
                details = [f"{path} → {preset}" for path, preset in sorted(projects.items())] or ["No project mappings."]
                action = self.menu("Project mappings", [("set", "Set mapping"), ("remove", "Remove mapping")], details)
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

    def run(self) -> int:
        while True:
            effective = self.controller.snapshot.get("effective", {})
            if "error" in effective:
                details = ["Configuration needs attention:", str(effective["error"])]
            else:
                shown_yolo = (
                    self.controller.yolo_override == "on"
                    or (not self.controller.yolo_override and effective.get("yolo"))
                )
                _, effective_value = self.controller.effective_preset()
                shown_net = (
                    self.controller.net_override
                    or effective_value.get("net")
                    or self.controller.data.get("defaults", {}).get("net")
                    or ("gate" if shown_yolo else "open")
                )
                details = [
                    f"Project: {self.controller.repo}",
                    f"Tool: {effective.get('tool')}  Auth: {effective.get('auth') or 'default'}",
                    f"Identity: {effective.get('identity') or 'none'}  Network: {shown_net}",
                    f"MCP: {', '.join(effective.get('mcp_packs', [])) or 'none'}",
                    f"Skills: {', '.join(effective.get('skill_packs', [])) or 'none'}",
                    f"History sync: {'on' if effective.get('session_sync') else 'off'}  Yolo: {'on' if shown_yolo else 'off'}",
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
            options += [("custom", "Customize launch"), ("manage", "Manage saved configuration"), ("quit", "Quit without launching")]
            choice = self.menu("Launch", options, details)
            if choice in ("", "quit"):
                return 1
            if choice == "manage":
                self.manage()
            elif choice == "custom":
                _, seed = self.controller.effective_preset()
                edited = self.edit_preset(seed or {"tool": "codex"})
                if edited is not None and self.launch_actions(edited):
                    return 0
            else:
                name, preset = self.controller.effective_preset()
                if self.risk_review(preset):
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
    parser.add_argument("--tool-override", choices=["codex", "claude"], default="")
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
            tool_override=args.tool_override,
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
