"""Synthetic monitor fixtures and fail-closed unit-test effect boundaries."""
import subprocess
import unittest
from unittest.mock import patch
from urllib.request import OpenerDirector

from cage_core import monitor


FINGERPRINT = {
    "name": "codex-state-demo",
    "driver": "local",
    "scope": "local",
    "created_at": "2026-08-27T00:00:00Z",
    "label_identity": "",
}


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        # A stale patch target must fail before reaching Docker or a real hub.
        # Individual tests explicitly replace their owning module's boundary.
        for target, attribute, message in (
            (subprocess, "run", "monitor unit test attempted a real subprocess"),
            (OpenerDirector, "open", "monitor unit test attempted real HTTP"),
        ):
            self.enterContext(patch.object(target, attribute, side_effect=AssertionError(message)))

    @staticmethod
    def _session(
        session_id,
        *,
        total,
        input_tokens,
        output_tokens,
        model="gpt-test",
        cost=0,
        provider="openai",
    ):
        return {
            "client": "codex",
            "sessionId": session_id,
            "totalTokens": total,
            "costUsd": cost,
            "messageCount": 1,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "reasoningTokens": 0,
            "startedAt": "2026-08-27T00:00:00Z",
            "lastUsedAt": "2026-08-27T00:01:00Z",
            "projectId": "",
            "projectLabel": "",
            "models": {model: total},
            "modelCosts": ({model: cost} if cost else {}),
            "providers": {provider: total},
        }


    @staticmethod
    def _summary(device_id, sessions, *, period_windows=None):
        total = sum(item["totalTokens"] for item in sessions.values())
        cost = sum(item["costUsd"] for item in sessions.values())
        period = {"totalTokens": total, "costUsd": cost, "sessions": sessions}
        summary = {
            "deviceId": device_id,
            "trackedClients": ["codex"],
            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
            "today": dict(period),
            "month": dict(period),
            "allTime": dict(period),
        }
        if period_windows is not None:
            summary["periodWindows"] = period_windows
        return summary


    @staticmethod
    def _period_windows(day):
        return {
            "today": {"key": day},
            "month": {"key": day[:7]},
        }

    def _registered_monitor_projects(self, root, *names):
        device_id = monitor.host_device_id(root)
        records = [
            monitor.VolumeRegistration(
                f"{index:032x}",
                device_id,
                f"codex-state-{name}",
                "container",
                f"/work/{name}",
                f"Cage: {name} (Container)",
                dict(FINGERPRINT, name=f"codex-state-{name}"),
            )
            for index, name in enumerate(names)
        ]
        monitor.save_registry(root, records)
        return records

    def _period_summary(self, record, session_id, total, day):
        return self._summary(
            record.device_id,
            {f"codex:{session_id}": self._session(
                session_id,
                total=total,
                input_tokens=total,
                output_tokens=0,
            )},
            period_windows=self._period_windows(day),
        )
