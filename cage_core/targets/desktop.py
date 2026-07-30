"""Persistent Desktop target execution."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from .container import (
    ContainerRuntime,
    ContainerTargetError,
    _temporary_path,
    _unlink,
)


def _write_runtime_environment(
    runtime: ContainerRuntime, docker_arguments: list[str]
) -> tuple[list[str], Path]:
    configured = set(runtime.resolved.extra_env)

    def is_secret(name: str) -> bool:
        return (
            name
            in {
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "OPENAI_API_KEY",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "MCP_BRIDGE_HOST",
                "MCP_BRIDGE_TOKEN",
                "HOST_CMD_BRIDGE_HOST",
                "HOST_CMD_BRIDGE_TOKEN",
            }
            or name.startswith("MCP_BRIDGE_PORT_")
            or name.startswith("HOST_CMD_BRIDGE_PORT_")
            or name in configured
        )

    filtered: list[str] = []
    secrets_by_name: dict[str, str] = {}
    index = 0
    while index < len(docker_arguments):
        argument = docker_arguments[index]
        if argument == "-e" and index + 1 < len(docker_arguments):
            specification = docker_arguments[index + 1]
            name, separator, value = specification.partition("=")
            if separator and is_secret(name):
                secrets_by_name[name] = value
                index += 2
                continue
        filtered.append(argument)
        index += 1
    state_dir = Path(os.environ["CAGE_DESKTOP_STATE_DIR"])
    path = _temporary_path(
        runtime,
        prefix=".runtime-env.",
        directory=state_dir,
    )
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContainerTargetError(
                "unsafe desktop runtime environment handoff"
            )
        with os.fdopen(
            descriptor, "w", encoding="utf-8", closefd=False
        ) as handle:
            json.dump(
                secrets_by_name,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    filtered.extend(("-v", f"{path}:/cage-desktop/runtime-env.json:ro"))
    return filtered, path


def run_desktop(
    runtime: ContainerRuntime,
    docker_arguments: list[str],
) -> int:
    filtered, secret_file = _write_runtime_environment(
        runtime, docker_arguments
    )
    result = runtime.run(
        ["run", "-d", *filtered, runtime.plan.image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        raise ContainerTargetError("desktop container failed to start")
    container_id = result.stdout.strip()
    container_reference = container_id or runtime.container_name

    def stop_container() -> int:
        runtime.run(
            ["stop", "--time", "10", container_reference],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        runtime.run(
            ["rm", "-f", container_reference],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return 0

    runtime.lifecycle.register("desktop container", stop_container)
    if not container_id:
        raise ContainerTargetError(
            "desktop Docker launch returned no container id"
        )
    ready = False
    for _ in range(150):
        running = runtime.output(
            ["inspect", "-f", "{{.State.Running}}", container_id],
            stderr=subprocess.DEVNULL,
        ).strip()
        if running != "true":
            break
        sshd = runtime.run(
            [
                "exec",
                "--user",
                "root",
                container_id,
                "/usr/sbin/sshd",
                "-T",
                "-f",
                "/run/cage/sshd_config",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        key = runtime.run(
            [
                "exec",
                "--user",
                "root",
                container_id,
                "test",
                "-f",
                "/home/codex/.codex/.cage-desktop/"
                "ssh_host_ed25519_key.pub",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if sshd.returncode == 0 and key.returncode == 0:
            ready = True
            break
        time.sleep(0.2)
    if not ready:
        print(
            "ERROR: desktop container did not become SSH-ready",
            file=sys.stderr,
        )
        logs = runtime.output(
            ["logs", container_id], stderr=subprocess.DEVNULL
        )
        if logs:
            print(logs, file=sys.stderr, end="")
        return 1
    _unlink(secret_file)
    mark = subprocess.run(
        [
            sys.executable,
            "-I",
            str(runtime.install_root / "cage-desktop.py"),
            "--config-dir",
            str(runtime.config_root),
            "--launcher",
            str(runtime.install_root / "cage"),
            "mark-ready",
            os.environ["CAGE_DESKTOP_TARGET_ID"],
            "--container-id",
            container_id,
            "--container-name",
            runtime.container_name,
            "--volume-name",
            runtime.plan.volume_name,
            "--fingerprint",
            os.environ["CAGE_DESKTOP_FINGERPRINT"],
        ],
        check=False,
    )
    if mark.returncode != 0:
        return mark.returncode
    while True:
        running = runtime.output(
            ["inspect", "-f", "{{.State.Running}}", container_id],
            stderr=subprocess.DEVNULL,
        ).strip()
        if running != "true":
            break
        for name, process in runtime.dependency_processes:
            if process.poll() is not None:
                print(
                    f"ERROR: desktop {name} exited; stopping target "
                    "fail-closed",
                    file=sys.stderr,
                )
                return 70
        time.sleep(1.0)
    exit_text = runtime.output(
        ["inspect", "-f", "{{.State.ExitCode}}", container_id],
        stderr=subprocess.DEVNULL,
    ).strip()
    return int(exit_text) if exit_text.isdigit() else 1
