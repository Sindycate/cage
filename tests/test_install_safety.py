import hashlib
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
CAGE = ROOT / "cage"


class InstallerSafetyTests(unittest.TestCase):
    def run_uninstall(self, home: pathlib.Path, install_dir: pathlib.Path):
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            CAGE_INSTALL_DIR=str(install_dir),
            CAGE_BIN_DIR=str(home / ".local" / "bin"),
        )
        return subprocess.run(
            ["bash", str(INSTALLER), "--uninstall"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def make_release(self, root: pathlib.Path, version: str, reported_version: str):
        release_root = root / f"cage-{version}"
        release_root.mkdir()
        required = [
            "cage",
            "cage-config.py",
            "cage-netgate.sh",
            "netgate-proxy.py",
            "mcp-bridge.py",
            "mcp-relay",
            "host-cmd-bridge.py",
            "host-cmd-relay",
        ]
        for name in required:
            path = release_root / name
            if name == "cage":
                path.write_text(
                    f"#!/bin/sh\nprintf 'cage %s\\n' {reported_version!r}\n",
                    encoding="utf-8",
                )
            else:
                path.write_text("placeholder\n", encoding="utf-8")
            path.chmod(0o755)
        archive = root / f"cage-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(release_root, arcname=release_root.name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = root / f"cage-{version}.tar.gz.sha256"
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        return archive, checksum

    def run_install(
        self,
        home: pathlib.Path,
        install_dir: pathlib.Path,
        archive: pathlib.Path,
        checksum: pathlib.Path,
        version: str,
    ):
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            "out=\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o) out=$2; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "case \"$out\" in\n"
            "  *.sha256) cp \"$TEST_CHECKSUM\" \"$out\" ;;\n"
            "  *) cp \"$TEST_TARBALL\" \"$out\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "docker").chmod(0o755)
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            CAGE_INSTALL_DIR=str(install_dir),
            CAGE_BIN_DIR=str(bin_dir),
            CAGE_VERSION=version,
            TEST_TARBALL=str(archive),
            TEST_CHECKSUM=str(checksum),
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        )
        return subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def run_source_install(self, home: pathlib.Path, install_dir: pathlib.Path):
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        docker = bin_dir / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o755)
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            CAGE_INSTALL_DIR=str(install_dir),
            CAGE_BIN_DIR=str(bin_dir),
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        )
        return subprocess.run(
            ["bash", str(INSTALLER), "--from-source"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    def test_refuses_home_as_install_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            sentinel = home / "keep-me"
            sentinel.write_text("safe", encoding="utf-8")

            result = self.run_uninstall(home, home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing unsafe install directory", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_refuses_unrecognized_custom_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            install_dir = pathlib.Path(temp) / "unrelated"
            home.mkdir()
            install_dir.mkdir()
            sentinel = install_dir / "keep-me"
            sentinel.write_text("safe", encoding="utf-8")

            result = self.run_uninstall(home, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized install directory", result.stderr)
            self.assertTrue(sentinel.exists())

    def test_refuses_trailing_newline_without_deleting_trimmed_path(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            install_dir = pathlib.Path(temp) / "managed"
            home.mkdir()
            install_dir.mkdir()
            (install_dir / ".cage-install").write_text("0.22.5\n", encoding="utf-8")
            sentinel = install_dir / "keep-me"
            sentinel.write_text("safe", encoding="utf-8")

            result = self.run_uninstall(home, pathlib.Path(str(install_dir) + "\n"))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain newlines", result.stderr)
            self.assertTrue(sentinel.exists())

    def test_refuses_install_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            target = pathlib.Path(temp) / "managed"
            link = pathlib.Path(temp) / "install-link"
            home.mkdir()
            target.mkdir()
            (target / ".cage-install").write_text("0.22.5\n", encoding="utf-8")
            sentinel = target / "keep-me"
            sentinel.write_text("safe", encoding="utf-8")
            link.symlink_to(target, target_is_directory=True)

            result = self.run_uninstall(home, link)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install-directory symlink", result.stderr)
            self.assertTrue(link.is_symlink())
            self.assertTrue(sentinel.exists())

    def test_refuses_to_delete_unrelated_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            home.mkdir()
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "cage"
            launcher.write_text("unrelated", encoding="utf-8")

            result = self.run_uninstall(home, pathlib.Path(temp) / "missing-install")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrelated launcher", result.stderr)
            self.assertEqual(launcher.read_text(encoding="utf-8"), "unrelated")

    def test_uninstalls_marker_owned_custom_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            install_dir = pathlib.Path(temp) / "managed-cage"
            home.mkdir()
            install_dir.mkdir()
            (install_dir / ".cage-install").write_text("0.22.5\n", encoding="utf-8")
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "cage").symlink_to(install_dir / "cage")
            (install_dir / "cage").write_text("#!/bin/sh\n", encoding="utf-8")

            result = self.run_uninstall(home, install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(install_dir.exists())
            self.assertFalse((bin_dir / "cage").exists())

    def test_failed_fresh_install_removes_new_launcher_and_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            home.mkdir()
            archive, checksum = self.make_release(root, "9.9.9", "0.0.0")

            result = self.run_install(home, install_dir, archive, checksum, "9.9.9")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected version", result.stderr)
            self.assertFalse(install_dir.exists())
            self.assertFalse((home / ".local" / "bin" / "cage").exists())

    def test_failed_upgrade_restores_previous_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            bin_dir = home / ".local" / "bin"
            home.mkdir()
            install_dir.mkdir()
            bin_dir.mkdir(parents=True)
            (install_dir / ".cage-install").write_text("1.2.3\n", encoding="utf-8")
            old_launcher = install_dir / "cage"
            old_launcher.write_text("#!/bin/sh\nprintf 'cage 1.2.3\\n'\n", encoding="utf-8")
            old_launcher.chmod(0o755)
            (install_dir / "old-state").write_text("preserve", encoding="utf-8")
            (bin_dir / "cage").symlink_to(old_launcher)
            archive, checksum = self.make_release(root, "9.9.9", "0.0.0")

            result = self.run_install(home, install_dir, archive, checksum, "9.9.9")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((install_dir / "old-state").read_text(), "preserve")
            self.assertEqual(
                subprocess.check_output([str(bin_dir / "cage")], text=True).strip(),
                "cage 1.2.3",
            )

    def test_source_install_uses_same_staged_managed_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            home.mkdir()

            result = self.run_source_install(home, install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((install_dir / ".cage-install").is_file())
            self.assertFalse((install_dir / ".cage-install").is_symlink())
            self.assertEqual(
                subprocess.check_output([str(install_dir / "cage"), "--version"], text=True).strip(),
                subprocess.check_output([str(CAGE), "--version"], text=True).strip(),
            )


if __name__ == "__main__":
    unittest.main()
