#!/usr/bin/env python3
"""
host-cmd-bridge.py — Host-side TCP relay for running host commands from
inside the container.

Mirrors mcp-bridge.py. For each configured host command, listens on a
random TCP port. When the container client connects (via host-cmd-relay),
spawns the configured command on the host and relays its stdio over the
socket. Unlike the MCP bridge, stderr is forwarded to this bridge's own
stderr so the user can see errors from e.g. token-minting commands.
"""

import argparse
import os
import select
import signal
import socket
import subprocess
import sys
import threading

BUFSIZE = 65536


def relay(sock, proc):
    """Bidirectional relay between a TCP socket and a subprocess stdin/stdout.

    Handles one-shot commands: when the client closes its write side (we see
    EOF on the socket), we close the subprocess's stdin but keep draining its
    stdout to the socket until the process exits. Without this the command's
    output would be lost on every "short" request (e.g. `ztoken` that ignores
    stdin and just prints a token).
    """
    sock.setblocking(False)
    proc_stdout_fd = proc.stdout.fileno()
    proc_stdin = proc.stdin
    sock_fd = sock.fileno()
    sock_readable = True

    try:
        while True:
            watch = [proc_stdout_fd]
            if sock_readable:
                watch.append(sock_fd)
            readable, _, _ = select.select(watch, [], [], 1.0)
            if proc.poll() is not None and not readable:
                break
            for fd in readable:
                if fd == sock_fd:
                    data = sock.recv(BUFSIZE)
                    if not data:
                        sock_readable = False
                        try:
                            proc_stdin.close()
                        except OSError:
                            pass
                    else:
                        proc_stdin.write(data)
                        proc_stdin.flush()
                elif fd == proc_stdout_fd:
                    data = os.read(proc_stdout_fd, BUFSIZE)
                    if not data:
                        return
                    sock.sendall(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
            proc.wait()


def serve_one(server_sock, command):
    """Accept connections one at a time, spawn command per connection."""
    while True:
        try:
            conn, _ = server_sock.accept()
        except OSError:
            break

        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            shell=True,
        )
        try:
            relay(conn, proc)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="Host command bridge for cage containers")
    parser.add_argument(
        "--command",
        nargs=2,
        action="append",
        metavar=("NAME", "COMMAND"),
        required=True,
        help="Command name (as exposed in container) and host command to run (repeatable)",
    )
    args = parser.parse_args()

    sockets = {}
    threads = []

    for name, command in args.command:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        sockets[name] = (sock, port)

        print(f"COMMAND:{name}=PORT:{port}", flush=True)

        t = threading.Thread(target=serve_one, args=(sock, command), daemon=True)
        t.start()
        threads.append(t)

    print("READY", flush=True)

    shutdown = threading.Event()

    def handle_signal(sig, frame):
        shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    shutdown.wait()

    for _, (sock, _) in sockets.items():
        sock.close()


if __name__ == "__main__":
    main()
