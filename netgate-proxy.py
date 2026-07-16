#!/usr/bin/env python3
"""
netgate-proxy.py — Domain-gating forward proxy for cage.

Runs on the host. Container traffic routes through it via HTTP_PROXY/HTTPS_PROXY.
For each unknown domain, prompts the user (macOS dialog or terminal prompt on Linux)
and holds the connection until the user decides.
"""

import argparse
import base64
import fcntl
import hmac
import http.server
import ipaddress
import json
import os
import platform
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import unicodedata
import urllib.parse

IS_MACOS = platform.system() == "Darwin"

# --- Global state for prompt deduplication ---
_pending_lock = threading.Lock()


class PendingPrompt:
    def __init__(self):
        self.event = threading.Event()
        self.result = "deny"


_pending_domains: dict[str, PendingPrompt] = {}

# --- Configuration (set from CLI args) ---
CONFIG = {
    "project_hash": "",
    "container_name": "unknown",
    "config_dir": "",
    "script_dir": "",
}

BUF_SIZE = 65536
MAX_REQUEST_BODY = 64 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 64
MAX_CONCURRENT_PROMPTS = 4
UPSTREAM_CONNECT_TIMEOUT = 15
CLIENT_IO_TIMEOUT = 30
TUNNEL_IDLE_TIMEOUT = 120
ALLOWED_CONNECT_PORTS = frozenset({443, 8443})
_prompt_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PROMPTS)


class RequestRejected(ValueError):
    """A client request that the proxy must reject without forwarding."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class UnsafeDestinationError(ValueError):
    """A destination that resolves to a non-public address."""


def _contains_control_characters(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def sanitize_prompt_value(value: str, limit: int = 253) -> str:
    """Return a bounded, terminal-safe value for a user-visible prompt."""
    cleaned = "".join(
        "?" if unicodedata.category(char).startswith("C") else char
        for char in str(value)
    )
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def normalize_hostname(hostname: str) -> str:
    """Validate and canonicalize an IP literal or DNS hostname."""
    if not isinstance(hostname, str) or not hostname:
        raise ValueError("empty hostname")
    if _contains_control_characters(hostname) or any(char.isspace() for char in hostname):
        raise ValueError("hostname contains whitespace or control characters")

    candidate = hostname[:-1] if hostname.endswith(".") else hostname
    if not candidate or len(candidate) > 253:
        raise ValueError("hostname has an invalid length")

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass

    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("hostname is not valid IDNA") from exc

    labels = ascii_hostname.split(".")
    for label in labels:
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
        ):
            raise ValueError("hostname contains an invalid DNS label")
    return ascii_hostname


def _is_public_destination(address: str) -> bool:
    """Return True only for globally routable unicast addresses."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not getattr(ip, "is_site_local", False)
        and not ip.is_unspecified
    )


def reject_unsafe_ip_literal(hostname: str) -> None:
    """Reject an unsafe numeric target without performing DNS or prompting."""
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return
    if not _is_public_destination(hostname):
        raise UnsafeDestinationError(f"{hostname} is not a public destination")


def resolve_public_endpoints(hostname: str, port: int) -> list[tuple]:
    """Resolve once and return only pinned, public TCP endpoints.

    If DNS returns even one unsafe address, reject the complete answer instead of
    selecting a public sibling. This prevents a hostname from alternating between
    an approved public address and a private target.
    """
    hostname = normalize_hostname(hostname)
    try:
        results = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise OSError(f"DNS resolution failed for {hostname}") from exc

    endpoints: list[tuple] = []
    unsafe_addresses: list[str] = []
    seen: set[tuple] = set()
    for family, socktype, proto, _canonname, sockaddr in results:
        address = sockaddr[0]
        try:
            is_public = _is_public_destination(address)
        except ValueError:
            is_public = False
        if not is_public:
            unsafe_addresses.append(address)
            continue

        key = (family, socktype, proto, sockaddr)
        if key not in seen:
            seen.add(key)
            endpoints.append(key)

    if unsafe_addresses:
        unique = ", ".join(sorted(set(unsafe_addresses)))
        raise UnsafeDestinationError(
            f"{hostname} resolves to blocked address(es): {unique}"
        )
    if not endpoints:
        raise OSError(f"No usable public address found for {hostname}")
    return endpoints


def connect_resolved_endpoints(
    endpoints: list[tuple], timeout: int = UPSTREAM_CONNECT_TIMEOUT
) -> socket.socket:
    """Connect directly to a previously resolved endpoint without another DNS lookup."""
    last_error: OSError | None = None
    for family, socktype, proto, sockaddr in endpoints:
        remote = socket.socket(family, socktype, proto)
        try:
            remote.settimeout(timeout)
            remote.connect(sockaddr)
            return remote
        except OSError as exc:
            last_error = exc
            remote.close()
    raise OSError("Cannot connect to any validated destination") from last_error


def _parse_connect_authority(authority: str) -> tuple[str, int]:
    if not authority or _contains_control_characters(authority) or any(
        char.isspace() for char in authority
    ):
        raise RequestRejected(400, "Invalid CONNECT authority")
    try:
        parsed = urllib.parse.urlsplit(f"//{authority}")
        port = parsed.port or 443
    except ValueError as exc:
        raise RequestRejected(400, "Invalid CONNECT authority") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RequestRejected(400, "Invalid CONNECT authority")
    try:
        hostname = normalize_hostname(parsed.hostname)
    except ValueError as exc:
        raise RequestRejected(400, f"Invalid CONNECT hostname: {exc}") from exc
    if port not in ALLOWED_CONNECT_PORTS:
        raise RequestRejected(403, f"CONNECT port {port} is not allowed")
    return hostname, port


def validate_request_body_headers(headers) -> int:
    """Validate framing and return a bounded request body length."""
    transfer_encodings = headers.get_all("Transfer-Encoding", [])
    if transfer_encodings:
        raise RequestRejected(501, "Transfer-Encoding request bodies are not supported")

    raw_lengths: list[str] = []
    for value in headers.get_all("Content-Length", []):
        raw_lengths.extend(part.strip() for part in value.split(","))
    if not raw_lengths:
        return 0
    if (
        len(raw_lengths) != 1
        or not raw_lengths[0]
        or len(raw_lengths[0]) > 20
        or any(char < "0" or char > "9" for char in raw_lengths[0])
    ):
        raise RequestRejected(400, "Invalid Content-Length")

    content_length = int(raw_lengths[0])
    if content_length > MAX_REQUEST_BODY:
        raise RequestRejected(413, "Request body exceeds the netgate limit")
    return content_length


def _linux_docker_interfaces() -> list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]]:
    """Discover default/local Docker bridge addresses without invoking host commands."""
    if platform.system() != "Linux":
        return []

    interfaces: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]] = []
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError:
        return interfaces

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if name != "docker0" and not name.startswith("br-"):
                continue
            request = struct.pack("256s", name.encode("ascii", "ignore")[:15])
            try:
                address_bytes = fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24]
                netmask_bytes = fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24]
            except OSError:
                continue
            address = ipaddress.IPv4Address(address_bytes)
            netmask = str(ipaddress.IPv4Address(netmask_bytes))
            network = ipaddress.IPv4Network((address, netmask), strict=False)
            interfaces.append((address, network))
    finally:
        sock.close()
    return interfaces


def client_is_allowed(
    peer_address: str,
    local_address: str,
    docker_interfaces: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]],
) -> bool:
    """Allow loopback or traffic arriving through a discovered Docker bridge only."""
    try:
        peer = ipaddress.ip_address(peer_address.split("%", 1)[0])
        local = ipaddress.ip_address(local_address.split("%", 1)[0])
    except ValueError:
        return False
    if peer.is_loopback and local.is_loopback:
        return True
    for bridge_address, bridge_network in docker_interfaces:
        if local == bridge_address and (peer.is_loopback or peer in bridge_network):
            return True
    return False


def expected_proxy_authorization(token: str) -> str:
    credentials = base64.b64encode(f"cage:{token}".encode("ascii")).decode("ascii")
    return f"Basic {credentials}"


def proxy_authorization_valid(headers, token: str) -> bool:
    if not token:
        return True
    supplied = headers.get("Proxy-Authorization", "")
    return isinstance(supplied, str) and hmac.compare_digest(
        supplied,
        expected_proxy_authorization(token),
    )


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
    # Values are supplied through the environment, never interpolated into
    # AppleScript source. This prevents quotes/control characters from changing
    # the script while still allowing readable Unicode names in the dialog.
    script = (
        'set requestedDomain to system attribute "CAGE_NETGATE_PROMPT_DOMAIN"\n'
        'set containerName to system attribute "CAGE_NETGATE_PROMPT_CONTAINER"\n'
        'set dialogResult to display dialog "Network access requested" & return & return '
        '& "Domain: " & requestedDomain & return '
        '& "Container: " & containerName & return & return '
        '& "Allow this domain?" '
        'buttons {"Deny", "Allow (project)", "Allow (always)"} '
        'default button "Deny" with icon caution giving up after 120\n'
        'if gave up of dialogResult then return "Deny"\n'
        'return button returned of dialogResult'
    )
    env = os.environ.copy()
    env["CAGE_NETGATE_PROMPT_DOMAIN"] = sanitize_prompt_value(hostname)
    env["CAGE_NETGATE_PROMPT_CONTAINER"] = sanitize_prompt_value(CONFIG["container_name"])
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=130,
            env=env,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return "deny"
        if output == "Allow (always)":
            return "always"
        if output == "Allow (project)":
            return "project"
        return "deny"
    except (subprocess.TimeoutExpired, OSError):
        return "deny"


def _prompt_terminal(hostname: str) -> str:
    """Linux/other: prompt via terminal (/dev/tty)."""
    safe_hostname = sanitize_prompt_value(hostname)
    safe_container_name = sanitize_prompt_value(CONFIG["container_name"])
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        log(f"Cannot open /dev/tty for prompt, denying {safe_hostname}")
        return "deny"
    try:
        tty.write(f"\n[netgate] Network access requested\n")
        tty.write(f"  Domain:    {safe_hostname}\n")
        tty.write(f"  Container: {safe_container_name}\n")
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

    def setup(self):
        super().setup()
        self.connection.settimeout(CLIENT_IO_TIMEOUT)

    def handle_expect_100(self):
        try:
            validate_request_body_headers(self.headers)
        except RequestRejected as exc:
            self.close_connection = True
            self.send_error(exc.status, exc.message)
            return False
        return super().handle_expect_100()

    def _require_proxy_authorization(self) -> bool:
        token = getattr(self.server, "auth_token", "")
        if proxy_authorization_valid(self.headers, token):
            return True
        self.close_connection = True
        self.send_response(407, "Proxy Authentication Required")
        self.send_header("Proxy-Authenticate", 'Basic realm="cage-netgate"')
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_CONNECT(self):
        """Handle HTTPS tunneling via CONNECT method."""
        if not self._require_proxy_authorization():
            return
        try:
            if validate_request_body_headers(self.headers):
                raise RequestRejected(400, "CONNECT requests cannot contain a body")
            hostname, port = _parse_connect_authority(self.path)
            reject_unsafe_ip_literal(hostname)
        except RequestRejected as exc:
            self.send_error(exc.status, exc.message)
            return
        except UnsafeDestinationError as exc:
            log(f"BLOCK {hostname}: {exc}")
            self.send_error(403, "Destination resolves to a blocked address")
            return

        if not gate_domain(hostname):
            self.send_error(403, f"Domain {hostname} blocked by netgate")
            return

        try:
            endpoints = resolve_public_endpoints(hostname, port)
            remote = connect_resolved_endpoints(endpoints)
        except UnsafeDestinationError as exc:
            log(f"BLOCK {hostname}: {exc}")
            self.send_error(403, "Destination resolves to a blocked address")
            return
        except OSError:
            self.send_error(502, f"Cannot connect to {hostname}:{port}")
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
        if not self._require_proxy_authorization():
            return
        try:
            content_length = validate_request_body_headers(self.headers)
            url = urllib.parse.urlsplit(self.path)
            if (
                url.scheme.lower() != "http"
                or not url.netloc
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.fragment
            ):
                raise RequestRejected(400, "Expected an absolute HTTP URL")
            if _contains_control_characters(url.path) or _contains_control_characters(url.query):
                raise RequestRejected(400, "Request target contains control characters")
            hostname = normalize_hostname(url.hostname)
            try:
                port = url.port or 80
            except ValueError as exc:
                raise RequestRejected(400, "Invalid destination port") from exc
            if not 1 <= port <= 65535:
                raise RequestRejected(400, "Invalid destination port")
            headers = self._forward_headers(hostname, port)
            reject_unsafe_ip_literal(hostname)
        except RequestRejected as exc:
            self.send_error(exc.status, exc.message)
            return
        except UnsafeDestinationError as exc:
            log(f"BLOCK {hostname}: {exc}")
            self.send_error(403, "Destination resolves to a blocked address")
            return
        except ValueError as exc:
            self.send_error(400, f"Invalid hostname: {exc}")
            return

        if not gate_domain(hostname):
            self.send_error(403, f"Domain {hostname} blocked by netgate")
            return

        try:
            endpoints = resolve_public_endpoints(hostname, port)
            remote = connect_resolved_endpoints(endpoints)
        except UnsafeDestinationError as exc:
            log(f"BLOCK {hostname}: {exc}")
            self.send_error(403, "Destination resolves to a blocked address")
            return
        except OSError:
            self.send_error(502, f"Cannot connect to {hostname}:{port}")
            return

        # Reconstruct the request line with path only (not full URL)
        path = url.path or "/"
        if url.query:
            path += f"?{url.query}"

        # Forward request
        request_line = f"{self.command} {path} {self.request_version}\r\n"
        try:
            remote.sendall((request_line + headers).encode("iso-8859-1"))

            # Stream the already-bounded body instead of allocating it at once.
            remaining = content_length
            while remaining:
                chunk = self.rfile.read(min(BUF_SIZE, remaining))
                if not chunk:
                    raise OSError("Client closed before sending the declared body")
                remote.sendall(chunk)
                remaining -= len(chunk)

            # Relay response back
            self._tunnel(self.connection, remote)
        except OSError:
            pass
        finally:
            remote.close()

    def _forward_headers(self, hostname: str, port: int) -> str:
        """Create safe origin-form request headers for the upstream server."""
        hop_by_hop = {
            "connection",
            "expect",
            "host",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "proxy-connection",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }
        output: list[str] = []
        for key, value in self.headers.items():
            if _contains_control_characters(key) or _contains_control_characters(value):
                raise RequestRejected(400, "Request headers contain control characters")
            if key.lower() not in hop_by_hop:
                output.append(f"{key}: {value}\r\n")

        host_header = f"[{hostname}]" if ":" in hostname else hostname
        if port != 80:
            host_header = f"{host_header}:{port}"
        output.append(f"Host: {host_header}\r\n")
        output.append("Connection: close\r\n")
        output.append("\r\n")
        return "".join(output)

    def _tunnel(self, client: socket.socket, remote: socket.socket):
        """Bidirectional data relay between client and remote."""
        sockets = [client, remote]
        try:
            while True:
                readable, _, errors = select.select(
                    sockets, [], sockets, TUNNEL_IDLE_TIMEOUT
                )
                if errors:
                    break
                if not readable:
                    break
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
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(
        self,
        server_address,
        request_handler_class,
        max_workers: int = MAX_CONCURRENT_REQUESTS,
        docker_interfaces=None,
        auth_token: str = "",
    ):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self.auth_token = auth_token
        self.docker_interfaces = (
            _linux_docker_interfaces()
            if docker_interfaces is None
            else docker_interfaces
        )
        super().__init__(server_address, request_handler_class)

    def verify_request(self, request, client_address):
        # Authenticated requests are accepted from any interface. The HTTP
        # handler validates the per-launch token before DNS, prompting, or any
        # upstream connection. This is portable across Docker Desktop, Colima,
        # rootless/custom bridges, and native Linux host-gateway networking.
        if self.auth_token:
            return True
        try:
            local_address = request.getsockname()[0]
        except OSError:
            return False
        allowed = client_is_allowed(
            client_address[0], local_address, self.docker_interfaces
        )
        if not allowed:
            log(
                "REJECT client "
                f"{sanitize_prompt_value(client_address[0])} via "
                f"{sanitize_prompt_value(local_address)}"
            )
        return allowed

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


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
            pending = _pending_domains[hostname]
        else:
            pending = PendingPrompt()
            _pending_domains[hostname] = pending
            is_prompter = True

    if not is_prompter:
        # Wait for the prompter thread's result
        if pending.event.wait(timeout=135):
            result = pending.result
        else:
            result = "deny"
    else:
        # We are the prompter
        result = "deny"
        persist_result = False
        try:
            if not _prompt_slots.acquire(blocking=False):
                log(f"DENY {hostname} (prompt limit reached)")
                result = "deny"
            else:
                try:
                    log(f"PROMPT {hostname}")
                    result = prompt_user(hostname)
                    if result not in ("always", "project", "deny"):
                        result = "deny"
                    log(f"PROMPT {hostname} -> {result}")
                    persist_result = True
                except Exception as exc:
                    log(f"PROMPT {hostname} failed: {exc}")
                    result = "deny"
                finally:
                    _prompt_slots.release()
            if persist_result:
                save_decision(hostname, result)
        finally:
            pending.result = result
            pending.event.set()
            with _pending_lock:
                if _pending_domains.get(hostname) is pending:
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
    auth_token = os.environ.get("CAGE_NETGATE_AUTH_TOKEN", "")
    if not re.fullmatch(r"[0-9a-f]{64}", auth_token):
        parser.error("CAGE_NETGATE_AUTH_TOKEN must contain a fresh 64-character hex token")

    CONFIG["project_hash"] = args.project_hash
    CONFIG["container_name"] = args.container_name
    CONFIG["config_dir"] = args.config_dir
    CONFIG["script_dir"] = os.path.dirname(os.path.abspath(__file__))

    os.makedirs(args.config_dir, exist_ok=True)

    server = ThreadingProxyServer(
        ("0.0.0.0", args.port),
        NetgateHandler,
        auth_token=auth_token,
    )
    actual_port = server.server_address[1]

    # Startup protocol: cage reads these lines from stdout
    print(f"PORT={actual_port}", flush=True)
    print("READY", flush=True)

    bridge_addresses = ", ".join(
        str(address) for address, _network in server.docker_interfaces
    ) or "none discovered"
    log(
        f"Listening on 0.0.0.0:{actual_port}; per-launch proxy authentication "
        f"required (discovered bridge addresses: {bridge_addresses})"
    )
    log(f"Config dir: {args.config_dir}")
    log(f"Project hash: {args.project_hash}")

    def shutdown_handler(signum, frame):
        log("Shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Interrupted")
        server.shutdown()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
