import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"
STATE_NAME = ".cage-oauth-sync-state.json"


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
volume = Path(os.environ["FAKE_VOLUME_DIR"])
log_path = Path(os.environ["FAKE_DOCKER_LOG"])


def mount_map(arguments):
    result = {}
    index = 0
    while index < len(arguments):
        if arguments[index] == "--mount" and index + 1 < len(arguments):
            fields = {}
            for item in arguments[index + 1].split(","):
                if "=" in item:
                    key, value = item.split("=", 1)
                    fields[key] = value
            destination = fields.get("dst") or fields.get("destination")
            source = fields.get("src") or fields.get("source")
            if destination:
                result[destination] = str(volume) if fields.get("type") == "volume" else source
            index += 2
            continue
        index += 1
    return result


helper = bool(args and args[0] == "run" and "--entrypoint" in args and "python3" in args)
mounts = mount_map(args)
with log_path.open("a", encoding="utf-8") as handle:
    json.dump(
        {
            "command": args[0] if args else "",
            "helper": helper,
            "mounts": mounts,
            "legacy_mounts": [arg for arg in args if ":/" in arg],
        },
        handle,
        sort_keys=True,
    )
    handle.write("\n")

if not args:
    raise SystemExit(0)
if args[0] in {"ps", "exec", "rm", "tag", "pull", "build"}:
    raise SystemExit(0)
if args[0] == "image":
    raise SystemExit(0)
if args[0] == "volume":
    volume.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0)
if args[0] != "run":
    raise SystemExit(0)

if helper:
    code_index = args.index("-c")
    program = args[code_index + 1]
    program_args = [mounts.get(value, value) for value in args[code_index + 2 :]]
    if "FAKE_RACE_ON_APPLY" in os.environ and "credential conflict: volume changed" in program:
        marker = Path(os.environ["FAKE_RACE_ON_APPLY"])
        if not marker.exists():
            marker.write_text("used", encoding="utf-8")
            (volume / ".credentials.json").write_text('{"raced":true}\n', encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-c", program, *program_args])
    raise SystemExit(result.returncode)

action = os.environ.get("FAKE_MAIN_ACTION", "")
credential = volume / ".credentials.json"
if action in {"rotate", "malformed", "oversized", "conflict", "symlink"}:
    if credential.is_symlink() or credential.exists():
        credential.unlink()
if action == "rotate":
    credential.write_text(os.environ["FAKE_VOLUME_CONTENT"], encoding="utf-8")
elif action == "malformed":
    credential.write_text("{not-json", encoding="utf-8")
elif action == "oversized":
    credential.write_bytes(b'{"value":"' + b"x" * (4 * 1024 * 1024) + b'"}\n')
elif action == "conflict":
    credential.write_text(os.environ["FAKE_VOLUME_CONTENT"], encoding="utf-8")
    Path(os.environ["FAKE_HOST_CREDENTIAL"]).write_text(
        os.environ["FAKE_HOST_CONTENT"], encoding="utf-8"
    )
elif action == "symlink":
    credential.symlink_to(Path(os.environ["FAKE_SYMLINK_TARGET"]))

if action in {"rotate", "malformed", "oversized", "conflict"}:
    os.chmod(credential, int(os.environ.get("FAKE_VOLUME_MODE", "600"), 8))
    if "FAKE_VOLUME_MTIME_NS" in os.environ:
        timestamp = int(os.environ["FAKE_VOLUME_MTIME_NS"])
        os.utime(credential, ns=(timestamp, timestamp))
raise SystemExit(int(os.environ.get("FAKE_MAIN_STATUS", "0")))
'''


class OAuthSyncHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.base = Path(self.temp.name)
        self.xdg = self.base / "xdg"
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.repo = self.base / "repo"
        self.volume = self.base / "volume"
        self.log = self.base / "docker.log"
        for directory in (self.xdg / "cage", self.home, self.bin, self.repo, self.volume):
            directory.mkdir(parents=True)
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        self.temp.cleanup()

    def make_codex_home(self, name, value=None, mode=0o600):
        directory = self.base / name
        directory.mkdir()
        if value is not None:
            self.write_json(directory / ".credentials.json", value, mode)
        return directory

    @staticmethod
    def write_json(path, value, mode=0o600):
        path.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
        path.chmod(mode)

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def write_config(self, codex_home):
        (self.xdg / "cage" / "config.toml").write_text(
            "\n".join(
                [
                    "version = 1",
                    'default_preset = "codex-test"',
                    "[auth.codex-test]",
                    'tool = "codex"',
                    f'host_codex_dir = "{codex_home}"',
                    "copy_auth = false",
                    "[presets.codex-test]",
                    'tool = "codex"',
                    'auth = "codex-test"',
                    'net = "open"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def launch(self, codex_home, **extra_env):
        self.write_config(codex_home)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg),
                "PATH": f"{self.bin}{os.pathsep}{environment['PATH']}",
                "FAKE_VOLUME_DIR": str(self.volume),
                "FAKE_DOCKER_LOG": str(self.log),
                "FAKE_HOST_CREDENTIAL": str(codex_home / ".credentials.json"),
            }
        )
        for name in ("OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            environment.pop(name, None)
        environment.update({name: str(value) for name, value in extra_env.items()})
        return subprocess.run(
            [str(CAGE), str(self.repo)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_refresh_uses_hashes_not_mtimes_and_normalizes_private_modes(self):
        codex_home = self.make_codex_home("codex-a", {"token": "initial"}, mode=0o644)
        first = self.launch(codex_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(stat.S_IMODE((codex_home / ".credentials.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.volume / ".credentials.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.volume / STATE_NAME).stat().st_mode), 0o600)

        # A deliberately future host mtime and an older/equal volume mtime must
        # not defeat a one-sided refresh-token change.
        future_ns = 4_102_444_800_000_000_000
        host_path = codex_home / ".credentials.json"
        os.utime(host_path, ns=(future_ns, future_ns))
        second = self.launch(
            codex_home,
            FAKE_MAIN_ACTION="rotate",
            FAKE_VOLUME_CONTENT='{"token":"rotated"}\n',
            FAKE_VOLUME_MODE="777",
            FAKE_VOLUME_MTIME_NS=host_path.stat().st_mtime_ns,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.read_json(host_path), {"token": "rotated"})
        self.assertEqual(self.read_json(self.volume / ".credentials.json"), {"token": "rotated"})
        self.assertEqual(stat.S_IMODE(host_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.volume / ".credentials.json").stat().st_mode), 0o600)

    def test_host_and_volume_symlinks_fail_without_following_targets(self):
        target = self.base / "target.json"
        target.write_text('{"untouched":true}\n', encoding="utf-8")
        codex_home = self.make_codex_home("codex-symlink")
        (codex_home / ".credentials.json").symlink_to(target)
        host_result = self.launch(codex_home)
        self.assertNotEqual(host_result.returncode, 0)
        self.assertIn("must not be a symlink", host_result.stderr)
        self.assertEqual(self.read_json(target), {"untouched": True})

        (codex_home / ".credentials.json").unlink()
        self.write_json(codex_home / ".credentials.json", {"token": "safe"})
        initialized = self.launch(codex_home)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        (self.volume / ".credentials.json").unlink()
        (self.volume / ".credentials.json").symlink_to(target)
        volume_result = self.launch(codex_home)
        self.assertNotEqual(volume_result.returncode, 0)
        self.assertIn("unsafe non-regular OAuth volume state", volume_result.stderr)
        self.assertEqual(self.read_json(target), {"untouched": True})
        self.assertEqual(self.read_json(codex_home / ".credentials.json"), {"token": "safe"})

    def test_malformed_and_oversized_volume_json_never_reaches_host(self):
        codex_home = self.make_codex_home("codex-invalid", {"token": "safe"})
        initialized = self.launch(codex_home)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        malformed = self.launch(codex_home, FAKE_MAIN_ACTION="malformed")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("not valid UTF-8 JSON", malformed.stderr)
        self.assertEqual(self.read_json(codex_home / ".credentials.json"), {"token": "safe"})

        # Repair from the authoritative host, then exercise the byte bound.
        (self.volume / ".credentials.json").unlink()
        repaired = self.launch(codex_home)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        oversized = self.launch(codex_home, FAKE_MAIN_ACTION="oversized")
        self.assertNotEqual(oversized.returncode, 0)
        self.assertIn("oversized OAuth volume state", oversized.stderr)
        self.assertEqual(self.read_json(codex_home / ".credentials.json"), {"token": "safe"})

    def test_two_sided_change_is_an_explicit_conflict(self):
        codex_home = self.make_codex_home("codex-conflict", {"token": "base"})
        initialized = self.launch(codex_home)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        result = self.launch(
            codex_home,
            FAKE_MAIN_ACTION="conflict",
            FAKE_HOST_CONTENT='{"token":"host-change"}\n',
            FAKE_VOLUME_CONTENT='{"token":"volume-change"}\n',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("host and volume both changed", result.stderr)
        self.assertEqual(self.read_json(codex_home / ".credentials.json"), {"token": "host-change"})
        self.assertEqual(self.read_json(self.volume / ".credentials.json"), {"token": "volume-change"})

    def test_volume_identity_switch_never_writes_old_account_into_new_source(self):
        first_home = self.make_codex_home("codex-first", {"account": "first"})
        second_home = self.make_codex_home("codex-second", {"account": "second"})
        logged_out_home = self.make_codex_home("codex-logged-out")
        initialized = self.launch(first_home)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(self.read_json(self.volume / ".credentials.json"), {"account": "first"})

        switched = self.launch(second_home)
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertIn("state identity changed", switched.stderr)
        self.assertEqual(self.read_json(second_home / ".credentials.json"), {"account": "second"})
        self.assertEqual(self.read_json(self.volume / ".credentials.json"), {"account": "second"})
        self.assertEqual(self.read_json(first_home / ".credentials.json"), {"account": "first"})

        logged_out = self.launch(logged_out_home)
        self.assertEqual(logged_out.returncode, 0, logged_out.stderr)
        self.assertFalse((logged_out_home / ".credentials.json").exists())
        self.assertFalse((self.volume / ".credentials.json").exists())
        self.assertEqual(self.read_json(second_home / ".credentials.json"), {"account": "second"})

    def test_apply_compare_and_swap_detects_a_race(self):
        codex_home = self.make_codex_home("codex-race", {"token": "host"})
        result = self.launch(
            codex_home,
            FAKE_RACE_ON_APPLY=self.base / "race-used",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("volume changed during reconciliation", result.stderr)
        self.assertEqual(self.read_json(codex_home / ".credentials.json"), {"token": "host"})

    def test_helpers_never_receive_the_host_codex_directory_writable(self):
        codex_home = self.make_codex_home("codex-mounts", {"token": "safe"})
        result = self.launch(codex_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        helpers = [entry for entry in entries if entry["helper"]]
        self.assertGreaterEqual(len(helpers), 1)
        self.assertTrue(all(str(codex_home) not in json.dumps(entry) for entry in helpers))
        main_mounts = [mount for entry in entries for mount in entry["legacy_mounts"]]
        self.assertIn(f"{codex_home}:/host-codex:ro", main_mounts)

    def test_oauth_helper_stage_uses_the_checked_config_directory(self):
        codex_home = self.make_codex_home("codex-stage", {"token": "safe"})
        system_temp = self.base / "system-temp"
        system_temp.mkdir()

        result = self.launch(codex_home, TMPDIR=system_temp)
        self.assertEqual(result.returncode, 0, result.stderr)

        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        helper_stages = {
            Path(source).parent
            for entry in entries
            if entry["helper"]
            for destination, source in entry["mounts"].items()
            if destination in {"/out", "/sync-in"}
        }
        self.assertTrue(helper_stages)
        self.assertEqual(helper_stages, {(self.xdg / "cage").resolve()})
        self.assertEqual(list((self.xdg / "cage").glob(".cage-oauth-sync-*")), [])

        failed = self.launch(codex_home, FAKE_MAIN_ACTION="malformed")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(list((self.xdg / "cage").glob(".cage-oauth-sync-*")), [])


if __name__ == "__main__":
    unittest.main()
