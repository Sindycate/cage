"""Regression coverage for same-project container collision handling."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cage_core.targets import container


class Terminal(io.StringIO):
    def isatty(self):
        return True


def runtime_with_running_base():
    return SimpleNamespace(
        container_name="codex-project-01234567",
        container_name_override="",
        parallel_label="",
        output=Mock(side_effect=["base-id\n", "base-id\n", ""]),
    )


class ContainerCollisionTests(unittest.TestCase):
    def test_interactive_stdin_is_used_when_dev_tty_is_unavailable(self):
        runtime = runtime_with_running_base()
        stdin = Terminal("1\n")
        stderr = io.StringIO()

        with patch("builtins.open", side_effect=PermissionError("denied")), patch.object(
            container.sys, "stdin", stdin
        ), patch.object(container.sys, "stderr", stderr):
            container._handle_collision(runtime)

        self.assertEqual(
            runtime.container_name_override,
            "codex-project-01234567-2",
        )
        self.assertEqual(runtime.parallel_label, " (parallel #2)")
        self.assertIn("Start a parallel instance", stderr.getvalue())

    def test_noninteractive_collision_fails_without_destructive_advice(self):
        runtime = runtime_with_running_base()
        stdin = io.StringIO("1\n")

        with patch("builtins.open", side_effect=OSError("no tty")), patch.object(
            container.sys, "stdin", stdin
        ):
            with self.assertRaisesRegex(
                container.ContainerTargetError,
                "no interactive input",
            ) as raised:
                container._handle_collision(runtime)

        self.assertNotIn("docker rm", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
