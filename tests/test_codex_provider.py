"""Selected auth/profile providers remain explicit across terminal resumes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from cage_core import codex_policy, codex_runtime
from cage_core.configuration.codex import host_codex_arg_lines


ROOT = Path(__file__).resolve().parents[1]


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.config = self.home / "config.toml"

    def test_explicit_provider_precedence_and_no_invented_default(self):
        for layers, expected in (
            ([], None),
            ([{}], None),
            ([{"model_provider": "current"}], 'model_provider="current"'),
            ([{"model_provider": "base"}, {}], 'model_provider="base"'),
            (
                [{"model_provider": "base"}, {"model_provider": "profile"}],
                'model_provider="profile"',
            ),
            ([{"model_provider": "openai"}], 'model_provider="openai"'),
        ):
            with self.subTest(layers=layers):
                self.assertEqual(codex_policy.model_provider_override(layers), expected)

    def test_provider_is_one_toml_assignment(self):
        name = 'quoted"provider\nwith\\escapes'
        override = codex_policy.model_provider_override([{"model_provider": name}])
        self.assertNotIn("\n", override)
        self.assertEqual(
            tomllib.loads(override), {"model_provider": name}
        )

    def test_invalid_provider_fails_without_disclosing_value(self):
        for value in ("", " ", 12, False, ["secret"], {"secret": "value"}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    codex_policy.PolicyError, "^selected Codex model_provider must"
                ):
                    codex_policy.model_provider_override([{"model_provider": value}])

    def test_only_selected_profile_is_read_and_state_is_unchanged(self):
        self.config.write_text('model_provider = "base"\n')
        (self.home / "chosen.config.toml").write_text('model_provider = "chosen"\n')
        (self.home / "unused.config.toml").write_text("invalid!")
        sessions = self.home / "sessions"
        sessions.mkdir()
        saved = sessions / "rollout.jsonl"
        saved.write_text('{"model_provider":"previous"}\n')
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(
            codex_runtime.configured_model_provider_override(self.home, "chosen"),
            'model_provider="chosen"',
        )
        self.assertEqual(
            {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}, before
        )

    def test_absent_base_preserves_native_provider_selection(self):
        self.assertIsNone(codex_runtime.configured_model_provider_override(self.home))
        (self.home / "chosen.config.toml").write_text('model_provider = "chosen"\n')
        self.assertEqual(
            codex_runtime.configured_model_provider_override(self.home, "chosen"),
            'model_provider="chosen"',
        )

    def test_invalid_layers_fail_closed(self):
        for content in (b"secret = [", b"\xff", b'model_provider = 4'):
            with self.subTest(content=content):
                self.config.write_bytes(content)
                with self.assertRaises(codex_policy.PolicyError) as caught:
                    codex_runtime.configured_model_provider_override(self.home)
                self.assertNotIn("secret", str(caught.exception))
        self.config.write_text('model_provider = "base"\n')
        with patch.object(codex_runtime, "MAX_INVENTORY_BYTES", 4):
            with self.assertRaisesRegex(codex_policy.PolicyError, "too large"):
                codex_runtime.configured_model_provider_override(self.home)
        for profile in ("missing", "../outside", "/outside", "bad.profile"):
            with self.subTest(profile=profile):
                with self.assertRaises(codex_policy.PolicyError):
                    codex_runtime.configured_model_provider_override(self.home, profile)
        self.config.unlink()
        self.config.mkdir()
        with self.assertRaisesRegex(codex_policy.PolicyError, "cannot read"):
            codex_runtime.configured_model_provider_override(self.home)
        self.config.rmdir()
        os.mkfifo(self.config)
        with self.assertRaisesRegex(codex_policy.PolicyError, "not a regular file"):
            codex_runtime.configured_model_provider_override(self.home)

    def test_host_arguments_promote_provider_before_caller_override(self):
        self.config.write_text('model_provider = "current"\n')
        args = host_codex_arg_lines({}, self.home, self.home)
        self.assertEqual(args, ["-c", 'model_provider="current"'])
        args += ["resume", "--config", 'model_provider="caller"', "session-id"]
        overrides = [
            args[i + 1] for i, arg in enumerate(args) if arg in ("-c", "--config")
        ]
        self.assertEqual(overrides, ['model_provider="current"', 'model_provider="caller"'])

    def test_host_named_profile_wins_over_base(self):
        self.config.write_text('model_provider = "base"\n')
        (self.home / "chosen.config.toml").write_text('model_provider = "chosen"\n')
        self.assertEqual(
            host_codex_arg_lines({"profile": "chosen"}, self.home, self.home),
            ["--profile", "chosen", "-c", 'model_provider="chosen"'],
        )

    def test_container_final_exec_promotes_provider_and_preserves_argv(self):
        self.config.write_text('model_provider = "current"\n')
        entrypoint = (ROOT / "entrypoint-codex.sh").read_text()
        tail = entrypoint[entrypoint.index("CAGE_CODEX_PROFILE_ARGS=()"):]
        tail = tail.replace(
            "/usr/local/lib/cage/cage_core/codex_runtime.py",
            str(ROOT / "cage_core/codex_runtime.py"),
        )
        capture = self.home / "capture.py"
        capture.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        script = (
            'gosu() { shift; "$@"; }\n'
            'codex() { python3 "$CAPTURE" "$@"; }\n'
            + tail.replace("exec gosu", "gosu")
        )
        for argv in (
            ["resume", "session-id"],
            ["resume", "--last"],
            ["fork", "session-id"],
            ["resume", "-c", 'model_provider="caller"', "session-id"],
            ["--", "prompt containing resume"],
        ):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    ["/bin/bash", "-c", script, "cage-test", *argv],
                    env={
                        "PATH": os.environ["PATH"],
                        "CODEX_DIR": str(self.home),
                        "WORK_DIR": str(self.home),
                        "TARGET_USER": "test",
                        "CAPTURE": str(capture),
                    },
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    ["-c", 'model_provider="current"', *argv],
                )

    def test_cli_error_does_not_print_config_contents(self):
        self.config.write_text('secret = "private"\nmodel_provider = []\n')
        result = subprocess.run(
            [
                sys.executable, "-I", str(ROOT / "cage_core/codex_runtime.py"),
                "provider-override", "--codex-home", str(self.home),
            ],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("model_provider must be a non-empty string", result.stderr)
        self.assertNotIn("private", result.stderr)


if __name__ == "__main__":
    unittest.main()
