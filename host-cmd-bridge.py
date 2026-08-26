#!/usr/bin/env python3
"""Authenticated host-command bridge for Cage containers.

The protocol carries argv, stdin, stdout, stderr, and the final process status
as bounded frames.  The configured command is parsed into argv once at startup
and is always executed with ``shell=False`` from a trusted working directory.
"""

import argparse
import hmac
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
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
HANDSHAKE_PREFIX = b"CAGE-HOSTCMD/1 "
MAX_HANDSHAKE_BYTES = 160
FRAME_HEADER = struct.Struct("!cI")
FRAME_ARGV = b"A"
FRAME_STDIN = b"I"
FRAME_STDIN_EOF = b"E"
FRAME_STDOUT = b"O"
FRAME_STDERR = b"R"
FRAME_EXIT = b"X"
FRAME_ERROR = b"!"
MAX_ARGV_BYTES = 64 * 1024
MAX_ARG_COUNT = 128
MAX_ARG_BYTES = 16 * 1024
MAX_FRAME_BYTES = 64 * 1024
DEFAULT_PROCESS_TIMEOUT_SECONDS = 5 * 60
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
class OutputBudget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.used = 0
        self.lock = threading.Lock()

    def consume(self, amount):
        with self.lock:
            if self.used + amount > self.maximum:
                return False
            self.used += amount
            return True


Runtime = lambda: bridge_common.BridgeRuntime("host-cmd-bridge")  # noqa: E731
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
        noun="host command",
        env_hint="preset env list",
    )

def authenticate(conn, token):
    return bridge_common.authenticate(
        conn,
        token,
        prefix=HANDSHAKE_PREFIX,
        maximum=MAX_HANDSHAKE_BYTES,
        timeout_seconds=AUTH_TIMEOUT_SECONDS,
        acknowledge_success=True,
    )


def recv_exact(conn, size, deadline=None):
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("frame timed out")
            conn.settimeout(remaining)
        chunk = conn.recv(size - len(chunks))
        if not chunk:
            raise EOFError("bridge connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(conn, maximum=MAX_FRAME_BYTES, deadline=None):
    kind, size = FRAME_HEADER.unpack(recv_exact(conn, FRAME_HEADER.size, deadline))
    if size > maximum:
        raise ValueError(f"frame exceeds {maximum} bytes")
    return kind, recv_exact(conn, size, deadline)


def send_frame(conn, lock, kind, payload=b""):
    if not lock.acquire(timeout=2.0):
        raise TimeoutError("bridge client stopped reading")
    try:
        conn.sendall(FRAME_HEADER.pack(kind, len(payload)) + payload)
    finally:
        lock.release()


def send_error(conn, lock, message):
    try:
        send_frame(conn, lock, FRAME_ERROR, message.encode("utf-8", "replace")[:4096])
    except (OSError, TimeoutError):
        pass


def finish_rejected_connection(conn):
    """Gracefully finish a request rejected before a child process starts.

    The relay sends stdin EOF immediately after its argv frame. Draining that
    pending input before closing prevents macOS from turning a close with
    unread peer data into a TCP reset, which could hide the explicit exit code.
    """

    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        conn.settimeout(0.25)
        while conn.recv(BUFSIZE):
            pass
    except (OSError, TimeoutError):
        pass
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass


def parse_client_argv(payload):
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid argv frame: {exc}") from exc
    if not isinstance(value, list) or len(value) > MAX_ARG_COUNT:
        raise ValueError(f"argv must be a list with at most {MAX_ARG_COUNT} entries")
    total = 0
    for argument in value:
        if not isinstance(argument, str):
            raise ValueError("every argv entry must be a string")
        encoded = argument.encode("utf-8")
        total += len(encoded)
        if len(encoded) > MAX_ARG_BYTES or "\x00" in argument:
            raise ValueError("argv entry is too large or contains NUL")
    if total > MAX_ARGV_BYTES:
        raise ValueError("argv exceeds the aggregate size limit")
    return value


def effective_command_argv(base_argv, client_argv, aws_profile=""):
    """Append caller arguments except for the exact legacy duplicate case.

    Before Cage 0.23.0, host-command shims did not forward caller arguments, so
    existing token-minter definitions commonly embedded the complete invocation
    (for example, ``ztoken token -n codex``). Newer Codex auth configuration
    supplies those arguments to the shim. Appending an identical suffix would
    execute the arguments twice and make authentication fail. Preserve general
    argument forwarding while de-duplicating only that exact compatibility case.
    """
    if aws_profile:
        bridge_common.validate_aws_profile(aws_profile)
        bridge_common.validate_aws_client_arguments(client_argv)
        return list(base_argv) + ["--profile", aws_profile] + list(client_argv)

    fixed_arguments = base_argv[1:]
    if fixed_arguments and client_argv == fixed_arguments:
        return list(base_argv)
    return list(base_argv) + list(client_argv)


def terminate_process_group(proc, grace_seconds=2.0):
    bridge_common.terminate_process_group(proc, grace_seconds)


def status_code(returncode):
    if returncode is None:
        return 126
    if returncode < 0:
        return min(255, 128 + abs(returncode))
    return min(255, returncode)


def run_command(
    conn,
    base_argv,
    client_argv,
    cwd,
    child_env,
    runtime,
    limits,
    aws_profile="",
):
    process_timeout, max_input, max_output = limits
    send_lock = threading.Lock()
    try:
        command_argv = effective_command_argv(
            base_argv, client_argv, aws_profile=aws_profile
        )
    except ValueError as exc:
        send_error(conn, send_lock, str(exc))
        try:
            send_frame(conn, send_lock, FRAME_EXIT, b'{"code":126}')
        except (OSError, TimeoutError):
            pass
        finish_rejected_connection(conn)
        return
    try:
        command_env = (
            bridge_common.scrub_aws_environment(child_env)
            if aws_profile
            else child_env
        )
        proc = subprocess.Popen(
            command_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=command_env,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        send_error(conn, send_lock, f"cannot start configured command: {exc}")
        try:
            send_frame(conn, send_lock, FRAME_EXIT, b'{"code":126}')
        except (OSError, TimeoutError):
            pass
        finish_rejected_connection(conn)
        return

    runtime.add_process(proc)
    disconnected = threading.Event()
    protocol_error = threading.Event()
    output_limited = threading.Event()
    error_messages = []
    output_budget = OutputBudget(max_output)

    def client_to_process():
        received = 0
        try:
            while not runtime.shutdown.is_set():
                kind, payload = recv_frame(conn)
                if kind == FRAME_STDIN:
                    received += len(payload)
                    if received > max_input:
                        error_messages.append("stdin limit exceeded")
                        protocol_error.set()
                        break
                    proc.stdin.write(payload)
                    proc.stdin.flush()
                elif kind == FRAME_STDIN_EOF and not payload:
                    break
                else:
                    error_messages.append("unexpected client frame")
                    protocol_error.set()
                    break
        except EOFError:
            disconnected.set()
        except BrokenPipeError:
            # The command may legitimately close stdin before it exits.  That
            # is not a client disconnect and must not suppress its exit code.
            pass
        except OSError:
            if proc.poll() is None:
                disconnected.set()
        except ValueError as exc:
            error_messages.append(str(exc))
            protocol_error.set()
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def process_output(pipe, frame_kind):
        try:
            while not runtime.shutdown.is_set():
                data = os.read(pipe.fileno(), BUFSIZE)
                if not data:
                    return
                if not output_budget.consume(len(data)):
                    output_limited.set()
                    return
                send_frame(conn, send_lock, frame_kind, data)
        except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            disconnected.set()

    input_thread = threading.Thread(target=client_to_process, daemon=True)
    stdout_thread = threading.Thread(target=process_output, args=(proc.stdout, FRAME_STDOUT), daemon=True)
    stderr_thread = threading.Thread(target=process_output, args=(proc.stderr, FRAME_STDERR), daemon=True)
    input_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + process_timeout
    timed_out = False
    while proc.poll() is None:
        if runtime.shutdown.is_set() or disconnected.is_set() or protocol_error.is_set():
            break
        if output_limited.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.05)

    if proc.poll() is None:
        terminate_process_group(proc)
    try:
        returncode = proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        terminate_process_group(proc, grace_seconds=0.2)
        returncode = proc.poll()

    stdout_thread.join(timeout=2.0)
    stderr_thread.join(timeout=2.0)

    if not disconnected.is_set():
        if timed_out:
            send_error(conn, send_lock, "host command exceeded its process timeout")
        elif output_limited.is_set():
            send_error(conn, send_lock, "host command exceeded its output limit")
        elif protocol_error.is_set():
            send_error(conn, send_lock, error_messages[0] if error_messages else "protocol error")
        if timed_out:
            code = 124
        elif output_limited.is_set():
            code = 125
        elif protocol_error.is_set():
            code = 126
        else:
            code = status_code(returncode)
        payload = json.dumps(
            {
                "code": code,
                "timed_out": timed_out,
                "output_limited": output_limited.is_set(),
            },
            separators=(",", ":"),
        ).encode("ascii")
        try:
            send_frame(conn, send_lock, FRAME_EXIT, payload)
        except (OSError, TimeoutError):
            pass

    # The enclosing connection cleanup wakes an input thread that is still
    # waiting for client data.  Do not SHUT_RD before sending status: that
    # would make the input thread misclassify our own shutdown as a disconnect.
    runtime.remove_process(proc)


def serve_one(
    server_sock,
    base_argv,
    token,
    cwd,
    child_env,
    runtime,
    limits,
    aws_profile="",
):
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
            send_lock = threading.Lock()
            try:
                request_deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
                kind, payload = recv_frame(conn, MAX_ARGV_BYTES, request_deadline)
                if kind != FRAME_ARGV:
                    raise ValueError("first client frame must contain argv")
                client_argv = parse_client_argv(payload)
            except (EOFError, OSError, TimeoutError, ValueError) as exc:
                send_error(conn, send_lock, str(exc))
                continue
            finally:
                conn.settimeout(None)
            run_command(
                conn,
                base_argv,
                client_argv,
                cwd,
                child_env,
                runtime,
                limits,
                aws_profile=aws_profile,
            )
        finally:
            runtime.remove_connection(conn)
            try:
                conn.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Host command bridge for Cage containers")
    parser.add_argument(
        "--command",
        nargs=2,
        action="append",
        metavar=("NAME", "COMMAND"),
        required=True,
        help="command name and argv-like host command (repeatable)",
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
        default=DEFAULT_MAX_INPUT_BYTES,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--aws-profile",
        default="",
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
        commands = parse_named_commands(args.command)
        aws_profile = (
            bridge_common.validate_aws_profile(args.aws_profile)
            if args.aws_profile
            else ""
        )
        if aws_profile:
            if not any(
                name.casefold() == bridge_common.AWS_COMMAND_NAME.casefold()
                for name, _ in commands
            ):
                raise ValueError(
                    "--aws-profile requires a command named 'aws'"
                )
        commands = [
            (name, pin_executable(argv, cwd, child_env, denied_roots))
            for name, argv in commands
        ]
    except ValueError as exc:
        parser.error(str(exc))

    runtime = Runtime()
    sockets = {}
    threads = []
    limits = (args.process_timeout, args.max_input_bytes, args.max_output_bytes)
    for name, argv in commands:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.listen_host, 0))
        sock.listen(4)
        port = sock.getsockname()[1]
        sockets[name] = (sock, port)
        print(f"COMMAND:{name}=PORT:{port}", flush=True)
        print(
            f"host-cmd-bridge: {name} listening on {args.listen_host}:{port}; authentication required",
            file=sys.stderr,
            flush=True,
        )
        thread = threading.Thread(
            target=serve_one,
            args=(
                sock,
                argv,
                token,
                cwd,
                child_env,
                runtime,
                limits,
                (
                    aws_profile
                    if name.casefold() == bridge_common.AWS_COMMAND_NAME.casefold()
                    else ""
                ),
            ),
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
