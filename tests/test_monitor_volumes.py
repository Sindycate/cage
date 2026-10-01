import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from cage_core import monitor
from cage_core.monitoring import volumes as volumes_api
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorVolumeTests(MonitorTestCase):
    @staticmethod
    def _inspected(name):
        return {
            "Name": name,
            "Driver": FINGERPRINT["driver"],
            "Scope": FINGERPRINT["scope"],
            "CreatedAt": FINGERPRINT["created_at"],
            "Labels": None,
        }

    def test_batch_inspects_exact_known_names_once_and_maps_reordered_output(self):
        names = [f"codex-state-{index}" for index in range(17)]
        raw = [self._inspected(name) for name in reversed(names)]
        with patch.object(
            volumes_api.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr=""),
        ) as run:
            fingerprints = volumes_api.volume_fingerprints("docker", names)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["docker", "volume", "inspect", *names])
        self.assertEqual(run.call_args.kwargs["timeout"], 15.0)
        self.assertEqual(fingerprints, {
            name: dict(FINGERPRINT, name=name) for name in names
        })

    def test_batch_bounds_maximum_length_names_and_deduplicates_requests(self):
        names = [f"{index:04d}" + "x" * 251 for index in range(130)]

        def inspect(_docker, arguments):
            self.assertEqual(arguments[:2], ["volume", "inspect"])
            self.assertLessEqual(len(arguments[2:]), volumes_api.VOLUME_INSPECT_BATCH_SIZE)
            self.assertLessEqual(sum(len(name) + 1 for name in arguments[2:]), 16384)
            return [self._inspected(name) for name in reversed(arguments[2:])]

        with patch.object(volumes_api, "_docker_json", side_effect=inspect) as query:
            fingerprints = volumes_api.volume_fingerprints("docker", names + names[:3])
        self.assertEqual(query.call_count, 3)
        self.assertEqual(set(fingerprints), set(names))

    def test_empty_batch_does_not_inspect(self):
        with patch.object(volumes_api, "_docker_json") as query:
            self.assertEqual(volumes_api.volume_fingerprints("docker", []), {})
        query.assert_not_called()

    def test_invalid_request_is_rejected_before_any_chunk(self):
        requests = [
            None, "codex-state-demo", {"codex-state-demo": {}},
            ["codex-state-demo", 1],
            [f"codex-state-{index}" for index in range(65)] + ["state,dst=/escape"],
        ]
        for names in requests:
            with self.subTest(names=names), patch.object(volumes_api, "_docker_json") as query:
                with self.assertRaises(monitor.MonitorError):
                    volumes_api.volume_fingerprints("docker", names)
                query.assert_not_called()

    def test_batch_rejects_missing_extra_duplicate_and_malformed_results(self):
        first, second = "codex-state-first", "codex-state-second"
        valid = [self._inspected(first), self._inspected(second)]
        malformed = [
            {}, valid[:1], valid + [self._inspected("codex-state-extra")],
            [valid[0], valid[0]], [valid[0], self._inspected("codex-state-extra")],
            [valid[0], None],
        ]
        for field, value in (
            ("Name", None), ("Driver", None), ("Scope", ""), ("CreatedAt", 123),
            ("CreatedAt", "x" * 513), ("Labels", []),
            ("Labels", {"io.cage.identity": 123}), ("Labels", {"unrelated": False}),
        ):
            changed = deepcopy(valid)
            changed[1][field] = value
            malformed.append(changed)
        for field in ("Name", "Driver", "Scope", "CreatedAt", "Labels"):
            changed = deepcopy(valid)
            del changed[1][field]
            malformed.append(changed)
        for value in malformed:
            with self.subTest(value=value), patch.object(volumes_api, "_docker_json", return_value=value):
                with self.assertRaises(monitor.MonitorError):
                    volumes_api.volume_fingerprints("docker", [first, second])

    def test_batch_rejects_partial_stdout_when_docker_fails(self):
        raw = json.dumps([self._inspected("codex-state-first")])
        with patch.object(
            volumes_api.subprocess, "run",
            return_value=SimpleNamespace(returncode=1, stdout=raw, stderr="volume missing"),
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "volume missing"):
                volumes_api.volume_fingerprints("docker", ["codex-state-first", "codex-state-second"])

    def test_batch_rejects_timeout_start_failure_and_invalid_json(self):
        for error in (subprocess.TimeoutExpired("docker", 15), OSError("unavailable")):
            with self.subTest(error=error), patch.object(volumes_api.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(monitor.MonitorError, "Docker monitor operation failed"):
                    volumes_api.volume_fingerprints("docker", ["codex-state-demo"])
        with patch.object(
            volumes_api.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="[", stderr=""),
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "invalid JSON"):
                volumes_api.volume_fingerprints("docker", ["codex-state-demo"])

    def test_later_invalid_chunk_never_returns_partial_inventory(self):
        names = [f"codex-state-{index}" for index in range(65)]
        with patch.object(
            volumes_api, "_docker_json",
            side_effect=[[self._inspected(name) for name in names[:64]], []],
        ) as query:
            with self.assertRaisesRegex(monitor.MonitorError, "incomplete"):
                volumes_api.volume_fingerprints("docker", names)
        self.assertEqual(query.call_count, 2)

    def test_single_volume_uses_same_strict_normalization_and_exact_name(self):
        raw = self._inspected("codex-state-demo")
        raw["Labels"] = {"io.cage.identity": "a" * 32}
        with patch.object(volumes_api, "_docker_json", return_value=[raw]):
            self.assertEqual(
                volumes_api.volume_fingerprint("docker", "codex-state-demo"),
                dict(FINGERPRINT, label_identity="a" * 32),
            )
            with self.assertRaisesRegex(monitor.MonitorError, "unexpected"):
                volumes_api.volume_fingerprint("docker", "codex-state-other")
        raw["Labels"] = {"io.cage.identity": False}
        with patch.object(volumes_api, "_docker_json", return_value=[raw]):
            with self.assertRaises(monitor.MonitorError):
                volumes_api.volume_fingerprint("docker", "codex-state-demo")
