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
) -> storage.ImageRecord:
    repository = {
        "base": "cage-base",
        "claude": "claude-code",
        "codex": "codex",
    }[role]
    return storage.ImageRecord(
        image_id=image_id or f"sha256:{role}-{version}",
        tags=tags if tags is not None else (f"{repository}:{version}",),
        created=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        size_bytes=1024,
        labels={
            storage.MANAGED_LABEL: "true",
            storage.ROLE_LABEL: role,
            storage.VERSION_LABEL: version,
        },
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
            retained_versions={"base": (), "claude": (), "codex": ()},
            legacy_cage_images=0,
        )

    def test_df_probe_is_portable_when_daemon_root_is_not_host_visible(self):
        results = [
            completed([], stdout='"/var/lib/docker"\n'),
            completed([], stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 1000 250 750 25% /\n"),
        ]
        managed = image("1.0.0")
        with patch("cage_core.storage.os.statvfs", side_effect=OSError("not visible")), patch(
            "cage_core.storage._run", side_effect=results
        ):
            capacity = storage.probe_capacity("docker", (managed,))

        self.assertEqual(capacity.free_bytes, 750 * 1024)
        self.assertIn("Docker overlay", capacity.source)

    def test_noninteractive_warning_proceeds_but_critical_and_build_block(self):
        noninteractive = Mock()
        noninteractive.isatty.return_value = False
        with patch("cage_core.storage.snapshot", return_value=self.state(10)):
            storage.preflight(
                "docker", self.policy, preferred_image="codex:current",
                requires_build=False, input_stream=noninteractive,
            )
        with patch("cage_core.storage.snapshot", return_value=self.state(4)):
            with self.assertRaisesRegex(storage.StorageError, "noninteractive launch blocked"):
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=False, input_stream=noninteractive,
                )
        with patch("cage_core.storage.snapshot", return_value=self.state(10)):
            with self.assertRaisesRegex(storage.StorageError, "noninteractive launch blocked"):
                storage.preflight(
                    "docker", self.policy, preferred_image="codex:current",
                    requires_build=True, input_stream=noninteractive,
                )

    def test_unknown_capacity_reports_warning_without_false_block(self):
        noninteractive = Mock()
        noninteractive.isatty.return_value = False
        stderr = io.StringIO()
        with patch("cage_core.storage.snapshot", return_value=self.state(None)), patch(
            "sys.stderr", stderr
        ):
            storage.preflight(
                "docker", self.policy, preferred_image="", requires_build=True,
                input_stream=noninteractive,
            )
        self.assertIn("could not be measured", stderr.getvalue())


class CleanupExecutionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
