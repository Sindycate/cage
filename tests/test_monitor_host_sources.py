import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import (
    collector as collector_api,
    host_sources as host_sources_api,
    hub as hub_api,
    registry as registry_api,
    service as service_api,
    split_state as split_state_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorHostSourcesTests(MonitorTestCase):
    def test_host_source_adoption_is_auth_root_scoped_and_never_imports_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "sessions").mkdir(parents=True)
            (source / "archived_sessions").mkdir()
            (source / "sessions" / "direct.jsonl").write_text("outside-cage", encoding="utf-8")
            (source / "config.toml").write_text('model = "example"\n', encoding="utf-8")
            (source / "work.config.toml").write_text('model = "profile"\n', encoding="utf-8")
            (source / "auth.json").write_text('{"credential":"example"}\n', encoding="utf-8")

            record = monitor.register_host_source(
                root, source, copy_auth=True, allow_replacement=True
            )
            same_source = monitor.register_host_source(
                root, source / ".", copy_auth=True, allow_replacement=True
            )
            home = monitor.host_source_home(root, record)

            self.assertEqual(record.target, "host")
            self.assertEqual(record.logical_id, same_source.logical_id)
            self.assertEqual(len(monitor.load_registry(root)), 1)
            self.assertNotEqual(home, source)
            self.assertEqual((home / "config.toml").read_text(), 'model = "example"\n')
            self.assertEqual((home / "work.config.toml").read_text(), 'model = "profile"\n')
            self.assertEqual((home / "auth.json").read_text(), '{"credential":"example"}\n')
            self.assertFalse((home / "sessions" / "direct.jsonl").exists())
            self.assertNotIn(str(source), json.dumps(record.public_dict_for(root)))
            self.assertNotIn(str(source), (root / "monitor" / "registry.json").read_text())
            payload, _status = monitor.aggregate_summaries(
                root,
                [(record, self._summary(record.device_id, {}))],
            )
            self.assertNotIn(str(source), json.dumps(hub_api._outbound_payload(root, payload)))
            self.assertEqual(monitor.registered_host_source(root, source), record)

    def test_host_source_respects_copy_auth_and_rejects_static_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / "auth.json").write_text('{"credential":"example"}\n', encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=True, allow_replacement=True
            )
            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=False,
                copy_oauth_credentials=False,
            )
            self.assertFalse((session.codex_home / "auth.json").exists())

            unsafe = root / "unsafe-source"
            unsafe.mkdir()
            outside = root / "outside.toml"
            outside.write_text("", encoding="utf-8")
            (unsafe / "config.toml").symlink_to(outside)
            with self.assertRaisesRegex(monitor.MonitorError, "non-symlink"):
                monitor.register_host_source(
                    root, unsafe, copy_auth=False, allow_replacement=True
                )

    def test_host_source_uses_only_managed_session_bind_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                output_mount = next(
                    item for item in command if "dst=/out/summary.json" in item
                )
                output = Path(output_mount.split("src=", 1)[1].split(",", 1)[0])
                output.write_text(
                    json.dumps(
                        {
                            "deviceId": record.device_id,
                            "trackedClients": ["codex"],
                            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
                            "today": {"totalTokens": 0},
                            "month": {"totalTokens": 0},
                            "allTime": {"totalTokens": 0},
                        }
                    ),
                    encoding="utf-8",
                )
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch('cage_core.monitoring.collector.subprocess.run', side_effect=fake_run):
                payload = collector_api._run_collector(
                    "docker", "collector", record, root, uid=os.getuid(), gid=os.getgid()
                )

            self.assertEqual(payload["allTime"]["totalTokens"], 0)
            command = " ".join(commands[-1])
            home = monitor.host_source_home(root, record)
            self.assertIn(f"src={home / 'sessions'},dst=/scan/codex/sessions,readonly", command)
            self.assertIn(
                f"src={home / 'archived_sessions'},dst=/scan/codex/archived_sessions,readonly",
                command,
            )
            self.assertNotIn(f"src={source},", command)
            self.assertNotIn("volume-subpath", command)

    def test_host_source_collector_error_does_not_reveal_a_managed_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            managed_home = monitor.host_source_home(root, record)
            result = type(
                "Result",
                (), {
                    "returncode": 1,
                    "stderr": f"invalid bind source path: {managed_home / 'sessions'}",
                },
            )()

            with patch('cage_core.monitoring.collector.subprocess.run', return_value=result):
                with self.assertRaisesRegex(
                    monitor.MonitorError, "managed host sessions"
                ) as raised:
                    collector_api._run_collector(
                        "docker",
                        "collector",
                        record,
                        root,
                        uid=os.getuid(),
                        gid=os.getgid(),
                    )

            self.assertNotIn(str(managed_home), str(raised.exception))
            registry_api._record_scan_error(
                root, record, f"collector bind failure: {managed_home / 'sessions'}"
            )
            stored = next(
                item
                for item in monitor.load_registry(root)
                if item.logical_id == record.logical_id
            )
            self.assertEqual(
                stored.last_error,
                "Token Monitor scan failed for managed host sessions",
            )
            self.assertNotIn(str(managed_home), stored.last_error)

    def test_host_aggregate_errors_without_a_managed_path_remain_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            safe_error = "monitor upload generation provider is invalid"

            self.assertEqual(
                registry_api._scan_error_for_records(root, [record], safe_error),
                safe_error,
            )
            source_root, _home, _snapshot = host_sources_api._host_source_paths(root, record)
            self.assertEqual(
                registry_api._scan_error_for_records(
                    root, [record], f"collector bind failure: {source_root / 'sessions'}"
                ),
                "Token Monitor scan failed for managed host sessions",
            )

    def test_host_scan_error_is_redacted_for_every_aggregate_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            host = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            volume = monitor.VolumeRegistration(
                "a" * 32,
                host.device_id,
                "codex-state-volume",
                "container",
                "/work/volume",
                "Cage: volume (Container)",
                dict(FINGERPRINT, name="codex-state-volume"),
            )
            monitor.save_registry(root, [host, volume])
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            managed_home = monitor.host_source_home(root, host)
            with patch.object(
                split_state_api, "provider_split_pending", return_value=False
            ), patch.object(
                service_api,
                "_collect_registered_summaries",
                side_effect=monitor.MonitorError(
                    f"collector bind failed: {managed_home / 'sessions'}"
                ),
            ):
                with self.assertRaisesRegex(
                    monitor.MonitorError, "managed host sessions"
                ) as raised:
                    monitor.scan_all_registrations(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.36.0",
                        storage_policy=object(),
                        allow_build=False,
                        force=True,
                    )

            self.assertNotIn(str(managed_home), str(raised.exception))
            records = monitor.load_registry(root)
            self.assertEqual(
                {record.last_error for record in records},
                {"Token Monitor scan failed for managed host sessions"},
            )
            self.assertNotIn(
                str(managed_home),
                json.dumps([record.public_dict_for(root) for record in records]),
            )

    def test_host_source_deduplicates_with_volume_sessions_and_source_replacement_is_unadopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            host = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            volume = monitor.VolumeRegistration(
                "a" * 32,
                host.device_id,
                "codex-state-volume",
                "container",
                "/work/volume",
                "Cage: volume (Container)",
                dict(FINGERPRINT, name="codex-state-volume"),
            )
            shared = self._session(
                "shared", total=100, input_tokens=80, output_tokens=20
            )
            payload, status = monitor.aggregate_summaries(
                root,
                [
                    (host, self._summary(host.device_id, {"codex:shared": shared})),
                    (volume, self._summary(volume.device_id, {"codex:shared": dict(shared)})),
                ],
            )
            self.assertEqual(payload["allTime"]["totalTokens"], 100)
            self.assertEqual(status["duplicate_sessions"], 1)
            self.assertEqual(
                payload["today"]["sessions"]["codex:shared"]["projectLabel"],
                "Cage: Unattributed",
            )

            source.rename(root / "old-source")
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            self.assertIsNone(monitor.registered_host_source(root, source))

    def test_host_source_oauth_writeback_is_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / ".credentials.json").write_text('{"credential":"one"}\n', encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=False,
                copy_oauth_credentials=True,
            )
            host_sources_api._write_private_bytes(
                session.codex_home / ".credentials.json",
                b'{"credential":"two"}\n',
            )
            monitor.finish_host_source(session)
            self.assertEqual(
                (source / ".credentials.json").read_text(), '{"credential":"two"}\n'
            )

            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=False,
                copy_oauth_credentials=True,
            )
            (source / ".credentials.json").write_text('{"credential":"outside"}\n', encoding="utf-8")
            host_sources_api._write_private_bytes(
                session.codex_home / ".credentials.json",
                b'{"credential":"managed"}\n',
            )
            with self.assertRaisesRegex(monitor.MonitorError, "source was preserved"):
                monitor.finish_host_source(session)
            self.assertEqual(
                (source / ".credentials.json").read_text(), '{"credential":"outside"}\n'
            )

    def test_host_source_auth_writeback_is_source_wins_and_never_deletes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / "auth.json").write_text('{"credential":"one"}\n', encoding="utf-8")
            record = monitor.register_host_source(
                root, source, copy_auth=True, allow_replacement=True
            )
            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=True,
                copy_oauth_credentials=False,
            )
            host_sources_api._write_private_bytes(
                session.codex_home / "auth.json", b'{"credential":"two"}\n'
            )
            monitor.finish_host_source(session)
            self.assertEqual(
                (source / "auth.json").read_text(), '{"credential":"two"}\n'
            )

            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=True,
                copy_oauth_credentials=False,
            )
            (source / "auth.json").write_text(
                '{"credential":"outside"}\n', encoding="utf-8"
            )
            host_sources_api._write_private_bytes(
                session.codex_home / "auth.json", b'{"credential":"managed"}\n'
            )
            with self.assertRaisesRegex(monitor.MonitorError, "source was preserved"):
                monitor.finish_host_source(session)
            self.assertEqual(
                (source / "auth.json").read_text(), '{"credential":"outside"}\n'
            )

            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=True,
                copy_oauth_credentials=False,
            )
            (session.codex_home / "auth.json").unlink()
            with self.assertRaisesRegex(monitor.MonitorError, "disappeared"):
                monitor.finish_host_source(session)
            self.assertEqual(
                (source / "auth.json").read_text(), '{"credential":"outside"}\n'
            )

    def test_host_source_writeback_rechecks_source_at_replace_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / ".credentials.json").write_text(
                '{"credential":"one"}\n', encoding="utf-8"
            )
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=False,
                copy_oauth_credentials=True,
            )
            host_sources_api._write_private_bytes(
                session.codex_home / ".credentials.json",
                b'{"credential":"managed"}\n',
            )
            original_read = host_sources_api._read_host_regular

            def race(path, **kwargs):
                result = original_read(path, **kwargs)
                if path == session.codex_home / ".credentials.json":
                    host_sources_api._write_private_bytes(
                        source / ".credentials.json", b'{"credential":"outside"}\n'
                    )
                return result

            with patch('cage_core.monitoring.host_sources._read_host_regular', side_effect=race):
                with self.assertRaisesRegex(monitor.MonitorError, "source was preserved"):
                    monitor.finish_host_source(session)
            self.assertEqual(
                (source / ".credentials.json").read_text(), '{"credential":"outside"}\n'
            )

    def test_host_source_writeback_rejects_replaced_source_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / ".credentials.json").write_text(
                '{"credential":"one"}\n', encoding="utf-8"
            )
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            session = monitor.prepare_host_source(
                root,
                record,
                source,
                copy_auth=False,
                copy_oauth_credentials=True,
            )
            host_sources_api._write_private_bytes(
                session.codex_home / ".credentials.json",
                b'{"credential":"managed"}\n',
            )
            source.rename(root / "old-source")
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (source / ".credentials.json").write_text(
                '{"credential":"replacement"}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(monitor.MonitorError, "source was preserved"):
                monitor.finish_host_source(session)
            self.assertEqual(
                (source / ".credentials.json").read_text(),
                '{"credential":"replacement"}\n',
            )
