import json
import os
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_BRIDGE = ROOT / "mcp-bridge.py"
MCP_RELAY = ROOT / "mcp-relay"
HOST_BRIDGE = ROOT / "host-cmd-bridge.py"
HOST_RELAY = ROOT / "host-cmd-relay"
CAGE = ROOT / "cage"
AUTH_TOKEN = "a" * 64
SAFE_TEST_ENV_NAMES = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
)


def safe_test_env():
    return {name: os.environ[name] for name in SAFE_TEST_ENV_NAMES if name in os.environ}


class RunningBridge:
    def __init__(self, script, option, prefix, name, command, *extra, env=None):
        process_env = safe_test_env()
        process_env["CAGE_BRIDGE_AUTH_TOKEN"] = AUTH_TOKEN
        if env:
            process_env.update(env)
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(script),
                option,
                name,
                command,
                *extra,
                "--listen-host",
                "127.0.0.1",
            ],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.port = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            readable, _, _ = select.select([self.process.stdout], [], [], 0.1)
            if not readable:
                continue
            line = self.process.stdout.readline().decode("utf-8", "replace").strip()
            if line.startswith(f"{prefix}:{name}=PORT:"):
                self.port = int(line.rsplit(":", 1)[1])
            if line == "READY" and self.port is not None:
                return
        stderr = (
            self.process.stderr.read().decode("utf-8", "replace")
            if self.process.poll() is not None
            else ""
        )
        self.close()
        raise AssertionError(f"bridge did not start: {stderr}")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def command_for(code, *arguments):
    return shlex.join([sys.executable, "-c", code, *map(str, arguments)])


def relay_env(prefix, name, port):
    key = name.upper().replace("-", "_")
    return {
        f"{prefix}_BRIDGE_HOST": "127.0.0.1",
        f"{prefix}_BRIDGE_PORT_{key}": str(port),
        f"{prefix}_BRIDGE_TOKEN": AUTH_TOKEN,
    }


def read_socket_line(sock):
    result = bytearray()
    while not result.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


class BridgeTests(unittest.TestCase):
    def test_bridges_pin_executables_outside_container_writable_path_entries(self):
        real_echo = Path(shutil.which("echo") or "/bin/echo").resolve()
        cases = (
            (MCP_BRIDGE, "--server", "SERVER", MCP_RELAY, "MCP"),
            (HOST_BRIDGE, "--command", "COMMAND", HOST_RELAY, "HOST_CMD"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            writable = Path(tmp) / "repo-bin"
            writable.mkdir()
            sentinel = Path(tmp) / "ambient-path-executed"
            fake_echo = writable / "echo"
            fake_echo.write_text(
                "#!/bin/sh\nprintf compromised > \"$BRIDGE_PATH_SENTINEL\"\n",
                encoding="utf-8",
            )
            fake_echo.chmod(0o755)
            hostile_path = f"{writable}{os.pathsep}{real_echo.parent}"

            for script, option, prefix, relay, relay_prefix in cases:
                with self.subTest(script=script.name):
                    with RunningBridge(
                        script,
                        option,
                        prefix,
                        "path-check",
                        "echo expected",
                        "--deny-executable-root",
                        str(writable),
                        env={
                            "PATH": hostile_path,
                            "BRIDGE_PATH_SENTINEL": str(sentinel),
                        },
                    ) as bridge:
                        result = subprocess.run(
                            [sys.executable, str(relay), "path-check"],
                            env={
                                **safe_test_env(),
                                **relay_env(relay_prefix, "path-check", bridge.port),
                            },
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=5,
                            text=True,
                        )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "expected\n")
                    self.assertFalse(sentinel.exists())

    def test_bridges_reject_explicit_executable_inside_writable_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            writable = Path(tmp) / "repo"
            writable.mkdir()
            executable = writable / "tool"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            env = safe_test_env()
            env["CAGE_BRIDGE_AUTH_TOKEN"] = AUTH_TOKEN

            for script, option in (
                (MCP_BRIDGE, "--server"),
                (HOST_BRIDGE, "--command"),
            ):
                with self.subTest(script=script.name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(script),
                            option,
                            "blocked",
                            str(executable),
                            "--deny-executable-root",
                            str(writable),
                        ],
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Cage-writable mount", result.stderr)

    def test_launcher_passes_fresh_host_command_authentication_token(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            xdg = root / "xdg"
            home = root / "home"
            bin_dir = root / "bin"
            repo = root / "repo"
            cli_rw = root / "cli-rw"
            args_file = root / "docker-args"
            bridge_args_file = root / "bridge-args.json"
            (xdg / "cage").mkdir(parents=True)
            home.mkdir()
            bin_dir.mkdir()
            repo.mkdir()
            cli_rw.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(
                """#!/bin/sh
case "$1" in
  ps|image|build|pull|tag|volume) exit 0 ;;
  run)
    shift
    : > "$DOCKER_ARGS_FILE"
    for arg in "$@"; do printf '%s\\0' "$arg" >> "$DOCKER_ARGS_FILE"; done
    exit 0
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            (xdg / "cage" / "config.toml").write_text(
                "\n".join(
                    [
                        "version = 1",
                        'default_preset = "codex-test"',
                        "[auth.codex-test]",
                        'tool = "codex"',
                        "copy_auth = false",
                        "[host_commands.token-tool]",
                        f'command = "{sys.executable} -c \'print(1)\'"',
                        "[presets.codex-test]",
                        'tool = "codex"',
                        'auth = "codex-test"',
                        'host_commands = ["token-tool"]',
                        'net = "open"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            env = safe_test_env()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(xdg),
                    "HOME": str(home),
                    "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                    "DOCKER_ARGS_FILE": str(args_file),
                    "BRIDGE_ARGS_FILE": str(bridge_args_file),
                }
            )
            launcher_dir = root / "launcher"
            launcher_dir.mkdir()
            shutil.copy2(CAGE, launcher_dir / "cage")
            shutil.copy2(ROOT / "cage-config.py", launcher_dir / "cage-config.py")
            fake_bridge = launcher_dir / "host-cmd-bridge.py"
            fake_bridge.write_text(
                """import json
import os
import re
import signal
import sys
import time

token = os.environ.get("CAGE_BRIDGE_AUTH_TOKEN", "")
if not re.fullmatch(r"[0-9a-f]{64}", token):
    raise SystemExit("missing bridge authentication token")
with open(os.environ["BRIDGE_ARGS_FILE"], "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
index = 1
port = 42000
while index < len(sys.argv):
    if sys.argv[index] == "--command":
        print("COMMAND:%s=PORT:%d" % (sys.argv[index + 1], port), flush=True)
        port += 1
        index += 3
    else:
        index += 1
print("READY", flush=True)
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
signal.signal(signal.SIGINT, lambda *_args: sys.exit(0))
while True:
    time.sleep(1)
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    str(launcher_dir / "cage"),
                    "--mount-rw",
                    str(cli_rw),
                    str(repo),
                ],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            docker_args = [
                os.fsdecode(value)
                for value in args_file.read_bytes().split(b"\0")
                if value
            ]
            token_arg = next(
                value for value in docker_args if value.startswith("HOST_CMD_BRIDGE_TOKEN=")
            )
            self.assertRegex(token_arg, r"^HOST_CMD_BRIDGE_TOKEN=[0-9a-f]{64}$")
            self.assertIn("CAGE_HOST_COMMANDS=token-tool", docker_args)
            bridge_args = json.loads(bridge_args_file.read_text(encoding="utf-8"))
            denied = [
                bridge_args[index + 1]
                for index, value in enumerate(bridge_args[:-1])
                if value == "--deny-executable-root"
            ]
            self.assertIn(str(repo.resolve()), denied)
            self.assertIn(str(cli_rw.resolve()), denied)

    def test_unauthorized_mcp_client_cannot_spawn_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "spawned"
            code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('spawned')"
            with RunningBridge(
                MCP_BRIDGE,
                "--server",
                "SERVER",
                "safe-name",
                command_for(code, marker),
                "--cwd",
                tmp,
            ) as bridge:
                with socket.create_connection(("127.0.0.1", bridge.port), timeout=2) as client:
                    client.sendall(b"CAGE-MCP/1 " + b"0" * 64 + b"\n")
                    self.assertEqual(read_socket_line(client), b"ERR\n")
                time.sleep(0.1)
                self.assertFalse(marker.exists())

    def test_mcp_relay_authenticates_and_preserves_raw_bytes(self):
        code = (
            "import sys; data=sys.stdin.buffer.readline(); "
            "sys.stdout.buffer.write(data); sys.stdout.buffer.flush()"
        )
        with RunningBridge(
            MCP_BRIDGE,
            "--server",
            "SERVER",
            "raw-server",
            command_for(code),
        ) as bridge:
            result = subprocess.run(
                [sys.executable, str(MCP_RELAY), "raw-server"],
                input=b"\x00json-rpc-byte-stream\xff\n",
                env={**safe_test_env(), **relay_env("MCP", "raw-server", bridge.port)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"\x00json-rpc-byte-stream\xff\n")

    def test_shell_operator_command_is_rejected_without_execution(self):
        env = safe_test_env()
        env["CAGE_BRIDGE_AUTH_TOKEN"] = AUTH_TOKEN
        result = subprocess.run(
            [
                sys.executable,
                str(MCP_BRIDGE),
                "--server",
                "bad",
                "printf safe | cat",
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shell operators are not supported", result.stderr)

    def test_bridge_rejects_transport_name_collision(self):
        env = safe_test_env()
        env["CAGE_BRIDGE_AUTH_TOKEN"] = AUTH_TOKEN
        command = command_for("pass")
        result = subprocess.run(
            [
                sys.executable,
                str(MCP_BRIDGE),
                "--server",
                "foo-bar",
                command,
                "--server",
                "FOO_BAR",
                command,
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("collide after relay normalization", result.stderr)

    def test_host_command_preserves_argv_stdio_stderr_and_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "injected"
            code = (
                "import json,sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode()+b'\\n'+data); "
                "sys.stderr.write('stderr-marker\\n'); sys.exit(7)"
            )
            with RunningBridge(
                HOST_BRIDGE,
                "--command",
                "COMMAND",
                "token-tool",
                command_for(code),
                "--cwd",
                tmp,
            ) as bridge:
                hostile_arg = f"$(touch {marker})"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(HOST_RELAY),
                        "token-tool",
                        ";",
                        hostile_arg,
                        "space containing argument",
                    ],
                    input=b"stdin-payload",
                    env={**safe_test_env(), **relay_env("HOST_CMD", "token-tool", bridge.port)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                )
        self.assertEqual(result.returncode, 7, result.stderr)
        argv_line, payload = result.stdout.split(b"\n", 1)
        self.assertEqual(
            json.loads(argv_line),
            [";", hostile_arg, "space containing argument"],
        )
        self.assertEqual(payload, b"stdin-payload")
        self.assertEqual(result.stderr, b"stderr-marker\n")
        self.assertFalse(marker.exists())

    def test_unauthorized_host_command_client_cannot_spawn_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "spawned"
            code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('spawned')"
            with RunningBridge(
                HOST_BRIDGE,
                "--command",
                "COMMAND",
                "protected-tool",
                command_for(code, marker),
                "--cwd",
                tmp,
            ) as bridge:
                with socket.create_connection(("127.0.0.1", bridge.port), timeout=2) as client:
                    client.sendall(b"CAGE-HOSTCMD/1 " + b"0" * 64 + b"\n")
                    self.assertEqual(read_socket_line(client), b"ERR\n")
                time.sleep(0.1)
                self.assertFalse(marker.exists())

    def test_host_command_gets_only_base_and_configured_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                "import json,os; print(json.dumps({"
                "'allowed':os.getenv('BRIDGE_ALLOWED'),"
                "'ambient':os.getenv('BRIDGE_AMBIENT'),"
                "'cwd':os.getcwd()}))"
            )
            with RunningBridge(
                HOST_BRIDGE,
                "--command",
                "COMMAND",
                "env-check",
                command_for(code),
                "--cwd",
                tmp,
                "--pass-env",
                "BRIDGE_ALLOWED",
                env={"BRIDGE_ALLOWED": "present", "BRIDGE_AMBIENT": "must-not-pass"},
            ) as bridge:
                result = subprocess.run(
                    [sys.executable, str(HOST_RELAY), "env-check"],
                    env={**safe_test_env(), **relay_env("HOST_CMD", "env-check", bridge.port)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    text=True,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(
            observed,
            {"allowed": "present", "ambient": None, "cwd": os.path.realpath(tmp)},
        )

    def test_host_command_timeout_kills_descendant_process_group(self):
        code = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)"
        )
        with RunningBridge(
            HOST_BRIDGE,
            "--command",
            "COMMAND",
            "bounded",
            command_for(code),
            "--process-timeout",
            "0.3",
        ) as bridge:
            result = subprocess.run(
                [sys.executable, str(HOST_RELAY), "bounded"],
                env={**safe_test_env(), **relay_env("HOST_CMD", "bounded", bridge.port)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                text=True,
            )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("exceeded its process timeout", result.stderr)
        child_pid = int(result.stdout.strip())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode != 0 or not probe.stdout.strip() or "Z" in probe.stdout:
                break
            time.sleep(0.1)
        else:
            self.fail(f"descendant process {child_pid} survived bridge timeout")

    def test_host_command_output_limit_fails_closed(self):
        code = "import sys; sys.stdout.write('x' * 1024); sys.stdout.flush()"
        with RunningBridge(
            HOST_BRIDGE,
            "--command",
            "COMMAND",
            "bounded-output",
            command_for(code),
            "--max-output-bytes",
            "32",
        ) as bridge:
            result = subprocess.run(
                [sys.executable, str(HOST_RELAY), "bounded-output"],
                env={**safe_test_env(), **relay_env("HOST_CMD", "bounded-output", bridge.port)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                text=True,
            )
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("exceeded its output limit", result.stderr)

    def test_host_command_closing_stdin_does_not_hide_exit_status(self):
        code = "import os,sys,time; os.close(0); print('done', flush=True); time.sleep(0.1); sys.exit(9)"
        with RunningBridge(
            HOST_BRIDGE,
            "--command",
            "COMMAND",
            "closes-stdin",
            command_for(code),
        ) as bridge:
            result = subprocess.run(
                [sys.executable, str(HOST_RELAY), "closes-stdin"],
                input=b"x" * (1024 * 1024),
                env={**safe_test_env(), **relay_env("HOST_CMD", "closes-stdin", bridge.port)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(result.stdout, b"done\n")


if __name__ == "__main__":
    unittest.main()
