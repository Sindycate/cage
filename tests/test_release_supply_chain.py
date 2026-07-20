import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
PACKAGER = ROOT / "scripts" / "build-release.py"
ACTION_REF_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^@\s]+)@([^\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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
            self.assertIn("cage-9.9.9/install.sh", names)
            self.assertIn("cage-9.9.9/netgate/defaults.json", names)
            self.assertIn("cage-9.9.9/docs/hardening/WORKFLOW.md", names)
            self.assertNotIn("cage-9.9.9/.git", names)
            modes = {member.name: member.mode for member in members}
            self.assertEqual(modes["cage-9.9.9/cage"], 0o755)
            self.assertEqual(modes["cage-9.9.9/README.md"], 0o644)
            for member in members:
                self.assertTrue(member.name == "cage-9.9.9" or member.name.startswith("cage-9.9.9/"))
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.mtime, 1700000000)


if __name__ == "__main__":
    unittest.main()
