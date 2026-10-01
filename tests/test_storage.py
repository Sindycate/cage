from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cage_core import cli, storage
from cage_core.models import ResolvedConfig, StoragePolicy


def completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def image(
    version: str,
    *,
    role: str = "base",
    tags: tuple[str, ...] | None = None,
    image_id: str | None = None,
    age_hours: int = 1,
    terminal: bool = True,
    lifecycle: str = "",
) -> storage.ImageRecord:
    repository = {
        "base": "cage-base",
        "claude": "claude-code",
        "codex": "codex",
        "opencode": "opencode",
        "monitor": "cage-token-monitor",
    }[role]
    labels = {
        storage.MANAGED_LABEL: "true",
        storage.ROLE_LABEL: role,
        storage.VERSION_LABEL: version,
    }
    if lifecycle:
        labels[storage.LIFECYCLE_LABEL] = lifecycle
    return storage.ImageRecord(
        image_id=image_id or f"sha256:{role}-{version}",
        tags=tags if tags is not None else (f"{repository}:{version}",),
        created=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        size_bytes=1024,
        labels=labels,
        terminal_managed=terminal,
    )


class CandidatePolicyTests(unittest.TestCase):
    def test_semantic_retention_and_container_references_are_exact(self):
        images = (
            image("1.0.0"),
            image("2.0.0"),
            image("10.0.0"),
            image("1.0.0", role="codex"),
            image("2.0.0", role="codex"),
            image("3.0.0", role="codex"),
        )
        referenced = frozenset({"sha256:codex-1.0.0"})

        candidates, retained, legacy = storage.cleanup_candidates(
            images,
            referenced,
            StoragePolicy(keep_versions=2),
        )

        self.assertEqual(retained["base"], ("10.0.0", "2.0.0"))
        self.assertEqual(retained["codex"], ("3.0.0", "2.0.0"))
        self.assertEqual([item.reference for item in candidates], ["cage-base:1.0.0"])
        self.assertEqual(legacy, 0)

    def test_unrelated_custom_and_legacy_images_are_never_candidates(self):
        custom = image("1.0.0", tags=("example/custom-agent:1.0.0",), terminal=False)
        custom_official_tag = image(
            "0.1.0",
            tags=("cage-base:0.1.0",),
            image_id="sha256:custom-derived",
            terminal=False,
        )
        legacy = storage.ImageRecord(
            image_id="sha256:legacy",
            tags=("cage-base:0.1.0",),
            created=datetime.now(timezone.utc) - timedelta(days=100),
            size_bytes=1024,
            labels={},
        )

        candidates, _, legacy_count = storage.cleanup_candidates(
            (custom, custom_official_tag, legacy), frozenset(), StoragePolicy()
        )

        self.assertEqual(candidates, ())
        self.assertEqual(legacy_count, 1)

    def test_only_terminal_managed_old_dangling_images_are_candidates(self):
        terminal = image(
            "1.0.0", tags=(), age_hours=25, terminal=True, image_id="sha256:terminal"
        )
        derived = image(
            "1.0.0", tags=(), age_hours=25, terminal=False, image_id="sha256:derived"
        )
        young = image(
            "1.0.0", tags=(), age_hours=23, terminal=True, image_id="sha256:young"
        )

        candidates, _, _ = storage.cleanup_candidates(
            (terminal, derived, young), frozenset(), StoragePolicy()
        )

        self.assertEqual([item.reference for item in candidates], ["sha256:terminal"])

    def test_ephemeral_images_require_age_terminal_identity_and_exact_tags(self):
        old = image(
            "ci",
            role="codex",
            tags=("codex:ci",),
            age_hours=169,
            lifecycle=storage.EPHEMERAL_LIFECYCLE,
            image_id="sha256:old-ephemeral",
        )
        young = image(
            "ci",
            role="codex",
            tags=("codex:ci-young",),
            age_hours=167,
            lifecycle=storage.EPHEMERAL_LIFECYCLE,
            image_id="sha256:young-ephemeral",
        )
        young.labels[storage.VERSION_LABEL] = "ci-young"
        custom_tagged = image(
            "ci",
            role="codex",
            tags=("codex:ci", "codex:latest"),
            age_hours=169,
            lifecycle=storage.EPHEMERAL_LIFECYCLE,
            image_id="sha256:custom-ephemeral",
        )
        referenced = image(
            "ci",
            role="codex",
            tags=("codex:ci-ref",),
            age_hours=169,
            lifecycle=storage.EPHEMERAL_LIFECYCLE,
            image_id="sha256:referenced-ephemeral",
        )
        referenced.labels[storage.VERSION_LABEL] = "ci-ref"

        candidates, _, legacy = storage.cleanup_candidates(
            (old, young, custom_tagged, referenced),
            frozenset({referenced.image_id}),
            StoragePolicy(ephemeral_min_age_hours=168),
        )

        self.assertEqual([item.reference for item in candidates], ["codex:ci"])
        self.assertEqual(legacy, 0)


class ProbeAndPreflightTests(unittest.TestCase):
    def setUp(self):
        self.policy = StoragePolicy()

    def state(self, free_gib: int | None) -> storage.StorageSnapshot:
        return storage.StorageSnapshot(
            capacity=storage.CapacityProbe(
                None if free_gib is None else free_gib * storage.GIB,
                100 * storage.GIB if free_gib is not None else None,
                "test",
                "probe unavailable" if free_gib is None else "",
            ),
            images=(),
            referenced_image_ids=frozenset(),
            candidates=(),
            retained_versions={"base": (), "claude": (), "codex": (), "opencode": ()},
            legacy_cage_images=0,
        )

    def test_df_probe_is_portable_when_daemon_root_is_not_host_visible(self):
        local_id = "sha256:" + "a" * 64
        results = [
            completed([], stdout='"/var/lib/docker"\n'),
            completed([], stdout=local_id + "\n"),
            completed([], stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1000 250 750 25% /\n"),
        ]
        managed = image("1.0.0")
        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=results
        ):
            capacity = storage.probe_capacity("docker", (managed,))

        self.assertEqual(capacity.free_bytes, 750 * 1024)
        self.assertIn("Docker overlay", capacity.source)

    def test_healthy_launch_probes_only_preferred_local_immutable_image(self):
        local_id = "sha256:" + "a" * 64
        results = [
            completed([], stdout='"/var/lib/docker"\n'),
            completed([], stdout=local_id + "\n"),
            completed([], stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 104857600 10485760 94371840 10% /\n"),
        ]
        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=results
        ) as run, patch(
            "cage_core.storage.inventory_images", side_effect=AssertionError("unexpected image inventory")
        ), patch(
            "cage_core.storage._referenced_image_ids", side_effect=AssertionError("unexpected container inventory")
        ):
            storage.preflight(
                "docker", self.policy, preferred_image="codex:current", requires_build=False,
            )

        commands = [call.args[1] for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[1], ["image", "inspect", "--format", "{{.Id}}", "codex:current"])
        probe = commands[2]
        self.assertEqual(probe[probe.index("--pull") + 1], "never")
        self.assertEqual(probe[probe.index("df") + 1], local_id)
        self.assertEqual(probe[probe.index("--network") + 1], "none")
        self.assertIn("--read-only", probe)

    def test_missing_preferred_image_uses_available_local_fallback_without_pull(self):
        local_id = "sha256:" + "b" * 64

        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "inspect"]:
                if arguments[-1] == "cage-base:latest":
                    return completed(arguments, stdout=local_id + "\n")
                return completed(arguments, returncode=1, stderr="No such image")
            if arguments[0] == "run":
                self.assertEqual(arguments[arguments.index("df") + 1], local_id)
                self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
                return completed(arguments, stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1000 250 750 25% /\n")
            self.fail(f"unexpected Docker command: {arguments[:2]}")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ) as docker_run:
            capacity = storage.probe_capacity("docker", preferred_image="codex:new-version")

        self.assertEqual(capacity.free_bytes, 750 * 1024)
        self.assertEqual(len(docker_run.call_args_list), 4)

    def test_no_available_probe_image_reports_unknown_without_starting_or_pulling(self):
        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                self.assertIn("--filter", arguments)
                return completed(arguments)
            self.assertEqual(arguments[:2], ["image", "inspect"])
            return completed(arguments, returncode=1, stderr="No such image")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ) as docker_run:
            capacity = storage.probe_capacity("docker", preferred_image="codex:new-version")

        self.assertIsNone(capacity.free_bytes)
        self.assertLessEqual(len(docker_run.call_args_list), 3 + len(storage.CAPACITY_FALLBACK_IMAGES))

    def test_old_versioned_local_image_is_probed_without_latest_alias(self):
        local_id = "sha256:" + "d" * 64

        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                self.assertIn("--filter", arguments)
                self.assertIn("reference=cage-base:*", arguments)
                self.assertIn("reference=codex:*", arguments)
                return completed(arguments, stdout=local_id + "\n")
            if arguments[:2] == ["image", "inspect"]:
                if arguments[-1] == local_id:
                    return completed(arguments, stdout=local_id + "\n")
                return completed(arguments, returncode=1, stderr="No such image")
            self.assertEqual(arguments[0], "run")
            self.assertEqual(arguments[arguments.index("df") + 1], local_id)
            self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
            return completed(arguments, stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1000 250 750 25% /\n")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ), patch("cage_core.storage.inventory_images", side_effect=AssertionError("unexpected full inventory")):
            capacity = storage.probe_capacity("docker", preferred_image="codex:new-version")

        self.assertEqual(capacity.free_bytes, 750 * 1024)

    def test_versioned_local_fallback_probes_are_bounded(self):
        local_ids = ["sha256:" + f"{index:064x}" for index in range(10)]

        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                return completed(arguments, stdout="\n".join(local_ids))
            if arguments[:2] == ["image", "inspect"]:
                if arguments[-1] in local_ids:
                    return completed(arguments, stdout=arguments[-1] + "\n")
                return completed(arguments, returncode=1, stderr="No such image")
            self.assertEqual(arguments[0], "run")
            self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
            return completed(arguments, returncode=1, stderr="Probe unavailable")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ) as docker_run:
            capacity = storage.probe_capacity("docker", preferred_image="codex:new-version")

        self.assertIsNone(capacity.free_bytes)
        probes = [call for call in docker_run.call_args_list if call.args[1][0] == "run"]
        self.assertEqual(len(probes), 3)

    def test_already_tried_images_do_not_consume_versioned_fallback_budget(self):
        tried_ids = ["sha256:" + f"{index:064x}" for index in range(3)]
        working_id = "sha256:" + "f" * 64
        aliases = dict(zip(storage.CAPACITY_FALLBACK_IMAGES, tried_ids))

        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                return completed(arguments, stdout="\n".join((*tried_ids, working_id)))
            if arguments[:2] == ["image", "inspect"]:
                reference = arguments[-1]
                local_id = aliases.get(reference)
                if local_id is not None or reference == working_id:
                    return completed(arguments, stdout=(local_id or working_id) + "\n")
                return completed(arguments, returncode=1, stderr="No such image")
            self.assertEqual(arguments[0], "run")
            if arguments[arguments.index("df") + 1] == working_id:
                return completed(arguments, stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1000 250 750 25% /\n")
            return completed(arguments, returncode=1, stderr="Probe unavailable")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ):
            capacity = storage.probe_capacity("docker", preferred_image="codex:new-version")

        self.assertEqual(capacity.free_bytes, 750 * 1024)

    def test_malformed_local_image_identity_is_never_executed(self):
        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                return completed(arguments)
            self.assertEqual(arguments[:2], ["image", "inspect"])
            return completed(arguments, stdout="codex:mutable-tag\n")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ):
            capacity = storage.probe_capacity("docker", preferred_image="codex:current")

        self.assertIsNone(capacity.free_bytes)

    def test_probe_does_not_pull_image_removed_after_local_inspection(self):
        local_id = "sha256:" + "c" * 64

        def run(_docker, arguments):
            if arguments[0] == "info":
                return completed(arguments, stdout='"/var/lib/docker"\n')
            if arguments[:2] == ["image", "ls"]:
                return completed(arguments)
            if arguments[:2] == ["image", "inspect"]:
                return completed(arguments, stdout=local_id + "\n")
            self.assertEqual(arguments[0], "run")
            self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
            self.assertEqual(arguments[arguments.index("df") + 1], local_id)
            return completed(arguments, returncode=1, stderr="Image was removed")

        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=run
        ) as docker_run:
            capacity = storage.probe_capacity("docker", preferred_image="codex:current")

        self.assertIsNone(capacity.free_bytes)
        self.assertEqual(sum(call.args[1][0] == "run" for call in docker_run.call_args_list), 1)

    def test_noninteractive_warning_proceeds_but_critical_and_build_block(self):
        noninteractive = Mock()
        noninteractive.isatty.return_value = False
        with patch("cage_core.storage.probe_capacity", return_value=self.state(10).capacity), patch(
            "cage_core.storage.snapshot", side_effect=AssertionError("noninteractive cleanup inventory")
        ):
            storage.preflight(
                "docker", self.policy, preferred_image="codex:current",
                requires_build=False, input_stream=noninteractive,
            )
        with patch("cage_core.storage.probe_capacity", return_value=self.state(4).capacity):
            with self.assertRaisesRegex(storage.StorageError, "noninteractive launch blocked"):
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=False, input_stream=noninteractive,
                )
        with patch("cage_core.storage.probe_capacity", return_value=self.state(10).capacity):
            with self.assertRaisesRegex(storage.StorageError, "noninteractive launch blocked"):
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=True, input_stream=noninteractive,
                )

    def test_unknown_capacity_reports_warning_without_false_block(self):
        noninteractive = Mock()
        noninteractive.isatty.return_value = False
        stderr = io.StringIO()
        with patch("cage_core.storage.probe_capacity", return_value=self.state(None).capacity), patch(
            "cage_core.storage.snapshot", side_effect=AssertionError("unknown capacity inventory")
        ), patch(
            "sys.stderr", stderr
        ):
            storage.preflight(
                "docker", self.policy, preferred_image="", requires_build=True,
                input_stream=noninteractive,
            )
        self.assertIn("could not be measured", stderr.getvalue())

    def test_healthy_launch_and_build_at_threshold_skip_full_snapshot(self):
        for requires_build in (False, True):
            with self.subTest(requires_build=requires_build), patch(
                "cage_core.storage.probe_capacity", return_value=self.state(20).capacity
            ), patch("cage_core.storage.snapshot") as snapshot:
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=requires_build,
                )
            snapshot.assert_not_called()

    def test_interactive_warning_builds_full_preview_and_allows_proceed(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with patch("cage_core.storage.probe_capacity", return_value=self.state(10).capacity), patch(
            "cage_core.storage.snapshot", return_value=self.state(10)
        ) as snapshot, patch("cage_core.storage.delete_candidates") as delete:
            storage.preflight(
                "docker", self.policy, preferred_image="codex:current",
                requires_build=False, input_stream=Terminal("p\n"),
            )
        snapshot.assert_called_once_with("docker", self.policy, preferred_image="codex:current")
        delete.assert_not_called()

    def test_interactive_critical_and_build_floors_reject_proceed(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        for requires_build, free_gib in ((False, 4), (True, 10)):
            with self.subTest(requires_build=requires_build), patch(
                "cage_core.storage.probe_capacity", return_value=self.state(free_gib).capacity
            ), patch("cage_core.storage.snapshot", return_value=self.state(free_gib)), patch(
                "cage_core.storage.delete_candidates"
            ) as delete:
                with self.assertRaisesRegex(storage.StorageError, "launch aborted"):
                    storage.preflight(
                        "docker", self.policy, preferred_image="codex:current",
                        requires_build=requires_build, input_stream=Terminal("p\n"),
                    )
                delete.assert_not_called()

    def test_fresh_low_space_snapshot_can_recover_without_cleanup(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        stream = Terminal("c\n")
        with patch("cage_core.storage.probe_capacity", return_value=self.state(4).capacity), patch(
            "cage_core.storage.snapshot", return_value=self.state(50)
        ), patch("cage_core.storage.delete_candidates") as delete:
            storage.preflight(
                "docker", self.policy, preferred_image="codex:current",
                requires_build=False, input_stream=stream,
            )
        self.assertEqual(stream.tell(), 0)
        delete.assert_not_called()

    def test_unknown_preview_capacity_does_not_bypass_a_measured_low_floor(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        for requires_build, free_gib in ((False, 4), (True, 10)):
            with self.subTest(requires_build=requires_build), patch(
                "cage_core.storage.probe_capacity", return_value=self.state(free_gib).capacity
            ), patch("cage_core.storage.snapshot", return_value=self.state(None)):
                with self.assertRaisesRegex(storage.StorageError, "launch aborted"):
                    storage.preflight(
                        "docker", self.policy, preferred_image="codex:current",
                        requires_build=requires_build, input_stream=Terminal("p\n"),
                    )

    def test_cleanup_remeasures_and_preserves_critical_and_build_floors(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        for requires_build, initial, recovered, succeeds in (
            (False, 4, 5, True), (False, 4, 4, False),
            (True, 10, 20, True), (True, 10, 19, False),
        ):
            with self.subTest(requires_build=requires_build, recovered=recovered), patch(
                "cage_core.storage.probe_capacity", return_value=self.state(initial).capacity
            ), patch(
                "cage_core.storage.snapshot", side_effect=(self.state(initial), self.state(recovered))
            ) as snapshot, patch("cage_core.storage.delete_candidates", return_value=(0, ())) as delete:
                if succeeds:
                    storage.preflight(
                        "docker", self.policy, preferred_image="codex:current",
                        requires_build=requires_build, input_stream=Terminal("c\n"),
                    )
                else:
                    with self.assertRaisesRegex(storage.StorageError, "is required"):
                        storage.preflight(
                            "docker", self.policy, preferred_image="codex:current",
                            requires_build=requires_build, input_stream=Terminal("c\n"),
                        )
                self.assertEqual(snapshot.call_count, 2)
                delete.assert_called_once_with("docker", ())

    def test_cleanup_unknown_capacity_still_blocks(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with patch("cage_core.storage.probe_capacity", return_value=self.state(4).capacity), patch(
            "cage_core.storage.snapshot", side_effect=(self.state(4), self.state(None))
        ), patch("cage_core.storage.delete_candidates", return_value=(0, ())):
            with self.assertRaisesRegex(storage.StorageError, "could not be re-measured"):
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=False, input_stream=Terminal("c\n"),
                )


class CleanupExecutionTests(unittest.TestCase):
    def test_cli_dispatches_maintenance_apply_without_a_tty(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "cage_core.cli.storage.run_storage_command", return_value=0
        ) as run:
            status = cli._run_storage(
                ["maintain", "--apply"], config_root=Path(raw)
            )

        self.assertEqual(status, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "maintain")
        self.assertTrue(run.call_args.kwargs["apply"])

    def test_clean_requires_tty_and_exact_confirmation(self):
        candidate = storage.CleanupCandidate("cage-base:1.0.0", "sha256:old", "old", 1)
        state = ProbeAndPreflightTests().state(50)
        state = storage.StorageSnapshot(
            capacity=state.capacity,
            images=(),
            referenced_image_ids=frozenset(),
            candidates=(candidate,),
            retained_versions=state.retained_versions,
            legacy_cage_images=0,
        )
        stream = Mock()
        stream.isatty.return_value = False
        with patch("cage_core.storage.snapshot", return_value=state):
            with self.assertRaisesRegex(storage.StorageError, "interactive TTY"):
                storage.run_storage_command(
                    "clean", StoragePolicy(), docker="docker", input_stream=stream
                )

    def test_clean_exact_confirmation_deletes_only_previewed_candidates(self):
        candidate = storage.CleanupCandidate("cage-base:1.0.0", "sha256:old", "old", 1)
        state = ProbeAndPreflightTests().state(50)
        state = storage.StorageSnapshot(
            capacity=state.capacity,
            images=(),
            referenced_image_ids=frozenset(),
            candidates=(candidate,),
            retained_versions=state.retained_versions,
            legacy_cage_images=0,
        )

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with patch("cage_core.storage.snapshot", return_value=state), patch(
            "cage_core.storage.delete_candidates", return_value=(1, ())
        ) as delete:
            status = storage.run_storage_command(
                "clean", StoragePolicy(), docker="docker", input_stream=Terminal("CLEAN\n")
            )

        self.assertEqual(status, 0)
        delete.assert_called_once_with("docker", (candidate,))

    def test_maintain_preview_is_default_and_apply_is_noninteractive(self):
        candidate = storage.CleanupCandidate("codex:ci", "sha256:old", "old", 1)
        state = ProbeAndPreflightTests().state(50)
        state = storage.StorageSnapshot(
            capacity=state.capacity,
            images=(),
            referenced_image_ids=frozenset(),
            candidates=(candidate,),
            retained_versions=state.retained_versions,
            legacy_cage_images=0,
        )
        noninteractive = Mock()
        noninteractive.isatty.return_value = False
        with patch("cage_core.storage.snapshot", return_value=state), patch(
            "cage_core.storage.delete_candidates", return_value=(1, ())
        ) as delete:
            status = storage.run_storage_command(
                "maintain", StoragePolicy(), docker="docker", input_stream=noninteractive
            )
            self.assertEqual(status, 0)
            delete.assert_not_called()

        with patch("cage_core.storage.snapshot", return_value=state), patch(
            "cage_core.storage.delete_candidates", return_value=(1, ())
        ) as delete:
            status = storage.run_storage_command(
                "maintain", StoragePolicy(), docker="docker", input_stream=noninteractive,
                apply=True,
            )

        self.assertEqual(status, 0)
        delete.assert_called_once_with("docker", (candidate,))

    def test_race_safe_delete_skips_newly_referenced_image_without_rm(self):
        candidate = storage.CleanupCandidate("cage-base:1.0.0", "sha256:old", "old", 1)
        inspect = completed([], stdout=json.dumps([{"Id": "sha256:old"}]))
        with patch(
            "cage_core.storage._referenced_image_ids",
            return_value=frozenset({"sha256:old"}),
        ), patch("cage_core.storage._run", return_value=inspect) as run:
            removed, failures = storage.delete_candidates("docker", (candidate,))

        self.assertEqual(removed, 0)
        self.assertIn("now referenced", failures[0])
        self.assertFalse(any(call.args[1][:2] == ["image", "rm"] for call in run.call_args_list))

    def test_docker_errors_are_actionable(self):
        with patch(
            "cage_core.storage._run",
            return_value=completed([], returncode=1, stderr="daemon unavailable"),
        ):
            with self.assertRaisesRegex(storage.StorageError, "daemon unavailable"):
                storage.inventory_images("docker")


class HostBypassTests(unittest.TestCase):
    def test_host_native_launch_does_not_probe_docker_storage(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as config_raw:
            repo = Path(raw)
            config_root = Path(config_raw)
            resolved = ResolvedConfig(
                config_path=config_root / "config.toml",
                repo_path=str(repo),
                preset_name="host",
                preset_source="flag",
                tool="codex",
                target="host",
                net="open",
            )
            with patch.dict(os.environ, {"CAGE_CONFIG_DIR": str(config_root)}), patch(
                "cage_core.cli._resolve", return_value=resolved
            ), patch("cage_core.cli.run_host_target", return_value=0), patch(
                "cage_core.cli.storage.preflight"
            ) as preflight:
                status = cli.main([str(repo)], cage_version="0.26.9")

        self.assertEqual(status, 0)
        preflight.assert_not_called()


class SideEffectOrderTests(unittest.TestCase):
    def prepared(self, *, target="container", rebuild=False):
        plan = SimpleNamespace(
            storage_policy=StoragePolicy(),
            image="codex:0.26.9",
            rebuild=rebuild,
            warnings=(),
            target=target,
        )
        return SimpleNamespace(plan=plan, request=SimpleNamespace(tool_arguments=()))

    def test_container_preflight_blocks_before_collision_or_image_acquisition(self):
        from cage_core.targets import container

        prepared = self.prepared()
        with patch("cage_core.targets.container.shutil.which", return_value="docker"), patch(
            "cage_core.targets.container._validate_before_effects"
        ), patch(
            "cage_core.targets.container.storage.preflight",
            side_effect=storage.StorageError("critical"),
        ), patch("cage_core.targets.container._handle_collision") as collision, patch(
            "cage_core.targets.container._acquire_image"
        ) as acquire:
            status = container.run_container_target(
                prepared, install_root=Path("/tmp"), config_root=Path("/tmp")
            )

        self.assertEqual(status, 1)
        collision.assert_not_called()
        acquire.assert_not_called()

    def test_desktop_preflight_blocks_before_supervisor_exec(self):
        prepared = self.prepared(target="desktop")
        prepared.plan.preset_name = "desktop"
        prepared.plan.network = "gate"
        prepared.plan.yolo = False
        prepared.plan.no_open = True
        prepared.plan.repository = "/tmp/repo"
        with patch("cage_core.cli.sys.platform", "darwin"), patch(
            "cage_core.cli.storage.docker_command", return_value="docker"
        ), patch(
            "cage_core.cli.storage.preflight",
            side_effect=storage.StorageError("critical"),
        ), patch("cage_core.cli._exec_python") as execute:
            with self.assertRaisesRegex(storage.StorageError, "critical"):
                cli._delegate_public_desktop(
                    prepared, install_root=Path("/tmp"), config_root=Path("/tmp")
                )

        execute.assert_not_called()

    def test_update_preflight_blocks_before_pull_or_build(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "cage_core.cli.shutil.which", return_value="docker"
        ), patch(
            "cage_core.cli.storage.preflight",
            side_effect=storage.StorageError("build floor"),
        ), patch("cage_core.cli.subprocess.run") as run:
            status = cli._run_update(
                ["codex"],
                install_root=Path(raw),
                config_root=Path(raw),
                cage_version="0.26.9",
            )

        self.assertEqual(status, 1)
        run.assert_not_called()

    def test_update_overlays_coalesce_installer_and_permission_changes(self):
        cases = (
            ("claude", "https://claude.ai/install.sh", "/home/claude/.local"),
            ("codex", "npm install -g @openai/codex@latest", "/home/codex/.npm-global"),
            (
                "opencode",
                "npm install -g --allow-scripts=opencode-ai",
                "/home/opencode/.npm-global",
            ),
        )
        for tool, installer, writable_tree in cases:
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as raw, patch(
                "cage_core.cli.shutil.which", return_value="docker"
            ), patch("cage_core.cli.storage.preflight"), patch(
                "cage_core.cli.subprocess.run",
                return_value=completed([], returncode=0),
            ) as run:
                status = cli._run_update(
                    [tool],
                    install_root=Path(raw),
                    config_root=Path(raw),
                    cage_version="9.9.9",
                )

            self.assertEqual(status, 0)
            overlay = run.call_args_list[-1].kwargs["input"]
            install_line = next(
                line for line in overlay.splitlines() if installer in line
            )
            self.assertIn(
                f"chmod -R a+rwX {writable_tree}", install_line
            )
            self.assertEqual(overlay.count("RUN "), 1)
            self.assertIn("\nUSER root\nLABEL ", overlay)


if __name__ == "__main__":
    unittest.main()
