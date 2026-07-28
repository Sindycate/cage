import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
PACKAGER = ROOT / "scripts" / "build-release.py"
GITLEAKS_CONFIG = ROOT / ".gitleaks.toml"
GITLEAKS_IGNORE = ROOT / ".gitleaksignore"
GITLEAKS_INSTALLER = ROOT / ".github" / "scripts" / "install-gitleaks.sh"
ACTION_REF_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^@\s]+)@([^\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PRIVATE_MACOS_HOME_RE = re.compile(
    r"/Users/(?!example(?:/|$)|me(?:/|$))[A-Za-z0-9._-]+(?:/|$)"
)
GENERATED_TARGET_RE = re.compile(
    r"\bcage-(?:claude|codex)-[A-Za-z0-9._-]+-[0-9a-f]{8,16}\b"
)


class ReleaseSupplyChainTests(unittest.TestCase):
    def test_all_remote_actions_are_pinned_to_full_commit_shas(self):
        for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            text = workflow.read_text(encoding="utf-8")
            references = ACTION_REF_RE.findall(text)
            self.assertTrue(references, f"no action references found in {workflow}")
            for action, revision in references:
                if action.startswith("./"):
                    continue
                self.assertRegex(
                    revision,
                    SHA_RE,
                    f"{workflow}: {action}@{revision} is not an immutable commit pin",
                )

    def test_release_workflow_attests_source_and_container_artifacts(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        required_fragments = (
            "id-token: write",
            "attestations: write",
            "anchore/sbom-action@",
            "syft-version: v1.44.0",
            "subject-path: cage-${{ env.VERSION }}.tar.gz",
            "sbom-path: cage-${{ env.VERSION }}.spdx.json",
            "sbom: true",
            "provenance: mode=max",
            "subject-digest: ${{ steps.build.outputs.digest }}",
            "push-to-registry: true",
            "create-storage-record: false",
            '"dist/${DIST}.spdx.json"',
        )
        for fragment in required_fragments:
            self.assertIn(fragment, text)

    def test_secret_scanning_is_pinned_narrow_and_release_blocking(self):
        installer = GITLEAKS_INSTALLER.read_text(encoding="utf-8")
        required_installer_fragments = (
            'GITLEAKS_VERSION="8.30.1"',
            "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
            "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
            "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
            "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
            "gitleaks_${GITLEAKS_VERSION}_${PLATFORM}.tar.gz",
            "shasum -a 256 --check",
            "sha256sum --check",
            'test "$("$DESTINATION/gitleaks" version)" = "$GITLEAKS_VERSION"',
        )
        for fragment in required_installer_fragments:
            self.assertIn(fragment, installer)

        for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW):
            text = workflow.read_text(encoding="utf-8")
            for fragment in (
                "secret-scan:",
                "fetch-depth: 0",
                ".github/scripts/install-gitleaks.sh",
                '--log-opts="--all"',
                "--redact=100",
            ):
                self.assertIn(fragment, text)

        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("needs: [validate, installer-macos-bash32, secret-scan]", release)
        self.assertIn("Scan source archive contents", release)
        self.assertIn('gitleaks" dir "$RUNNER_TEMP/release-scan"', release)
        self.assertLess(
            release.index("Scan source archive contents"),
            release.index("Generate source archive SBOM"),
        )

        config = tomllib.loads(GITLEAKS_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(config["extend"], {"useDefault": True})
        self.assertEqual(len(config["allowlists"]), 1)
        allowlist = config["allowlists"][0]
        self.assertEqual(allowlist["targetRules"], ["generic-api-key"])
        self.assertEqual(allowlist["condition"], "AND")
        self.assertEqual(allowlist["regexTarget"], "line")
        self.assertEqual(allowlist["paths"], [r"(?:^|/)cage$"])
        self.assertEqual(
            allowlist["regexes"],
            [
                r'^\s*current = credential_blob\(path, "host OAuth credentials", '
                r"missing_ok=observed is None\)\s*$"
            ],
        )

        ignored = [
            line
            for line in GITLEAKS_IGNORE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(
            ignored,
            [
                "1f4e9672720a5e53e6103b7604ab41f0f5e8dafa:"
                "cage-setup.sh:private-key:82"
            ],
        )

        ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for fragment in (
            "/.env",
            "/.env.*",
            "!/.env.example",
            "!/.env.sample",
            "!/.env.template",
            "!/.env.*.example",
            "/auth.json",
            "/.credentials.json",
            "/.auth/",
            "/.config/gh/",
            "/.aws/",
            "/.ssh/",
            "/*.pem",
            "/*.key",
            "/config.local.toml",
            "/secrets.toml",
            "/*.log",
            "/logs/",
            "/gitleaks-report.*",
            "/secret-scan-report.*",
        ):
            self.assertIn(fragment, ignore_text)

    def test_public_evidence_uses_neutral_placeholders(self):
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            check=True,
        )
        violations = []
        for raw_path in result.stdout.split(b"\0"):
            if not raw_path:
                continue
            relative = Path(os.fsdecode(raw_path))
            try:
                text = (ROOT / relative).read_text(encoding="utf-8")
            except (UnicodeDecodeError, IsADirectoryError):
                continue
            for pattern in (PRIVATE_MACOS_HOME_RE, GENERATED_TARGET_RE):
                match = pattern.search(text)
                if match:
                    violations.append(f"{relative}:{match.group(0)}")

        self.assertEqual(violations, [])
        progress = (ROOT / "docs" / "hardening" / "PROGRESS.md").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(
            progress,
            r"\bapproval(?:s)?_(?:policy|reviewer)\s*=",
        )

        neutral_examples = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "README.md",
                "tests/test_host_execution.py",
                "tests/test_desktop_execution.py",
            )
        )
        for placeholder in (
            "provider-preview",
            "PROVIDER_TEST_TOKEN",
            "PROVIDER_API_KEY",
        ):
            self.assertIn(placeholder, neutral_examples)

    def test_dependabot_tracks_pinned_action_updates(self):
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', text)
        self.assertIn('directory: "/"', text)
        self.assertIn('interval: "weekly"', text)

    def test_release_archive_is_reproducible_and_scoped(self):
        env = os.environ.copy()
        env["SOURCE_DATE_EPOCH"] = "1700000000"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first"
            second = root / "second"
            for output in (first, second):
                result = subprocess.run(
                    [sys.executable, str(PACKAGER), "9.9.9", str(output)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            archive_name = "cage-9.9.9.tar.gz"
            first_archive = first / archive_name
            second_archive = second / archive_name
            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())

            digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
            self.assertEqual(
                (first / f"{archive_name}.sha256").read_text(encoding="utf-8"),
                f"{digest}  {archive_name}\n",
            )

            with tarfile.open(first_archive, "r:gz") as archive:
                members = archive.getmembers()
            names = [member.name for member in members]
            self.assertIn("cage-9.9.9/cage", names)
            self.assertIn("cage-9.9.9/cage-tui.py", names)
            self.assertIn("cage-9.9.9/install.sh", names)
            self.assertIn("cage-9.9.9/netgate/defaults.json", names)
            self.assertIn("cage-9.9.9/docs/hardening/WORKFLOW.md", names)
            self.assertNotIn("cage-9.9.9/.git", names)
            modes = {member.name: member.mode for member in members}
            self.assertEqual(modes["cage-9.9.9/cage"], 0o755)
            self.assertEqual(modes["cage-9.9.9/cage-tui.py"], 0o755)
            self.assertEqual(modes["cage-9.9.9/README.md"], 0o644)
            for member in members:
                self.assertTrue(member.name == "cage-9.9.9" or member.name.startswith("cage-9.9.9/"))
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.mtime, 1700000000)


if __name__ == "__main__":
    unittest.main()
