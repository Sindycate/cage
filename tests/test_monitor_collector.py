import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import (
    collector as collector_api,
    snapshots as snapshots_api,
    state as state_api,
    validation as validation_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorCollectorTests(MonitorTestCase):
    def test_collector_mounts_only_existing_session_subpaths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = monitor.VolumeRegistration(
                logical_id="a" * 32,
                device_id="cage-" + "a" * 29,
                volume_name="codex-state-demo",
                target="container",
                repository="/work/demo",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
                registered_at="now",
            )
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                if command[-1] == "true":
                    if "volume-subpath=sessions" in " ".join(command):
                        return type("Result", (), {"returncode": 0, "stderr": ""})()
                    if "volume-subpath=archived_sessions" in " ".join(command):
                        return type("Result", (), {"returncode": 1, "stderr": "volume-subpath archived_sessions does not exist"})()
                if command[1:3] == ["run", "--rm"]:
                    output_mount = next(item for item in command if "dst=/out/summary.json" in item)
                    output = Path(output_mount.split("src=", 1)[1].split(",", 1)[0])
                    output.write_text(
                        json.dumps({
                            "deviceId": state.device_id,
                            "trackedClients": ["codex"],
                            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
                            "today": {"totalTokens": 1},
                            "month": {"totalTokens": 2},
                            "allTime": {"totalTokens": 3},
                        }),
                        encoding="utf-8",
                    )
                    state_api._write_json(
                        snapshots_api._project_state_path(root, state) / "provider-evidence.json",
                        {"version": 1, "observations": [{}, {}]},
                    )
                    return type("Result", (), {"returncode": 0, "stderr": ""})()
                raise AssertionError(command)

            with patch('cage_core.monitoring.collector._subpath_available', side_effect=[True, False, False]), patch(
                'cage_core.monitoring.collector.subprocess.run', side_effect=fake_run
            ):
                result = collector_api._run_collector("docker", "cage-token-monitor:dev", state, root, uid=os.getuid(), gid=os.getgid())
            self.assertEqual(result["allTime"]["totalTokens"], 3)
            collector_command = commands[-1]
            joined = " ".join(collector_command)
            self.assertIn("volume-subpath=sessions", joined)
            self.assertIn("volume-nocopy", joined)
            self.assertNotIn("volume-subpath=archived_sessions", joined)
            self.assertIn("TOKEN_MONITOR_OPENCODE_AMBIENT=0", joined)
            self.assertIn("TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS=0", joined)
            self.assertIn("TOKEN_MONITOR_WSL_SCAN=0", joined)
            self.assertIn("TOKSCALE_CONFIG_DIR=/state/tokscale", joined)
            self.assertIn(
                f"/scan/codex:rw,noexec,nosuid,nodev,size=32m,uid={os.getuid()},gid={os.getgid()},mode=700",
                joined,
            )
            pricing = (
                snapshots_api._project_state_path(root, state)
                / "tokscale"
                / "custom-pricing.json"
            )
            self.assertEqual(json.loads(pricing.read_text()), {"models": {}})

    def test_unsupported_volume_subpath_fails_closed(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": 'invalid mount config for type "volume": volume-subpath is not supported',
            },
        )()
        with patch('cage_core.monitoring.collector.subprocess.run', return_value=result) as run:
            with self.assertRaisesRegex(monitor.MonitorError, "does not support volume-subpath"):
                collector_api._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "sessions"
                )
        self.assertIn("volume-nocopy", " ".join(run.call_args.args[0]))

    def test_missing_volume_subpath_is_only_the_empty_directory_case(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": "invalid mount config: volume-subpath archived_sessions does not exist",
            },
        )()
        with patch('cage_core.monitoring.collector.subprocess.run', return_value=result) as run:
            self.assertFalse(
                collector_api._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "archived_sessions"
                )
            )
        self.assertIn("volume-nocopy", " ".join(run.call_args.args[0]))

    def test_missing_volume_subpath_daemon_lstat_error_is_empty_directory_case(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": (
                    "docker: Error response from daemon: cannot access path "
                    "/var/lib/docker/volumes/codex-state-demo/_data/archived_sessions: "
                    "lstat /var/lib/docker/volumes/codex-state-demo/_data/archived_sessions: "
                    "no such file or directory"
                ),
            },
        )()
        with patch('cage_core.monitoring.collector.subprocess.run', return_value=result):
            self.assertFalse(
                collector_api._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "archived_sessions"
                )
            )

    def test_missing_volume_subpath_long_daemon_error_classifies_before_display_truncation(self):
        volume_name = "codex-state-" + ("x" * 180)
        volume_path = f"/var/lib/docker/volumes/{volume_name}/_data/archived_sessions"
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": (
                    f"docker: Error response from daemon: cannot access path {volume_path}: "
                    f"lstat {volume_path}: no such file or directory"
                ),
            },
        )()
        with patch('cage_core.monitoring.collector.subprocess.run', return_value=result):
            self.assertFalse(
                collector_api._subpath_available(
                    "docker", "cage-token-monitor:dev", volume_name, "archived_sessions"
                )
            )

    def test_empty_account_limits_may_have_probe_timestamp(self):
        payload = {
            "deviceId": "cage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "trackedClients": ["codex"],
            "limits": {
                "updatedAt": "2026-08-27T10:51:30.341Z",
                "refreshMs": 300000,
                "providers": [],
            },
            "today": {"totalTokens": 0},
            "month": {"totalTokens": 0},
            "allTime": {"totalTokens": 0},
        }
        self.assertEqual(
            validation_api._validate_summary(payload, payload["deviceId"])["limits"]["providers"],
            [],
        )

    def test_v049_headless_summary_wire_is_allowlisted_and_pinned(self):
        self.assertEqual(monitor.COLLECTOR_SOURCE_VERSION, "0.49.0")
        self.assertEqual(
            monitor.COLLECTOR_SOURCE_COMMIT,
            "7c74e61fd8f9d592e647f14107738746a51e49ff",
        )
        self.assertEqual(
            monitor.COLLECTOR_SOURCE_SHA256,
            "c2f72a31e372b495c0816af561ff789233e0cb2cae2e7e8098d686f9b7fd441e",
        )
        payload = {
            "deviceId": "cage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "hostname": "headless-agent",
            "platform": "linux",
            "osName": "Linux",
            "osVersion": "",
            "updatedAt": "2026-08-28T00:00:00.000Z",
            "agentVersion": "0.49.0",
            "agentRuntime": "headless-agent",
            "projectsEnabled": False,
            "trackedClients": ["codex"],
            "clientStatus": {"codex": {"status": "ok"}},
            "historyAvailable": False,
            "history": None,
            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
            "today": {"totalTokens": 0, "costUsd": 0},
            "month": {"totalTokens": 0, "costUsd": 0},
            "allTime": {"totalTokens": 0, "costUsd": 0},
        }
        self.assertEqual(
            validation_api._validate_summary(payload, payload["deviceId"])["agentRuntime"],
            "headless-agent",
        )

    def test_summary_rejects_unexpected_wire_fields_and_source_paths(self):
        payload = {
            "deviceId": "cage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "trackedClients": ["codex"],
            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
            "today": {"totalTokens": 0},
            "month": {"totalTokens": 0},
            "allTime": {"totalTokens": 0},
        }
        for field in ("nativeSessions", "sentinel"):
            with self.subTest(field=field):
                candidate = dict(payload)
                candidate[field] = {"value": "unexpected"}
                with self.assertRaisesRegex(monitor.MonitorError, "unexpected fields"):
                    validation_api._validate_summary(candidate, payload["deviceId"])

        candidate = dict(payload)
        candidate["today"] = {"totalTokens": 0, "source": "/Users/example/.codex"}
        with self.assertRaisesRegex(monitor.MonitorError, "source path"):
            validation_api._validate_summary(candidate, payload["deviceId"])

    def test_legacy_collector_archive_is_secured_without_following_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.json"
            archive.write_text("{}", encoding="utf-8")
            os.chmod(archive, 0o644)
            state_api._secure_collector_file(archive, max_bytes=1024)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, 0o644)
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(monitor.MonitorError, "unsafe collector"):
                state_api._secure_collector_file(link, max_bytes=1024)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_volume_name_cannot_inject_mount_options(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.volume_fingerprint("docker", "state,dst=/escape")
        with self.assertRaises(monitor.MonitorError):
            monitor.ensure_codex_volume_labels(
                "docker", "state,dst=/escape", logical_id="a" * 32
            )
