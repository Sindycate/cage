import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"


class CageCliTests(unittest.TestCase):
    def run_cage(self, *args):
        return subprocess.run(
            [str(CAGE), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_top_level_help_succeeds(self):
        result = self.run_cage("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: cage", result.stdout)

    def test_tool_scoped_help_succeeds(self):
        result = self.run_cage("codex", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: cage", result.stdout)

    def test_missing_net_mode_fails_clearly(self):
        result = self.run_cage("--net")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing mode after --net", result.stderr)

    def test_no_arguments_is_usage_error(self):
        result = self.run_cage()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage: cage", result.stdout)


if __name__ == "__main__":
    unittest.main()
