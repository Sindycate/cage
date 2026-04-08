#!/usr/bin/env python3
"""
netgate-proxy.py — Domain-gating forward proxy for cage.

Runs on the host. Container traffic routes through it via HTTP_PROXY/HTTPS_PROXY.
For each unknown domain, prompts the user (macOS dialog or terminal prompt on Linux)
and holds the connection until the user decides.
"""

import argparse
import fcntl
import http.server
import json
import os
import platform
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse

IS_MACOS = platform.system() == "Darwin"

# --- Global state for prompt deduplication ---
_pending_lock = threading.Lock()
_pending_domains: dict[str, threading.Event] = {}
_pending_results: dict[str, str] = {}

# --- Configuration (set from CLI args) ---
CONFIG = {
    "project_hash": "",
    "container_name": "unknown",
    "config_dir": "",
    "script_dir": "",
}

BUF_SIZE = 65536


def log(msg: str):
    print(f"[netgate] {msg}", file=sys.stderr, flush=True)


# --- Allowlist management ---

def load_json_domains(path: str) -> tuple[list[str], list[str]]:
    """Load domains and denied lists from a JSON file. Returns (allowed, denied)."""
    if not os.path.isfile(path):
        return [], []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("domains", []), data.get("denied", [])
    except (json.JSONDecodeError, OSError):
        return [], []


def domain_matches(hostname: str, pattern: str) -> bool:
    """Check if hostname matches a pattern. Supports exact match and *.example.com wildcards."""
    hostname = hostname.lower()
    pattern = pattern.lower()
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # e.g., ".amazonaws.com"
        return hostname.endswith(suffix) or hostname == pattern[2:]
    return False


def check_domain(hostname: str) -> str:
    """Check domain against all allowlists. Returns 'allow', 'deny', or 'prompt'."""
    defaults_path = os.path.join(CONFIG["script_dir"], "netgate", "defaults.json")
    global_path = os.path.join(CONFIG["config_dir"], "global.json")
    project_path = os.path.join(CONFIG["config_dir"], f"project-{CONFIG['project_hash']}.json")

    # Collect all lists
    all_allowed = []
    all_denied = []

    for path in [defaults_path, global_path, project_path]:
        allowed, denied = load_json_domains(path)
        all_allowed.extend(allowed)
        all_denied.extend(denied)

    # Denied takes priority
    for pattern in all_denied:
        if domain_matches(hostname, pattern):
            return "deny"

    for pattern in all_allowed:
        if domain_matches(hostname, pattern):
            return "allow"

    return "prompt"


def prompt_user(hostname: str) -> str:
    """Prompt user for domain approval. Returns 'project', 'always', or 'deny'.

    On macOS: shows a native dialog via osascript.
    On Linux: prompts in the terminal via /dev/tty.
    """
    if IS_MACOS:
        return _prompt_osascript(hostname)
    return _prompt_terminal(hostname)


def _prompt_osascript(hostname: str) -> str:
    """macOS: native dialog via osascript."""
    script = (
        f'display dialog "Network access requested" & return & return '
        f'& "Domain: {hostname}" & return '
        f'& "Container: {CONFIG["container_name"]}" & return & return '
        f'& "Allow this domain?" '
        f'buttons {{"Deny", "Allow (project)", "Allow (always)"}} '
        f'default button "Deny" with icon caution '
        f'giving up after 120'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=130,
        )
        output = result.stdout.strip()
        if "Allow (always)" in output:
            return "always"
        elif "Allow (project)" in output:
            return "project"
        else:
            return "deny"
    except (subprocess.TimeoutExpired, OSError):
        return "deny"


def _prompt_terminal(hostname: str) -> str:
    """Linux/other: prompt via terminal (/dev/tty)."""
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        log(f"Cannot open /dev/tty for prompt, denying {hostname}")
        return "deny"
    try:
        tty.write(f"\n[netgate] Network access requested\n")
        tty.write(f"  Domain:    {hostname}\n")
        tty.write(f"  Container: {CONFIG['container_name']}\n")
        tty.write(f"\n  1) Deny\n  2) Allow (this project)\n  3) Allow (always)\n")
        tty.write(f"\nChoice [1]: ")
        tty.flush()
        choice = tty.readline().strip()
        if choice == "3":
            return "always"
        elif choice == "2":
            return "project"
        else:
            return "deny"
    except (OSError, EOFError):
        return "deny"
    finally:
        tty.close()


def save_decision(hostname: str, decision: str):
    """Persist the user's decision to the appropriate allowlist file."""
    os.makedirs(CONFIG["config_dir"], exist_ok=True)

    if decision == "always":
        path = os.path.join(CONFIG["config_dir"], "global.json")
        key = "domains"
    elif decision == "project":
        path = os.path.join(CONFIG["config_dir"], f"project-{CONFIG['project_hash']}.json")
        key = "domains"
    elif decision == "deny":
        path = os.path.join(CONFIG["config_dir"], f"project-{CONFIG['project_hash']}.json")
        key = "denied"
    else:
        return

    fd = None
    try:
        fd = open(path, "r+") if os.path.isfile(path) else open(path, "w+")
        fcntl.flock(fd, fcntl.LOCK_EX)
        content = fd.read()
        data = json.loads(content) if content.strip() else {}
        entries = data.setdefault(key, [])
        if hostname not in entries:
            entries.append(hostname)
        fd.seek(0)
        fd.truncate()
        json.dump(data, fd, indent=2)
        fd.write("\n")
    except (json.JSONDecodeError, OSError) as e:
        log(f"Error saving decision: {e}")
    finally:
        if fd:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()


# --- Proxy handler ---

class NetgateHandler(http.server.BaseHTTPRequestHandler):
    """Forward proxy handler with domain gating."""

    def do_CONNECT(self):
        """Handle HTTPS tunneling via CONNECT method."""
        host_port = self.path.split(":")
        hostname = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 443

        if not gate_domain(hostname):
            self.send_error(403, f"Domain {hostname} blocked by netgate")
            return

        try:
            remote = socket.create_connection((hostname, port), timeout=30)
        except OSError as e:
            self.send_error(502, f"Cannot connect to {hostname}:{port}: {e}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        self._tunnel(self.connection, remote)

    def do_GET(self):
        self._handle_http()

    def do_POST(self):
        self._handle_http()

    def do_PUT(self):
        self._handle_http()

    def do_DELETE(self):
        self._handle_http()

    def do_PATCH(self):
        self._handle_http()

    def do_HEAD(self):
        self._handle_http()

    def do_OPTIONS(self):
        self._handle_http()

    def _handle_http(self):
        """Handle plain HTTP forwarding."""
        url = urllib.parse.urlparse(self.path)
        hostname = url.hostname
        if not hostname:
            self.send_error(400, "Cannot determine hostname")
            return

        if not gate_domain(hostname):
            self.send_error(403, f"Domain {hostname} blocked by netgate")
            return

        port = url.port or 80
        try:
            remote = socket.create_connection((hostname, port), timeout=30)
        except OSError as e:
            self.send_error(502, f"Cannot connect to {hostname}:{port}: {e}")
            return

        # Reconstruct the request line with path only (not full URL)
        path = url.path or "/"
        if url.query:
            path += f"?{url.query}"

        # Forward request
        request_line = f"{self.command} {path} {self.request_version}\r\n"
        headers = ""
        for key, val in self.headers.items():
            if key.lower() not in ("proxy-connection",):
                headers += f"{key}: {val}\r\n"
        headers += "\r\n"

        try:
            remote.sendall((request_line + headers).encode())

            # Forward body if present
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                remote.sendall(body)

            # Relay response back
            self._tunnel(self.connection, remote)
        except OSError:
            pass
        finally:
            remote.close()

    def _tunnel(self, client: socket.socket, remote: socket.socket):
        """Bidirectional data relay between client and remote."""
        sockets = [client, remote]
        try:
            while True:
                readable, _, errors = select.select(sockets, [], sockets, 60)
                if errors:
                    break
                if not readable:
                    continue  # timeout, keep waiting
                for sock in readable:
                    other = remote if sock is client else client
                    try:
                        data = sock.recv(BUF_SIZE)
                    except OSError:
                        data = b""
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        except (OSError, ValueError):
            pass
        finally:
            try:
                remote.close()
            except OSError:
                pass

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        pass


class ThreadingProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def gate_domain(hostname: str) -> bool:
    """Gate a domain: check allowlist, prompt if needed. Returns True if allowed."""
    decision = check_domain(hostname)

    if decision == "allow":
        log(f"ALLOW {hostname}")
        return True
    if decision == "deny":
        log(f"DENY {hostname} (cached)")
        return False

    # Need to prompt — deduplicate concurrent requests for the same domain
    is_prompter = False
    with _pending_lock:
        if hostname in _pending_domains:
            event = _pending_domains[hostname]
        else:
            event = threading.Event()
            _pending_domains[hostname] = event
            is_prompter = True

    if not is_prompter:
        # Wait for the prompter thread's result
        event.wait(timeout=135)
        result = _pending_results.get(hostname, "deny")
    else:
        # We are the prompter
        log(f"PROMPT {hostname}")
        result = prompt_user(hostname)
        log(f"PROMPT {hostname} -> {result}")
        save_decision(hostname, result)
        _pending_results[hostname] = result
        event.set()
        # Clean up
        with _pending_lock:
            _pending_domains.pop(hostname, None)

    return result in ("always", "project")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Domain-gating forward proxy for cage")
    parser.add_argument("--project-hash", required=True, help="8-char repo hash from cage")
    parser.add_argument("--container-name", default="unknown", help="Container name for dialogs")
    parser.add_argument("--port", type=int, default=0, help="Port to bind (0 = auto)")
    parser.add_argument("--config-dir", default=os.path.expanduser("~/.claude/netgate"),
                        help="Directory for allowlist files")
    args = parser.parse_args()

    CONFIG["project_hash"] = args.project_hash
    CONFIG["container_name"] = args.container_name
    CONFIG["config_dir"] = args.config_dir
    CONFIG["script_dir"] = os.path.dirname(os.path.abspath(__file__))

    os.makedirs(args.config_dir, exist_ok=True)

    server = ThreadingProxyServer(("0.0.0.0", args.port), NetgateHandler)
    actual_port = server.server_address[1]

    # Startup protocol: cage reads these lines from stdout
    print(f"PORT={actual_port}", flush=True)
    print("READY", flush=True)

    log(f"Listening on 0.0.0.0:{actual_port}")
    log(f"Config dir: {args.config_dir}")
    log(f"Project hash: {args.project_hash}")

    def shutdown_handler(signum, frame):
        log("Shutting down")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Interrupted")
        server.shutdown()


if __name__ == "__main__":
    main()
