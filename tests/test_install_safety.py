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
SYSTEM_BASH = "/bin/bash"


class InstallerSafetyTests(unittest.TestCase):
    def test_installer_rejects_python_311_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            binary = root / "bin"
            home.mkdir()
            binary.mkdir()
            python = binary / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -I ] && [ \"$2\" = -c ]; then\n"
                "  case \"$3\" in\n"
                "    *'sys.version_info >= (3, 12)'*) exit 1 ;;\n"
                "    *) printf 'unexpected Python version probe\\n' >&2; exit 97 ;;\n"
                "  esac\n"
                "fi\n"
                "exit 98\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            install_dir = root / "install"
            environment = os.environ.copy()
            environment.update(
                HOME=str(home),
                CAGE_INSTALL_DIR=str(install_dir),
                CAGE_BIN_DIR=str(root / "launcher"),
                PATH=f"{binary}{os.pathsep}{environment['PATH']}",
            )
            result = subprocess.run(
                [SYSTEM_BASH, str(INSTALLER), "--from-source"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3 3.12+ is required", result.stderr)
        self.assertNotIn("unexpected Python version probe", result.stderr)
        self.assertFalse(install_dir.exists())

    def run_uninstall(self, home: pathlib.Path, install_dir: pathlib.Path):
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            CAGE_INSTALL_DIR=str(install_dir),
            CAGE_BIN_DIR=str(home / ".local" / "bin"),
        )
        return subprocess.run(
            [SYSTEM_BASH, str(INSTALLER), "--uninstall"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def make_release(
        self,
        root: pathlib.Path,
        version: str,
        reported_version: str,
        *,
        unsafe_core_symlink: bool = False,
    ):
        release_root = root / f"cage-{version}"
        release_root.mkdir()
        required = [
            "cage",
            "cage-main.py",
            "cage-config.py",
            "cage-desktop.py",
            "cage-tui.py",
            "cage-netgate.sh",
            "netgate-proxy.py",
            "mcp-bridge.py",
            "mcp-relay",
            "host-cmd-bridge.py",
            "host-cmd-relay",
            "codex-remote.py",
            "entrypoint.sh",
            "entrypoint-codex.sh",
            "entrypoint-opencode.sh",
            "cage-user-remap.py",
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
        core_files = [
            "__init__.py",
            "bridge.py",
            "cli.py",
            "codex_policy.py",
            "codex_runtime.py",
            "config.py",
            "lifecycle.py",
            "models.py",
            "opencode.py",
            "opencode_policy.py",
            "planning.py",
            "storage.py",
            "state/__init__.py",
            "state/oauth.py",
            "state/opencode.py",
            "state/sessions.py",
            "targets/__init__.py",
            "targets/container.py",
            "targets/desktop.py",
            "targets/host.py",
        ]
        for name in core_files:
            path = release_root / "cage_core" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if unsafe_core_symlink and name == "models.py":
                path.symlink_to("__init__.py")
            else:
                path.write_text("placeholder = True\n", encoding="utf-8")
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
        discover_version: bool = False,
    ):
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            "out=\n"
            "url=\n"
            "auth_header=0\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    '') exit 40 ;;\n"
            "    -o) out=$2; shift 2 ;;\n"
            "    -H) auth_header=1; shift 2 ;;\n"
            "    http*) url=$1; shift ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "if [ -z \"$out\" ]; then\n"
            "  [ \"$auth_header\" -eq 0 ] || exit 41\n"
            "  [ \"$url\" = 'https://api.github.com/repos/Sindycate/cage/releases/latest' ] || exit 42\n"
            "  printf '{\"tag_name\":\"v%s\"}\\n' \"$TEST_VERSION\"\n"
            "  exit 0\n"
            "fi\n"
            "case \"$out\" in\n"
            "  *.sha256) cp \"$TEST_CHECKSUM\" \"$out\" ;;\n"
            "  *) cp \"$TEST_TARBALL\" \"$out\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "docker").chmod(0o755)
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        env = os.environ.copy()
        env.update(
            HOME=str(home),
            CAGE_INSTALL_DIR=str(install_dir),
            CAGE_BIN_DIR=str(bin_dir),
            TEST_TARBALL=str(archive),
            TEST_CHECKSUM=str(checksum),
            TEST_VERSION=version,
            PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        )
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        if discover_version:
            env.pop("CAGE_VERSION", None)
        else:
            env["CAGE_VERSION"] = version
        return subprocess.run(
            [SYSTEM_BASH, str(INSTALLER)],
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
            [SYSTEM_BASH, str(INSTALLER), "--from-source"],
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
            self.assertTrue((install_dir / "cage-tui.py").is_file())
            self.assertTrue((install_dir / "cage-main.py").is_file())
            self.assertTrue((install_dir / "cage_core" / "models.py").is_file())
            self.assertFalse((install_dir / "cage_core").is_symlink())
            self.assertTrue((install_dir / "Dockerfile.base").is_file())
            self.assertTrue((install_dir / "cage-user-remap.py").is_file())
            self.assertTrue(os.access(install_dir / "cage-tui.py", os.X_OK))
            self.assertTrue(os.access(install_dir / "cage-desktop.py", os.X_OK))
            self.assertTrue(os.access(install_dir / "codex-remote.py", os.X_OK))
            self.assertTrue(os.access(install_dir / "entrypoint-opencode.sh", os.X_OK))
            self.assertEqual(
                subprocess.check_output(
                    [str(install_dir / "cage"), "--version"], text=True
                ).strip(),
                subprocess.check_output([str(CAGE), "--version"], text=True).strip(),
            )

    def test_release_install_rejects_core_package_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            home.mkdir()
            archive, checksum = self.make_release(
                root,
                "9.9.9",
                "9.9.9",
                unsafe_core_symlink=True,
            )

            result = self.run_install(
                home, install_dir, archive, checksum, "9.9.9"
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("package symlink", result.stderr)
            self.assertFalse(install_dir.exists())

    def test_generated_release_install_matches_source_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            output = root / "dist"
            home.mkdir()
            version = subprocess.check_output(
                [str(CAGE), "--version"], text=True
            ).strip().split()[-1]
            built = subprocess.run(
                [
                    os.environ.get("PYTHON", "python3"),
                    str(ROOT / "scripts" / "build-release.py"),
                    version,
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)

            archive = output / f"cage-{version}.tar.gz"
            checksum = output / f"cage-{version}.tar.gz.sha256"
            result = self.run_install(
                home, install_dir, archive, checksum, version
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            installed = home / ".local" / "bin" / "cage"
            self.assertEqual(
                subprocess.check_output(
                    [str(installed), "--version"], text=True
                ).strip(),
                subprocess.check_output(
                    [str(CAGE), "--version"], text=True
                ).strip(),
            )
            self.assertIn(
                "Usage: cage",
                subprocess.check_output(
                    [str(installed), "--help"], text=True
                ),
            )
            self.assertTrue(
                (install_dir / "cage_core" / "targets" / "desktop.py").is_file()
            )

    def test_unauthenticated_latest_release_lookup_installs_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            install_dir = root / "install"
            home.mkdir()
            archive, checksum = self.make_release(root, "9.9.9", "9.9.9")

            result = self.run_install(
                home,
                install_dir,
                archive,
                checksum,
                "9.9.9",
                discover_version=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                subprocess.check_output(
                    [str(home / ".local" / "bin" / "cage")], text=True
                ).strip(),
                "cage 9.9.9",
            )


if __name__ == "__main__":
    unittest.main()
