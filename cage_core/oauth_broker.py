"""Shared, host-owned MCP OAuth transport (no refresh tokens in agent clients).

The lifetime lease excludes older Cage launchers. A private control connection
owns each client's routes; EOF revokes them. Only the broker reads/refreshes the
Codex file store. HTTP sessions remain per client, not per credential. Login and
logout borrow the same owner's credential mutex via a maintenance connection.
"""
from __future__ import annotations

import contextlib
import base64
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import select
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state.oauth import (
    MAX_CREDENTIAL_BYTES, OAuthSessionLease, SyncError, atomic_credential_write,
    _canonical_json, _credential_blob, _read_regular,
)

PROTOCOL = 1
MANIFEST = ".cage-oauth-broker.json"
TOKEN_ENV = "CAGE_OAUTH_BROKER_TOKEN"
MAX_BODY = 8 * 1024 * 1024
MAX_CONTROL = 256 * 1024
TIMEOUT = 60
IDLE_SECONDS = 5
NAME = re.compile(r"[A-Za-z0-9_-]+\Z")


class BrokerError(SyncError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerError("OAuth broker refused an upstream redirect")


def checked_url(value: Any) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise BrokerError("invalid OAuth endpoint")
    parsed = urllib.parse.urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise BrokerError("OAuth endpoints require HTTPS (HTTP is allowed only on loopback)")
    return value


class SelectedProxy(urllib.request.ProxyHandler):
    def proxy_open(self, request, proxy, scheme):
        # Netgate is a selected capability, never bypassed by ambient NO_PROXY.
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise BrokerError("invalid selected OAuth proxy")
        if parsed.username is not None:
            credentials = urllib.parse.unquote(parsed.username) + ":" + urllib.parse.unquote(parsed.password or "")
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            request.add_header("Proxy-Authorization", "Basic " + encoded)
        request.set_proxy(f"{parsed.hostname}:{parsed.port}", "http")
        return None


def opener(proxy: str = ""):
    # Explicit handler prevents inheriting the first launcher's proxy environment.
    return urllib.request.build_opener(
        SelectedProxy({"https": proxy, "http": proxy} if proxy else {}),
        NoRedirect(),
    )


def json_request(url: str, *, proxy: str = "", data: bytes | None = None) -> dict:
    request = urllib.request.Request(checked_url(url), data=data, headers={
        "Accept": "application/json",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {}),
    })
    with opener(proxy).open(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_CREDENTIAL_BYTES + 1)
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise BrokerError("OAuth response exceeds size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise BrokerError("invalid OAuth response")
    return result


def authorization_metadata(server: dict, entry: dict, proxy: str) -> tuple[str, str]:
    resource = checked_url(server.get("oauth_resource") or server["url"])
    parsed = urllib.parse.urlsplit(resource)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    issuer = entry.get("issuer")
    for suffix in dict.fromkeys((parsed.path.rstrip("/"), "")):
        try:
            metadata = json_request(origin + "/.well-known/oauth-protected-resource" + suffix, proxy=proxy)
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 405}:
                continue
            raise
        if metadata.get("resource", "").rstrip("/") != resource.rstrip("/"):
            raise BrokerError("OAuth resource metadata does not match the selected MCP")
        issuers = metadata.get("authorization_servers")
        if not isinstance(issuers, list) or not issuers:
            raise BrokerError("OAuth resource has no authorization server")
        if issuer:
            if issuer not in issuers:
                raise BrokerError("OAuth issuer changed; run cage mcp login")
        elif len(issuers) == 1:
            issuer = issuers[0]
        else:
            raise BrokerError("ambiguous OAuth issuer; run cage mcp login")
        break
    # Older MCP servers publish authorization metadata at their own origin.
    issuer = checked_url(issuer or origin)
    issuer_parts = urllib.parse.urlsplit(issuer)
    base = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    path = issuer_parts.path.rstrip("/")
    candidates = [base + "/.well-known/oauth-authorization-server" + path,
                  issuer.rstrip("/") + "/.well-known/openid-configuration"]
    for url in candidates:
        try:
            metadata = json_request(url, proxy=proxy)
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 405}:
                continue
            raise
        if metadata.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
            raise BrokerError("OAuth authorization metadata issuer mismatch")
        return issuer, checked_url(metadata.get("token_endpoint"))
    raise BrokerError("OAuth authorization metadata unavailable; run cage mcp login")


class CredentialOwner:
    def __init__(self, home: Path):
        self.path = str(home / ".credentials.json")
        self.lock = threading.RLock()

    def read(self):
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise BrokerError("host OAuth credentials must not be a symlink")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise BrokerError("host OAuth credentials must be a private user-owned regular file")
        return _credential_blob(self.path, "host OAuth credentials", normalize_mode=True)

    def write(self, value, observed):
        data = _canonical_json(value)
        if len(data) > MAX_CREDENTIAL_BYTES:
            raise BrokerError("OAuth credential store exceeds size limit")
        atomic_credential_write(self.path, data, observed)

    def access(self, server: dict, proxy: str = "", *, rejected: str = "") -> str:
        with self.lock:
            observed = self.read()
            entries = observed["value"] if observed else {}
            matches = [(key, value) for key, value in entries.items()
                       if isinstance(value, dict)
                       and value.get("server_name") in {server["name"], "local:" + server["name"]}
                       and value.get("server_url") == server["url"]
                       and not value.get("executor_owned")]
            if len(matches) != 1:
                raise BrokerError("MCP OAuth login missing or ambiguous; run cage mcp login")
            key, entry = matches[0]
            if server.get("oauth_client_id") and server["oauth_client_id"] != entry.get("client_id"):
                raise BrokerError("OAuth client changed; run cage mcp login")
            if not set(server.get("oauth_scopes") or []).issubset(set(entry.get("scopes") or [])):
                raise BrokerError("OAuth scopes changed; run cage mcp login")
            if entry.get("cage_refresh_pending"):
                raise BrokerError("OAuth refresh was interrupted; run cage mcp login")
            token = entry.get("access_token")
            expires = entry.get("expires_at")
            if isinstance(token, str) and token and token != rejected and (
                    expires is None or (isinstance(expires, (int, float)) and expires > time.time() * 1000 + 30000)):
                if any(c in token for c in "\r\n"):
                    raise BrokerError("invalid OAuth access token")
                return token
            refresh = entry.get("refresh_token")
            client = entry.get("client_id")
            if not isinstance(refresh, str) or not refresh or not isinstance(client, str) or not client:
                raise BrokerError("MCP OAuth authorization expired; run cage mcp login")
            issuer, endpoint = authorization_metadata(server, entry, proxy)
            # A crash/timeout after submitting a rotating token has unknown outcome.
            # Persist before spending it; never blindly reuse it after restart.
            entry["cage_refresh_pending"] = True
            entry["issuer"] = issuer
            self.write(entries, observed)
            observed = self.read()
            payload = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client,
                       "resource": server.get("oauth_resource") or server["url"]}
            fresh = json_request(endpoint, proxy=proxy, data=urllib.parse.urlencode(payload).encode())
            token = fresh.get("access_token")
            if not isinstance(token, str) or not token or fresh.get("token_type", "bearer").lower() != "bearer":
                raise BrokerError("invalid OAuth refresh response; run cage mcp login")
            if any(c in token for c in "\r\n"):
                raise BrokerError("invalid OAuth access token")
            entry["access_token"] = token
            if "refresh_token" in fresh:
                if not isinstance(fresh["refresh_token"], str) or not fresh["refresh_token"]:
                    raise BrokerError("invalid rotated OAuth credential")
                entry["refresh_token"] = fresh["refresh_token"]
            expiry = fresh.get("expires_in")
            if expiry is not None and (not isinstance(expiry, (float, int)) or expiry <= 0):
                raise BrokerError("invalid OAuth expiry")
            entry["expires_at"] = int((time.time() + expiry) * 1000) if expiry is not None else None
            if "scope" in fresh:
                if not isinstance(fresh["scope"], str):
                    raise BrokerError("invalid OAuth scopes")
                entry["scopes"] = fresh["scope"].split()
            entry.pop("cage_refresh_pending", None)
            self.write(entries, observed)
            return token


@dataclass
class ClientRoutes:
    servers: dict[str, dict]
    proxy: str
    token: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    sessions: dict[str, set[str]] = field(default_factory=dict)
    connections: set[socket.socket] = field(default_factory=set)
    closed: bool = False


class Broker:
    def __init__(self, home: Path):
        self.home = home
        self.owner = CredentialOwner(home)
        self.key = secrets.token_hex(32)
        self.lock = threading.RLock()
        self.clients: dict[str, ClientRoutes] = {}
        self.controls = 0
        self.last_used = time.monotonic()
        self.stopping = False
        self.capacity = threading.BoundedSemaphore(128)
        self.http = BoundedHTTPServer(("0.0.0.0", 0), ForwardHandler)
        self.control = ControlServer(("127.0.0.1", 0), ControlHandler)
        self.http.broker = self
        self.control.broker = self

    def revoke(self, ident):
        with self.lock:
            client = self.clients.pop(ident, None)
            if client:
                client.closed = True
                for connection in list(client.connections):
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
                        connection.close()
            self.last_used = time.monotonic()


class BoundedMixIn(socketserver.ThreadingMixIn):
    daemon_threads = True

    def process_request(self, request, address):
        if not self.broker.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.broker.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.broker.capacity.release()

    def handle_error(self, request, address):
        # Never log request paths, authorization headers or upstream errors.
        pass


class BoundedHTTPServer(BoundedMixIn, http.server.HTTPServer):
    pass


class ControlServer(BoundedMixIn, socketserver.TCPServer):
    allow_reuse_address = False


def wire_write(handle, value):
    handle.write(_canonical_json(value))
    handle.flush()


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        broker = self.server.broker
        ident = None
        maintenance = False
        registered = False
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(MAX_CONTROL + 1)
            if len(raw) > MAX_CONTROL or not raw.endswith(b"\n"):
                return
            request = json.loads(raw)
            if not hmac.compare_digest(str(request.get("key", "")), broker.key):
                return
            with broker.lock:
                if broker.stopping:
                    return
                broker.controls += 1
                registered = True
            if request.get("op") == "maintenance":
                broker.owner.lock.acquire()
                maintenance = True
                response = {"ok": True}
            elif request.get("op") == "attach":
                definitions = request.get("servers")
                if not isinstance(definitions, list) or not 1 <= len(definitions) <= 64:
                    raise BrokerError("invalid broker selection")
                servers = {}
                for server in definitions:
                    if not isinstance(server, dict) or not NAME.fullmatch(str(server.get("name", ""))):
                        raise BrokerError("invalid broker server")
                    checked_url(server.get("url"))
                    if server["name"] in servers:
                        raise BrokerError("duplicate broker server")
                    servers[server["name"]] = server
                proxy = request.get("proxy", "")
                if not isinstance(proxy, str):
                    raise BrokerError("invalid broker network route")
                ident = secrets.token_hex(16)
                client = ClientRoutes(servers, proxy)
                with broker.lock:
                    broker.clients[ident] = client
                response = {"ok": True, "id": ident, "token": client.token, "port": broker.http.server_port}
            else:
                raise BrokerError("invalid broker operation")
            wire_write(self.wfile, response)
            self.connection.settimeout(None)
            # The launcher holds this connection across the target's lifetime.
            # A killed launcher closes it in the kernel; no PID reuse or heartbeat lease.
            self.rfile.read(1)
        except (OSError, ValueError, SyncError):
            with contextlib.suppress(OSError):
                wire_write(self.wfile, {"ok": False, "error": "OAuth broker rejected the control request"})
        finally:
            if maintenance:
                broker.owner.lock.release()
            if ident:
                broker.revoke(ident)
            if registered:
                with broker.lock:
                    broker.controls -= 1
                    broker.last_used = time.monotonic()


class ForwardHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(TIMEOUT)

    def fail(self, status, message):
        data = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def forward(self):
        broker = self.server.broker
        client = None
        started = False
        try:
            parts = self.path.split("/")
            if len(parts) != 3 or self.headers.get("Origin"):
                self.fail(403, "OAuth broker access denied")
                return
            _, ident, name = parts
            with broker.lock:
                client = broker.clients.get(ident)
                if (not client or client.closed or name not in client.servers
                        or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + client.token)):
                    self.fail(403, "OAuth broker access denied")
                    return
                client.connections.add(self.connection)
                upstream_session = self.headers.get("Mcp-Session-Id")
                if upstream_session and upstream_session not in client.sessions.get(name, set()):
                    self.fail(404, "Unknown MCP session")
                    return
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                self.fail(400, "Unsupported request framing")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                self.fail(413, "MCP request too large")
                return
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                return
            server = client.servers[name]
            token = broker.owner.access(server, client.proxy)
            headers = {k: v for k, v in self.headers.items() if k.lower() in {
                "accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id"}}
            headers["Authorization"] = "Bearer " + token
            request = urllib.request.Request(server["url"], data=body, headers=headers, method=self.command)
            try:
                upstream = opener(client.proxy).open(request, timeout=TIMEOUT)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    exc.close()
                    broker.owner.access(server, client.proxy, rejected=token)
                    self.fail(503, "MCP authorization refreshed; retry the request")
                    return
                upstream = exc
            with upstream:
                with broker.lock:
                    if client.closed:
                        return
                    session = upstream.headers.get("Mcp-Session-Id")
                    if session:
                        sessions = client.sessions.setdefault(name, set())
                        if len(session) > 1024 or len(sessions) >= 256:
                            raise BrokerError("MCP session limit exceeded")
                        sessions.add(session)
                self.send_response(upstream.status)
                for key in ("Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version", "Retry-After"):
                    value = upstream.headers.get(key)
                    if value:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                started = True
                # read1 preserves SSE latency; read(size) could buffer until 64 KiB.
                while not client.closed:
                    chunk = upstream.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except BrokerError as exc:
            if not started:
                with contextlib.suppress(OSError):
                    self.fail(503, str(exc))
        except (OSError, ValueError, SyncError, http.client.HTTPException):
            if not started:
                with contextlib.suppress(OSError):
                    self.fail(503, "MCP OAuth unavailable; check cage mcp login")
        finally:
            if client:
                with broker.lock:
                    client.connections.discard(self.connection)


def read_manifest(home: Path):
    blob = _read_regular(str(home / MANIFEST), 4096, "OAuth broker manifest")
    if blob is None:
        return None
    info = os.lstat(home / MANIFEST)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise BrokerError("OAuth broker manifest must be private and user-owned")
    return json.loads(blob["raw"])


def serve(home: Path) -> int:
    # A legacy session retains its lease: never replace/delete its lock.
    try:
        lease = OAuthSessionLease.acquire(home, create=True)
    except SyncError:
        print("OAuth broker cannot start: finish older Cage sessions sharing this auth directory first", file=sys.stderr)
        return 1
    with lease:
        broker = Broker(home)
        manifest = {"pid": os.getpid(), "protocol": PROTOCOL, "port": broker.control.server_address[1], "key": broker.key}
        path = home / MANIFEST
        # The old manifest is never trusted for writes and must be a safe file.
        read_manifest(home)
        descriptor, staged = tempfile.mkstemp(prefix=".cage-oauth-broker-", dir=home)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, _canonical_json(manifest))
            os.fsync(descriptor)
            os.replace(staged, path)
        finally:
            os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (broker.http, broker.control)]
        for thread in threads:
            thread.start()
        print("READY", flush=True)
        try:
            while True:
                time.sleep(0.2)
                with broker.lock:
                    if broker.controls == 0 and time.monotonic() - broker.last_used > IDLE_SECONDS:
                        broker.stopping = True
                        break
        finally:
            broker.control.shutdown()
            broker.http.shutdown()
            broker.control.server_close()
            broker.http.server_close()
            with contextlib.suppress(OSError):
                path.unlink()
    return 0


@dataclass
class BrokerConnection:
    socket: socket.socket
    stream: Any
    result: dict = field(repr=False)

    def poll(self):
        if self.socket.fileno() < 0:
            return 1
        readable, _, _ = select.select([self.socket], [], [], 0)
        if readable:
            return 1
        return None

    def close(self) -> int:
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.stream.close()
        self.socket.close()
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def connect(home: Path, install_root: Path, *, servers: list[dict] | None = None,
            proxy: str = "") -> BrokerConnection:
    home = home.expanduser().absolute()
    # Validate without acquiring the exclusive legacy lease when a broker exists.
    if home.is_symlink():
        raise BrokerError("OAuth directory must not be a symlink")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BrokerError("OAuth directory must be owned by the current user")
    process = None
    started = time.monotonic()
    deadline = started + 10
    while time.monotonic() < deadline:
        manifest = read_manifest(home)
        if manifest:
            if manifest.get("protocol") != PROTOCOL:
                raise BrokerError("OAuth broker version changed; finish existing sessions first")
            connection = None
            stream = None
            try:
                connection = socket.create_connection(("127.0.0.1", int(manifest["port"])), timeout=2)
                connection.settimeout(TIMEOUT + 10)
                stream = connection.makefile("rwb")
                wire_write(stream, {"key": manifest["key"], "op": "attach" if servers else "maintenance",
                                    "servers": servers, "proxy": proxy})
                response = stream.readline(MAX_CONTROL + 1)
                result = json.loads(response)
                if not result.get("ok"):
                    raise BrokerError("OAuth broker rejected session registration")
                connection.settimeout(None)
                return BrokerConnection(connection, stream, result)
            except (OSError, ValueError, BrokerError):
                if stream:
                    stream.close()
                if connection:
                    connection.close()
        if process is None:
            process = subprocess.Popen(
                [sys.executable, "-I", str(install_root / "cage-main.py"), "_oauth-broker", str(home)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(install_root), start_new_session=True, close_fds=True,
            )
        elif process.poll() is not None:
            # Another simultaneous launcher may have won the lifetime lease.
            time.sleep(0.1)
            if time.monotonic() - started > 2 and not read_manifest(home):
                raise BrokerError("OAuth broker could not start; finish older Cage sessions sharing this auth directory first")
        time.sleep(0.05)
    raise BrokerError("OAuth broker unavailable; finish older Cage sessions sharing this auth directory first")


def selected_servers(servers: list[dict]) -> list[dict]:
    selected = []
    for definition in servers:
        if definition.get("auth") != "oauth":
            continue
        server = dict(definition)
        client_env = server.pop("oauth_client_id_env_var", None)
        if client_env:
            value = os.environ.get(client_env)
            if not value:
                raise BrokerError("OAuth client environment variable is missing")
            server["oauth_client_id"] = value
        selected.append(server)
    return selected


def routed_servers(servers: list[dict], connection: BrokerConnection, host: str) -> list[dict]:
    result = []
    for server in servers:
        if server.get("auth") == "oauth":
            result.append({"name": server["name"], "type": "http",
                           "url": f"http://{host}:{connection.result['port']}/{connection.result['id']}/{server['name']}",
                           "bearer_token_env_var": TOKEN_ENV})
        else:
            result.append(dict(server))
    return result
