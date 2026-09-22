"""Command-line parsing and thin configuration command handlers."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from .codex import (
    host_codex_arg_lines,
    reject_unsafe_codex_passthrough_args,
    validate_codex_layers,
)
from .diagnostics import (
    emit_resolved_json,
    explain,
)
from .oauth import (
    command_mcp_login,
    command_mcp_logout,
)
from .rendering import replace_projects_section
from .resolution import resolve_config
from .schema import (
    ConfigError,
    VALID_TOOLS,
    as_table,
)
from .selection import normalize_project_path
from .storage import (
    atomic_write_text,
    default_config_path,
    load_config,
)
from .ui import (
    command_ui_commit,
    command_ui_export,
    command_ui_preview,
    command_ui_resolve_json,
)


def command_host_codex_args(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid host Codex configuration payload: {exc}") from exc
    for line in host_codex_arg_lines(
        payload,
        Path(normalize_project_path(args.repo)),
        Path(args.codex_home).expanduser(),
    ):
        print(line)
    return 0


def command_validate_codex_argv(args: argparse.Namespace) -> int:
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    reject_unsafe_codex_passthrough_args(argv)
    return 0


def command_validate_codex_layers(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid Codex configuration payload: {exc}") from exc
    validate_codex_layers(
        payload,
        Path(normalize_project_path(args.repo)),
        Path(args.codex_home).expanduser(),
    )
    return 0


def command_resolve_json(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(
        data, args.config, args.repo, args.preset or "", args.tool or "",
        mcp_inventory=True,
    )
    emit_resolved_json(resolved)
    return 0


def command_explain(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(
        data, args.config, args.repo, args.preset or "", args.tool or "",
        mcp_inventory=True,
    )
    return explain(resolved, doctor=False)


def command_doctor(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(
        data, args.config, args.repo, args.preset or "", args.tool or "",
        mcp_inventory=True,
    )
    return explain(resolved, doctor=True)


def command_default_tool(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    preset_name = data.get("default_preset") or as_table(data, "defaults").get("default_preset")
    if not isinstance(preset_name, str) or not preset_name:
        print("claude")
        return 0
    preset = as_table(data, "presets").get(preset_name, {})
    if isinstance(preset, dict):
        tool = preset.get("tool")
        auth_name = preset.get("auth")
        if not tool and isinstance(auth_name, str):
            auth = as_table(data, "auth").get(auth_name, {})
            if isinstance(auth, dict):
                tool = auth.get("tool")
        if tool in VALID_TOOLS:
            print(tool)
            return 0
    print("claude")
    return 0


def command_list(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    default = data.get("default_preset") or as_table(data, "defaults").get("default_preset") or ""
    presets = as_table(data, "presets")
    projects = as_table(data, "projects")
    print(f"Config: {args.config}")
    print("Presets:")
    if presets:
        for name in sorted(presets):
            marker = " (default)" if name == default else ""
            tool = presets[name].get("tool", "") if isinstance(presets[name], dict) else ""
            suffix = f" [{tool}]" if tool else ""
            print(f"  - {name}{suffix}{marker}")
    else:
        print("  (none)")
    if projects:
        print("Projects:")
        for path, preset in sorted(projects.items()):
            print(f"  - {path} -> {preset}")
    return 0


def command_show(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    presets = as_table(data, "presets")
    preset = presets.get(args.preset)
    if not isinstance(preset, dict):
        raise ConfigError(f"preset not found: {args.preset}")
    print(f"Preset: {args.preset}")
    print(json.dumps(preset, indent=2, sort_keys=True))
    return 0


SAMPLE_CONFIG = """# cage central configuration
# This starter is intentionally minimal and uses existing host Codex state.
# Add identities, MCP packs, skill packs, host commands, and extra mounts only
# after the base launch works. See README.md for advanced examples.
version = 1
default_preset = "codex-local"

[defaults]
net = "gate"
session_sync = true

[storage]
warn_free_gib = 20
critical_free_gib = 5
min_build_free_gib = 20
keep_versions = 2
dangling_min_age_hours = 24
ephemeral_min_age_hours = 168

[auth.codex-local]
tool = "codex"
host_codex_dir = "~/.codex"
host_agents_dir = "~/.agents"
copy_auth = true

[presets.codex-local]
tool = "codex"
auth = "codex-local"
net = "gate"

# Optional profile-pinned AWS CLI relay. The host AWS CLI and browser/SSO state
# stay on the host; use one preset per AWS profile and relaunch to switch.
# [presets.aws-staging-readonly]
# tool = "codex"
# aws_profile = "aws-staging.ReadOnly"
# aws_access = "host-cli"
# net = "gate"

[projects]
# "/Users/me/code/project-a" = "codex-local"
"""


def command_init(args: argparse.Namespace) -> int:
    if args.config.exists() and not args.force:
        print(f"Config already exists: {args.config}")
        print("Use --force to overwrite, or 'cage config edit'.")
        return 0
    args.config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.config, SAMPLE_CONFIG)
    print(f"Wrote {args.config}")
    return 0


def command_edit(args: argparse.Namespace) -> int:
    if not args.config.exists():
        command_init(argparse.Namespace(config=args.config, force=False))
    editor = os.environ.get("EDITOR") or "vi"
    try:
        editor_args = shlex.split(editor)
    except ValueError as exc:
        raise ConfigError(f"invalid EDITOR value: {exc}") from exc
    if not editor_args:
        editor_args = ["vi"]
    return subprocess.call(editor_args + [str(args.config)])


def command_set_project(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    presets = as_table(data, "presets")
    if args.preset not in presets:
        raise ConfigError(f"preset not found: {args.preset}")
    projects = {str(k): str(v) for k, v in as_table(data, "projects").items()}
    project_path = normalize_project_path(args.path)
    projects[project_path] = args.preset
    text = args.config.read_text(encoding="utf-8")
    updated = replace_projects_section(text, projects)
    # Validate the complete result before replacing the source file.
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write invalid TOML: {exc}") from exc
    atomic_write_text(args.config, updated)
    print(f"Set {project_path} -> {args.preset}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cage config")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to central cage config.toml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve-json", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--preset")
    p.add_argument("--tool", choices=sorted(VALID_TOOLS))
    p.set_defaults(func=command_resolve_json)

    p = sub.add_parser("default-tool", help=argparse.SUPPRESS)
    p.set_defaults(func=command_default_tool)

    p = sub.add_parser("host-codex-args", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--payload", required=True)
    p.set_defaults(func=command_host_codex_args)

    p = sub.add_parser("validate-codex-argv", help=argparse.SUPPRESS)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(func=command_validate_codex_argv)

    p = sub.add_parser("validate-codex-layers", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--payload", required=True)
    p.set_defaults(func=command_validate_codex_layers)

    p = sub.add_parser("init", help="Create a starter central config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=command_init)

    p = sub.add_parser("edit", help="Open the central config in $EDITOR")
    p.set_defaults(func=command_edit)

    p = sub.add_parser("list", help="List presets and project mappings")
    p.set_defaults(func=command_list)

    p = sub.add_parser("show", help="Show one preset")
    p.add_argument("preset")
    p.set_defaults(func=command_show)

    p = sub.add_parser("explain", help="Explain the resolved launch config")
    p.add_argument("--preset")
    p.add_argument("--tool", choices=sorted(VALID_TOOLS))
    p.add_argument("repo")
    p.set_defaults(func=command_explain)

    p = sub.add_parser("doctor", help="Validate the resolved launch config")
    p.add_argument("--preset")
    p.add_argument("--tool", choices=sorted(VALID_TOOLS))
    p.add_argument("repo")
    p.set_defaults(func=command_doctor)

    p = sub.add_parser("set-project", help="Map a project path to a preset")
    p.add_argument("path")
    p.add_argument("preset")
    p.set_defaults(func=command_set_project)

    p = sub.add_parser("ui-export", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.set_defaults(func=command_ui_export)

    p = sub.add_parser("ui-preview", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--request", type=Path, required=True)
    p.set_defaults(func=command_ui_preview)

    p = sub.add_parser("ui-commit", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--request", type=Path, required=True)
    p.set_defaults(func=command_ui_commit)

    p = sub.add_parser("ui-resolve-json", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--tool", choices=sorted(VALID_TOOLS))
    p.set_defaults(func=command_ui_resolve_json)

    p = sub.add_parser("mcp", help="Manage OAuth MCP authentication")
    mcp_sub = p.add_subparsers(dest="mcp_command", required=True)

    p_login = mcp_sub.add_parser("login", help="Authenticate with an OAuth MCP server")
    login_selector = p_login.add_mutually_exclusive_group()
    login_selector.add_argument("--preset")
    login_selector.add_argument(
        "--auth",
        help="Select a Codex auth block directly (does not take a repository path)",
    )
    p_login.add_argument("name")
    p_login.add_argument("repo", nargs="?")
    p_login.set_defaults(func=command_mcp_login)

    p_logout = mcp_sub.add_parser("logout", help="Remove OAuth MCP authentication")
    logout_selector = p_logout.add_mutually_exclusive_group()
    logout_selector.add_argument("--preset")
    logout_selector.add_argument(
        "--auth",
        help="Select a Codex auth block directly (does not take a repository path)",
    )
    p_logout.add_argument("name")
    p_logout.add_argument("repo", nargs="?")
    p_logout.set_defaults(func=command_mcp_logout)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
