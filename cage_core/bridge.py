"""Shared authenticated host-process boundary for Cage bridge frontends."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
RESERVED_ENV_NAMES = {"CAGE_BRIDGE_AUTH_TOKEN"}
SHELL_OPERATORS = {
    "|",
    "||",
    "&&",
    ";",
    "&",
    ">",
    ">>",
    "<",
    "<<",
    "2>",
    "2>>",
}
BASE_ENV_NAMES = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "SECURITYSESSIONID",
    "__CF_USER_TEXT_ENCODING",
)

AWS_COMMAND_NAME = "aws"
AWS_ACCESS_HOST_CLI = "host-cli"
AWS_PROFILE_MAX_LENGTH = 256
AWS_PROFILE_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CA_BUNDLE",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL_STS",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
}
AWS_BLOCKED_ARGUMENTS = {
    "--debug",
    "--no-verify-ssl",
    "--profile",
    "--endpoint-url",
    "--ca-bundle",
    "--cli-config-file",
}
AWS_BLOCKED_PREFIXES = (
    "--debug=",
    "--no-verify-ssl=",
    "--profile=",
    "--endpoint-url=",
    "--ca-bundle=",
    "--cli-config-file=",
)


def validate_aws_profile(value: object, label: str = "AWS profile") -> str:
    """Validate one profile name before it enters a host-side argv list."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > AWS_PROFILE_MAX_LENGTH:
        raise ValueError(
            f"{label} must be at most {AWS_PROFILE_MAX_LENGTH} characters"
        )
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} must not contain NUL or newline characters")
    return value


def scrub_aws_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove ambient AWS identity/configuration overrides for one host call."""

    sanitized = dict(environment)
    for name in list(sanitized):
        if name in AWS_PROFILE_ENV_NAMES or name.startswith("AWS_"):
            sanitized.pop(name, None)
    return sanitized


def validate_aws_client_arguments(arguments: list[str]) -> None:
    """Reject arguments that could escape the selected host AWS profile.

    The host CLI is deliberately still a broad AWS capability: IAM policy is
    the authority for which AWS actions succeed. Cage does, however, keep the
    selected profile and host credential/configuration sources fixed, and
    blocks debug/configuration options that could expose or redirect them.
    """

    for argument in arguments:
        if argument in AWS_BLOCKED_ARGUMENTS or argument.startswith(
            AWS_BLOCKED_PREFIXES
        ):
            raise ValueError(
                f"AWS argument {argument!r} is managed or blocked by Cage"
            )
    if "configure" in arguments:
        raise ValueError(
            "AWS configure commands are blocked by Cage; use the host AWS CLI"
        )
    for first, second in zip(arguments, arguments[1:]):
        if first == "sso" and second == "logout":
            raise ValueError(
                "AWS SSO logout is blocked by Cage; use the host AWS CLI"
            )


class BridgeRuntime:
    """Track connections and process groups for fail-closed shutdown."""

    def __init__(
        self,
        label: str,
        *,
        maximum_logged_stderr: int | None = None,
    ) -> None:
        self.label = label
        self.maximum_logged_stderr = maximum_logged_stderr
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.processes: set[subprocess.Popen[object]] = set()
        self.connections: set[socket.socket] = set()
        self.logged_server_stderr = 0
        self.stderr_truncated = False
        self.rejection_logged = False

    def add_connection(self, connection: socket.socket) -> None:
        with self.lock:
            self.connections.add(connection)

    def remove_connection(self, connection: socket.socket) -> None:
        with self.lock:
            self.connections.discard(connection)

    def add_process(self, process: subprocess.Popen[object]) -> None:
        with self.lock:
            self.processes.add(process)

    def remove_process(self, process: subprocess.Popen[object]) -> None:
        with self.lock:
            self.processes.discard(process)

    def note_rejected_client(self) -> None:
        with self.log_lock:
            if self.rejection_logged:
                return
            self.rejection_logged = True
            print(
                f"{self.label}: rejected unauthenticated client "
                "(further rejections are suppressed)",
                file=sys.stderr,
                flush=True,
            )

    def write_server_stderr(self, data: bytes) -> None:
        maximum = self.maximum_logged_stderr
        if maximum is None:
            return
        with self.log_lock:
            remaining = maximum - self.logged_server_stderr
            if remaining > 0:
                visible = data[:remaining]
                sys.stderr.buffer.write(visible)
                sys.stderr.buffer.flush()
                self.logged_server_stderr += len(visible)
            if len(data) > max(remaining, 0) and not self.stderr_truncated:
                print(
                    f"\n{self.label}: configured server stderr log capped "
                    f"at {maximum // (1024 * 1024)} MiB",
                    file=sys.stderr,
                    flush=True,
                )
                self.stderr_truncated = True

    def stop(self) -> None:
        self.shutdown.set()
        with self.lock:
            connections = list(self.connections)
            processes = list(self.processes)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for process in processes:
            terminate_process_group(process)


def positive_number(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_command(command: str, *, env_hint: str) -> list[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not argv:
        raise ValueError("command is empty")
    if any(token in SHELL_OPERATORS for token in argv):
        raise ValueError(
            "shell operators are not supported; use an executable wrapper "
            "script or explicitly configure a shell command"
        )
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
        raise ValueError(
            "leading environment assignments are not supported; declare the "
            f"variable in the selected {env_hint}"
        )
    return argv


def parse_named_commands(
    entries: list[list[str]],
    *,
    noun: str,
    env_hint: str,
) -> list[tuple[str, list[str]]]:
    parsed: list[tuple[str, list[str]]] = []
    seen: dict[str, str] = {}
    for name, command in entries:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid {noun} name: {name!r}")
        transport_name = name.upper().replace("-", "_")
        if transport_name in seen:
            raise ValueError(
                f"{noun} names collide after relay normalization: "
                f"{seen[transport_name]!r} and {name!r}"
            )
        seen[transport_name] = name
        parsed.append((name, parse_command(command, env_hint=env_hint)))
    return parsed


def build_child_environment(pass_env: list[str]) -> dict[str, str]:
    requested = list(BASE_ENV_NAMES)
    for name in pass_env:
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name in RESERVED_ENV_NAMES:
            raise ValueError(
                f"refusing to forward internal bridge variable: {name}"
            )
        if name not in requested:
            requested.append(name)
    child_env = {
        name: os.environ[name] for name in requested if name in os.environ
    }
    child_env.setdefault("HOME", str(Path.home()))
    child_env.setdefault(
        "PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    return child_env


def normalize_denied_roots(values: list[str]) -> list[Path]:
    roots: list[Path] = []
    for value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                f"untrusted executable root must be absolute: {value!r}"
            )
        resolved = candidate.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def path_is_within(candidate: Path, roots: list[Path]) -> bool:
    return any(
        candidate == root or candidate.is_relative_to(root)
        for root in roots
    )


def sanitize_child_path(
    child_env: dict[str, str], denied_roots: list[Path]
) -> None:
    safe_entries: list[str] = []
    for value in child_env.get("PATH", "").split(os.pathsep):
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve(strict=False)
        if path_is_within(resolved, denied_roots):
            continue
        rendered = str(resolved)
        if rendered not in safe_entries:
            safe_entries.append(rendered)
    if not safe_entries:
        raise ValueError(
            "host PATH has no entries outside writable Cage mounts"
        )
    child_env["PATH"] = os.pathsep.join(safe_entries)


def pin_executable(
    argv: list[str],
    cwd: str,
    child_env: dict[str, str],
    denied_roots: list[Path],
) -> list[str]:
    program = argv[0]
    if os.path.sep in program:
        candidate = Path(program).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
    else:
        located = shutil.which(program, path=child_env["PATH"])
        if located is None:
            raise ValueError(
                f"host executable not found on sanitized PATH: {program!r}"
            )
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"cannot resolve host executable {program!r}: {exc}"
        ) from exc
    if path_is_within(resolved, denied_roots):
        raise ValueError(
            f"refusing host executable from a Cage-writable mount: {resolved}"
        )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(
            f"host executable is not a runnable regular file: {resolved}"
        )
    return [str(resolved), *argv[1:]]


def read_line(
    connection: socket.socket, maximum: int, timeout_seconds: float
) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout_seconds
    while len(data) <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("handshake timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(1)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data)
    raise ValueError("handshake is too large")


def authenticate(
    connection: socket.socket,
    token: str,
    *,
    prefix: bytes,
    maximum: int,
    timeout_seconds: float,
    acknowledge_success: bool,
) -> bool:
    connection.settimeout(timeout_seconds)
    try:
        supplied = read_line(connection, maximum, timeout_seconds)
        expected = prefix + token.encode("ascii") + b"\n"
        accepted = hmac.compare_digest(supplied, expected)
        if acknowledge_success:
            connection.sendall(b"OK\n" if accepted else b"ERR\n")
        elif not accepted:
            connection.sendall(b"ERR\n")
        return accepted
    except (OSError, TimeoutError, UnicodeError, ValueError):
        return False
    finally:
        connection.settimeout(None)


def terminate_process_group(
    process: subprocess.Popen[object], grace_seconds: float = 2.0
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
