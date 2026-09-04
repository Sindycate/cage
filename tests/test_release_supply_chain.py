import copy
import hashlib
import json
import os
import shutil
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
AGENT_INSTRUCTIONS = ROOT / "AGENTS.md"
HARDENING_WORKFLOW = ROOT / "docs" / "hardening" / "WORKFLOW.md"
ACTION_REF_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^@\s]+)@([^\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PRIVATE_MACOS_HOME_RE = re.compile(
    r"/Users/(?!example(?:/|$)|me(?:/|$))[A-Za-z0-9._-]+(?:/|$)"
)
GENERATED_TARGET_RE = re.compile(
    r"\bcage-(?:claude|codex|opencode)-[A-Za-z0-9._-]+-[0-9a-f]{8,16}\b"
)


class ReleaseSupplyChainTests(unittest.TestCase):
    def test_release_handoff_requires_public_install_before_owner_testing(self):
        agent_instructions = re.sub(
            r"\s+", " ", AGENT_INSTRUCTIONS.read_text(encoding="utf-8")
        )
        workflow = re.sub(
            r"\s+", " ", HARDENING_WORKFLOW.read_text(encoding="utf-8")
        )

        for fragment in (
            "only a *prepared release candidate*",
            "the exact commit is on remote `main`",
            "the canonical publisher reaches `public_verified`",
            "fresh unauthenticated",
            "never ask them to check out or locally test repository `main`",
            "Do not pause for a redundant push/tag/",
        ):
            self.assertIn(fragment, agent_instructions)

        for fragment in (
            "The product owner does not test local `main`",
            "Product-owner acceptance starts from a fresh `curl` installation",
            "A local checkpoint is described as `prepared` or `release candidate`",
            "This is standing publication authorization",
            "any release-bound completion claim is backed by the remote-main",
        ):
            self.assertIn(fragment, workflow)

    def test_publisher_is_the_single_release_orchestrator(self):
        agent_instructions = re.sub(
            r"\s+", " ", AGENT_INSTRUCTIONS.read_text(encoding="utf-8")
        )
        workflow = re.sub(
            r"\s+", " ", HARDENING_WORKFLOW.read_text(encoding="utf-8")
        )

        for fragment in (
            "as the single release orchestrator",
            "`--dry-run` is diagnostic, not a mandatory duplicate gate",
            "routine `gh run view`/`watch` polling",
            "final schema-v2 JSON result",
            "resume the same journaled command",
            "minimizes wall time, tool calls, and token use",
        ):
            self.assertIn(fragment, agent_instructions)

        for fragment in (
            "is the single controller for an ordinary release",
            "`--dry-run` is optional",
            "without extra GitHub or registry queries",
            "resume its private journal",
            "do not reconstruct or manually replay its phases",
        ):
            self.assertIn(fragment, workflow)

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

    def test_release_workflow_attests_source_archive(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        for fragment in (
            "id-token: write",
            "attestations: write",
            "anchore/sbom-action@",
            "syft-version: v1.44.0",
            "subject-path: cage-${{ env.VERSION }}.tar.gz",
            "sbom-path: cage-${{ env.VERSION }}.spdx.json",
            '"dist/${DIST}.spdx.json"',
        ):
            self.assertIn(fragment, text)

    def test_ci_candidate_runs_only_after_all_main_gates(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("candidate:", text)
        self.assertIn(
            "needs: [secret-scan, installer-macos-bash32, test-python, test-docker, test-static]",
            text,
        )
        self.assertIn(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main'",
            text,
        )
        # Serialized per-SHA, never cancelled mid-flight.
        self.assertIn("group: candidate-${{ github.sha }}", text)
        self.assertIn("cancel-in-progress: false", text)
        # Candidate publication follows every gate in file order.
        candidate_index = text.index("  candidate-base-plan:")
        for gate in ("test-python", "test-docker", "test-static"):
            self.assertLess(text.index(f"  {gate}:"), candidate_index)
        self.assertLess(text.index("  candidate-base-plan:"), text.index("  candidate:"))
        self.assertLess(text.index("  candidate-base:"), text.index("  candidate:"))
        self.assertLess(text.index("  candidate-leaf-assemble:"), text.index("  candidate:"))

    def test_ci_uses_one_python_312_lane_and_preserves_all_smokes(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("matrix.python-version", text)
        self.assertNotIn("python-version: ['3.11', '3.12']", text)
        self.assertEqual(text.count("python-version: '3.12'"), 4)
        for fragment in (
            "python -m pytest -q",
            "CAGE_RUN_DOCKER_SMOKE: '1'",
            "Build shared base, Desktop Codex, OpenCode, and Token Monitor smoke images",
            "CAGE_RUN_OPENCODE_DOCKER_CONTRACT=1",
            "Run Desktop SSH integration smoke test",
            "CAGE_DESKTOP_SMOKE_IMAGE: codex:desktop-smoke",
            "python -m compileall -q",
            "Validate Compose configuration",
        ):
            self.assertIn(fragment, text)

    def test_ci_candidate_tags_use_full_sha_and_exact_base_digest(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('echo "SHA=${GITHUB_SHA}" >> "$GITHUB_ENV"', text)
        candidate = text[text.index("  candidate-base-plan:") :]
        self.assertIn("candidate-${SHA}", candidate)
        self.assertIn("--tag \"$REF\"", candidate)
        # Leaves build from the exact assembled base digest, never a mutable tag.
        leaf_base = (
            "CAGE_BASE=ghcr.io/sindycate/cage/base@${{ needs.candidate-base.outputs.digest }}"
        )
        self.assertIn(leaf_base, text)
        # Candidate architecture images retain SBOM + max provenance and per-image
        # attestations; their only workflow handoff is a tiny digest artifact.
        self.assertIn("sbom: true", text)
        self.assertIn("provenance: mode=max", text)
        self.assertIn("subject-name: ghcr.io/sindycate/cage/base", text)
        self.assertIn("subject-name: ghcr.io/sindycate/cage/${{ matrix.image }}", text)
        self.assertIn("push-by-digest=true", text)
        self.assertIn("name-canonical=true", text)
        self.assertIn("actions/download-artifact@", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("merge-multiple: true", text)
        for job in ("candidate-base-arch:", "candidate-leaf-arch:"):
            start = text.index(f"  {job}")
            end = text.find("\n  candidate-", start + 1)
            block = text[start:] if end == -1 else text[start:end]
            retention = re.search(r"retention-days:\s*(\d+)", block)
            self.assertIsNotNone(retention, f"{job} has no artifact retention")
            self.assertGreaterEqual(int(retention.group(1)), 30, job)
            self.assertNotIn("retention-days: 1", block)
        for step_name in (
            "Assemble or verify final base candidate",
            "Assemble or verify final leaf candidate",
        ):
            assembly = _extract_run_block(CI_WORKFLOW, step_name)
            self.assertLess(
                assembly.index("verify_architecture_indexes"),
                assembly.index("docker buildx imagetools create"),
                step_name,
            )
        self.assertIn(
            'printf \'%s\\n\' "$DIGEST" > "$RUNNER_TEMP/cage-digests/${{ matrix.arch }}"',
            text,
        )
        self.assertNotIn("docker save", candidate)
        # Verify exact native matrix structure for all four leaf images.
        self.assertIn("runner: ubuntu-24.04", text)
        self.assertIn("runner: ubuntu-24.04-arm", text)
        self.assertIn("platform: linux/amd64", text)
        self.assertIn("platform: linux/arm64", text)
        self.assertRegex(
            text,
            r"arch: amd64\s+platform: linux/amd64\s+runner: ubuntu-24\.04",
        )
        self.assertRegex(
            text,
            r"arch: arm64\s+platform: linux/arm64\s+runner: ubuntu-24\.04-arm",
        )
        self.assertNotIn("setup-qemu-action@", candidate)
        self.assertNotIn("setup-qemu-action@", text)
        self.assertNotIn("platforms: linux/amd64,linux/arm64", candidate)
        self.assertNotIn("tags:", candidate)
        for image, dockerfile in (
            ("claude-code", "Dockerfile"),
            ("codex", "Dockerfile.codex"),
            ("opencode", "Dockerfile.opencode"),
            ("token-monitor", "Dockerfile.monitor"),
        ):
            self.assertIn(f"image: {image}", text)
            self.assertIn(f'"{image}": "{dockerfile}"', text)
        # Manifest artifact is SHA-named, schema-typed, and uploaded.
        self.assertIn("name: release-candidate-${{ needs.candidate-base.outputs.sha }}", text)
        self.assertIn('"schema": "cage.release-candidate"', text)
        self.assertIn('"schema_version": 3', text)
        self.assertIn("resolve_candidate()", text)
        self.assertIn("docker buildx imagetools create", text)
        self.assertIn("if ! gh attestation verify", text)
        self.assertIn(
            'if [ -n "$expected" ] && [ "$digest" != "$expected" ]; then',
            text,
        )
        self.assertIn(
            'BASE_DIGEST="$(resolve_candidate "base" "$BASE_DIGEST")"',
            text,
        )

    def test_ci_candidates_are_write_once_verify_reuse_or_fail_closed(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        # A resolve step verifies an existing candidate before reuse and fails
        # closed; it never merely prints a message and then overwrites the tag.
        self.assertIn(
            "Resolve final base candidate (verify and reuse) or authorize a build", text
        )
        self.assertIn(
            "Resolve final leaf candidates (verify and reuse) or authorize builds", text
        )
        self.assertIn("Assemble or verify final base candidate", text)
        self.assertIn("Assemble or verify final leaf candidate", text)
        # Existing candidates are verified by attestation (OCI reference form),
        # signer workflow, source SHA and ref before reuse.
        self.assertIn(
            'gh attestation verify "oci://ghcr.io/sindycate/cage/base@${digest}"',
            text,
        )
        self.assertIn(
            'gh attestation verify "oci://${IMAGE}@${digest}"',
            text,
        )
        self.assertIn(
            '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/ci.yml"', text
        )
        self.assertIn('--source-digest "${SHA}"', text)
        self.assertIn("--source-ref refs/heads/main", text)
        # An existing candidate missing amd64/arm64 fails closed.
        self.assertIn(
            "must contain exactly linux/amd64 and linux/arm64",
            text,
        )
        self.assertIn(
            "missing or unsafe architecture digest artifact", text
        )
        # Build and assembly writes are authorized only by an authoritative 404;
        # existing final candidates skip both and are reverified instead.
        self.assertIn('echo "base_exists=true"', text)
        self.assertIn('echo "base_exists=false"', text)
        self.assertIn('echo "${key}_build=false"', text)
        self.assertIn('echo "${key}_build=true"', text)
        self.assertIn("candidate disappeared after a verified existing result", text)
        self.assertIn("refusing to recreate it", text)
        self.assertIn("needs_attestation=false", text)
        self.assertIn("needs_attestation=true", text)
        self.assertIn("candidate_attestation_state", text)
        self.assertEqual(text.count(". .github/scripts/candidate-index-verify.sh"), 2)
        self.assertIn("PLAN_RECOVERY_REQUESTED", text)
        self.assertIn("verify_index_matches_arch_digests", text)
        self.assertIn("Reattesting unchanged existing", text)
        # Recovery is allowed to emit the existing digest for actions/attest;
        # it must never invoke imagetools create on the 200 path.
        self.assertIn("existing unattested", text)
        # Effective digests are validated before the manifest is written.
        self.assertIn("Resolve candidate digests from GHCR and write candidate manifest", text)
        self.assertIn(
            'if [[ ! "$BASE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then', text
        )
        # Existence is decided by an authoritative registry status code (via the
        # shared ghcr-status helper), never by free-form error text. Only a 404
        # authorizes creation; 200 reuses; anything else fails closed.
        self.assertIn(". .github/scripts/ghcr-status.sh", text)
        self.assertIn('status="$(ghcr_status "$repo_path" "candidate-${SHA}")"', text)
        self.assertIn("ambiguous registry status", text)
        # Matrix must NOT use fail-fast so independent sibling builds finish attestations
        self.assertIn("fail-fast: false", text)
        self.assertNotIn("setup-qemu-action@", text[text.index("  candidate-base-plan:") :])
        # The unsafe bare-substring not-found matching is gone.
        self.assertNotIn('*"not found"*|*"manifest unknown"*|*"404"*)', text)
        self.assertNotIn("inspect_ok=true", text)

    def test_release_version_tag_checks_use_authoritative_status(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        # Both the gate and the promotion step source the shared helper and decide
        # version-tag existence by the registry status code, not a failed inspect.
        self.assertGreaterEqual(text.count(". .github/scripts/ghcr-status.sh"), 2)
        self.assertIn('ghcr_status "sindycate/cage/${image}" "${VERSION}"', text)  # gate
        self.assertIn('versioned_status="$(ghcr_status "$repo_path" "${VERSION}")"', text)  # promote
        self.assertIn("ambiguous registry status", text)
        # Promotion creates the immutable tag only on an authoritative 404.
        self.assertIn("404)", text)
        self.assertIn(
            'docker buildx imagetools create -t "${repo}:${VERSION}" "${repo}@${DIGEST}"',
            text,
        )
        # The ambiguous "any failed inspect means absent" pattern is gone: the
        # version-tag checks no longer swallow inspect errors to infer absence.
        self.assertNotIn('2>/dev/null)"; then', text)

    def test_ghcr_status_helper_is_present_and_valid_bash(self):
        helper = ROOT / ".github" / "scripts" / "ghcr-status.sh"
        self.assertTrue(helper.is_file(), "shared ghcr-status.sh helper missing")
        body = helper.read_text(encoding="utf-8")
        self.assertIn("ghcr_status()", body)
        self.assertIn("ghcr.io/token", body)
        self.assertIn("'%{http_code}'", body)
        result = subprocess.run(
            ["bash", "-n", str(helper)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_attestation_status_helper_is_present_and_valid_bash(self):
        helper = ROOT / ".github" / "scripts" / "candidate-attestation-status.sh"
        self.assertTrue(helper.is_file(), "candidate attestation status helper missing")
        body = helper.read_text(encoding="utf-8")
        self.assertIn("candidate_attestation_state()", body)
        self.assertIn("/attestations/", body)
        self.assertIn("repository access could not be confirmed", body)
        result = subprocess.run(
            ["bash", "-n", str(helper)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_index_verifier_is_present_and_valid_bash(self):
        helper = ROOT / ".github" / "scripts" / "candidate-index-verify.sh"
        self.assertTrue(helper.is_file(), "candidate index verifier missing")
        body = helper.read_text(encoding="utf-8")
        for fragment in (
            "candidate_inspect_architecture_descriptor_set",
            "unknown/unknown",
            "child descriptor set does not equal the union",
            "imagetools create",
        ):
            self.assertIn(fragment, body)
        result = subprocess.run(
            ["bash", "-n", str(helper)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_creation_is_idempotent(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        # A rerun verifies an existing release's metadata AND asset contents and
        # resumes only on an exact match; any difference fails closed instead of
        # failing on "release already exists" or silently overwriting.
        self.assertIn(
            'if gh release view "v${VERSION}" --repo "$GITHUB_REPOSITORY" --json tagName >/dev/null 2>&1; then',
            text,
        )
        # Release metadata is validated (not draft/prerelease, tag matches).
        self.assertIn("--json isDraft --jq '.isDraft'", text)
        self.assertIn("--json isPrerelease --jq '.isPrerelease'", text)
        self.assertIn("unexpected metadata", text)
        # Asset contents (not just names) are downloaded and digested; empty,
        # truncated, or different files under the right names are rejected.
        self.assertIn('gh release download "v${VERSION}"', text)
        self.assertIn('sha256sum "${verify_dir}/${asset}"', text)
        self.assertIn('sha256sum "dist/${asset}"', text)
        self.assertIn("is missing or empty; refusing to treat as recovered", text)
        self.assertIn("sha256/size mismatch", text)
        self.assertIn(
            "Release v${VERSION} already exists with identical metadata and assets; resuming.",
            text,
        )
        self.assertIn(
            "exists but its asset set differs; refusing to overwrite.",
            text,
        )
        # Release creation still happens last and preserves generated notes.
        self.assertIn("--generate-notes", text)

    def test_release_gate_verifies_exact_ci_and_candidate_attestations(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        for fragment in (
            "gate:",
            "--workflow ci.yml --event push",
            "--branch main",
            "select(.headSha==",
            "select(.conclusion==",
            'gh run download "$RUN_ID"',
            '--name "release-candidate-${GITHUB_SHA}"',
            "gh attestation verify",
            # gh attestation verify requires an oci:// image URI; a bare
            # ghcr.io/... argument is interpreted as a local file path.
            'gh attestation verify "oci://ghcr.io/sindycate/cage/${image}@${expected}"',
            '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/ci.yml"',
            '--source-digest "$GITHUB_SHA"',
            "--source-ref refs/heads/main",
            'git ls-remote --tags origin',
            '"refs/tags/${TAG}^{}"',
            'if [ -z "$TAG_OBJECT" ] || [ -z "$TAG_COMMIT" ]; then',
        ):
            self.assertIn(fragment, text)
        self.assertIn('manifest.get("schema_version") != 3', text)
        self.assertIn('manifest.get("ci_run_id") != int(run_id)', text)
        self.assertIn('manifest.get("platforms") != ["linux/amd64", "linux/arm64"]', text)
        self.assertIn(
            'set(images) != set(names)', text
        )
        # The gate refuses a conflicting existing version tag.
        self.assertIn(
            "version tag ${versioned} exists with digest ${existing} != ${expected}",
            text,
        )

    def test_release_workflow_promotes_candidates_not_rebuilds(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        # Promotion retags exact digests; no rebuild, no QEMU.
        self.assertIn("docker buildx imagetools create", text)
        self.assertNotIn("docker/build-push-action@", text)
        self.assertNotIn("setup-qemu-action@", text)
        # Re-attest promoted digests from the release workflow.
        self.assertIn("subject-digest: ${{ matrix.digest }}", text)
        self.assertIn("push-to-registry: true", text)
        self.assertIn("create-storage-record: false", text)

    def test_release_workflow_orders_version_before_latest_and_release_last(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("promote:", text)
        self.assertIn("latest:", text)
        self.assertIn("release:", text)
        # latest depends on all promote legs; the public consumer gate follows
        # latest; release depends on that gate and stays the final job.
        self.assertIn("needs: [gate, promote]", text)
        self.assertIn("needs: [gate, promote, latest]", text)
        self.assertIn("needs: [package, promote, latest, public-images]", text)
        # Version-tag creation precedes latest, then anonymous verification, then
        # GitHub Release creation.
        self.assertLess(
            text.index("Create immutable version tag from exact candidate digest"),
            text.index("Move latest to the promoted digest"),
        )
        self.assertLess(
            text.index("Move latest to the promoted digest"),
            text.index("Verify anonymous version and latest manifests and pulls"),
        )
        self.assertLess(
            text.index("Verify anonymous version and latest manifests and pulls"),
            text.index("Create GitHub Release"),
        )

    def test_release_workflow_has_post_publication_anonymous_image_gate(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("public-images:", text)
        self.assertIn("needs: [gate, promote, latest]", text)
        self.assertLess(text.index("  public-images:"), text.index("  release:"))
        for image in ("base", "claude-code", "codex", "opencode", "token-monitor"):
            self.assertIn(f"- image: {image}", text)
        for fragment in (
            'credential_dir="$(mktemp -d "$RUNNER_TEMP/cage-anonymous-docker.XXXXXX")"',
            'find "$credential_dir" -mindepth 1 -print -quit',
            '-u GH_TOKEN -u GITHUB_TOKEN',
            '-u DOCKER_AUTH_CONFIG -u REGISTRY_AUTH_FILE',
            'DOCKER_CONFIG="$credential_dir"',
            'for tag in "$VERSION" latest; do',
            "docker buildx imagetools inspect",
            "{{.Manifest.Digest}}",
            "linux/amd64",
            "linux/arm64",
            'docker pull --platform linux/amd64 "$ref"',
        ):
            self.assertIn(fragment, text)
        public_gate = text[text.index("  public-images:") :]
        self.assertNotIn("docker/login-action@", public_gate)

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

        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        for fragment in (
            "secret-scan:",
            "fetch-depth: 0",
            ".github/scripts/install-gitleaks.sh",
            '--log-opts="--all"',
            "--redact=100",
        ):
            self.assertIn(fragment, ci)

        # The tag workflow no longer duplicates the full-history scan; its exact
        # commit gate verifies the successful CI run that performed it. The
        # archive-content scan stays because it validates the generated deliverable.
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("gate:", release)
        self.assertIn("--workflow ci.yml --event push", release)
        self.assertIn("needs: [gate]", release)
        self.assertIn("Scan source archive contents", release)
        self.assertIn(".github/scripts/install-gitleaks.sh", release)
        self.assertIn("--redact=100", release)
        self.assertIn('gitleaks" dir "$RUNNER_TEMP/release-scan"', release)
        self.assertNotIn('--log-opts="--all"', release)
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
            self.assertIn("cage-9.9.9/cage-main.py", names)
            self.assertIn("cage-9.9.9/cage_core/models.py", names)
            self.assertIn("cage-9.9.9/cage_core/storage.py", names)
            self.assertIn(
                "cage-9.9.9/cage_core/targets/desktop.py", names
            )
            self.assertIn("cage-9.9.9/cage-tui.py", names)
            self.assertIn("cage-9.9.9/install.sh", names)
            self.assertIn("cage-9.9.9/Dockerfile.base", names)
            self.assertIn("cage-9.9.9/cage-user-remap.py", names)
            self.assertIn("cage-9.9.9/netgate/defaults.json", names)
            self.assertIn("cage-9.9.9/docs/hardening/WORKFLOW.md", names)
            self.assertNotIn("cage-9.9.9/.git", names)
            # Maintainer-only release automation is excluded from the payload.
            self.assertNotIn("cage-9.9.9/scripts/publish_release.py", names)
            self.assertFalse(
                any(name.startswith("cage-9.9.9/scripts") for name in names),
                "release archive must not ship maintainer-only scripts/",
            )
            modes = {member.name: member.mode for member in members}
            self.assertEqual(modes["cage-9.9.9/cage"], 0o755)
            self.assertEqual(modes["cage-9.9.9/cage-tui.py"], 0o755)
            self.assertEqual(modes["cage-9.9.9/README.md"], 0o644)
            for member in members:
                self.assertTrue(member.name == "cage-9.9.9" or member.name.startswith("cage-9.9.9/"))
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.mtime, 1700000000)

class SharedBaseImageTests(unittest.TestCase):
    """Validate the shared base image separation (ADR-001)."""

    def test_base_dockerfile_contains_no_agent_binaries(self):
        base = (ROOT / "Dockerfile.base").read_text(encoding="utf-8")
        # Base must not install any agent binaries or agent-specific packages.
        # Comments may mention agent names for context; check instructions only.
        instructions = [
            line for line in base.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        instruction_text = "\n".join(instructions)
        self.assertNotIn("openssh-server", instruction_text)
        self.assertNotIn("entrypoint", instruction_text)
        self.assertNotIn("npm install", instruction_text)
        self.assertNotIn("install.sh", instruction_text)
        self.assertNotIn("useradd", instruction_text)
        self.assertNotIn("codex-remote", instruction_text)

    def test_base_dockerfile_contains_shared_infrastructure(self):
        base = (ROOT / "Dockerfile.base").read_text(encoding="utf-8")
        self.assertIn("ubuntu:24.04", base)
        self.assertIn("bubblewrap", base)
        self.assertIn("gosu", base)
        self.assertIn("nodejs", base)
        self.assertIn("gh", base)
        self.assertIn("mcp-relay", base)
        self.assertIn("host-cmd-relay", base)
        self.assertIn("cage-user-remap.py", base)
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("!cage-user-remap.py", dockerignore)

    def test_leaf_dockerfiles_reference_base(self):
        for dockerfile_name in ("Dockerfile", "Dockerfile.codex", "Dockerfile.opencode", "Dockerfile.monitor"):
            content = (ROOT / dockerfile_name).read_text(encoding="utf-8")
            self.assertIn("CAGE_BASE", content, f"{dockerfile_name} must reference CAGE_BASE build arg")
            self.assertIn("FROM ${CAGE_BASE}", content, f"{dockerfile_name} must build FROM the base image")

    def test_opencode_image_pins_runtime_isolation_and_callback_contracts(self):
        content = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
        for contract in (
            "--pure",
            "OPENCODE_DISABLE_PROJECT_CONFIG",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS",
            "127.0.0.1:19876",
            "[[:alpha:]_$][[:alnum:]_$]*=1455([,;]|$)",
            "/auth/callback",
            "/.well-known/opencode",
        ):
            self.assertIn(contract, content)

    def test_leaf_permission_normalization_shares_the_install_layer(self):
        cases = (
            ("Dockerfile", "https://claude.ai/install.sh", "/home/claude"),
            ("Dockerfile.codex", "npm install -g @openai/codex", "/home/codex"),
            (
                "Dockerfile.opencode",
                "npm install -g --allow-scripts=opencode-ai",
                "/home/opencode",
            ),
        )
        for dockerfile_name, installer, home in cases:
            content = (ROOT / dockerfile_name).read_text(encoding="utf-8")
            instructions = []
            current = []
            for line in content.splitlines():
                current.append(line)
                if not line.rstrip().endswith("\\"):
                    instructions.append("\n".join(current))
                    current = []
            install_instruction = next(
                instruction
                for instruction in instructions
                if instruction.startswith("RUN ") and installer in instruction
            )
            self.assertIn(f"chmod -R a+rwX {home}", install_instruction)
            self.assertNotIn(f"\nRUN chmod -R a+rwX {home}", content)
            self.assertLess(
                content.index(f"chmod -R a+rwX {home}"),
                content.index("COPY entrypoint"),
                f"{dockerfile_name} must keep the root-owned entrypoint out "
                "of the writable tool tree",
            )

    def test_all_managed_images_have_role_and_version_labels(self):
        roles = {
            "Dockerfile.base": "base",
            "Dockerfile": "claude",
            "Dockerfile.codex": "codex",
            "Dockerfile.opencode": "opencode",
            "Dockerfile.monitor": "monitor",
        }
        for dockerfile_name, role in roles.items():
            content = (ROOT / dockerfile_name).read_text(encoding="utf-8")
            self.assertIn('io.cage.managed="true"', content)
            self.assertIn(f'io.cage.role="{role}"', content)
            self.assertIn('io.cage.version="${CAGE_VERSION}"', content)
            self.assertIn('org.opencontainers.image.version="${CAGE_VERSION}"', content)
            instructions = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            self.assertTrue(
                any(line.startswith("LABEL org.opencontainers.image.version") for line in instructions[-5:]),
                f"{dockerfile_name} must end with the managed identity label",
            )

    def test_leaf_dockerfiles_do_not_duplicate_base_packages(self):
        """Leaf images must not reinstall packages already in the base."""
        base_packages = {"bubblewrap", "gosu", "ripgrep", "python3-venv"}
        for dockerfile_name in ("Dockerfile", "Dockerfile.codex", "Dockerfile.opencode", "Dockerfile.monitor"):
            content = (ROOT / dockerfile_name).read_text(encoding="utf-8")
            for pkg in base_packages:
                self.assertNotIn(pkg, content,
                    f"{dockerfile_name} should not reinstall base package '{pkg}'")

    def test_codex_leaf_retains_openssh_server(self):
        codex = (ROOT / "Dockerfile.codex").read_text(encoding="utf-8")
        self.assertIn("openssh-server", codex)
        self.assertIn(
            "COPY cage_core /usr/local/lib/cage/cage_core", codex
        )
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("!cage_core/**", dockerignore)
        self.assertIn("cage_core/**/*.pyc", dockerignore)
        self.assertIn("!Dockerfile.monitor", dockerignore)
        self.assertIn("!token-monitor-collector.js", dockerignore)

    def test_claude_leaf_does_not_have_openssh(self):
        claude = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("openssh-server", claude)

    def test_docker_compose_builds_base_first(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("Dockerfile.base", compose)
        self.assertIn("depends_on: [base]", compose)

    def test_cage_script_ensures_base_before_local_build(self):
        launcher = (ROOT / "cage").read_text(encoding="utf-8")
        container_target = (
            ROOT / "cage_core" / "targets" / "container.py"
        ).read_text(encoding="utf-8")
        self.assertLessEqual(len(launcher.splitlines()), 100)
        self.assertNotIn("docker ", launcher)
        self.assertIn("def _ensure_base_image", container_target)
        self.assertIn("Dockerfile.base", container_target)
        self.assertIn('base_image = f"cage-base:', container_target)
        self.assertIn('f"CAGE_VERSION={runtime.plan.cage_version}"', container_target)

    def test_candidate_base_built_in_ci_and_promoted_in_release(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("Dockerfile.base", ci)
        self.assertIn('IMAGE="ghcr.io/sindycate/cage/base"', ci)
        self.assertIn('REF="${IMAGE}:candidate-${SHA}"', ci)
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        # Release promotes the exact base digest; it never rebuilds the base image.
        self.assertNotIn("Dockerfile.base", release)
        self.assertIn("docker buildx imagetools create", release)
        self.assertIn("base_digest: ${{ steps.candidates.outputs.base_digest }}", release)

    def test_ci_builds_base_before_desktop_codex_smoke_image(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        base_build = (
            "docker build --label io.cage.lifecycle=ephemeral "
            "--build-arg CAGE_VERSION=ci -t cage-base:ci -f Dockerfile.base ."
        )
        leaf_build = (
            "docker build --label io.cage.lifecycle=ephemeral "
            "--build-arg CAGE_BASE=cage-base:ci "
            "--build-arg CAGE_VERSION=ci "
            "-t codex:desktop-smoke -f Dockerfile.codex ."
        )
        self.assertIn(base_build, workflow)
        self.assertIn(leaf_build, workflow)
        self.assertLess(workflow.index(base_build), workflow.index(leaf_build))

    def test_candidate_builds_receive_release_version_for_oci_labels(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("CAGE_VERSION=${{ needs.candidate-base.outputs.version }}", workflow)
        self.assertIn("CAGE_BASE=ghcr.io/sindycate/cage/base@${{ needs.candidate-base.outputs.digest }}", workflow)



# Stub curl/docker/gh as bash *functions* prepended to the real workflow run
# blocks. Functions are internal to the shell, so the test needs no executable
# files and works even where the temp dir is mounted noexec. The workflow blocks
# source .github/scripts/ghcr-status.sh, so these tests exercise the real
# ghcr_status helper and the real status-code branching.

CURL_FUNC = '''
curl() {
  local url="${@: -1}"
  case "$url" in
    *ghcr.io/token*)
      case "${STUB_MODE:-}" in
        token_notfound)
          echo "curl: (22) The requested URL returned error: 404, not found in credential helper" >&2
          return 22 ;;
        *)
          echo '{"token":"stub-token"}'
          return 0 ;;
      esac ;;
    *"/v2/"*"/manifests/"*)
      if [ -n "${STUB_MISSING_REPO:-}" ] && [[ "$url" == *"/${STUB_MISSING_REPO}/manifests/"* ]]; then
        printf '404'; return 0
      fi
      case "${STUB_MODE:-}" in
        absent) printf '404'; return 0 ;;
        present|present_match|present_conflict) printf '200'; return 0 ;;
        unauthorized|unauthorized_sha404) printf '401'; return 0 ;;
        denied) printf '403'; return 0 ;;
        server) printf '503'; return 0 ;;
        timeout) printf '000'; return 0 ;;
        *) printf '200'; return 0 ;;
      esac ;;
  esac
  return 0
}
'''

# docker stub for the candidate resolve step (digest + platform inspection).
DOCKER_FUNC_CANDIDATE = '''
docker() {
  if [ "$1" = "buildx" ] && [ "$2" = "imagetools" ] && [ "$3" = "inspect" ]; then
    for a in "$@"; do
      case "$a" in
        *Manifest.Digest*) printf 'sha256:1111111111111111111111111111111111111111111111111111111111111111'; return 0 ;;
        *Platform.Architecture*) printf '%s ' "${STUB_PLATFORMS:-linux/amd64 linux/arm64}"; return 0 ;;
      esac
    done
    return 1
  fi
  return 0
}
'''

# The assembly blocks perform an existence check before and after the write.
# This stub models that transition without making any registry calls.
CURL_FUNC_ASSEMBLY = '''
curl() {
  local url="${@: -1}"
  case "$url" in
    *ghcr.io/token*)
      printf '{"token":"stub-token"}'
      return 0 ;;
    *"/v2/"*"/manifests/"*)
      case "${STUB_MODE:-}" in
        absent_then_present)
          if [ ! -e "${STUB_STATE_FILE}" ]; then
            : > "${STUB_STATE_FILE}"
            printf '404'
          else
            printf '200'
          fi
          return 0 ;;
        absent) printf '404'; return 0 ;;
        present) printf '200'; return 0 ;;
        server) printf '503'; return 0 ;;
        timeout) printf '000'; return 0 ;;
      esac ;;
  esac
  return 0
}
'''

DOCKER_FUNC_ASSEMBLY = '''
docker() {
  if [ "$1" = "buildx" ] && [ "$2" = "imagetools" ]; then
    if [ "$3" = "inspect" ]; then
      local ref="${4:-}"
      local format="${@: -1}"
      if [[ "$format" == *json* ]]; then
        if [[ "$ref" == *@"${STUB_ARCH_AMD64_DIGEST:-}" ]]; then
          printf '%s' "${STUB_AMD64_DESCRIPTORS:-}"; return 0
        fi
        if [[ "$ref" == *@"${STUB_ARCH_ARM64_DIGEST:-}" ]]; then
          printf '%s' "${STUB_ARM64_DESCRIPTORS:-}"; return 0
        fi
        printf '%s' "${STUB_FINAL_DESCRIPTORS:-}"; return 0
      fi
      for a in "$@"; do
        case "$a" in
          *Manifest.Digest*) printf '%s' "${STUB_DIGEST:-sha256:1111111111111111111111111111111111111111111111111111111111111111}"; return 0 ;;
          *Platform.Architecture*) printf '%s ' "${STUB_PLATFORMS:-linux/amd64 linux/arm64}"; return 0 ;;
        esac
      done
      return 1
    fi
    if [ "$3" = "create" ]; then
      echo "CREATE_CALLED $*" >> "${CREATE_MARKER}"
      return 0
    fi
  fi
  return 0
}
'''

# docker stub for the promotion step (inspect returns a controllable existing
# digest; create records that it was invoked).
DOCKER_FUNC_PROMOTE = '''
docker() {
  if [ "$1" = "buildx" ] && [ "$2" = "imagetools" ]; then
    if [ "$3" = "inspect" ]; then
      printf '%s' "${STUB_EXISTING_DIGEST:-}"
      return 0
    fi
    if [ "$3" = "create" ]; then
      echo "CREATE_CALLED $*" >> "${CREATE_MARKER}"
      return 0
    fi
  fi
  return 0
}
'''

GH_FUNC = '''
gh() {
  if [ "$1" = "api" ]; then
    if [[ "$*" == *"/attestations/"* ]]; then
      case "${STUB_ATTEST_LOOKUP:-absent}" in
        absent)
          printf 'HTTP/2.0 404 Not Found\\n\\n'
          return 1 ;;
        present)
          printf 'HTTP/2.0 200 OK\\n\\n1\\n'
          return 0 ;;
        empty)
          printf 'HTTP/2.0 200 OK\\n\\n0\\n'
          return 0 ;;
        error)
          printf 'HTTP/2.0 503 Service Unavailable\\n\\n'
          return 1 ;;
        malformed)
          printf 'HTTP/2.0 200 OK\\n\\nnot-a-count\\n'
          return 0 ;;
      esac
    fi
    # The helper validates repository access before accepting a 404 from the
    # subject-digest endpoint as an absent attestation.
    printf 'HTTP/2.0 200 OK\\n\\n'
    return 0
  fi
  if [ "$1" = "attestation" ] && [ "$2" = "verify" ]; then
    if [ -n "${STUB_ATTEST_LOG:-}" ]; then
      printf '%s\n' "$*" >> "${STUB_ATTEST_LOG}"
    fi
    if [ "${STUB_ATTEST:-}" = "fail" ] || [ "${STUB_ATTEST:-}" = "missing" ]; then
      echo "attestation verification failed" >&2
      return 1
    fi
    echo "verified"
    return 0
  fi
  return 0
}
'''


def _extract_run_block(workflow_path, step_name_substr):
    """Extract a step's ``run: |`` block from a workflow as plain bash."""
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    name_idx = next(i for i, l in enumerate(lines) if step_name_substr in l)
    run_idx = next(
        i for i in range(name_idx, len(lines))
        if re.match(r"^\s*run:\s*\|\s*$", lines[i])
    )
    indent = len(lines[run_idx]) - len(lines[run_idx].lstrip())
    body = []
    for line in lines[run_idx + 1:]:
        if line.strip() == "":
            body.append("")
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    nonempty = [b for b in body if b.strip()]
    pad = min(len(b) - len(b.lstrip()) for b in nonempty)
    return "\n".join(b[pad:] if len(b) >= pad else "" for b in body)


class ReleaseTagGateTests(unittest.TestCase):
    """Execute the real release metadata gate with remote tag-ref stubs.

    GitHub Actions may check out an annotated trigger tag as a lightweight local
    ref. The gate must therefore prove annotation and commit identity from the
    remote direct + peeled refs, not from ``refs/tags/*`` in the checkout.
    """

    SHA = "a" * 40
    TAG_OBJECT = "b" * 40
    VERSION = re.search(
        r'^CAGE_VERSION="([^"]+)"$',
        (ROOT / "cage").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    TAG = f"v{VERSION}"

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(
            RELEASE_WORKFLOW, "Verify tag, version, and commit agree"
        )
        assert "git ls-remote --tags origin" in cls.script
        assert '"refs/tags/${TAG}^{}"' in cls.script
        assert "git cat-file -t" not in cls.script

    def _run_gate(self, mode):
        base = Path(tempfile.mkdtemp(prefix="release-tag-gate-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        github_output = base / "github_output.txt"
        github_output.write_text("", encoding="utf-8")
        git_stub = r'''
git() {
  if [ "$1" = "ls-remote" ]; then
    case "${TAG_STUB_MODE}" in
      annotated)
        printf '%s\trefs/tags/v0.32.0\n' "${TAG_OBJECT}"
        printf '%s\trefs/tags/v0.32.0^{}\n' "${GITHUB_SHA}"
        ;;
      lightweight)
        printf '%s\trefs/tags/v0.32.0\n' "${GITHUB_SHA}"
        ;;
      mismatch)
        printf '%s\trefs/tags/v0.32.0\n' "${TAG_OBJECT}"
        printf '%s\trefs/tags/v0.32.0^{}\n' "cccccccccccccccccccccccccccccccccccccccc"
        ;;
    esac
    return 0
  fi
  return 127
}
'''.replace("v0.32.0", self.TAG)
        script_file = base / "gate.sh"
        script_file.write_text(git_stub + "\n" + self.script, encoding="utf-8")
        env = dict(os.environ)
        env.update(
            {
                "GITHUB_REF": f"refs/tags/{self.TAG}",
                "GITHUB_SHA": self.SHA,
                "GITHUB_OUTPUT": str(github_output),
                "TAG_STUB_MODE": mode,
                "TAG_OBJECT": self.TAG_OBJECT,
            }
        )
        result = subprocess.run(
            ["bash", str(script_file)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        return result, github_output.read_text(encoding="utf-8")

    def test_remote_annotated_tag_passes_even_without_local_tag_object(self):
        result, output = self._run_gate("annotated")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"version={self.VERSION}", output)
        self.assertIn(f"tag={self.TAG}", output)

    def test_remote_lightweight_tag_fails_closed(self):
        result, _ = self._run_gate("lightweight")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be an annotated tag", result.stdout)

    def test_remote_annotated_tag_for_other_commit_fails_closed(self):
        result, _ = self._run_gate("mismatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not dereference", result.stdout)


class CandidateResolveFailClosedTests(unittest.TestCase):
    """Execute the real candidate resolve block against stubbed curl/docker/gh.

    Existence is decided by the registry HTTP status code (via ghcr_status), so
    only an authoritative 404 authorizes candidate creation; 401/403, timeout,
    and 5xx fail closed. Regression cases cover a SHA containing "404" and a
    credential-helper error containing "not found".
    """

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(
            CI_WORKFLOW, "Resolve final base candidate"
        )
        assert ". .github/scripts/ghcr-status.sh" in cls.script
        assert "ghcr_status" in cls.script

    def _run_resolve(
        self, mode, attest="ok", sha=None, platforms=None, missing_repo=None
    ):
        base = Path(tempfile.mkdtemp(prefix="resolve-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        github_output = base / "github_output.txt"
        github_output.write_text("", encoding="utf-8")
        script_file = base / "resolve.sh"
        script_file.write_text(
            CURL_FUNC + DOCKER_FUNC_CANDIDATE + GH_FUNC + "\n" + self.script,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["SHA"] = sha or ("a" * 40)
        env["GITHUB_REPOSITORY"] = "Sindycate/cage"
        env["GITHUB_OUTPUT"] = str(github_output)
        env["GITHUB_ACTOR"] = "stub-actor"
        env["GH_TOKEN"] = "dummy-token"
        env["STUB_MODE"] = mode
        env["STUB_ATTEST"] = attest
        env["STUB_ATTEST_LOOKUP"] = "present" if attest == "fail" else "absent"
        if platforms is not None:
            env["STUB_PLATFORMS"] = platforms
        if missing_repo is not None:
            env["STUB_MISSING_REPO"] = missing_repo
        result = subprocess.run(
            ["bash", str(script_file)], env=env, capture_output=True, text=True,
            cwd=str(ROOT),
        )
        outputs = {}
        for line in github_output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                outputs[key] = value
        return result, outputs

    def test_authoritative_404_authorizes_creation(self):
        result, outputs = self._run_resolve("absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("base_exists"), "false", outputs)

    def test_200_verified_candidate_is_reused(self):
        result, outputs = self._run_resolve("present")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("base_exists"), "true", outputs)
        self.assertTrue(outputs.get("base_digest", "").startswith("sha256:"))
        self.assertEqual(outputs.get("recovery_requested"), "false", outputs)

    def test_existing_unattested_candidate_authorizes_artifact_recovery(self):
        result, outputs = self._run_resolve("present", attest="missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("base_exists"), "true", outputs)
        self.assertEqual(outputs.get("needs_build"), "false", outputs)
        self.assertEqual(outputs.get("recovery_requested"), "true", outputs)

    def test_attestation_descriptors_do_not_count_as_runnable_platforms(self):
        result, outputs = self._run_resolve(
            "present", platforms="linux/amd64 linux/arm64 unknown/unknown"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("base_exists"), "true", outputs)

    def test_401_unauthorized_fails_closed(self):
        result, outputs = self._run_resolve("unauthorized")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (401)", result.stderr)
        self.assertNotEqual(outputs.get("base_exists"), "false")

    def test_403_denied_fails_closed(self):
        result, _ = self._run_resolve("denied")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (403)", result.stderr)

    def test_registry_5xx_fails_closed(self):
        result, _ = self._run_resolve("server")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (503)", result.stderr)

    def test_timeout_fails_closed(self):
        result, _ = self._run_resolve("timeout")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (000)", result.stderr)

    def test_401_with_sha_containing_404_still_fails_closed(self):
        # False-positive regression: a bare "404" substring match would treat the
        # "404" inside this SHA as a not-found and authorize creation. The
        # status-code mechanism must fail closed on the 401 regardless.
        sha = "404" + "a" * 37
        result, outputs = self._run_resolve("unauthorized_sha404", sha=sha)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (401)", result.stderr)
        self.assertNotEqual(outputs.get("base_exists"), "false")

    def test_credential_helper_not_found_fails_closed(self):
        # A credential-helper/network error containing "not found" must not be
        # mistaken for an absent candidate; ghcr_status reports 000 -> fail closed.
        result, outputs = self._run_resolve("token_notfound")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (000)", result.stderr)
        self.assertNotEqual(outputs.get("base_exists"), "false")

    def test_existing_but_unverifiable_candidate_fails_closed(self):
        result, _ = self._run_resolve("present", attest="fail")
        self.assertEqual(result.returncode, 1)


class CandidateLeafResolveFailClosedTests(unittest.TestCase):
    """Execute the matrix leaf candidate resolve block against stubbed curl/docker/gh."""

    @classmethod
    def setUpClass(cls):
        raw_script = _extract_run_block(
            CI_WORKFLOW, "Resolve final leaf candidates"
        )
        assert ". .github/scripts/ghcr-status.sh" in raw_script
        assert "ghcr_status" in raw_script
        cls.script = raw_script.replace("${{ matrix.image }}", "claude-code")

    def _run_resolve(
        self, mode, attest="ok", sha=None, platforms=None, missing_repo=None
    ):
        base = Path(tempfile.mkdtemp(prefix="resolve-leaf-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        github_output = base / "github_output.txt"
        github_output.write_text("", encoding="utf-8")
        script_file = base / "resolve_leaf.sh"
        script_file.write_text(
            CURL_FUNC + DOCKER_FUNC_CANDIDATE + GH_FUNC + "\n" + self.script,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["SHA"] = sha or ("a" * 40)
        env["GITHUB_REPOSITORY"] = "Sindycate/cage"
        env["GITHUB_OUTPUT"] = str(github_output)
        env["GITHUB_ACTOR"] = "stub-actor"
        env["GH_TOKEN"] = "dummy-token"
        env["STUB_MODE"] = mode
        env["STUB_ATTEST"] = attest
        env["STUB_ATTEST_LOOKUP"] = "present" if attest == "fail" else "absent"
        if platforms is not None:
            env["STUB_PLATFORMS"] = platforms
        if missing_repo is not None:
            env["STUB_MISSING_REPO"] = missing_repo
        result = subprocess.run(
            ["bash", str(script_file)], env=env, capture_output=True, text=True,
            cwd=str(ROOT),
        )
        outputs = {}
        for line in github_output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                outputs[key] = value
        return result, outputs

    def test_authoritative_404_authorizes_creation(self):
        result, outputs = self._run_resolve("absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("claude_build"), "true", outputs)
        self.assertEqual(outputs.get("needs_build"), "true", outputs)

    def test_missing_images_emit_only_their_native_digest_build_matrix(self):
        result, outputs = self._run_resolve("present", missing_repo="opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("opencode_build"), "true", outputs)
        self.assertEqual(outputs.get("claude_build"), "false", outputs)
        self.assertEqual(outputs.get("needs_build"), "true", outputs)
        matrix = json.loads(outputs["build_matrix"])
        self.assertEqual(len(matrix["include"]), 2)
        self.assertEqual(
            {(entry["arch"], entry["platform"], entry["runner"]) for entry in matrix["include"]},
            {
                ("amd64", "linux/amd64", "ubuntu-24.04"),
                ("arm64", "linux/arm64", "ubuntu-24.04-arm"),
            },
        )
        self.assertEqual(
            {entry["image"] for entry in matrix["include"]}, {"opencode"}
        )

    def test_200_verified_candidate_is_reused(self):
        result, outputs = self._run_resolve("present")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("claude_build"), "false", outputs)
        self.assertEqual(outputs.get("needs_build"), "false", outputs)
        self.assertTrue(outputs.get("claude_digest", "").startswith("sha256:"))
        self.assertEqual(outputs.get("claude_recovery_requested"), "false", outputs)

    def test_existing_unattested_candidate_authorizes_artifact_recovery(self):
        result, outputs = self._run_resolve("present", attest="missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.get("claude_build"), "false", outputs)
        self.assertEqual(outputs.get("claude_recovery_requested"), "true", outputs)

    def test_401_unauthorized_fails_closed(self):
        result, outputs = self._run_resolve("unauthorized")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (401)", result.stderr)
        self.assertNotEqual(outputs.get("claude_build"), "false")

    def test_403_denied_fails_closed(self):
        result, _ = self._run_resolve("denied")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (403)", result.stderr)

    def test_registry_5xx_fails_closed(self):
        result, _ = self._run_resolve("server")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (503)", result.stderr)

    def test_timeout_fails_closed(self):
        result, _ = self._run_resolve("timeout")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (000)", result.stderr)

    def test_existing_but_unverifiable_candidate_fails_closed(self):
        result, _ = self._run_resolve("present", attest="fail")
        self.assertEqual(result.returncode, 1)

    def test_existing_candidate_with_wrong_platform_set_fails_closed(self):
        result, _ = self._run_resolve(
            "present", platforms="linux/amd64 linux/arm64 linux/arm/v7"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain exactly linux/amd64 and linux/arm64", result.stderr)

    def test_ambiguous_registry_status_fails_closed_before_planning_builds(self):
        result, outputs = self._run_resolve("server")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (503)", result.stderr)
        self.assertNotIn("build_matrix", outputs)


class CandidateAssemblyTests(unittest.TestCase):
    """Execute the native digest handoff and write-once assembly block."""

    IMAGE_NAME = "base"
    # BuildKit emits these parent architecture indexes. Their runnable child
    # manifests and unknown/unknown SBOM/provenance descriptors are distinct.
    # The amd64 values mirror the real benchmark shape: dc11626... contains
    # bf746c..., while the final 411e972... index contains bf746c....
    ARCH_INDEX_AMD64 = "sha256:dc11626" + "a" * 57
    ARCH_INDEX_ARM64 = "sha256:dc11627" + "b" * 57
    RUNNABLE_AMD64 = "sha256:bf746c" + "c" * 58
    RUNNABLE_ARM64 = "sha256:bf746d" + "d" * 58
    ATTESTATION_AMD64 = "sha256:" + "e" * 64
    ATTESTATION_ARM64 = "sha256:" + "f" * 64
    INDEX_DIGEST = "sha256:411e972" + "1" * 57
    OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
    SOURCE_AMD64_DESCRIPTORS = [
        {
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "digest": RUNNABLE_AMD64,
            "size": 1001,
            "platform": {"architecture": "amd64", "os": "linux"},
        },
        {
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "digest": ATTESTATION_AMD64,
            "size": 2001,
            "annotations": {
                "vnd.docker.reference.digest": RUNNABLE_AMD64,
                "vnd.docker.reference.type": "attestation-manifest",
            },
            "platform": {"architecture": "unknown", "os": "unknown"},
        },
    ]
    SOURCE_ARM64_DESCRIPTORS = [
        {
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "digest": RUNNABLE_ARM64,
            "size": 1002,
            "platform": {"architecture": "arm64", "os": "linux"},
        },
        {
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "digest": ATTESTATION_ARM64,
            "size": 2002,
            "annotations": {
                "vnd.docker.reference.digest": RUNNABLE_ARM64,
                "vnd.docker.reference.type": "attestation-manifest",
            },
            "platform": {"architecture": "unknown", "os": "unknown"},
        },
    ]
    FINAL_DESCRIPTORS = SOURCE_AMD64_DESCRIPTORS + SOURCE_ARM64_DESCRIPTORS

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(
            CI_WORKFLOW, "Assemble or verify final base candidate"
        )
        assert "push-by-digest" not in cls.script
        assert "docker buildx imagetools create" in cls.script
        assert "read_arch_digest" in cls.script

    def _run_assembly(
        self,
        mode,
        *,
        plan_needs_build="true",
        plan_digest="",
        platforms=None,
        attest="ok",
        artifacts=(),
        manifests=None,
        amd64_descriptors=None,
        arm64_descriptors=None,
        plan_recovery_requested="false",
        attestation_lookup=None,
    ):
        base = Path(tempfile.mkdtemp(prefix="candidate-assembly-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        runner_temp = base / "runner-temp"
        runner_temp.mkdir()
        digest_dir = runner_temp / "cage-digests"
        if artifacts:
            digest_dir.mkdir()
            for arch, digest in artifacts:
                (digest_dir / arch).write_text(digest + "\n", encoding="utf-8")

        github_output = base / "github_output.txt"
        github_output.write_text("", encoding="utf-8")
        marker = base / "create_marker.txt"
        marker.write_text("", encoding="utf-8")
        state_file = base / "registry-state"
        script_file = base / "assemble.sh"
        script_file.write_text(
            CURL_FUNC_ASSEMBLY + DOCKER_FUNC_ASSEMBLY + GH_FUNC + "\n" + self.script,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(
            {
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_REPOSITORY": "Sindycate/cage",
                "GITHUB_ACTOR": "stub-actor",
                "GH_TOKEN": "dummy-token",
                "SHA": "a" * 40,
                "PLAN_NEEDS_BUILD": plan_needs_build,
                "PLAN_DIGEST": plan_digest,
                "PLAN_RECOVERY_REQUESTED": plan_recovery_requested,
                "STUB_MODE": mode,
                "STUB_STATE_FILE": str(state_file),
                "CREATE_MARKER": str(marker),
                "STUB_ATTEST": attest,
                "STUB_ATTEST_LOOKUP": attestation_lookup or (
                    "present" if attest == "fail" else "absent"
                ),
                "STUB_DIGEST": self.INDEX_DIGEST,
                "STUB_ARCH_AMD64_DIGEST": self.ARCH_INDEX_AMD64,
                "STUB_ARCH_ARM64_DIGEST": self.ARCH_INDEX_ARM64,
                "STUB_AMD64_DESCRIPTORS": json.dumps(
                    amd64_descriptors or self.SOURCE_AMD64_DESCRIPTORS,
                    separators=(",", ":"),
                ),
                "STUB_ARM64_DESCRIPTORS": json.dumps(
                    arm64_descriptors or self.SOURCE_ARM64_DESCRIPTORS,
                    separators=(",", ":"),
                ),
                "STUB_FINAL_DESCRIPTORS": json.dumps(
                    manifests or self.FINAL_DESCRIPTORS,
                    separators=(",", ":"),
                ),
                "IMAGE": f"ghcr.io/sindycate/cage/{self.IMAGE_NAME}",
                "IMAGE_NAME": self.IMAGE_NAME,
            }
        )
        if platforms is not None:
            env["STUB_PLATFORMS"] = platforms
        result = subprocess.run(
            ["bash", str(script_file)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        outputs = {}
        for line in github_output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                outputs[key] = value
        self.last_create_marker = marker.read_text(encoding="utf-8")
        return result, outputs, "CREATE_CALLED" in self.last_create_marker

    def test_fresh_candidate_is_assembled_from_exact_architecture_digests(self):
        final_digests = [descriptor["digest"] for descriptor in self.FINAL_DESCRIPTORS]
        self.assertIn(self.RUNNABLE_AMD64, final_digests)
        self.assertNotIn(self.ARCH_INDEX_AMD64, final_digests)
        result, outputs, created = self._run_assembly(
            "absent_then_present",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(created)
        self.assertEqual(outputs.get("digest"), self.INDEX_DIGEST)
        self.assertEqual(outputs.get("needs_attestation"), "true", outputs)

    def test_fresh_assembly_rejects_invalid_source_indexes_before_create(self):
        malformed_amd64 = copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS)
        malformed_amd64[0]["size"] = "not-an-integer"

        missing_arm64 = [copy.deepcopy(self.SOURCE_ARM64_DESCRIPTORS[0])]

        duplicate_amd64 = copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS)
        duplicate_amd64.append(copy.deepcopy(duplicate_amd64[1]))

        mislinked_arm64 = copy.deepcopy(self.SOURCE_ARM64_DESCRIPTORS)
        mislinked_arm64[1]["annotations"][
            "vnd.docker.reference.digest"
        ] = self.RUNNABLE_AMD64

        cases = (
            ("malformed", {"amd64_descriptors": malformed_amd64}),
            ("missing attestation", {"arm64_descriptors": missing_arm64}),
            ("duplicate descriptor", {"amd64_descriptors": duplicate_amd64}),
            ("mis-linked attestation", {"arm64_descriptors": mislinked_arm64}),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                result, _, created = self._run_assembly(
                    "absent_then_present",
                    artifacts=(
                        ("amd64", self.ARCH_INDEX_AMD64),
                        ("arm64", self.ARCH_INDEX_ARM64),
                    ),
                    **overrides,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(
                    created,
                    f"invalid source index must fail before CREATE_CALLED: {result.stderr}",
                )

    def test_partial_rerun_reuses_candidate_that_appeared_after_planning(self):
        result, outputs, created = self._run_assembly(
            "present", plan_needs_build="true"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(created, "a rerun must not recreate an existing candidate")
        self.assertEqual(outputs.get("digest"), self.INDEX_DIGEST)
        self.assertEqual(outputs.get("needs_attestation"), "false", outputs)

    def test_failed_final_attestation_rerun_recovers_matching_unchanged_index(self):
        # The original plan authorized the architecture builds and the index
        # write succeeded, but the final actions/attest step failed. A
        # gh-run-rerun-failed-equivalent assembly sees the existing tag and may
        # only request attestation after matching the retained artifacts.
        result, outputs, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(created, "recovery must never recreate the existing tag")
        self.assertEqual(outputs.get("digest"), self.INDEX_DIGEST)
        self.assertEqual(outputs.get("needs_attestation"), "true", outputs)

    def test_plan_recovery_state_also_reuses_matching_unchanged_index(self):
        result, outputs, created = self._run_assembly(
            "present",
            plan_needs_build="false",
            plan_recovery_requested="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(created)
        self.assertEqual(outputs.get("needs_attestation"), "true", outputs)

    def test_recovery_rejects_existing_index_with_wrong_nested_child(self):
        # Regression for the real shape: the final index must contain the
        # runnable bf746c... child, never the dc11626... parent index digest.
        wrong_child = self.ARCH_INDEX_AMD64
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors[0]["digest"] = wrong_child
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("child descriptor set", result.stderr)
        self.assertFalse(created, "mismatched recovery must never overwrite the tag")

    def test_recovery_rejects_extra_runnable_platform_in_nested_index(self):
        extra_runnable = "sha256:" + "9" * 64
        arm64_descriptors = copy.deepcopy(self.SOURCE_ARM64_DESCRIPTORS)
        arm64_descriptors.insert(
            1,
            {
                "mediaType": self.OCI_MANIFEST_MEDIA_TYPE,
                "digest": extra_runnable,
                "size": 1003,
                "platform": {"architecture": "ppc64le", "os": "linux"},
            },
        )
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            arm64_descriptors=arm64_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected runnable platform", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_missing_architecture_attestation(self):
        amd64_descriptors = [copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS[0])]
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            amd64_descriptors=amd64_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("linked OCI BuildKit attestation", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_duplicate_architecture_attestation(self):
        amd64_descriptors = copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS)
        amd64_descriptors.append(copy.deepcopy(amd64_descriptors[1]))
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            amd64_descriptors=amd64_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("linked OCI BuildKit attestation", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_mislinked_architecture_attestation(self):
        amd64_descriptors = copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS)
        amd64_descriptors[1]["annotations"][
            "vnd.docker.reference.digest"
        ] = self.RUNNABLE_ARM64
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            amd64_descriptors=amd64_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("linked OCI BuildKit attestation", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_wrong_architecture_attestation_reference_type(self):
        amd64_descriptors = copy.deepcopy(self.SOURCE_AMD64_DESCRIPTORS)
        amd64_descriptors[1]["annotations"][
            "vnd.docker.reference.type"
        ] = "not-attestation-manifest"
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            amd64_descriptors=amd64_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("linked OCI BuildKit attestation", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_missing_final_child_descriptor(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors.pop()
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_extra_final_child_descriptor(self):
        extra = "sha256:" + "9" * 64
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors.append(
            {
                "mediaType": self.OCI_MANIFEST_MEDIA_TYPE,
                "digest": extra,
                "size": 3001,
                "platform": {"architecture": "unknown", "os": "unknown"},
            }
        )
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_duplicate_final_descriptor(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors.append(copy.deepcopy(final_descriptors[0]))
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate child descriptors", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_final_descriptor_with_changed_size(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors[0]["size"] += 1
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("complete child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_final_descriptor_with_changed_media_type(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors[0]["mediaType"] = "application/vnd.docker.distribution.manifest.v2+json"
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("complete child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_final_descriptor_with_extra_platform_field(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors[0]["platform"]["variant"] = "v8"
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("complete child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_final_descriptor_with_extra_annotation(self):
        final_descriptors = copy.deepcopy(self.FINAL_DESCRIPTORS)
        final_descriptors[0]["annotations"] = {"unexpected": "value"}
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
            manifests=final_descriptors,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("complete child descriptor set", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_existing_index_when_plan_digest_differs(self):
        wrong_index = "sha256:" + "2" * 64
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            plan_digest=wrong_index,
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed between plan and assembly", result.stderr)
        self.assertFalse(created)

    def test_recovery_requires_both_architecture_artifacts(self):
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(("amd64", self.ARCH_INDEX_AMD64),),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or unsafe architecture digest artifact", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_extra_architecture_artifact(self):
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
                ("ppc64le", self.ARCH_INDEX_AMD64),
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected", result.stderr)
        self.assertFalse(created)

    def test_recovery_fails_closed_when_attestation_lookup_is_ambiguous(self):
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="missing",
            attestation_lookup="error",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unable to determine attestation state", result.stderr)
        self.assertFalse(created)

    def test_recovery_rejects_present_but_invalid_attestation(self):
        result, _, created = self._run_assembly(
            "present",
            plan_needs_build="true",
            attest="fail",
            artifacts=(
                ("amd64", self.ARCH_INDEX_AMD64),
                ("arm64", self.ARCH_INDEX_ARM64),
            ),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed the exact policy", result.stderr)
        self.assertFalse(created)

    def test_missing_architecture_artifact_fails_before_assembly(self):
        result, _, created = self._run_assembly(
            "absent_then_present", artifacts=(("amd64", self.ARCH_INDEX_AMD64),)
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or unsafe architecture digest artifact", result.stderr)
        self.assertFalse(created)

    def test_existing_candidate_with_mismatched_platforms_fails_closed(self):
        result, _, created = self._run_assembly(
            "present", platforms="linux/amd64 linux/arm64 linux/arm/v7"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain exactly linux/amd64 and linux/arm64", result.stderr)
        self.assertFalse(created)

    def test_existing_candidate_with_failed_attestation_fails_closed(self):
        result, _, created = self._run_assembly("present", attest="fail")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created)

    def test_candidate_disappearance_after_verified_plan_is_not_recreated(self):
        result, _, created = self._run_assembly(
            "absent", plan_needs_build="false"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to recreate it", result.stderr)
        self.assertFalse(created)

    def test_ambiguous_registry_status_fails_closed_before_assembly(self):
        result, _, created = self._run_assembly("server")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous registry status (503)", result.stderr)
        self.assertFalse(created)


class CandidateLeafAssemblyTests(CandidateAssemblyTests):
    """Run the same recovery contract against the leaf assembly path."""

    IMAGE_NAME = "claude-code"

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(
            CI_WORKFLOW, "Assemble or verify final leaf candidate"
        )
        assert "push-by-digest" not in cls.script
        assert "docker buildx imagetools create" in cls.script
        assert "read_arch_digest" in cls.script


class ManifestAssemblerTests(unittest.TestCase):
    """Execute the candidate manifest generator against stubbed GHCR/docker/gh."""

    @classmethod
    def setUpClass(cls):
        cls.script = _extract_run_block(CI_WORKFLOW, "Resolve candidate digests from GHCR")
        assert ". .github/scripts/ghcr-status.sh" in cls.script
        assert "release-candidate-" in cls.script

    def _run_manifest(self, *, mode="present", platforms=None, attest="ok", missing_repo=None):
        base = Path(tempfile.mkdtemp(prefix="manifest-assemble-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        script_file = base / "assemble.sh"
        script_file.write_text(
            CURL_FUNC + DOCKER_FUNC_CANDIDATE + GH_FUNC + "\n" + self.script,
            encoding="utf-8"
        )
        attest_log = base / "attestations.log"
        env = dict(os.environ)
        sha = "a" * 40
        env["SHA"] = sha
        env["VERSION"] = "0.33.0"
        env["BASE_DIGEST"] = f"sha256:{'1' * 64}"
        env["RUN_ID"] = "12345"
        env["GITHUB_REPOSITORY"] = "Sindycate/cage"
        env["GITHUB_ACTOR"] = "stub-actor"
        env["GH_TOKEN"] = "dummy-token"
        env["STUB_MODE"] = mode
        env["STUB_ATTEST"] = attest
        env["STUB_ATTEST_LOG"] = str(attest_log)
        if platforms is not None:
            env["STUB_PLATFORMS"] = platforms
        if missing_repo is not None:
            env["STUB_MISSING_REPO"] = missing_repo
        result = subprocess.run(
            ["bash", str(script_file)], env=env, capture_output=True, text=True,
            cwd=str(ROOT),
        )
        manifest_file = ROOT / f"release-candidate-{sha}.json"
        self.addCleanup(manifest_file.unlink, missing_ok=True)
        return result, manifest_file, attest_log

    def test_manifest_assembler_resolves_and_creates_valid_schema_v3_manifest(self):
        result, manifest_file, attest_log = self._run_manifest()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(manifest_file.is_file())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        sha = "a" * 40
        self.assertEqual(manifest["schema"], "cage.release-candidate")
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["source_sha"], sha)
        self.assertEqual(manifest["version"], "0.33.0")
        self.assertEqual(manifest["ci_run_id"], 12345)
        self.assertEqual(
            set(manifest["images"].keys()),
            {"base", "claude-code", "codex", "opencode", "token-monitor"},
        )
        for image_name, info in manifest["images"].items():
            self.assertEqual(info["name"], f"ghcr.io/sindycate/cage/{image_name}")
            self.assertEqual(info["tag"], f"candidate-{sha}")
            self.assertTrue(info["digest"].startswith("sha256:"))
        self.assertEqual(
            len(attest_log.read_text(encoding="utf-8").splitlines()), 5,
            "final candidate verification must attest all five image indexes",
        )

    def test_manifest_verification_fails_if_any_of_the_five_candidates_is_missing(self):
        result, manifest_file, _ = self._run_manifest(missing_repo="token-monitor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate token-monitor is not present", result.stderr)
        self.assertFalse(manifest_file.exists())

    def test_manifest_verification_requires_exact_platform_set_for_all_candidates(self):
        result, manifest_file, _ = self._run_manifest(
            platforms="linux/amd64 linux/arm64 linux/arm/v7"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain exactly linux/amd64 and linux/arm64", result.stderr)
        self.assertFalse(manifest_file.exists())

    def test_manifest_verification_fails_on_any_candidate_attestation_failure(self):
        result, manifest_file, _ = self._run_manifest(attest="fail")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(manifest_file.exists())


class VersionTagFailClosedTests(unittest.TestCase):
    """Execute the real promotion block against stubbed curl/docker.

    An immutable version tag is created only on an authoritative 404; a matching
    digest is resumable success; a conflicting digest or any ambiguous registry
    failure (401/403/timeout/5xx) fails closed and never reaches imagetools create.
    """

    DIGEST = "sha256:" + "1" * 64
    OTHER = "sha256:" + "9" * 64

    @classmethod
    def setUpClass(cls):
        block = _extract_run_block(RELEASE_WORKFLOW, "Create immutable version tag")
        assert ". .github/scripts/ghcr-status.sh" in block
        # Concrete matrix values so the extracted block is runnable.
        cls.script = block.replace("${{ matrix.image }}", "base")

    def _run_promote(self, mode, existing_digest):
        base = Path(tempfile.mkdtemp(prefix="promote-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        marker = base / "create_marker.txt"
        marker.write_text("", encoding="utf-8")
        script_file = base / "promote.sh"
        script_file.write_text(
            CURL_FUNC + DOCKER_FUNC_PROMOTE + "\n" + self.script,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["VERSION"] = "0.26.7"
        env["DIGEST"] = self.DIGEST
        env["GITHUB_ACTOR"] = "stub-actor"
        env["GH_TOKEN"] = "dummy-token"
        env["STUB_MODE"] = mode
        env["STUB_EXISTING_DIGEST"] = existing_digest
        env["CREATE_MARKER"] = str(marker)
        result = subprocess.run(
            ["bash", str(script_file)], env=env, capture_output=True, text=True,
            cwd=str(ROOT),
        )
        create_called = "CREATE_CALLED" in marker.read_text(encoding="utf-8")
        return result, create_called

    def test_authoritative_404_creates_version_tag(self):
        result, created = self._run_promote("absent", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(created, "imagetools create must run on authoritative 404")

    def test_matching_digest_is_resumable_and_does_not_create(self):
        result, created = self._run_promote("present_match", self.DIGEST)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(created, "must not recreate an identical version tag")
        self.assertIn("already points to", result.stdout)

    def test_conflicting_digest_fails_closed_without_create(self):
        result, created = self._run_promote("present_conflict", self.OTHER)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created)
        self.assertIn("exists with digest", result.stderr)

    def test_401_fails_closed_without_create(self):
        result, created = self._run_promote("unauthorized", "")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created, "a 401 must never reach imagetools create")
        self.assertIn("ambiguous registry status (401)", result.stderr)

    def test_403_fails_closed_without_create(self):
        result, created = self._run_promote("denied", "")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created)

    def test_5xx_fails_closed_without_create(self):
        result, created = self._run_promote("server", "")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created, "a registry outage must never reach imagetools create")
        self.assertIn("ambiguous registry status (503)", result.stderr)

    def test_timeout_fails_closed_without_create(self):
        result, created = self._run_promote("timeout", "")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(created)


class PublicImageGateTests(unittest.TestCase):
    """Execute the real public-consumer block with a credential-auditing stub."""

    DIGEST = "sha256:" + "1" * 64

    @classmethod
    def setUpClass(cls):
        block = _extract_run_block(
            RELEASE_WORKFLOW, "Verify anonymous version and latest manifests and pulls"
        )
        cls.script = block.replace("${{ matrix.image }}", "base")

    def _run_gate(self, mode):
        base = Path(tempfile.mkdtemp(prefix="public-images-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        log = base / "docker-calls.txt"
        docker = base / "docker"
        docker.write_text(
            r'''#!/bin/bash
set -euo pipefail
for secret_var in GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN DOCKER_AUTH_CONFIG REGISTRY_AUTH_FILE; do
  if [ -n "${!secret_var+x}" ]; then
    echo "credential variable leaked: ${secret_var}" >&2
    exit 90
  fi
done
if [ -z "${DOCKER_CONFIG:-}" ] || [ ! -d "$DOCKER_CONFIG" ]; then
  echo "fresh DOCKER_CONFIG missing" >&2
  exit 91
fi
if find "$DOCKER_CONFIG" -mindepth 1 -print -quit | grep -q .; then
  echo "fresh DOCKER_CONFIG is not empty" >&2
  exit 92
fi
printf '%s\t%s\n' "$DOCKER_CONFIG" "$*" >> "$PUBLIC_STUB_LOG"
if [ "$1 $2 $3" = "buildx imagetools inspect" ]; then
  format="${@: -1}"
  case "$format" in
    *Manifest.Digest*)
      if [ "$PUBLIC_STUB_MODE" = "digest-mismatch" ]; then
        printf 'sha256:%064d' 9
      else
        printf '%s' "$EXPECTED_DIGEST"
      fi
      ;;
    *Platform.OS*)
      if [ "$PUBLIC_STUB_MODE" = "missing-arm64" ]; then
        printf 'linux/amd64 unknown/unknown '
      else
        printf 'linux/amd64 linux/arm64 unknown/unknown '
      fi
      ;;
    *) exit 93 ;;
  esac
  exit 0
fi
if [ "$1" = "pull" ]; then
  [ "$PUBLIC_STUB_MODE" != "pull-failure" ]
  exit $?
fi
exit 94
''',
            encoding="utf-8",
        )
        docker.chmod(0o755)
        script = base / "public-images.sh"
        script.write_text(self.script, encoding="utf-8")
        runner_temp = base / "runner-temp"
        runner_temp.mkdir()
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(base) + os.pathsep + env.get("PATH", ""),
                "RUNNER_TEMP": str(runner_temp),
                "VERSION": "0.28.0",
                "EXPECTED_DIGEST": self.DIGEST,
                "PUBLIC_STUB_MODE": mode,
                "PUBLIC_STUB_LOG": str(log),
                # Deliberately seed every prohibited credential source. The
                # workflow's env -u boundary must remove all of them.
                "GH_TOKEN": "must-not-reach-docker",
                "GITHUB_TOKEN": "must-not-reach-docker",
                "GH_ENTERPRISE_TOKEN": "must-not-reach-docker",
                "GITHUB_ENTERPRISE_TOKEN": "must-not-reach-docker",
                "DOCKER_AUTH_CONFIG": "must-not-reach-docker",
                "REGISTRY_AUTH_FILE": "must-not-reach-docker",
            }
        )
        result = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, calls

    def test_public_version_and_latest_pass_with_fresh_credentials(self):
        result, calls = self._run_gate("public")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 6)
        self.assertTrue(any(":0.28.0" in call for call in calls))
        self.assertTrue(any(":latest" in call for call in calls))

    def test_digest_mismatch_fails_closed_before_pull(self):
        result, calls = self._run_gate("digest-mismatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("digest", result.stderr)
        self.assertFalse(any("pull" in call for call in calls))

    def test_missing_platform_fails_closed_before_pull(self):
        result, calls = self._run_gate("missing-arm64")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain exactly linux/amd64 and linux/arm64", result.stderr)
        self.assertFalse(any("pull" in call for call in calls))

    def test_anonymous_pull_failure_fails_closed(self):
        result, calls = self._run_gate("pull-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("pull --platform linux/amd64" in call for call in calls))



class GhcrStatusCurlOptionsTests(unittest.TestCase):
    """Validate the real ghcr_status helper's curl invocations against the real
    curl argument parser. A fake runner would accept the invalid `--no-config`;
    this executes the helper and probes each captured curl command for real."""

    def test_ghcr_status_curl_options_are_accepted_by_real_curl(self):
        if shutil.which("curl") is None:
            self.skipTest("curl not installed")
        helper = (ROOT / ".github" / "scripts" / "ghcr-status.sh").read_text(encoding="utf-8")
        base = Path(tempfile.mkdtemp(prefix="ghcr-curl-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        args_file = base / "curl_args.txt"
        args_file.write_text("", encoding="utf-8")
        # Recording curl stub: logs each call's args (one per line, --END-- between
        # calls) and returns values so ghcr_status reaches both curl invocations.
        recording_curl = r"""
curl() {
  { for a in "$@"; do printf '%s\n' "$a"; done; printf -- '--END--\n'; } >> "ARGS_FILE_PLACEHOLDER"
  local url="${@: -1}"
  case "$url" in
    *ghcr.io/token*) echo '{"token":"stub-token"}'; return 0 ;;
    *) printf '200'; return 0 ;;
  esac
}
""".replace("ARGS_FILE_PLACEHOLDER", str(args_file))
        script = recording_curl + helper + "\nghcr_status sindycate/cage/base candidate-abc\n"
        script_file = base / "probe.sh"
        script_file.write_text(script, encoding="utf-8")
        env = dict(os.environ)
        env["GITHUB_ACTOR"] = "stub-actor"
        env["GH_TOKEN"] = "dummy-token"
        result = subprocess.run(
            ["bash", str(script_file)], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "200")

        # Parse the recorded curl calls.
        calls = []
        current = []
        for line in args_file.read_text(encoding="utf-8").splitlines():
            if line == "--END--":
                if current:
                    calls.append(current)
                current = []
            else:
                current.append(line)
        if current:
            calls.append(current)
        self.assertGreaterEqual(len(calls), 2, "expected token + manifest curl calls")

        # Control: real curl must reject the invalid option.
        control = subprocess.run(
            ["curl", "--no-config", "-fsS", "http://127.0.0.1:1/"],
            capture_output=True, text=True,
        )
        self.assertIn("curl: option", control.stderr)

        # Each recorded curl invocation's options must be accepted by real curl.
        # The recording function captures curl's arguments (the command name is
        # stripped), so argv[0] is the first option and argv[-1] is the URL.
        for argv in calls:
            self.assertEqual(argv[0], "-q", f"-q must be the first option: {argv}")
            probe = ["curl"] + argv[:-1] + ["http://127.0.0.1:1/"]
            r = subprocess.run(probe, capture_output=True, text=True, timeout=30)
            self.assertNotIn(
                "curl: option", r.stderr,
                f"curl rejected options in {argv}: {r.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
