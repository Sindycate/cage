"""Behavioral tests with a rotating OAuth provider and real broker processes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cage_core import oauth_broker as ob
from cage_core.state.oauth import OAuthSessionLease

ROOT = Path(__file__).resolve().parents[1]


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value, status=200, session=None):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        if session:
            self.send_header('Mcp-Session-Id', session)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        base = self.server.base
        if self.path.startswith('/.well-known/oauth-protected-resource'):
            self.send({'resource': base + '/mcp', 'authorization_servers': [base]})
        elif self.path == '/.well-known/oauth-authorization-server':
            self.send({'issuer': base, 'token_endpoint': base + '/token'})
        else:
            self.send({}, 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/token':
            params = urllib.parse.parse_qs(body.decode())
            with self.server.lock:
                self.server.refreshes += 1
                if params.get('refresh_token') != [self.server.refresh]:
                    self.send({'error': 'invalid_grant'}, 400)
                    return
                self.server.refresh = 'rotated-' + str(self.server.refreshes)
                self.server.access = 'access-' + str(self.server.refreshes)
            time.sleep(.1)  # Force refresh contention between clients.
            if self.server.lose_response:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send({'access_token': self.server.access, 'refresh_token': self.server.refresh,
                       'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'read'})
        elif self.path == '/mcp':
            if self.headers.get('Authorization') != 'Bearer ' + self.server.access:
                self.send({}, 401)
                return
            request = json.loads(body)
            session = self.headers.get('Mcp-Session-Id')
            if not session:
                with self.server.lock:
                    self.server.sessions += 1
                    session = str(self.server.sessions)
            if request.get('method') == 'initialize':
                self.send({'jsonrpc': '2.0', 'id': request.get('id'), 'result': {
                    'session': session, 'protocolVersion': '2024-11-05',
                    'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}}, session=session)
            elif request.get('method') == 'tools/list':
                self.send({'jsonrpc': '2.0', 'id': request.get('id'), 'result': {'tools': [
                    {'name': 'fixture_echo', 'description': 'Disposable fixture', 'inputSchema': {'type': 'object', 'properties': {}}}]}})
            elif request.get('method', '').startswith('notifications/'):
                self.send_response(202)
                self.end_headers()
            elif request.get('method') == 'stream':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: first\n\n')
                self.wfile.flush()
                time.sleep(.5)
                self.wfile.write(b'data: last\n\n')
            else:
                self.send({'jsonrpc': '2.0', 'id': request.get('id'),
                           'result': {'session': session}}, session=session)
        else:
            self.send({}, 404)


@pytest.fixture
def fixture(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server.base = f'http://127.0.0.1:{server.server_port}'
    server.refresh = 'initial-refresh'
    server.access = 'initial-access'
    server.refreshes = 0
    server.sessions = 0
    server.lose_response = False
    server.lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / 'auth'
    home.mkdir(mode=0o700)
    entry = {'server_name': 'example', 'server_url': server.base + '/mcp',
             'client_id': 'public-client', 'access_token': server.access,
             'refresh_token': server.refresh, 'expires_at': 1, 'scopes': ['read']}
    path = home / '.credentials.json'
    path.write_text(json.dumps({'fixture-key': entry}))
    path.chmod(0o600)
    definition = {'name': 'example', 'url': server.base + '/mcp', 'auth': 'oauth',
                  'oauth_scopes': ['read']}
    yield home, server, definition
    server.shutdown()
    server.server_close()


@contextmanager
def client(fixture):
    home, _, definition = fixture
    with ob.connect(home, ROOT, servers=[definition]) as connection:
        yield connection


def request(connection, *, session=None, name='example', token=None, method='initialize'):
    result = connection.result
    headers = {'Authorization': 'Bearer ' + (token or result['token']),
               'Content-Type': 'application/json'}
    if session:
        headers['Mcp-Session-Id'] = session
    return urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{result['port']}/{result['id']}/{name}",
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method}).encode(),
        headers=headers), timeout=5)


def test_parallel_clients_refresh_once_keep_sessions_separate_and_survive_first_exit(fixture):
    home, provider, _ = fixture
    with ThreadPoolExecutor(2) as pool:
        clients = list(pool.map(lambda _: ob.connect(home, ROOT, servers=[fixture[2]]), range(2)))
    first, second = clients
    try:
        with ThreadPoolExecutor(2) as pool:
            sessions = list(pool.map(lambda c: json.load(request(c))['result']['session'], clients))
        assert provider.refreshes == 1
        assert sessions[0] != sessions[1]
        with pytest.raises(urllib.error.HTTPError) as error:
            request(second, session=sessions[0])
        assert error.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as error:
            request(first, token=second.result['token'])
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            request(first, name='unselected')
        assert error.value.code == 403
        first.close()
        time.sleep(.1)
        assert json.load(request(second, session=sessions[1]))['result']['session'] == sessions[1]
        with pytest.raises(urllib.error.HTTPError) as error:
            request(first)
        assert error.value.code == 403
        stored = json.loads((home / '.credentials.json').read_text())['fixture-key']
        assert stored['refresh_token'] == provider.refresh
        assert stored['issuer'] == provider.base
        assert 'cage_refresh_pending' not in stored
    finally:
        second.close()


def test_interrupted_refresh_is_not_replayed(fixture):
    home, provider, definition = fixture
    provider.lose_response = True
    owner = ob.CredentialOwner(home)
    with pytest.raises(Exception):
        owner.access(definition)
    assert provider.refreshes == 1
    with pytest.raises(ob.BrokerError, match='interrupted'):
        ob.CredentialOwner(home).access(definition)
    assert provider.refreshes == 1


def test_old_session_lease_is_preserved(fixture):
    home, _, definition = fixture
    with OAuthSessionLease.acquire(home):
        with pytest.raises(ob.BrokerError, match='older Cage sessions'):
            ob.connect(home, ROOT, servers=[definition])
        assert not (home / ob.MANIFEST).exists()


def test_maintenance_serializes_login_and_live_refresh(fixture):
    home, provider, _ = fixture
    with client(fixture) as connection, ob.connect(home, ROOT) as maintenance:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(lambda: json.load(request(connection)))
            time.sleep(.2)
            assert provider.refreshes == 0
            assert not future.done()
            maintenance.close()
            assert future.result(timeout=5)['result']
    assert provider.refreshes == 1


def test_streams_events_without_buffering(fixture):
    with client(fixture) as connection:
        json.load(request(connection))
        started = time.monotonic()
        with request(connection, method='stream') as response:
            assert response.readline() == b'data: first\n'
            assert time.monotonic() - started < .4
            assert b'data: last' in response.read()


def test_scope_and_client_binding_and_symlink_rejection(fixture, tmp_path):
    home, provider, definition = fixture
    owner = ob.CredentialOwner(home)
    with pytest.raises(ob.BrokerError, match='scopes changed'):
        owner.access({**definition, 'oauth_scopes': ['write']})
    with pytest.raises(ob.BrokerError, match='client changed'):
        owner.access({**definition, 'oauth_client_id': 'another-client'})
    assert provider.refreshes == 0
    original = home / '.credentials.json'
    alternate = tmp_path / 'credential'
    original.rename(alternate)
    original.symlink_to(alternate)
    with pytest.raises(ob.SyncError, match='symlink'):
        owner.access(definition)


def test_session_configuration_contains_only_local_capability(fixture):
    with client(fixture) as connection:
        definitions = ob.routed_servers([fixture[2]], connection, 'host.docker.internal')
        assert definitions[0]['bearer_token_env_var'] == ob.TOKEN_ENV
        assert definitions[0]['url'].startswith('http://host.docker.internal:')
        rendered = json.dumps(definitions)
        for forbidden in ('initial-refresh', connection.result['token'], 'oauth_scopes', 'oauth_client_id'):
            assert forbidden not in rendered


def test_netgate_proxy_cannot_be_bypassed_by_no_proxy(monkeypatch):
    seen = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get('Proxy-Authorization')))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"through_proxy":true}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv('NO_PROXY', '*')
        value = ob.json_request('http://127.0.0.1:1/never-connect-directly',
                                proxy=f'http://cage:fixture@127.0.0.1:{server.server_port}')
        assert value['through_proxy'] is True
        assert seen == [('http://127.0.0.1:1/never-connect-directly', 'Basic Y2FnZTpmaXh0dXJl')]
    finally:
        server.shutdown()
        server.server_close()


def test_restart_preserves_rotated_credential(fixture):
    home, provider, _ = fixture
    with client(fixture) as connection:
        json.load(request(connection))
    deadline = time.monotonic() + 10
    while (home / ob.MANIFEST).exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert not (home / ob.MANIFEST).exists()
    with client(fixture) as connection:
        json.load(request(connection))
    assert provider.refreshes == 1


def test_credentials_hardlinks_are_rejected(fixture, tmp_path):
    home, _, definition = fixture
    os.link(home / '.credentials.json', tmp_path / 'hardlink')
    with pytest.raises(ob.BrokerError, match='regular file'):
        ob.CredentialOwner(home).access(definition)


@pytest.mark.skipif(not os.environ.get('CAGE_CODEX_SMOKE_IMAGE'), reason='set CAGE_CODEX_SMOKE_IMAGE for real Codex Docker transport test')
def test_two_real_codex_containers_share_one_oauth_login(fixture, tmp_path, request):
    temporary = tempfile.TemporaryDirectory(dir=ROOT)
    request.addfinalizer(temporary.cleanup)
    tmp_path = Path(temporary.name)
    home, provider, definition = fixture
    program = tmp_path / 'check.py'
    program.write_text(r'''import json, os, subprocess, sys
config = json.load(open('/fixture/connection.json'))
env = dict(os.environ, CODEX_HOME=os.path.expanduser('~/.codex'))
assert env.get('CAGE_OAUTH_BROKER_TOKEN') == config['token']
assert not os.path.exists(env['CODEX_HOME'] + '/.credentials.json')
assert config['url'] in open(env['CODEX_HOME'] + '/config.toml').read()
os.makedirs(env['CODEX_HOME'], exist_ok=True)
child = subprocess.Popen(['/home/codex/.npm-global/bin/codex', 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, env=env)
def send(value):
    child.stdin.write(json.dumps(value) + '\n')
    child.stdin.flush()
def receive(ident):
    while True:
        line = child.stdout.readline()
        if not line:
            raise RuntimeError('app-server exited')
        value = json.loads(line)
        if value.get('id') == ident:
            return value
try:
    send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'cage-fixture', 'version': '1'}, 'capabilities': {'experimentalApi': True}}})
    assert 'result' in receive(1)
    send({'method': 'initialized'})
    send({'id': 2, 'method': 'mcpServerStatus/list', 'params': {}})
    result = receive(2)
    assert 'fixture_echo' in json.dumps(result), 'MCP fixture tool was not available'
    print('MCP_BROKER_OK')
finally:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
''')
    with client(fixture) as first, client(fixture) as second:
        def run(index, connection):
            directory = tmp_path / str(index)
            directory.mkdir()
            (directory / 'check.py').write_text(program.read_text())
            (directory / 'connection.json').write_text(json.dumps({
                'token': connection.result['token'],
                'url': ob.routed_servers([definition], connection, 'cage-oauth.internal')[0]['url']}))
            # Use a root-only fixture file; local capability never enters Docker metadata.
            (directory / 'connection.json').chmod(0o600)
            (directory / 'codex').write_text('#!/bin/sh\nexec python3 /fixture/check.py\n')
            (directory / 'codex').chmod(0o755)
            (directory / 'token').write_text(connection.result['token'])
            (directory / 'token').chmod(0o600)
            result = subprocess.run(['docker', 'run', '--rm', '--add-host', 'cage-oauth.internal:host-gateway',
                                     '--mount', f'type=bind,src={directory},dst=/fixture,readonly',
                                     '--mount', f'type=bind,src={directory / "token"},dst=/run/cage-oauth-token,readonly',
                                     '-e', 'CAGE_OAUTH_BROKER=1', '-e', 'CODEX_COPY_AUTH=0',
                                     '-e', 'WORKSPACE_DIR=/fixture',
                                     '-e', f'HOST_UID={os.getuid()}', '-e', f'HOST_GID={os.getgid()}',
                                     '-e', 'PATH=/fixture:/home/codex/.npm-global/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                                     '-e', 'CAGE_REMOTE_MCP_SERVERS=' + json.dumps(ob.routed_servers([definition], connection, 'cage-oauth.internal')),
                                     os.environ['CAGE_CODEX_SMOKE_IMAGE']],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            assert result.returncode == 0, result.stderr
            assert 'MCP_BROKER_OK' in result.stdout
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(run, index, connection) for index, connection in enumerate((first, second))]
            for future in futures:
                future.result()
        assert provider.refreshes == 1


def test_container_snapshot_excludes_oauth_and_runtime_state(fixture, tmp_path):
    from types import SimpleNamespace
    from cage_core.lifecycle import LifecycleCoordinator
    from cage_core.targets.container import _codex_host_snapshot
    home, _, _ = fixture
    (home / 'auth.json').write_text('{"fixture":true}')
    (home / 'config.toml').write_text('model="fixture"')
    (home / 'history.jsonl').write_text('private history')
    runtime = SimpleNamespace(resolved=SimpleNamespace(codex_copy_auth='0'),
                              lifecycle=LifecycleCoordinator(), config_root=tmp_path)
    snapshot = _codex_host_snapshot(runtime, home)
    try:
        assert {item.name for item in snapshot.iterdir()} == {'config.toml'}
        assert (home / '.credentials.json').exists()
    finally:
        runtime.lifecycle.cleanup(0)
    assert not snapshot.exists()
