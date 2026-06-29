import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"


def write_fake_docker(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
case "$1" in
  ps) exit 0 ;;
  image) exit 0 ;;
  run) echo "fake docker run"; exit 0 ;;
  build|pull|tag|volume) exit 0 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_codex(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
echo "fake codex CODEX_HOME=$CODEX_HOME args=$*"
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class CageLauncherTests(unittest.TestCase):
    def test_launch_requires_central_config(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
            env["HOME"] = str(tmp_path / "home")
            result = subprocess.run(
                [str(CAGE), str(repo)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("central config not found", result.stderr)
        self.assertIn("cage config init", result.stderr)

    def test_legacy_files_are_not_read(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            xdg = tmp_path / "xdg"
            home = tmp_path / "home"
            bin_dir = tmp_path / "bin"
            cage_dir = xdg / "cage"
            repo = tmp_path / "repo"
            bin_dir.mkdir(parents=True)
            cage_dir.mkdir(parents=True)
            home.mkdir(parents=True)
            repo.mkdir()
            write_fake_docker(bin_dir / "docker")

            (cage_dir / "cage.conf").write_text("exit 99\n", encoding="utf-8")
            (cage_dir / "profiles").mkdir()
            (repo / ".cage.conf").write_text("exit 99\n", encoding="utf-8")
            (cage_dir / "config.toml").write_text(
                '\n'.join(
                    [
                        'version = 1',
                        'default_preset = "codex-test"',
                        '[auth.codex-test]',
                        'tool = "codex"',
                        'copy_auth = false',
                        '[presets.codex-test]',
                        'tool = "codex"',
                        'auth = "codex-test"',
                        'net = "open"',
                        '',
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(xdg)
            env["HOME"] = str(home)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env.pop("OPENAI_API_KEY", None)
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)

            result = subprocess.run(
                [str(CAGE), str(repo), "--version"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preset:    codex-test", result.stdout)
        self.assertIn("fake docker run", result.stdout)

    def test_profile_option_is_removed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
            env["HOME"] = str(tmp_path / "home")
            result = subprocess.run(
                [str(CAGE), "--profile", "work", str(repo)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown option: --profile", result.stderr)

    def test_interactive_cannot_be_combined_with_preset(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
            env["HOME"] = str(tmp_path / "home")
            result = subprocess.run(
                [str(CAGE), "--interactive", "--preset", "codex-test", str(repo)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--interactive cannot be combined with --preset", result.stderr)

    def test_interactive_requires_tty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            xdg = tmp_path / "xdg"
            home = tmp_path / "home"
            cage_dir = xdg / "cage"
            repo = tmp_path / "repo"
            cage_dir.mkdir(parents=True)
            home.mkdir(parents=True)
            repo.mkdir()
            (cage_dir / "config.toml").write_text("version = 1\n", encoding="utf-8")
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(xdg)
            env["HOME"] = str(home)

            result = subprocess.run(
                [str(CAGE), "--interactive", str(repo)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive mode requires a TTY", result.stderr)

    def test_mcp_login_dispatches_without_docker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            tmp_path = Path(tmp)
            xdg = tmp_path / "xdg"
            home = tmp_path / "home"
            bin_dir = tmp_path / "bin"
            cage_dir = xdg / "cage"
            repo = tmp_path / "repo"
            codex_home = tmp_path / "codex-home"
            bin_dir.mkdir(parents=True)
            cage_dir.mkdir(parents=True)
            home.mkdir(parents=True)
            repo.mkdir()
            write_fake_codex(bin_dir / "codex")
            (cage_dir / "config.toml").write_text(
                "\n".join(
                    [
                        "version = 1",
                        'default_preset = "codex-dash0"',
                        "[auth.codex-dash0]",
                        'tool = "codex"',
                        f'host_codex_dir = "{codex_home}"',
                        "[mcp_packs.dash0]",
                        "servers = [",
                        '  { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", auth = "oauth", oauth_resource = "https://api.eu-central-1.aws.dash0.com/mcp", oauth_client_id = "client-public-id" },',
                        "]",
                        "[presets.codex-dash0]",
                        'tool = "codex"',
                        'auth = "codex-dash0"',
                        'mcp_packs = ["dash0"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(xdg)
            env["HOME"] = str(home)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [str(CAGE), "mcp", "login", "dash0", str(repo)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Codex dir: {codex_home}", result.stdout)
        self.assertIn(f"fake codex CODEX_HOME={codex_home}", result.stdout)
        self.assertIn('mcp_oauth_credentials_store="file"', result.stdout)
        self.assertIn("mcp login dash0", result.stdout)


if __name__ == "__main__":
    unittest.main()
