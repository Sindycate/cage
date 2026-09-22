import json
from pathlib import Path
import tempfile
from cage_core import monitor
from cage_core.monitoring import constants as constants_api, identity as identity_api
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorProvidersTests(MonitorTestCase):
    def test_custom_provider_approval_is_private_and_inactive_until_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            label = "approved-provider"
            session = self._session(
                "approved", total=100, input_tokens=80, output_tokens=20, provider=label
            )
            summaries = [
                (record, self._summary(record.device_id, {"codex:approved": session}))
            ]

            monitor.approve_provider_label(root, label)

            state_path = root / "monitor" / constants_api.PROVIDER_LABELS_FILE
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(monitor.provider_label_status(root)["pending"], [label])
            baseline, _ = monitor.aggregate_provider_summaries(root, summaries)
            self.assertEqual(set(baseline), {"unattributed"})
            self.assertNotIn(label, json.dumps(baseline, sort_keys=True))
            with self.assertRaisesRegex(monitor.MonitorError, "provider identity"):
                monitor.provider_device_id(root, label)
            self.assertEqual(monitor.provider_display_name(label), "Unattributed")
            self.assertEqual(
                monitor.provider_display_name(
                    label,
                    allowed_provider_ids=frozenset({"unattributed", label}),
                ),
                label,
            )
            self.assertEqual(
                monitor.provider_device_id(root, label, include_approved=True),
                f"cage-{label}-{identity_api._platform_slug()}-"
                f"{identity_api.host_install_id(root)[:8]}",
            )
