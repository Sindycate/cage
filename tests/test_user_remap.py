import os
import pwd
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REMAP = ROOT / "cage-user-remap.py"
ENTRYPOINTS = (
    ROOT / "entrypoint.sh",
    ROOT / "entrypoint-codex.sh",
    ROOT / "entrypoint-opencode.sh",
)


class UserRemapTests(unittest.TestCase):
    def test_all_entrypoints_use_shared_fail_closed_remapper(self):
        invocation = '/usr/bin/python3 -I /usr/local/lib/cage/cage-user-remap.py "$TARGET_USER"'
        for entrypoint in ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                source = entrypoint.read_text(encoding="utf-8")
                self.assertIn(invocation, source)
                self.assertNotIn("usermod -u", source)
                self.assertNotIn("groupmod -g", source)

    def test_root_and_incomplete_host_identities_fail_before_mutation(self):
        target_user = pwd.getpwuid(os.getuid()).pw_name
        cases = (
            ({"HOST_UID": "0", "HOST_GID": "0"}, "HOST_UID=0 is not allowed"),
            ({"HOST_UID": "1000"}, "must be provided together"),
            ({"HOST_UID": "not-a-number", "HOST_GID": "1000"}, "decimal integer"),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                environment = os.environ.copy()
                environment.pop("HOST_UID", None)
                environment.pop("HOST_GID", None)
                environment.update(values)
                result = subprocess.run(
                    [sys.executable, "-I", str(REMAP), target_user],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
