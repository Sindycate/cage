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

_INSTALL_ROOT = Path(__file__).resolve().parent
if str(_INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(_INSTALL_ROOT))

from cage_core import bridge as bridge_common

BUFSIZE = 65536
AUTH_TIMEOUT_SECONDS = 5.0
HANDSHAKE_PREFIX = b"CAGE-MCP/1 "
MAX_HANDSHAKE_BYTES = 160
DEFAULT_PROCESS_TIMEOUT_SECONDS = 12 * 60 * 60
DEFAULT_MAX_IO_BYTES = 256 * 1024 * 1024
MAX_LOGGED_STDERR_BYTES = 1024 * 1024
Runtime = lambda: bridge_common.BridgeRuntime(  # noqa: E731
    "mcp-bridge",
    maximum_logged_stderr=MAX_LOGGED_STDERR_BYTES,
)
positive_number = bridge_common.positive_number
positive_integer = bridge_common.positive_integer
build_child_environment = bridge_common.build_child_environment
normalize_denied_roots = bridge_common.normalize_denied_roots
sanitize_child_path = bridge_common.sanitize_child_path
pin_executable = bridge_common.pin_executable
terminate_process_group = bridge_common.terminate_process_group

def parse_named_commands(entries):
    return bridge_common.parse_named_commands(
        entries,
        noun="MCP server",
        env_hint="preset/MCP env list",
    )

def authenticate(conn, token):
    return bridge_common.authenticate(
        conn,
        token,
        prefix=HANDSHAKE_PREFIX,
        maximum=MAX_HANDSHAKE_BYTES,
        timeout_seconds=AUTH_TIMEOUT_SECONDS,
        acknowledge_success=False,
    )


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
