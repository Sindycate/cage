#!/usr/bin/env python3
"""
mcp-bridge.py — Host-side TCP relay for MCP servers.

Runs on the host. For each configured MCP server, listens on a random TCP port.
When a container client connects, spawns the configured command as a subprocess
and relays bidirectionally between the TCP connection and the subprocess stdio.
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
    """Bidirectional relay between a TCP socket and a subprocess stdin/stdout."""
    sock.setblocking(False)
    proc_stdout_fd = proc.stdout.fileno()
    proc_stdin = proc.stdin
    sock_fd = sock.fileno()

    try:
        while True:
            readable, _, _ = select.select([sock_fd, proc_stdout_fd], [], [], 1.0)
            if proc.poll() is not None and not readable:
                break
            for fd in readable:
                if fd == sock_fd:
                    data = sock.recv(BUFSIZE)
                    if not data:
                        return
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
            stderr=subprocess.DEVNULL,
            shell=True,
        )
        try:
            relay(conn, proc)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="MCP bridge for cage containers")
    parser.add_argument(
        "--server",
        nargs=2,
        action="append",
        metavar=("NAME", "COMMAND"),
        required=True,
        help="MCP server name and host command (repeatable)",
    )
    args = parser.parse_args()

    sockets = {}
    threads = []

    for name, command in args.server:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        sockets[name] = (sock, port)

        print(f"SERVER:{name}=PORT:{port}", flush=True)

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
