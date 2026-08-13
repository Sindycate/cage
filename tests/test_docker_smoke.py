import os
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cage_core import storage


ROOT = Path(__file__).resolve().parents[1]
NETGATE = ROOT / "netgate-proxy.py"
ENTRYPOINT_CODEX = ROOT / "entrypoint-codex.sh"
ENTRYPOINT_CLAUDE = ROOT / "entrypoint.sh"
ENTRYPOINT_OPENCODE = ROOT / "entrypoint-opencode.sh"
USER_REMAP = ROOT / "cage-user-remap.py"
CODEX_CORE = ROOT / "cage_core"


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(
    os.environ.get("CAGE_RUN_DOCKER_SMOKE") == "1",
    "set CAGE_RUN_DOCKER_SMOKE=1 to run local Docker integration smoke tests",
)
class DockerSmokeTests(unittest.TestCase):
    def test_all_entrypoints_remap_linux_ids_and_write_as_mapped_owner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            snapshot = temp_path / "opencode-snapshot"
            fake_bin.mkdir()
            snapshot.mkdir()
            (snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "selected_mcp": {"stdio": [], "remote": []},
                        "selected_skill_names": [],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_env = snapshot / "runtime-env.json"
            runtime_env.write_text("{}\n", encoding="utf-8")
            runtime_env.chmod(0o600)
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(fake_bin / "jq", "#!/bin/sh\nexit 0\n")
            proof = (
                "actual=$(id -u):$(id -g)\n"
                "[ \"$actual\" = \"$HOST_UID:$HOST_GID\" ] || { "
                "echo \"unexpected identity $actual\" >&2; exit 81; }\n"
                "proof=$WORKSPACE_DIR/$CAGE_TEST_TOOL-write-proof\n"
                "printf '%s uid=%s gid=%s\\n' \"$CAGE_TEST_TOOL\" "
                "\"$(id -u)\" \"$(id -g)\" > \"$proof\"\n"
                "[ \"$(stat -c %u:%g \"$proof\")\" = \"$HOST_UID:$HOST_GID\" ] || exit 82\n"
                "cat \"$proof\"\n"
            )
            write_executable(fake_bin / "claude", "#!/bin/sh\n" + proof)
            write_executable(
                fake_bin / "codex",
                "#!/bin/sh\n"
                "case \"$*\" in *\"mcp list --json\"*) printf '[]\\n'; exit 0;; esac\n"
                + proof,
            )
            write_executable(
                fake_bin / "opencode",
                "#!/bin/sh\n"
                "case \" $* \" in\n"
                "  *\" debug config \"*) printf '{\"mcp\":{}}\\n'; exit 0;;\n"
                "  *\" debug skill \"*) printf '[]\\n'; exit 0;;\n"
                "esac\n"
                + proof,
            )

            tools = (
                ("claude", "claude", ENTRYPOINT_CLAUDE),
                ("codex", "codex", ENTRYPOINT_CODEX),
                ("opencode", "opencode", ENTRYPOINT_OPENCODE),
            )
            for tool, target_user, entrypoint in tools:
                for host_id in (1000, 22001):
                    container = f"cage-uid-{tool}-{host_id}-{os.getpid()}"
                    with self.subTest(tool=tool, host_id=host_id):
                        mounts = [
                            "--mount",
                            f"type=bind,src={entrypoint},dst=/entrypoint.sh,readonly",
                            "--mount",
                            f"type=bind,src={USER_REMAP},dst=/usr/local/lib/cage/cage-user-remap.py,readonly",
                            "--mount",
                            f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                        ]
                        if tool == "codex":
                            mounts.extend(
                                (
                                    "--mount",
                                    f"type=bind,src={CODEX_CORE},dst=/usr/local/lib/cage/cage_core,readonly",
                                )
                            )
                        if tool == "opencode":
                            mounts.extend(
                                (
                                    "--mount",
                                    f"type=bind,src={snapshot},dst=/cage-opencode-snapshot,readonly",
                                )
                            )
                        collision = (
                            "groupadd -g 1000 occupied && "
                            "useradd -u 1000 -g 1000 -M -s /bin/sh occupied && "
                            if host_id == 1000
                            else ""
                        )
                        tool_setup = ""
                        if tool == "codex":
                            tool_setup = (
                                "mkdir -p /home/codex/.codex /home/codex/.npm-global/bin && "
                                "ln -s /test-bin/codex /home/codex/.npm-global/bin/codex && "
                            )
                        elif tool == "opencode":
                            tool_setup = "mkdir -p /home/opencode/.cage-state && "
                        command = (
                            "(test -x /usr/bin/python3 || "
                            "ln -s /usr/local/bin/python3 /usr/bin/python3) && "
                            + collision
                            + f"groupadd -g 22000 {target_user} && "
                            + f"useradd -u 22000 -g 22000 -M -s /bin/sh {target_user} && "
                            + f"mkdir -p /home/{target_user} /workspace && "
                            + tool_setup
                            + f"chown -R {target_user}:{target_user} /home/{target_user} && "
                            + f"chmod 700 /workspace && chown {host_id}:{host_id} /workspace && "
                            + "exec /bin/bash /entrypoint.sh --version"
                        )
                        try:
                            result = subprocess.run(
                                [
                                    "docker",
                                    "run",
                                    "--rm",
                                    "--name",
                                    container,
                                    "--cap-drop",
                                    "ALL",
                                    "--cap-add",
                                    "CHOWN",
                                    "--cap-add",
                                    "DAC_OVERRIDE",
                                    "--cap-add",
                                    "SETGID",
                                    "--cap-add",
                                    "SETUID",
                                    *mounts,
                                    "-e",
                                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                    "-e",
                                    f"HOME=/home/{target_user}",
                                    "-e",
                                    f"HOST_UID={host_id}",
                                    "-e",
                                    f"HOST_GID={host_id}",
                                    "-e",
                                    "WORKSPACE_DIR=/workspace",
                                    "-e",
                                    f"CAGE_TEST_TOOL={tool}",
                                    "-e",
                                    "CODEX_COPY_AUTH=0",
                                    "python:3.12-slim",
                                    "sh",
                                    "-c",
                                    command,
                                ],
                                cwd=ROOT,
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=30,
                                check=False,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn(
                                f"{tool} uid={host_id} gid={host_id}", result.stdout
                            )
                        finally:
                            subprocess.run(
                                ["docker", "rm", "-f", container],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=False,
                            )

    def test_storage_cleanup_preserves_references_custom_image_and_named_volume(self):
        major = 900000 + os.getpid()
        versions = [f"{major}.0.{index}" for index in range(5)]
        tags = [f"cage-base:{version}" for version in versions]
        running = f"cage-storage-running-{os.getpid()}"
        stopped = f"cage-storage-stopped-{os.getpid()}"
        volume = f"cage-storage-volume-{os.getpid()}"
        custom = f"cage-storage-custom-{os.getpid()}:latest"
        created_ids: set[str] = set()
        try:
            with tempfile.TemporaryDirectory(dir=ROOT) as raw:
                context = Path(raw)
                dockerfile = context / "Dockerfile"
                dockerfile.write_text(
                    "FROM busybox:1.36\n"
                    "ARG VERSION\n"
                    "CMD [\"sleep\", \"300\"]\n"
                    "LABEL io.cage.managed=\"true\" "
                    "io.cage.role=\"base\" io.cage.version=\"${VERSION}\"\n",
                    encoding="utf-8",
                )
                for version, tag in zip(versions, tags):
                    built = subprocess.run(
                        [
                            "docker", "build", "--build-arg", f"VERSION={version}",
                            "-t", tag, "-f", str(dockerfile), str(context),
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(built.returncode, 0, built.stderr)
                derived = context / "Dockerfile.derived"
                derived.write_text(
                    f"FROM {tags[0]}\nRUN true\n",
                    encoding="utf-8",
                )
                built = subprocess.run(
                    ["docker", "build", "-t", custom, "-f", str(derived), str(context)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(built.returncode, 0, built.stderr)

            subprocess.run(
                ["docker", "run", "-d", "--name", running, tags[1]],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["docker", "create", "--name", stopped, tags[2], "true"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(["docker", "start", "-a", stopped], check=True)
            subprocess.run(
                ["docker", "volume", "create", volume],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    "docker", "run", "--rm", "-v", f"{volume}:/data",
                    "busybox:1.36", "sh", "-c", "printf sentinel >/data/sentinel",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )

            for reference in [*tags, custom]:
                inspected = json.loads(
                    subprocess.check_output(["docker", "image", "inspect", reference], text=True)
                )
                created_ids.add(inspected[0]["Id"])
            images = tuple(
                item for item in storage.inventory_images("docker")
                if item.image_id in created_ids
            )
            referenced = storage._referenced_image_ids("docker")
            candidates, _, _ = storage.cleanup_candidates(
                images, referenced, storage.StoragePolicy(keep_versions=2)
            )

            self.assertEqual([item.reference for item in candidates], [tags[0]])
            removed, failures = storage.delete_candidates("docker", candidates)
            self.assertEqual((removed, failures), (1, ()))
            self.assertNotEqual(
                subprocess.run(["docker", "image", "inspect", tags[0]], check=False).returncode,
                0,
            )
            for reference in (tags[1], tags[2], custom):
                self.assertEqual(
                    subprocess.run(
                        ["docker", "image", "inspect", reference],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode,
                    0,
                )
            self.assertEqual(
                subprocess.check_output(
                    [
                        "docker", "run", "--rm", "-v", f"{volume}:/data:ro",
                        "busybox:1.36", "cat", "/data/sentinel",
                    ],
                    text=True,
                ),
                "sentinel",
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", running, stopped],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            for reference in [*tags, custom]:
                subprocess.run(
                    ["docker", "image", "rm", "-f", reference],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            subprocess.run(
                ["docker", "volume", "rm", volume],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def test_desktop_remote_ssh_injects_profile_provider_and_yolo(self):
        image = os.environ.get("CAGE_DESKTOP_SMOKE_IMAGE")
        if not image:
            self.skipTest("set CAGE_DESKTOP_SMOKE_IMAGE to a built Codex image")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo"
            state = temp_path / "state"
            repo.mkdir()
            state.mkdir()
            key = state / "id_ed25519"
            generated = subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            heartbeat = state / "heartbeat"
            heartbeat.write_text("alive\n", encoding="utf-8")
            runtime_env = state / "runtime-env.json"
            runtime_env.write_text(
                json.dumps(
                    {
                        "TEST_DESKTOP_PROVIDER": "provider-secret",
                        "MCP_BRIDGE_HOST": "host.docker.internal",
                        "MCP_BRIDGE_PORT_PROBE": "4321",
                        "MCP_BRIDGE_TOKEN": "mcp-bridge-secret",
                        "HOST_CMD_BRIDGE_HOST": "host.docker.internal",
                        "HOST_CMD_BRIDGE_PORT_HOSTPROBE": "5432",
                        "HOST_CMD_BRIDGE_TOKEN": "host-bridge-secret",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_env.chmod(0o600)
            fake_codex = state / "real-codex"
            write_executable(
                fake_codex,
                "#!/bin/sh\n"
                "case \"$*\" in *\"mcp list --json\"*) printf '[]\\n'; exit 0;; esac\n"
                "[ \"$TEST_DESKTOP_PROVIDER\" = provider-secret ] || exit 41\n"
                "[ \"$MCP_BRIDGE_TOKEN\" = mcp-bridge-secret ] || exit 42\n"
                "[ \"$MCP_BRIDGE_PORT_PROBE\" = 4321 ] || exit 43\n"
                "[ \"$HOST_CMD_BRIDGE_TOKEN\" = host-bridge-secret ] || exit 44\n"
                "[ \"$HOST_CMD_BRIDGE_PORT_HOSTPROBE\" = 5432 ] || exit 45\n"
                "[ -x /usr/local/bin/hostprobe ] || exit 46\n"
                "! grep -q 'mcp-bridge-secret\\|host-bridge-secret' "
                "\"$HOME/.codex/config.toml\" || exit 47\n"
                "printf 'ARGS=%s\\n' \"$*\"\n"
                "printf 'PWD=%s\\n' \"$(pwd)\"\n",
            )
            container = f"cage-desktop-smoke-{os.getpid()}"
            volume = f"cage-desktop-smoke-{os.getpid()}"
            container_id = ""
            try:
                launched = subprocess.run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        container,
                        "--network",
                        "none",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "CHOWN",
                        "--cap-add",
                        "DAC_OVERRIDE",
                        "--cap-add",
                        "SETGID",
                        "--cap-add",
                        "SETUID",
                        "--cap-add",
                        "SYS_CHROOT",
                        "--tmpfs",
                        "/run",
                        "--tmpfs",
                        "/tmp",
                        "-v",
                        f"{repo}:{repo}",
                        "-v",
                        f"{volume}:/home/codex/.codex",
                        "-v",
                        f"{key}.pub:/cage-desktop/client.pub:ro",
                        "-v",
                        f"{heartbeat}:/cage-desktop/heartbeat:ro",
                        "-v",
                        f"{runtime_env}:/cage-desktop/runtime-env.json:ro",
                        "-v",
                        f"{fake_codex}:/home/codex/.npm-global/bin/codex:ro",
                        "-e",
                        f"WORKSPACE_DIR={repo}",
                        "-e",
                        f"HOST_UID={os.getuid()}",
                        "-e",
                        f"HOST_GID={os.getgid()}",
                        "-e",
                        "CAGE_DESKTOP_REMOTE=1",
                        "-e",
                        "CAGE_DESKTOP_ENV_NAMES=TEST_DESKTOP_PROVIDER",
                        "-e",
                        'CAGE_MCP_SERVERS={"probe":4321}',
                        "-e",
                        "CAGE_HOST_COMMANDS=hostprobe",
                        "-e",
                        "CAGE_CODEX_PROFILE=desktop_probe",
                        "-e",
                        "CAGE_YOLO=1",
                        "-e",
                        "CODEX_COPY_AUTH=0",
                        image,
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(launched.returncode, 0, launched.stderr)
                container_id = launched.stdout.strip()
                inspected_env = subprocess.check_output(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{json .Config.Env}}",
                        container_id,
                    ],
                    text=True,
                )
                self.assertNotIn("TEST_DESKTOP_PROVIDER=", inspected_env)
                self.assertNotIn("provider-secret", inspected_env)
                ready = False
                for _ in range(100):
                    checked = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "--user",
                            "root",
                            container_id,
                            "/usr/sbin/sshd",
                            "-T",
                            "-f",
                            "/run/cage/sshd_config",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    if checked.returncode == 0:
                        ready = True
                        break
                    time.sleep(0.1)
                self.assertTrue(ready, "desktop container did not become SSH-ready")
                host_public = subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        container_id,
                        "cat",
                        "/home/codex/.codex/.cage-desktop/ssh_host_ed25519_key.pub",
                    ],
                    text=True,
                ).strip().split()
                known_hosts = state / "known_hosts"
                known_hosts.write_text(
                    f"desktop-smoke {host_public[0]} {host_public[1]}\n",
                    encoding="utf-8",
                )
                ssh = subprocess.run(
                    [
                        "/usr/bin/ssh",
                        "-o",
                        (
                            "ProxyCommand=docker exec -i --user root "
                            f"{container_id} /usr/sbin/sshd -i -e "
                            "-f /run/cage/sshd_config"
                        ),
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        f"UserKnownHostsFile={known_hosts}",
                        "-o",
                        "HostKeyAlias=desktop-smoke",
                        "-i",
                        str(key),
                        "codex@desktop-smoke",
                        "cd",
                        str(repo),
                        "&&",
                        "codex",
                        "app-server",
                        "--probe",
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(ssh.returncode, 0, ssh.stderr)
                self.assertIn(
                    "ARGS=--profile desktop_probe --yolo app-server --probe",
                    ssh.stdout,
                )
                self.assertIn(f"PWD={repo}", ssh.stdout)
                self.assertNotIn("provider-secret", ssh.stdout + ssh.stderr)

                remote_env = subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        container_id,
                        "stat",
                        "-c",
                        "%a:%U",
                        "/run/cage-user/remote-env.json",
                    ],
                    text=True,
                ).strip()
                self.assertEqual(remote_env, "600:codex")
                policy_state = subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        container_id,
                        "stat",
                        "-c",
                        "%a:%U",
                        "/run/cage",
                        "/run/cage/remote-launch.json",
                    ],
                    text=True,
                ).splitlines()
                self.assertEqual(policy_state, ["755:root", "644:root"])
                replace_policy = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "codex",
                        container_id,
                        "sh",
                        "-c",
                        "printf '{}\\n' > /run/cage/remote-launch.json",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(replace_policy.returncode, 0)
            finally:
                if container_id:
                    subprocess.run(
                        ["docker", "rm", "-f", container_id],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                subprocess.run(
                    ["docker", "volume", "rm", volume],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def test_codex_entrypoint_preserves_volume_owned_history(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            host_codex = temp_path / "host-codex"
            volume_codex = temp_path / "volume-codex"
            fake_bin.mkdir()
            host_codex.mkdir()
            volume_codex.mkdir()
            control_config = temp_path / "config.toml"
            control_config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            request = temp_path / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": hashlib.sha256(control_config.read_bytes()).hexdigest(),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "codex", "net": "gate"},
                }],
            }), encoding="utf-8")
            saved = subprocess.run(
                [sys.executable, str(ROOT / "cage-config.py"), "--config", str(control_config),
                 "ui-commit", "--repo", str(temp_path), "--request", str(request)],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            (host_codex / "config.toml").write_text(
                'model = "host-config"\n',
                encoding="utf-8",
            )
            (host_codex / "rules").mkdir()
            (host_codex / "rules" / "host.rules").write_text(
                'prefix_rule(pattern=["git", "status"], decision="allow")\n',
                encoding="utf-8",
            )

            runtime_files = (
                "history.jsonl",
                "session_index.jsonl",
                "state_5.sqlite",
                "state_5.sqlite-wal",
                "logs_2.sqlite",
                "memories_1.sqlite",
                "goals_1.sqlite",
            )
            runtime_directories = (
                "sessions",
                "archived_sessions",
                "log",
                "memories",
                "cache",
                "shell_snapshots",
            )
            for name in runtime_files:
                (host_codex / name).write_bytes(("host:" + name).encode())
                (volume_codex / name).write_bytes(("volume:" + name).encode())
            for name in runtime_directories:
                (host_codex / name).mkdir()
                (host_codex / name / "host-only.bin").write_bytes(b"host")
                (volume_codex / name).mkdir()
                (volume_codex / name / "volume.bin").write_bytes(
                    ("volume:" + name).encode()
                )

            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "codex",
                "#!/bin/sh\n"
                "case \"$*\" in *\"mcp list --json\"*) printf '[]\\n'; exit 0;; esac\n"
                "grep -q 'host-config' \"$HOME/.codex/config.toml\" && "
                "grep -q 'git' \"$HOME/.codex/rules/host.rules\" && "
                "for name in history.jsonl session_index.jsonl state_5.sqlite "
                "state_5.sqlite-wal logs_2.sqlite memories_1.sqlite goals_1.sqlite; do "
                "cmp -s \"$HOME/.codex/$name\" \"/volume-codex-source/$name\" || exit 1; "
                "done && "
                "for name in sessions archived_sessions log memories cache shell_snapshots; do "
                "cmp -s \"$HOME/.codex/$name/volume.bin\" "
                "\"/volume-codex-source/$name/volume.bin\" || exit 1; "
                "[ ! -e \"$HOME/.codex/$name/host-only.bin\" ] || exit 1; "
                "done\n",
            )

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={USER_REMAP},dst=/usr/local/lib/cage/cage-user-remap.py,readonly",
                    "--mount",
                    f"type=bind,src={CODEX_CORE},dst=/usr/local/lib/cage/cage_core,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "--mount",
                    f"type=bind,src={host_codex},dst=/host-codex-source,readonly",
                    "--mount",
                    f"type=bind,src={volume_codex},dst=/volume-codex-source,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "-e",
                    "CODEX_COPY_AUTH=0",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "(test -x /usr/bin/python3 || "
                    "ln -s /usr/local/bin/python3 /usr/bin/python3) && "
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "mkdir -p /home/codex/.npm-global/bin && "
                    "ln -s /test-bin/codex /home/codex/.npm-global/bin/codex && "
                    "cp -R /host-codex-source /host-codex && "
                    "chown -R 21001:21001 /host-codex && "
                    "mkdir -p /workspace /home/codex && "
                    "cp -R /volume-codex-source /home/codex/.codex && "
                    "/entrypoint.sh --version && /entrypoint.sh --version",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_entrypoint_preserves_session_tree_across_config_save_and_two_launches(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            control_config = temp_path / "config.toml"
            control_config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "claude"\n',
                encoding="utf-8",
            )
            request = temp_path / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": hashlib.sha256(control_config.read_bytes()).hexdigest(),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "claude", "net": "gate", "session_sync": True},
                }],
            }), encoding="utf-8")
            saved = subprocess.run(
                [sys.executable, str(ROOT / "cage-config.py"), "--config", str(control_config),
                 "ui-commit", "--repo", str(temp_path), "--request", str(request)],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\nuser=$1\nshift\nexec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(fake_bin / "jq", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "claude",
                "#!/bin/sh\n"
                "grep -q volume-session \"$HOME/.claude/projects/repo/session.jsonl\" && "
                "grep -q volume-memory \"$HOME/.claude/projects/repo/memory/MEMORY.md\"\n",
            )

            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--cap-drop", "ALL",
                    "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                    "--cap-add", "SETGID", "--cap-add", "SETUID",
                    "--mount", f"type=bind,src={ENTRYPOINT_CLAUDE},dst=/entrypoint.sh,readonly",
                    "--mount", f"type=bind,src={USER_REMAP},dst=/usr/local/lib/cage/cage-user-remap.py,readonly",
                    "--mount", f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e", "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e", "HOME=/home/claude", "-e", "HOST_UID=22001", "-e", "HOST_GID=22001",
                    "-e", "WORKSPACE_DIR=/workspace", "python:3.12-slim", "sh", "-c",
                    "(test -x /usr/bin/python3 || "
                    "ln -s /usr/local/bin/python3 /usr/bin/python3) && "
                    "groupadd -g 22000 claude && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh claude && "
                    "mkdir -p /workspace /home/claude/.claude/projects/repo/memory && "
                    "printf 'volume-session\\n' > /home/claude/.claude/projects/repo/session.jsonl && "
                    "printf 'volume-memory\\n' > /home/claude/.claude/projects/repo/memory/MEMORY.md && "
                    "bash /entrypoint.sh --version && bash /entrypoint.sh --version",
                ],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_sensitive_modes_are_set_by_the_remapped_owner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "codex",
                "#!/bin/sh\n"
                "case \"$*\" in *\"mcp list --json\"*) printf '[]\\n'; exit 0;; esac\n"
                "mode=$(stat -c %a \"$HOME/.codex/.credentials.json\")\n"
                "owner=$(stat -c %u \"$HOME/.codex/.credentials.json\")\n"
                "[ \"$mode:$owner\" = 600:22001 ] || { "
                "echo \"unexpected credential state: $mode:$owner\" >&2; exit 1; }\n",
            )

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={USER_REMAP},dst=/usr/local/lib/cage/cage-user-remap.py,readonly",
                    "--mount",
                    f"type=bind,src={CODEX_CORE},dst=/usr/local/lib/cage/cage_core,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "(test -x /usr/bin/python3 || "
                    "ln -s /usr/local/bin/python3 /usr/bin/python3) && "
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "mkdir -p /home/codex/.npm-global/bin && "
                    "ln -s /test-bin/codex /home/codex/.npm-global/bin/codex && "
                    "mkdir -p /workspace /home/codex/.codex && "
                    "printf '{\\\"test\\\":true}\\n' > /home/codex/.codex/.credentials.json && "
                    "chmod 640 /home/codex/.codex/.credentials.json && "
                    "chown 22001:22001 /home/codex/.codex/.credentials.json && "
                    "exec /entrypoint.sh --version",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_sensitive_mode_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(fake_bin / "codex", "#!/bin/sh\nexit 0\n")

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={USER_REMAP},dst=/usr/local/lib/cage/cage-user-remap.py,readonly",
                    "--mount",
                    f"type=bind,src={CODEX_CORE},dst=/usr/local/lib/cage/cage_core,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "(test -x /usr/bin/python3 || "
                    "ln -s /usr/local/bin/python3 /usr/bin/python3) && "
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "mkdir -p /workspace /home/codex/.codex && "
                    "printf 'host file\\n' > /workspace/victim && "
                    "chmod 644 /workspace/victim && "
                    "chown 22001:22001 /workspace/victim && "
                    "ln -s /workspace/victim /home/codex/.codex/.credentials.json && "
                    "/entrypoint.sh --version; "
                    "status=$?; "
                    "[ \"$status\" -ne 0 ] && "
                    "[ \"$(stat -c %a /workspace/victim)\" = 644 ] && "
                    "[ \"$(cat /workspace/victim)\" = 'host file' ]",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "refusing unsafe sensitive Codex state .credentials.json",
                result.stderr,
            )

    def test_nested_read_only_file_overlay_protects_repository_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo"
            repo.mkdir()
            source = repo / ".mcp.json"
            overlay = temp_path / "overlay.json"
            source.write_text('{"source":"repository"}\n', encoding="utf-8")
            overlay.write_text('{"source":"cage-overlay"}\n', encoding="utf-8")
            overlay.chmod(0o600)

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{repo}:{repo}",
                    "-v",
                    f"{overlay}:{repo / '.mcp.json'}:ro",
                    "alpine:3",
                    "sh",
                    "-c",
                    "grep -q cage-overlay \"$1/.mcp.json\" && "
                    "! (printf x >> \"$1/.mcp.json\" 2>/dev/null)",
                    "sh",
                    str(repo),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                source.read_text(encoding="utf-8"),
                '{"source":"repository"}\n',
            )

    def test_netgate_accepts_container_client_but_rejects_malformed_target(self):
        with tempfile.TemporaryDirectory() as config_dir:
            auth_token = "b" * 64
            proxy_env = os.environ.copy()
            proxy_env["CAGE_NETGATE_AUTH_TOKEN"] = auth_token
            proxy = subprocess.Popen(
                [
                    sys.executable,
                    str(NETGATE),
                    "--project-hash",
                    "smoketest",
                    "--container-name",
                    "cage-netgate-smoke",
                    "--config-dir",
                    config_dir,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proxy_env,
            )
            try:
                self.assertIsNotNone(proxy.stdout)
                port_line = proxy.stdout.readline().strip()
                ready_line = proxy.stdout.readline().strip()
                startup_stderr = ""
                if not port_line.startswith("PORT=") or ready_line != "READY":
                    self.assertIsNotNone(proxy.stderr)
                    startup_stderr = proxy.stderr.read()
                self.assertTrue(
                    port_line.startswith("PORT="),
                    f"stdout={port_line!r}/{ready_line!r}; stderr={startup_stderr}",
                )
                self.assertEqual(
                    ready_line,
                    "READY",
                    f"stdout={port_line!r}/{ready_line!r}; stderr={startup_stderr}",
                )
                port = port_line.split("=", 1)[1]
                auth = base64.b64encode(f"cage:{auth_token}".encode()).decode()

                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--add-host",
                        "host.docker.internal:host-gateway",
                        "alpine:3",
                        "sh",
                        "-c",
                        "{ printf 'GET / HTTP/1.1\\r\\nHost: invalid\\r\\n"
                        f"Proxy-Authorization: Basic {auth}\\r\\n"
                        "Connection: close\\r\\n\\r\\n'; sleep 1; } "
                        "| nc -w 3 host.docker.internal \"$1\"",
                        "sh",
                        port,
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("400 Expected an absolute HTTP URL", result.stdout)
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
