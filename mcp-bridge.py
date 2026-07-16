#!/usr/bin/env python3
"""Authenticated host-side relay for stdio MCP servers.

The bridge deliberately keeps the MCP stream byte-for-byte unchanged after a
small authenticated handshake.  Repository/container code never supplies the
host command: commands come from Cage's host-owned central configuration.
"""

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

BUFSIZE = 65536
AUTH_TIMEOUT_SECONDS = 5.0
HANDSHAKE_PREFIX = b"CAGE-MCP/1 "
MAX_HANDSHAKE_BYTES = 160
DEFAULT_PROCESS_TIMEOUT_SECONDS = 12 * 60 * 60
DEFAULT_MAX_IO_BYTES = 256 * 1024 * 1024
MAX_LOGGED_STDERR_BYTES = 1024 * 1024
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


class Runtime:
    """Track resources so bridge shutdown also stops spawned process groups."""

    def __init__(self):
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.processes = set()
        self.connections = set()
        self.logged_server_stderr = 0
        self.stderr_truncated = False
        self.rejection_logged = False

    def add_connection(self, conn):
        with self.lock:
            self.connections.add(conn)

    def remove_connection(self, conn):
        with self.lock:
            self.connections.discard(conn)

    def add_process(self, proc):
        with self.lock:
            self.processes.add(proc)

    def remove_process(self, proc):
        with self.lock:
            self.processes.discard(proc)

    def note_rejected_client(self):
        with self.log_lock:
            if self.rejection_logged:
                return
            self.rejection_logged = True
            print(
                "mcp-bridge: rejected unauthenticated client "
                "(further rejections are suppressed)",
                file=sys.stderr,
                flush=True,
            )

    def write_server_stderr(self, data):
        with self.log_lock:
            remaining = MAX_LOGGED_STDERR_BYTES - self.logged_server_stderr
            if remaining > 0:
                visible = data[:remaining]
                sys.stderr.buffer.write(visible)
                sys.stderr.buffer.flush()
                self.logged_server_stderr += len(visible)
            if len(data) > max(remaining, 0) and not self.stderr_truncated:
                print(
                    "\nmcp-bridge: configured server stderr log capped at 1 MiB",
                    file=sys.stderr,
                    flush=True,
                )
                self.stderr_truncated = True

    def stop(self):
        self.shutdown.set()
        with self.lock:
            connections = list(self.connections)
            processes = list(self.processes)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for proc in processes:
            terminate_process_group(proc)


def positive_number(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_command(command):
    """Parse a host-owned command without invoking a shell."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not argv:
        raise ValueError("command is empty")
    operators = [token for token in argv if token in SHELL_OPERATORS]
    if operators:
        raise ValueError(
            "shell operators are not supported; use an executable wrapper "
            "script or explicitly configure a shell command"
        )
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
        raise ValueError(
            "leading environment assignments are not supported; declare the "
            "variable in the selected preset/MCP env list"
        )
    return argv


def parse_named_commands(entries):
    parsed = []
    seen = {}
    for name, command in entries:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid MCP server name: {name!r}")
        transport_name = name.upper().replace("-", "_")
        if transport_name in seen:
            raise ValueError(
                "MCP server names collide after relay normalization: "
                f"{seen[transport_name]!r} and {name!r}"
            )
        seen[transport_name] = name
        parsed.append((name, parse_command(command)))
    return parsed


def build_child_environment(pass_env):
    requested = list(BASE_ENV_NAMES)
    for name in pass_env:
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name in RESERVED_ENV_NAMES:
            raise ValueError(f"refusing to forward internal bridge variable: {name}")
        if name not in requested:
            requested.append(name)
    child_env = {name: os.environ[name] for name in requested if name in os.environ}
    child_env.setdefault("HOME", str(Path.home()))
    child_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    return child_env


def normalize_denied_roots(values):
    roots = []
    for value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"untrusted executable root must be absolute: {value!r}")
        resolved = candidate.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def path_is_within(candidate, roots):
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def sanitize_child_path(child_env, denied_roots):
    safe_entries = []
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
        raise ValueError("host PATH has no entries outside writable Cage mounts")
    child_env["PATH"] = os.pathsep.join(safe_entries)


def pin_executable(argv, cwd, child_env, denied_roots):
    program = argv[0]
    if os.path.sep in program:
        candidate = Path(program).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
    else:
        located = shutil.which(program, path=child_env["PATH"])
        if located is None:
            raise ValueError(f"host executable not found on sanitized PATH: {program!r}")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve host executable {program!r}: {exc}") from exc
    if path_is_within(resolved, denied_roots):
        raise ValueError(
            f"refusing host executable from a Cage-writable mount: {resolved}"
        )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"host executable is not a runnable regular file: {resolved}")
    return [str(resolved), *argv[1:]]


def read_line(conn, maximum, timeout_seconds):
    data = bytearray()
    deadline = time.monotonic() + timeout_seconds
    while len(data) <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("handshake timed out")
        conn.settimeout(remaining)
        chunk = conn.recv(1)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data)
    raise ValueError("handshake is too large")


def authenticate(conn, token):
    conn.settimeout(AUTH_TIMEOUT_SECONDS)
    try:
        supplied = read_line(conn, MAX_HANDSHAKE_BYTES, AUTH_TIMEOUT_SECONDS)
        expected = HANDSHAKE_PREFIX + token.encode("ascii") + b"\n"
        accepted = hmac.compare_digest(supplied, expected)
        if not accepted:
            conn.sendall(b"ERR\n")
        return accepted
    except (OSError, TimeoutError, UnicodeError, ValueError):
        return False
    finally:
        conn.settimeout(None)


def terminate_process_group(proc, grace_seconds=2.0):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def relay(conn, proc, runtime, process_timeout, max_input, max_output):
    """Relay raw MCP bytes with bounded lifetime and aggregate I/O."""
    disconnected = threading.Event()
    limit_exceeded = threading.Event()
    input_count = 0
    output_count = 0

    def socket_to_process():
        nonlocal input_count
        try:
            while not runtime.shutdown.is_set():
                data = conn.recv(BUFSIZE)
                if not data:
                    disconnected.set()
                    break
                input_count += len(data)
                if input_count > max_input:
                    limit_exceeded.set()
                    break
                proc.stdin.write(data)
                proc.stdin.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            disconnected.set()
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def process_to_socket():
        nonlocal output_count
        try:
            while not runtime.shutdown.is_set():
                data = os.read(proc.stdout.fileno(), BUFSIZE)
                if not data:
                    break
                output_count += len(data)
                if output_count > max_output:
                    limit_exceeded.set()
                    break
                conn.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            disconnected.set()

    def process_stderr_to_log():
        try:
            while not runtime.shutdown.is_set():
                data = os.read(proc.stderr.fileno(), BUFSIZE)
                if not data:
                    break
                runtime.write_server_stderr(data)
        except OSError:
            pass

    input_thread = threading.Thread(target=socket_to_process, daemon=True)
    output_thread = threading.Thread(target=process_to_socket, daemon=True)
    stderr_thread = threading.Thread(target=process_stderr_to_log, daemon=True)
    input_thread.start()
    output_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + process_timeout
    reason = ""
    while proc.poll() is None:
        if runtime.shutdown.is_set():
            reason = "bridge shutdown"
            break
        if disconnected.is_set():
            reason = "client disconnected"
            break
        if limit_exceeded.is_set():
            reason = "I/O limit exceeded"
            break
        if time.monotonic() >= deadline:
            reason = "process timeout"
            break
        time.sleep(0.05)

    if proc.poll() is None:
        terminate_process_group(proc)
    try:
        conn.shutdown(socket.SHUT_RD)
    except OSError:
        pass
    output_thread.join(timeout=2.0)
    stderr_thread.join(timeout=2.0)
    input_thread.join(timeout=0.2)
    if reason and reason != "client disconnected":
        print(f"mcp-bridge: closing server process: {reason}", file=sys.stderr)


def serve_one(server_sock, argv, token, cwd, child_env, runtime, limits):
    """Serve a bounded single active connection for one configured server."""
    while not runtime.shutdown.is_set():
        try:
            server_sock.settimeout(0.5)
            conn, _ = server_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        runtime.add_connection(conn)
        try:
            if not authenticate(conn, token):
                runtime.note_rejected_client()
                continue
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=child_env,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                print(f"mcp-bridge: cannot start configured command: {exc}", file=sys.stderr)
                try:
                    conn.sendall(b"ERR\n")
                except OSError:
                    pass
                continue
            try:
                conn.sendall(b"OK\n")
            except OSError:
                terminate_process_group(proc)
                continue
            runtime.add_process(proc)
            try:
                relay(conn, proc, runtime, *limits)
            finally:
                terminate_process_group(proc)
                runtime.remove_process(proc)
        finally:
            runtime.remove_connection(conn)
            try:
                conn.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="MCP bridge for Cage containers")
    parser.add_argument(
        "--server",
        nargs=2,
        action="append",
        metavar=("NAME", "COMMAND"),
        required=True,
        help="MCP server name and argv-like host command (repeatable)",
    )
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    parser.add_argument(
        "--deny-executable-root",
        action="append",
        default=[],
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--cwd", default=str(Path.home()), help=argparse.SUPPRESS)
    parser.add_argument(
        "--listen-host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="0.0.0.0",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--process-timeout",
        type=positive_number,
        default=DEFAULT_PROCESS_TIMEOUT_SECONDS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-input-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_IO_BYTES,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_IO_BYTES,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    token = os.environ.get("CAGE_BRIDGE_AUTH_TOKEN", "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        parser.error("CAGE_BRIDGE_AUTH_TOKEN must contain a fresh 64-character hex token")

    cwd = os.path.realpath(os.path.expanduser(args.cwd))
    if not os.path.isdir(cwd):
        parser.error(f"trusted bridge cwd is not a directory: {cwd}")
    try:
        denied_roots = normalize_denied_roots(args.deny_executable_root)
        child_env = build_child_environment(args.pass_env)
        sanitize_child_path(child_env, denied_roots)
        servers = parse_named_commands(args.server)
        servers = [
            (name, pin_executable(argv, cwd, child_env, denied_roots))
            for name, argv in servers
        ]
    except ValueError as exc:
        parser.error(str(exc))

    runtime = Runtime()
    sockets = {}
    threads = []
    limits = (args.process_timeout, args.max_input_bytes, args.max_output_bytes)

    for name, argv in servers:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.listen_host, 0))
        sock.listen(4)
        port = sock.getsockname()[1]
        sockets[name] = (sock, port)
        print(f"SERVER:{name}=PORT:{port}", flush=True)
        print(
            f"mcp-bridge: {name} listening on {args.listen_host}:{port}; authentication required",
            file=sys.stderr,
            flush=True,
        )
        thread = threading.Thread(
            target=serve_one,
            args=(sock, argv, token, cwd, child_env, runtime, limits),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    print("READY", flush=True)

    def handle_signal(_sig, _frame):
        runtime.shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    runtime.shutdown.wait()
    for sock, _ in sockets.values():
        try:
            sock.close()
        except OSError:
            pass
    runtime.stop()
    for thread in threads:
        thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
