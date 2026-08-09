"""Tests for scripts/publish_release.py (maintainer-only release automation).

External systems (git, gh, docker, curl) are dependency-injected. Most tests use
a fully scripted fake runner; the end-to-end ordering test uses a real temporary
bare Git remote for git while faking gh/docker/curl.
"""

from __future__ import annotations

import importlib.util
import fcntl
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_release.py"


def load_module():
    spec = importlib.util.spec_from_file_location("publish_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["publish_release"] = module
    spec.loader.exec_module(module)
    return module


pr = load_module()


# --- Fakes -----------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.value = start

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSleeper:
    def __init__(self, clock: FakeClock, step: float = 1.0):
        self.clock = clock
        self.step = step
        self.sleeps = 0

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.clock.advance(self.step)
        if self.sleeps > 100000:
            raise AssertionError("sleeper looped too many times")


class FakePrompter:
    def __init__(self, answer: str = ""):
        self.answer = answer
        self.prompts = []

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return self.answer


class FakeHttpResponse:
    def __init__(self, document, headers=None):
        self.payload = json.dumps(document).encode("utf-8")
        self.headers = headers or {}

    def read(self, size=-1):
        return self.payload[:size] if size >= 0 else self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeHttpOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected registry request")
        return self.responses.pop(0)


class FakeRunner:
    """Scripted command runner. First matching handler wins."""

    def __init__(self, real_git: bool = False):
        self.calls = []
        self.call_records = []
        self.handlers = []
        self.last_env = None
        self._real = pr.SubprocessRunner()
        self.real_git = real_git

    def add(self, predicate, responder):
        self.handlers.append((predicate, responder))

    def run(self, argv, *, cwd=None, env=None, input_text=None, timeout=None):
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        self.call_records.append(
            {
                "argv": argv,
                "cwd": str(cwd) if cwd is not None else None,
                "env": env,
                "input_text": input_text,
                "timeout": timeout,
            }
        )
        self.last_env = env
        if argv and argv[0] == "git" and self.real_git:
            return self._real.run(argv, cwd=cwd, env=env, input_text=input_text, timeout=timeout)
        for predicate, responder in self.handlers:
            if predicate(argv):
                result = responder(argv) if callable(responder) else responder
                return pr.CommandResult(argv, result.returncode, result.stdout, result.stderr)
        return pr.CommandResult(argv, 0, "", "")

    # assertion helpers
    def commands(self, prog=None):
        if prog is None:
            return self.calls
        return [call for call in self.calls if call and call[0] == prog]

    def mutating_commands(self):
        mutating = []
        for call in self.calls:
            if not call:
                continue
            if call[0] == "git" and "push" in call:
                mutating.append(call)
            elif call[0] == "git" and "tag" in call and "-a" in call:
                mutating.append(call)
            elif call[0] == "gh" and "rerun" in call:
                mutating.append(call)
        return mutating

    def has_argv(self, *expected):
        return any(call[: len(expected)] == list(expected) for call in self.calls)


# --- predicate factories ---------------------------------------------------


def eq(*argv):
    target = list(argv)

    def predicate(call):
        return call == target

    return predicate


def starts(*argv):
    target = list(argv)

    def predicate(call):
        return call[: len(target)] == target

    return predicate


def prog(name):
    def predicate(call):
        return bool(call) and call[0] == name

    return predicate


def R(out: str = "", err: str = "", code: int = 0) -> pr.CommandResult:
    return pr.CommandResult([], code, out, err)


class Sequence:
    """Returns successive results on each match; repeats the last one."""

    def __init__(self, *results):
        self.results = list(results)
        self.index = 0

    def __call__(self, argv):
        result = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return result


# --- Scenario builder ------------------------------------------------------


def make_archive_bytes(version: str) -> bytes:
    """Build a small but real tar.gz archive for verification tests."""
    import gzip
    import io
    import tarfile

    payload = {
        f"cage-{version}/cage": b"#!/bin/bash\necho cage %s\n" % version.encode(),
        f"cage-{version}/cage-main.py": b"print('main')\n",
        f"cage-{version}/cage_core/__init__.py": b"",
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(payload.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1700000000
            info.uid = 0
            info.gid = 0
            info.mode = 0o755 if name.endswith("/cage") else 0o644
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=1700000000) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


class Scenario:
    def __init__(
        self,
        *,
        version="0.26.7",
        sha=None,
        origin_main_sha=None,
        ahead=1,
        behind=0,
        pushed=False,
        clean=True,
        branch="main",
        upstream="origin/main",
        origin_url="ssh://git@github.com/Sindycate/cage.git",
        local_tag_sha=None,
        local_tag_type="tag",
        remote_tag=None,
        remote_tag_peeled=True,
        remote_tag_commit=None,
        diff_check_ok=True,
        release_exists=False,
        release_draft=False,
        release_prerelease=False,
        ci_runs=None,
        release_runs=None,
        gh_auth_ok=True,
        docker_ok=True,
        attestation_ok=True,
        real_git=False,
        with_candidate=True,
        repo_root=None,
        push_responses=None,
        remote_main_responses=None,
        anonymous_pull_ok=True,
    ):
        self.version = version
        self.tag = f"v{version}"
        self.sha = sha or ("a" * 40)
        if pushed:
            self.origin_main_sha = self.sha
            self.ahead = 0
        else:
            self.origin_main_sha = origin_main_sha or ("b" * 40)
            self.ahead = ahead
        self.behind = behind
        self.clean = clean
        self.branch = branch
        self.upstream = upstream
        self.origin_url = origin_url
        self.local_tag_sha = local_tag_sha
        self.local_tag_type = local_tag_type
        self.remote_tag = remote_tag
        self.remote_tag_peeled = remote_tag_peeled
        self.remote_tag_commit = remote_tag_commit
        self.diff_check_ok = diff_check_ok
        self.release_exists = release_exists
        self.release_draft = release_draft
        self.release_prerelease = release_prerelease
        self.ci_runs = ci_runs if ci_runs is not None else []
        self.release_runs = release_runs if release_runs is not None else []
        self.gh_auth_ok = gh_auth_ok
        self.docker_ok = docker_ok
        self.attestation_ok = attestation_ok
        self.with_candidate = with_candidate
        self.anonymous_pull_ok = anonymous_pull_ok
        self.push_responses = push_responses
        self.remote_main_responses = remote_main_responses

        self.digests = {
            "base": "sha256:" + "1" * 64,
            "claude-code": "sha256:" + "2" * 64,
            "codex": "sha256:" + "3" * 64,
            "opencode": "sha256:" + "4" * 64,
        }
        self.archive_bytes = make_archive_bytes(version)
        self.archive_digest = __import__("hashlib").sha256(self.archive_bytes).hexdigest()
        self.image_inspect_calls = []
        self.install_dir_preexisted = None

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        if repo_root is not None:
            self.repo_root = Path(repo_root)
        else:
            self.repo_root = base / "repo"
            self.repo_root.mkdir()
            (self.repo_root / "cage").write_text(
                f'#!/bin/bash\nCAGE_VERSION="{version}"\n', encoding="utf-8"
            )
            (self.repo_root / "CHANGELOG.md").write_text(
                f"# Changelog\n\n## {version}\n\n- change\n", encoding="utf-8"
            )
            (self.repo_root / "install.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.state_dir = base / "gitstate" / "cage-release"
        self.runner = FakeRunner(real_git=real_git)
        self._program()

    # response helpers
    def manifest(self):
        matching_runs = [
            run
            for run in self.ci_runs
            if run.get("headSha") == self.sha
            and run.get("headBranch") == "main"
            and run.get("conclusion") == "success"
        ]
        ci_run_id = max(
            (int(run["databaseId"]) for run in matching_runs),
            default=123,
        )
        return {
            "schema": "cage.release-candidate",
            "schema_version": 2,
            "source_sha": self.sha,
            "version": self.version,
            "ci_run_id": ci_run_id,
            "platforms": ["linux/amd64", "linux/arm64"],
            "images": {
                name: {
                    "name": f"ghcr.io/sindycate/cage/{name}",
                    "tag": f"candidate-{self.sha}",
                    "digest": digest,
                }
                for name, digest in self.digests.items()
            },
        }

    def image_manifest_json(self, ref):
        for name, digest in self.digests.items():
            if f"/{name}:" in ref or f"/{name}@" in ref:
                return json.dumps(
                    {
                        "digest": digest,
                        "manifests": [
                            {"platform": {"os": "linux", "architecture": "amd64"}},
                            {"platform": {"os": "linux", "architecture": "arm64"}},
                        ],
                    }
                )
        return json.dumps({"digest": "sha256:" + "0" * 64, "manifests": []})

    def inspect_image(self, ref):
        self.image_inspect_calls.append(ref)
        return json.loads(self.image_manifest_json(ref))

    def _remote_tag_listing(self):
        if self.remote_tag is None:
            return ""
        commit = self.remote_tag_commit or self.sha
        if self.remote_tag_peeled:
            return (
                f"{self.remote_tag}\trefs/tags/{self.tag}\n"
                f"{commit}\trefs/tags/{self.tag}^{{}}\n"
            )
        return f"{commit}\trefs/tags/{self.tag}\n"

    def _program(self):
        r = self.runner
        tag = self.tag
        # git plumbing used during lock + preflight + detection
        r.add(eq("git", "rev-parse", "--git-path", "cage-release"), R(str(self.state_dir) + "\n"))
        r.add(eq("git", "rev-parse", "--show-toplevel"), R(str(self.repo_root.resolve()) + "\n"))
        r.add(eq("git", "rev-parse", "--abbrev-ref", "HEAD"), R(self.branch + "\n"))
        r.add(eq("git", "remote", "get-url", "origin"), R(self.origin_url + "\n"))
        r.add(eq("git", "rev-parse", "HEAD"), R(self.sha + "\n"))
        r.add(starts("git", "fetch"), R(""))
        r.add(eq("git", "rev-parse", "origin/main"), R(self.origin_main_sha + "\n"))
        r.add(
            eq("git", "rev-list", "--left-right", "--count", "origin/main...HEAD"),
            R(f"{self.behind}\t{self.ahead}\n"),
        )
        r.add(
            eq("git", "status", "--porcelain"),
            R("" if self.clean else " M cage\n"),
        )
        r.add(eq("git", "diff", "--check"), R("" if self.diff_check_ok else "trailing whitespace\n", code=0 if self.diff_check_ok else 2))
        r.add(
            eq("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
            R(self.upstream + "\n" if self.upstream else "", code=0 if self.upstream else 1),
        )
        # local tag
        if self.local_tag_sha:
            r.add(eq("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"), R(self.local_tag_sha + "\n"))
            r.add(eq("git", "cat-file", "-t", tag), R(self.local_tag_type + "\n"))
            r.add(eq("git", "rev-list", "-n1", tag), R(self.local_tag_sha + "\n"))
        else:
            r.add(eq("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"), R("", code=1))
        # remote tag listing
        r.add(starts("git", "ls-remote", "--tags", "origin"), R(self._remote_tag_listing()))
        # remote main listing (for push verification / ambiguous-push inspection)
        remote_main = self.remote_main_responses or [self.origin_main_sha]
        r.add(
            eq("git", "ls-remote", "origin", "refs/heads/main"),
            Sequence(*[R(f"{sha}\trefs/heads/main\n") for sha in remote_main]),
        )
        # timestamp for archive gates / verification
        r.add(starts("git", "show", "-s", "--format=%ct"), R("1700000000\n"))
        # tag creation (ordering captured)
        r.add(starts("git", "tag"), R(""))
        # push (optionally a sequence to simulate ambiguous/failed pushes)
        if self.push_responses:
            r.add(starts("git", "push"), Sequence(*self.push_responses))
        else:
            r.add(starts("git", "push"), R(""))

        # gh
        r.add(eq("gh", "auth", "status"), R("ok\n", code=0 if self.gh_auth_ok else 1))
        r.add(
            starts("gh", "release", "view"),
            R(
                json.dumps(
                    {
                        "tagName": tag,
                        "isDraft": self.release_draft,
                        "isPrerelease": self.release_prerelease,
                        "targetCommitish": self.sha,
                        "url": f"https://github.com/Sindycate/cage/releases/tag/{tag}",
                        "assets": [
                            {"name": f"cage-{self.version}.tar.gz", "size": len(self.archive_bytes)},
                            {"name": f"cage-{self.version}.tar.gz.sha256", "size": 100},
                            {"name": f"cage-{self.version}.spdx.json", "size": 100},
                        ],
                    }
                ) + "\n",
                code=0 if self.release_exists else 1,
            ),
        )

        def ci_run_list(argv):
            return R(json.dumps(self.ci_runs) + "\n")

        def release_run_list(argv):
            return R(json.dumps(self.release_runs) + "\n")

        r.add(
            lambda c: c[:2] == ["gh", "run"] and "list" in c and "ci.yml" in c,
            ci_run_list,
        )
        r.add(
            lambda c: c[:2] == ["gh", "run"] and "list" in c and "release.yml" in c,
            release_run_list,
        )

        def run_download(argv):
            # create the candidate manifest file in the --dir target
            if "--dir" in argv:
                target = Path(argv[argv.index("--dir") + 1])
                target.mkdir(parents=True, exist_ok=True)
                (target / f"release-candidate-{self.sha}.json").write_text(
                    json.dumps(self.manifest()), encoding="utf-8"
                )
            return R("")

        r.add(starts("gh", "run", "download"), run_download)
        r.add(starts("gh", "run", "rerun"), R(""))
        r.add(starts("gh", "run", "view"), R("2026-07-30T10:00:00Z some log line\nERROR: boom\n"))
        r.add(
            starts("gh", "attestation", "verify"),
            R("verified\n", code=0 if self.attestation_ok else 1),
        )

        # docker
        r.add(eq("docker", "info"), R("ok\n", code=0 if self.docker_ok else 1))
        r.add(eq("docker", "compose", "config"), R("ok\n", code=0 if self.docker_ok else 1))
        r.add(starts("docker", "manifest", "inspect"), R("{}\n"))
        r.add(
            starts("docker", "buildx", "imagetools", "inspect"),
            lambda argv: R(self.image_manifest_json(argv[4]) + "\n"),
        )
        # Anonymous verification performs a real pull (not just manifest inspect).
        r.add(
            starts("docker", "pull"),
            R("pulled\n", code=0 if self.anonymous_pull_ok else 1,
              err="" if self.anonymous_pull_ok else "pull access denied"),
        )
        r.add(starts("docker", "rmi"), R(""))

        # curl anonymous downloads
        def curl(argv):
            if "-o" in argv:
                dest = Path(argv[argv.index("-o") + 1])
                url = argv[-1]
                if url.endswith(".tar.gz"):
                    dest.write_bytes(self.archive_bytes)
                elif url.endswith(".sha256"):
                    dest.write_text(
                        f"{self.archive_digest}  cage-{self.version}.tar.gz\n", encoding="utf-8"
                    )
                elif url.endswith(".spdx.json"):
                    dest.write_text(
                        json.dumps({"files": [{"name": f"cage-{self.version}.tar.gz"}]}),
                        encoding="utf-8",
                    )
                elif url.endswith(".sh"):
                    dest.write_text(
                        "#!/bin/bash\n# public installer fetched anonymously\nexit 0\n",
                        encoding="utf-8",
                    )
                else:
                    dest.write_bytes(b"")
            return R("")

        r.add(starts("curl"), curl)

        # git archive: materialize a minimal committed tree (non-real_git only;
        # real_git routes git to the real runner before handlers are consulted).
        def git_archive(argv):
            if "-o" in argv:
                import io
                import tarfile as _tf

                out = Path(argv[argv.index("-o") + 1])
                raw = io.BytesIO()
                with _tf.open(fileobj=raw, mode="w") as tar:
                    for name, data in {
                        "cage": f'#!/bin/bash\nCAGE_VERSION="{self.version}"\n'.encode(),
                        "scripts/build-release.py": b"# packager (materialized stub)\n",
                    }.items():
                        info = _tf.TarInfo(name)
                        info.size = len(data)
                        info.mtime = 1700000000
                        tar.addfile(info, io.BytesIO(data))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(raw.getvalue())
            return R("")

        r.add(starts("git", "archive"), git_archive)

        # build-release.py side effect (archive gates / reproducible verification)
        def build_release(argv):
            out_dir = Path(argv[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"cage-{self.version}.tar.gz").write_bytes(self.archive_bytes)
            return R("")

        r.add(lambda c: c and c[0] == sys.executable and any("build-release.py" in part for part in c), build_release)

        # installer + installed launcher
        def installer(argv):
            env = r.last_env or {}
            install_dir = env.get("CAGE_INSTALL_DIR")
            if install_dir:
                self.install_dir_preexisted = Path(install_dir).exists()
                if self.install_dir_preexisted:
                    return R("", code=1, err="refusing existing install directory")
                import io
                import tarfile

                with tarfile.open(fileobj=io.BytesIO(self.archive_bytes), mode="r:gz") as tar:
                    for member in tar.getmembers():
                        relative = member.name.split("/", 1)[1] if "/" in member.name else ""
                        if not relative:
                            continue
                        dest = Path(install_dir) / relative
                        if member.isdir():
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(tar.extractfile(member).read())
            return R("Installed cage\n")

        r.add(lambda c: c and c[0] == "/bin/bash" and any("install.sh" in part for part in c), installer)
        r.add(lambda c: c and c[-1:] == ["--version"] and c[0].endswith("cage"), R(f"cage {self.version}\n"))

    def cleanup(self):
        self._tmp.cleanup()


def make_orch(scenario, *, dry_run=False, json_output=False, answer="", run_local_gates=False,
              poll=0.0, timeout=3600.0):
    clock = FakeClock()
    sleeper = FakeSleeper(clock, step=1.0)
    prompter = FakePrompter(answer)
    out_lines = []
    err_lines = []
    options = pr.Options(
        dry_run=dry_run,
        json_output=json_output,
        repo_root=scenario.repo_root,
        run_local_gates=run_local_gates,
        poll_interval_seconds=poll,
        workflow_timeout_seconds=timeout,
    )
    orch = pr.Orchestrator(
        options,
        runner=scenario.runner,
        clock=clock,
        sleeper=sleeper,
        prompter=prompter,
        image_inspector=scenario.inspect_image,
        out=out_lines.append,
        err=err_lines.append,
    )
    return orch, clock, sleeper, prompter, out_lines, err_lines


# --- Tests: preflight & validation -----------------------------------------


class PublishReleaseTestCase(unittest.TestCase):
    """Stubs host executable discovery; docker/gh are not present in every sandbox."""

    def setUp(self):
        self._real_which = pr.shutil.which
        pr.shutil.which = lambda name: f"/usr/bin/{name}"

    def tearDown(self):
        pr.shutil.which = self._real_which


class PublicGhcrManifestTests(unittest.TestCase):
    def test_anonymous_registry_probe_returns_authoritative_digest_and_platforms(self):
        digest = "sha256:" + "a" * 64
        opener = FakeHttpOpener(
            FakeHttpResponse({"token": "short-lived-public-token"}),
            FakeHttpResponse(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {"platform": {"os": "linux", "architecture": "amd64"}},
                        {"platform": {"os": "linux", "architecture": "arm64"}},
                    ],
                },
                {"Docker-Content-Digest": digest},
            ),
        )

        manifest = pr.inspect_public_ghcr_manifest(
            "ghcr.io/sindycate/cage/base:0.26.9", opener=opener
        )

        self.assertEqual(manifest["digest"], digest)
        self.assertEqual(len(opener.requests), 2)
        token_request = opener.requests[0][0]
        manifest_request = opener.requests[1][0]
        self.assertIn("scope=repository%3Asindycate%2Fcage%2Fbase%3Apull", token_request.full_url)
        self.assertIsNone(token_request.get_header("Authorization"))
        self.assertEqual(
            manifest_request.get_header("Authorization"),
            "Bearer short-lived-public-token",
        )
        self.assertEqual(manifest_request.get_header("Accept"), pr.GHCR_MANIFEST_ACCEPT)
        self.assertEqual(opener.requests[0][1], 30)
        self.assertEqual(opener.requests[1][1], 30)

    def test_registry_probe_fails_closed_without_authoritative_digest(self):
        opener = FakeHttpOpener(
            FakeHttpResponse({"token": "short-lived-public-token"}),
            FakeHttpResponse({"schemaVersion": 2, "manifests": []}),
        )
        with self.assertRaises(pr.VerificationError) as ctx:
            pr.inspect_public_ghcr_manifest(
                "ghcr.io/sindycate/cage/base:0.26.9", opener=opener
            )
        self.assertIn("content digest", str(ctx.exception))


class SubprocessRunnerTests(unittest.TestCase):
    def test_children_never_inherit_the_publishers_tty(self):
        if not hasattr(os, "openpty"):
            self.skipTest("PTY support unavailable")
        child_code = """\
import os
import sys
print("tty=" + str(sys.stdin.isatty()))
print("eof=" + str(sys.stdin.read() == ""))
try:
    os.open("/dev/tty", os.O_RDONLY)
    print("devtty=True")
except OSError:
    print("devtty=False")
"""
        helper = f"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('publish_release_tty', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
sys.modules['publish_release_tty'] = module
spec.loader.exec_module(module)
result = module.SubprocessRunner().run([
    sys.executable,
    '-c',
    {child_code!r},
], timeout=2)
print(json.dumps({{'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}}))
"""
        master, slave = os.openpty()

        def make_controlling_tty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            process = subprocess.Popen(
                [sys.executable, "-c", helper],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=make_controlling_tty,
            )
            os.close(slave)
            slave = -1
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["returncode"], 0, payload)
        self.assertIn("tty=False", payload["stdout"])
        self.assertIn("eof=True", payload["stdout"])
        self.assertIn("devtty=False", payload["stdout"])

    def test_explicit_input_is_still_delivered(self):
        result = pr.SubprocessRunner().run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            input_text="expected-input",
            timeout=5,
        )
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout.strip(), "expected-input")

    def test_timeout_diagnostic_is_unambiguous_with_partial_stderr(self):
        result = pr.SubprocessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys,time; print('partial', file=sys.stderr, flush=True); time.sleep(5)",
            ],
            timeout=0.1,
        )
        self.assertEqual(result.returncode, 124)
        self.assertIn("partial", result.stderr)
        self.assertIn("timed out after 0.1s", result.stderr)

    def test_timeout_terminates_descendant_process_group(self):
        if shutil.which("ps") is None:
            self.skipTest("ps not installed")
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "descendant.pid"
            child_code = (
                "import pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
                "time.sleep(30)"
            )
            result = pr.SubprocessRunner().run(
                [sys.executable, "-c", child_code], timeout=0.3
            )
            self.assertEqual(result.returncode, 124)
            self.assertTrue(pid_file.is_file())
            descendant_pid = pid_file.read_text(encoding="utf-8")
            alive = True
            for _ in range(20):
                probe = subprocess.run(
                    ["ps", "-p", descendant_pid, "-o", "stat="],
                    capture_output=True,
                    text=True,
                )
                alive = probe.returncode == 0 and bool(probe.stdout.strip())
                if not alive:
                    break
                time.sleep(0.05)
            self.assertFalse(alive, f"descendant {descendant_pid} survived timeout")



class PreflightTests(PublishReleaseTestCase):
    def test_baseline_preflight_passes_and_detects_local_ready(self):
        scenario = Scenario()  # ahead=1, clean, no tags, no release
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch.context.version, "0.26.7")
        self.assertEqual(orch.context.commit_sha, scenario.sha)
        self.assertEqual(orch.context.tag, "v0.26.7")
        self.assertEqual(orch._detect_phase(), "local_ready")

    def test_wrong_branch_fails_closed(self):
        scenario = Scenario(branch="feature")
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("main branch", str(ctx.exception))

    def test_wrong_origin_fails_closed(self):
        scenario = Scenario(origin_url="git@github.com:Someone/else.git")
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("Sindycate/cage", str(ctx.exception))

    def test_invalid_version_fails_closed(self):
        scenario = Scenario()
        (scenario.repo_root / "cage").write_text('CAGE_VERSION="not a version"\n')
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError):
            orch._preflight()

    def test_dirty_worktree_fails_closed(self):
        scenario = Scenario(clean=False)
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("worktree-clean", str(ctx.exception))

    def test_diverged_behind_fails_closed(self):
        scenario = Scenario(behind=1, ahead=0, origin_main_sha="c" * 40)
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("behind", str(ctx.exception))

    def test_multiple_unpublished_commits_fail_closed(self):
        scenario = Scenario(ahead=2)
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("ahead by 2", str(ctx.exception))

    def test_missing_executable_fails_closed(self):
        scenario = Scenario()
        orch, *_ = make_orch(scenario)
        real_which = pr.shutil.which
        pr.shutil.which = lambda name: None if name == "docker" else real_which(name)
        try:
            with self.assertRaises(pr.PreflightError) as ctx:
                orch._preflight()
            self.assertIn("docker", str(ctx.exception))
        finally:
            pr.shutil.which = real_which

    def test_changelog_section_required(self):
        scenario = Scenario()
        (scenario.repo_root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.0.1\n- old\n")
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("changelog", str(ctx.exception))


class TagValidationTests(PublishReleaseTestCase):
    def test_annotated_local_tag_matching_commit_passes(self):
        scenario = Scenario(local_tag_sha=None)  # absent is fine pre-release
        orch, *_ = make_orch(scenario)
        orch._preflight()  # no raise

        scenario = Scenario(local_tag_sha="a" * 40, local_tag_type="tag")
        # local tag sha must equal commit sha
        scenario.local_tag_sha = scenario.sha
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertTrue(orch.context.local_tag_annotated)

    def test_lightweight_local_tag_rejected(self):
        scenario = Scenario(local_tag_sha="a" * 40, local_tag_type="commit")
        scenario.local_tag_sha = scenario.sha
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("annotated", str(ctx.exception))

    def test_mismatched_local_tag_rejected(self):
        scenario = Scenario(local_tag_sha="c" * 40, local_tag_type="tag")
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("local-tag", str(ctx.exception))

    def test_mismatched_remote_tag_rejected(self):
        scenario = Scenario(remote_tag="d" * 40, remote_tag_commit="e" * 40)
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("remote-tag", str(ctx.exception))

    def test_lightweight_remote_tag_rejected(self):
        scenario = Scenario(remote_tag="d" * 40, remote_tag_peeled=False)
        orch, *_ = make_orch(scenario)
        with self.assertRaises(pr.PreflightError) as ctx:
            orch._preflight()
        self.assertIn("annotated", str(ctx.exception))

    def test_matching_remote_tag_passes(self):
        scenario = Scenario(remote_tag="d" * 40)  # peeled commit defaults to sha
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch.context.remote_tag_sha, scenario.sha)


# --- Tests: resume detection -----------------------------------------------


def make_run(sha, conclusion="success", databaseId=11, branch="main"):
    return {
        "databaseId": databaseId,
        "headSha": sha,
        "headBranch": branch,
        "event": "push",
        "conclusion": conclusion,
        "status": "completed",
        "url": f"https://github.com/Sindycate/cage/actions/runs/{databaseId}",
        "displayTitle": "CI",
    }


class ResumeDetectionTests(PublishReleaseTestCase):
    SHA = "a" * 40

    def test_phase_local_ready(self):
        scenario = Scenario(ahead=1)
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "local_ready")

    def test_phase_main_pushed(self):
        scenario = Scenario(pushed=True)
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "main_pushed")

    def test_phase_ci_passed(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run(self.SHA)])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "ci_passed")
        self.assertEqual(orch.state.ci_run_id, 11)

    def test_phase_tag_pushed(self):
        scenario = Scenario(
            pushed=True, remote_tag="d" * 40, ci_runs=[make_run(self.SHA)]
        )
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "tag_pushed")

    def test_phase_release_workflow_passed(self):
        scenario = Scenario(
            pushed=True,
            remote_tag="d" * 40,
            ci_runs=[make_run(self.SHA)],
            release_runs=[make_run(self.SHA, databaseId=22, branch="v0.26.7")],
        )
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "release_workflow_passed")
        self.assertEqual(orch.state.release_run_id, 22)

    def test_exact_sha_required_rejects_branch_latest(self):
        other = make_run(self.SHA)
        other["headSha"] = "f" * 40  # a different commit's successful run
        scenario = Scenario(pushed=True, ci_runs=[other])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "main_pushed")
        self.assertIsNone(orch.state.ci_run_id)

    def test_all_six_resume_phases_are_ordered(self):
        self.assertEqual(
            pr.PHASES,
            (
                "local_ready",
                "main_pushed",
                "ci_passed",
                "tag_pushed",
                "release_workflow_passed",
                "public_verified",
            ),
        )


# --- Tests: confirmation & dry-run -----------------------------------------


class ConfirmationTests(PublishReleaseTestCase):
    def test_confirmation_mismatch_aborts_without_mutation(self):
        scenario = Scenario(ahead=1)
        orch, *_ = make_orch(scenario, answer="not the phrase")
        with self.assertRaises(pr.ReleaseError) as ctx:
            orch.run()
        self.assertIn("confirmation did not match", str(ctx.exception))
        self.assertEqual(scenario.runner.mutating_commands(), [])

    def test_dry_run_performs_no_mutation_and_reports_plan(self):
        scenario = Scenario(ahead=1)
        orch, *_ = make_orch(scenario, dry_run=True)
        orch.run()
        self.assertEqual(scenario.runner.mutating_commands(), [])
        forbidden_prefixes = (
            ["docker", "pull"],
            ["docker", "rmi"],
            ["docker", "build"],
            ["gh", "run", "rerun"],
            ["gh", "attestation"],
            ["curl"],
        )
        for call in scenario.runner.calls:
            self.assertFalse(
                any(call[: len(prefix)] == prefix for prefix in forbidden_prefixes),
                f"dry-run reached a publication/installation command: {call}",
            )
            self.assertFalse(
                call[:1] == ["/bin/bash"] and "-n" not in call,
                f"dry-run executed an installer: {call}",
            )
        self.assertEqual(orch.state.phase, "local_ready")
        self.assertTrue(any("push main" in m for m in orch.planned_mutations))
        self.assertTrue(any("tag" in m for m in orch.planned_mutations))

    def test_dry_run_at_release_passed_has_no_mutations(self):
        sha = "a" * 40
        scenario = Scenario(
            pushed=True,
            remote_tag="d" * 40,
            ci_runs=[make_run(sha)],
            release_runs=[make_run(sha, databaseId=22, branch="v0.26.7")],
        )
        orch, *_ = make_orch(scenario, dry_run=True)
        orch.run()
        self.assertEqual(orch.planned_mutations, [])
        self.assertEqual(orch.state.phase, "release_workflow_passed")

    def test_confirmation_phrase_format(self):
        scenario = Scenario(ahead=1)
        orch, *_ = make_orch(scenario, answer="release v0.26.7 from " + scenario.sha[:12])
        # Correct phrase but no CI yet -> will time out waiting; use tiny timeout.
        orch.options.workflow_timeout_seconds = 1.0
        with self.assertRaises(pr.ReleaseError):
            orch.run()
        # The push must have happened (confirmation accepted) before CI wait.
        self.assertTrue(scenario.runner.has_argv("git", "push", "origin"))


# --- Tests: push behaviour -------------------------------------------------


class PushTests(PublishReleaseTestCase):
    def test_push_uses_explicit_refspec_and_verifies(self):
        scenario = Scenario(ahead=1, remote_main_responses=["a" * 40])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        orch._push_main()
        self.assertTrue(
            scenario.runner.has_argv("git", "push", "origin", f"{scenario.sha}:refs/heads/main")
        )

    def test_ambiguous_push_inspects_remote_then_retries(self):
        scenario = Scenario(
            ahead=1,
            push_responses=[R("", code=1), R("")],
            remote_main_responses=["b" * 40, "a" * 40],
        )
        orch, *_ = make_orch(scenario)
        orch._preflight()
        orch._push_main()  # must not raise
        pushes = [c for c in scenario.runner.commands("git") if "push" in c]
        self.assertEqual(len(pushes), 2)

    def test_ambiguous_push_remote_already_at_sha_no_retry(self):
        scenario = Scenario(
            ahead=1,
            push_responses=[R("", code=1)],
            remote_main_responses=["a" * 40],
        )
        orch, *_ = make_orch(scenario)
        orch._preflight()
        orch._push_main()
        pushes = [c for c in scenario.runner.commands("git") if "push" in c]
        self.assertEqual(len(pushes), 1)

    def test_push_failure_raises_when_remote_not_at_sha(self):
        scenario = Scenario(
            ahead=1,
            push_responses=[R("", code=1), R("", code=1)],
            remote_main_responses=["b" * 40, "b" * 40, "b" * 40],
        )
        orch, *_ = make_orch(scenario)
        orch._preflight()
        with self.assertRaises(pr.MutationError):
            orch._push_main()


# --- Tests: workflow waiting & failure handling ----------------------------


class WorkflowFailureTests(PublishReleaseTestCase):
    def _answer(self, scenario):
        return f"release v{scenario.version} from {scenario.sha[:12]}"

    def test_ci_failure_prevents_tag(self):
        scenario = Scenario(
            pushed=True,
            ci_runs=[make_run("a" * 40, conclusion="failure", databaseId=5)],
        )
        orch, *_ = make_orch(scenario, answer=self._answer(scenario))
        with self.assertRaises(pr.MutationError) as ctx:
            orch.run()
        self.assertIn("conclusion=failure", str(ctx.exception))
        # No tag may be created or pushed after a CI failure.
        self.assertFalse(
            any(c[:2] == ["git", "tag"] for c in scenario.runner.calls),
            "tag must not be created when CI failed",
        )
        # A transient failure is rerun exactly once before giving up.
        self.assertTrue(scenario.runner.has_argv("gh", "run", "rerun"))

    def test_post_tag_release_failure_never_moves_or_deletes_tag(self):
        scenario = Scenario(
            pushed=True,
            remote_tag="d" * 40,
            ci_runs=[make_run("a" * 40)],
            release_runs=[make_run("a" * 40, conclusion="failure", databaseId=22, branch="v0.26.7")],
        )
        orch, *_ = make_orch(scenario, answer=self._answer(scenario))
        with self.assertRaises(pr.MutationError):
            orch.run()
        git_calls = scenario.runner.commands("git")
        self.assertFalse(any("push" in c for c in git_calls), "no git push after tag")
        self.assertFalse(any("tag" in c for c in git_calls), "no tag mutation after tag")
        self.assertFalse(
            any("-d" in c for c in git_calls), "tag must never be deleted"
        )

    def test_workflow_timeout_raises(self):
        scenario = Scenario(pushed=True, ci_runs=[])  # never starts
        orch, clock, sleeper, *_ = make_orch(
            scenario, answer=self._answer(scenario), timeout=3.0, poll=1.0
        )
        with self.assertRaises(pr.MutationError) as ctx:
            orch.run()
        self.assertIn("timed out", str(ctx.exception))

    def test_workflow_discovery_retries_transient_gh_failure(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run("a" * 40)])
        predicate = lambda c: c[:2] == ["gh", "run"] and "list" in c and "ci.yml" in c
        scenario.runner.handlers.insert(
            0,
            (
                predicate,
                Sequence(R(code=1, err="temporary API failure"), R(json.dumps(scenario.ci_runs))),
            ),
        )
        orch, _, sleeper, *_ = make_orch(scenario)
        orch._preflight()
        self.assertEqual(orch._detect_phase(), "ci_passed")
        self.assertEqual(sleeper.sleeps, 1)

    def test_workflow_discovery_persistent_failure_is_bounded(self):
        scenario = Scenario(pushed=True)
        predicate = lambda c: c[:2] == ["gh", "run"] and "list" in c and "ci.yml" in c
        scenario.runner.handlers.insert(
            0, (predicate, R(code=1, err="persistent API failure"))
        )
        orch, _, sleeper, *_ = make_orch(scenario)
        orch._preflight()
        with self.assertRaises(pr.VerificationError) as ctx:
            orch._detect_phase()
        self.assertIn("after 3 attempts", str(ctx.exception))
        calls = [c for c in scenario.runner.calls if predicate(c)]
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeper.sleeps, 2)


# --- Tests: digest conflicts -----------------------------------------------


class DigestConflictTests(PublishReleaseTestCase):
    def test_candidate_manifest_schema_mismatch(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run("a" * 40)])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        bad = scenario.manifest()
        bad["schema"] = "evil"
        with self.assertRaises(pr.ReleaseError):
            orch._validate_candidate_manifest(bad)

    def test_candidate_manifest_requires_exact_v2_identity(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run("a" * 40)])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        mutations = (
            ("schema_version", 1),
            ("ci_run_id", 999),
            ("platforms", ["linux/amd64"]),
        )
        for field, value in mutations:
            bad = scenario.manifest()
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(pr.ReleaseError):
                orch._validate_candidate_manifest(bad)

        bad = scenario.manifest()
        bad["images"]["extra"] = dict(bad["images"]["base"])
        with self.assertRaises(pr.ReleaseError):
            orch._validate_candidate_manifest(bad)
        for field, value in (
            ("name", "ghcr.io/other/opencode"),
            ("tag", "latest"),
        ):
            bad = scenario.manifest()
            bad["images"]["opencode"][field] = value
            with self.subTest(image_field=field), self.assertRaises(pr.ReleaseError):
                orch._validate_candidate_manifest(bad)

    def test_candidate_manifest_source_sha_mismatch(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run("a" * 40)])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        bad = scenario.manifest()
        bad["source_sha"] = "f" * 40
        with self.assertRaises(pr.ReleaseError):
            orch._validate_candidate_manifest(bad)

    def test_candidate_manifest_missing_digest(self):
        scenario = Scenario(pushed=True, ci_runs=[make_run("a" * 40)])
        orch, *_ = make_orch(scenario)
        orch._preflight()
        bad = scenario.manifest()
        bad["images"]["base"]["digest"] = "not-a-digest"
        with self.assertRaises(pr.ReleaseError):
            orch._validate_candidate_manifest(bad)

    def test_version_tag_digest_conflict_fails_verification(self):
        scenario = Scenario(pushed=True)
        orch, *_ = make_orch(scenario)
        orch._preflight()
        orch.state.images = {name: "sha256:" + "9" * 64 for name in pr.IMAGE_NAMES}
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            with self.assertRaises(pr.VerificationError):
                orch._verify_image_version_digests()
        self.assertEqual(
            len(scenario.image_inspect_calls),
            1,
            "a successful immutable-tag read with a conflicting digest must fail immediately",
        )


class PublicVerificationResilienceTests(PublishReleaseTestCase):
    def _prepared(self, scenario):
        orch, clock, sleeper, *_ = make_orch(scenario)
        orch._preflight()
        orch.state.images = dict(scenario.digests)
        return orch, clock, sleeper

    def test_registry_reads_retry_transient_failures_then_succeed(self):
        scenario = Scenario(pushed=True)
        orch, _, sleeper = self._prepared(scenario)
        calls = 0

        def inspect(ref):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise pr.VerificationError("temporary GHCR 503")
            return json.loads(scenario.image_manifest_json(ref))

        orch.image_inspector = inspect
        self.assertIn("version tags match", orch._verify_image_version_digests())
        self.assertEqual(calls, 6)  # three base attempts, then one per leaf
        self.assertEqual(sleeper.sleeps, 2)

    def test_latest_digest_propagation_is_retried_but_bounded(self):
        scenario = Scenario(pushed=True)
        orch, _, sleeper = self._prepared(scenario)
        base_ref = f"{pr.GHCR_ROOT}/base:latest"
        attempts = 0

        def inspect(ref):
            nonlocal attempts
            manifest = json.loads(scenario.image_manifest_json(ref))
            if ref == base_ref:
                attempts += 1
                if attempts < 3:
                    manifest["digest"] = "sha256:" + "f" * 64
            return manifest

        orch.image_inspector = inspect
        self.assertIn("latest tags match", orch._verify_image_latest_digests())
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeper.sleeps, 2)

    def test_platform_index_propagation_is_retried(self):
        scenario = Scenario(pushed=True)
        orch, _, sleeper = self._prepared(scenario)
        base_ref = f"{pr.GHCR_ROOT}/base:{scenario.version}"
        attempts = 0

        def inspect(ref):
            nonlocal attempts
            manifest = json.loads(scenario.image_manifest_json(ref))
            if ref == base_ref:
                attempts += 1
                if attempts == 1:
                    manifest["manifests"] = manifest["manifests"][:1]
            return manifest

        orch.image_inspector = inspect
        self.assertIn("amd64+arm64", orch._verify_image_platforms())
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeper.sleeps, 1)

    def test_persistent_registry_failure_stops_after_fixed_attempt_count(self):
        scenario = Scenario(pushed=True)
        orch, _, sleeper = self._prepared(scenario)
        calls = 0

        def inspect(ref):
            nonlocal calls
            calls += 1
            raise pr.VerificationError("temporary GHCR 503")

        orch.image_inspector = inspect
        with self.assertRaises(pr.VerificationError) as ctx:
            orch._verify_image_version_digests()
        self.assertIn(f"after {pr.PUBLIC_RETRY_ATTEMPTS} attempts", str(ctx.exception))
        self.assertEqual(calls, pr.PUBLIC_RETRY_ATTEMPTS)
        self.assertEqual(sleeper.sleeps, pr.PUBLIC_RETRY_ATTEMPTS - 1)

    def test_anonymous_pull_timeout_retries_with_fixed_timeouts(self):
        scenario = Scenario(pushed=True)
        orch, _, sleeper = self._prepared(scenario)
        base_ref = f"{pr.GHCR_ROOT}/base:{scenario.version}"
        scenario.runner.handlers.insert(
            0,
            (
                eq("docker", "pull", base_ref),
                Sequence(R(code=124, err="command timed out"), R("pulled\n")),
            ),
        )

        self.assertIn("anonymous pull ok", orch._verify_anonymous_docker())
        records = [
            record
            for record in scenario.runner.call_records
            if record["argv"] == ["docker", "pull", base_ref]
        ]
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all(0 < record["timeout"] <= pr.ANONYMOUS_PULL_ATTEMPT_TIMEOUT for record in records)
        )
        self.assertEqual(sleeper.sleeps, 1)

    def test_anonymous_pull_persistent_failure_is_bounded(self):
        scenario = Scenario(pushed=True, anonymous_pull_ok=False)
        orch, _, sleeper = self._prepared(scenario)
        with self.assertRaises(pr.VerificationError) as ctx:
            orch._verify_anonymous_docker()
        self.assertIn(f"after {pr.ANONYMOUS_PULL_ATTEMPTS} attempts", str(ctx.exception))
        pulls = [c for c in scenario.runner.calls if c[:2] == ["docker", "pull"]]
        self.assertEqual(len(pulls), pr.ANONYMOUS_PULL_ATTEMPTS)
        self.assertEqual(sleeper.sleeps, pr.ANONYMOUS_PULL_ATTEMPTS - 1)


# --- Tests: locking & state journal ----------------------------------------


class LockingTests(PublishReleaseTestCase):
    def test_lock_is_exclusive(self):
        scenario = Scenario()
        orch1, *_ = make_orch(scenario)
        orch2, *_ = make_orch(scenario)
        orch1._acquire_lock()
        try:
            with self.assertRaises(pr.ReleaseError) as ctx:
                orch2._acquire_lock()
            self.assertIn("lock", str(ctx.exception))
        finally:
            orch1._release_lock()
        # After release, a second process can acquire it.
        orch2._acquire_lock()
        orch2._release_lock()

    def test_state_journal_is_atomic_private_and_valid(self):
        scenario = Scenario(ahead=1)
        orch, *_ = make_orch(scenario, dry_run=True)
        orch.run()
        state_file = scenario.state_dir / "v0.26.7.json"
        self.assertTrue(state_file.is_file())
        mode = state_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        data = json.loads(state_file.read_text())
        self.assertEqual(data["schema"], pr.STATE_SCHEMA)
        self.assertEqual(data["version"], "0.26.7")
        self.assertEqual(data["commit_sha"], scenario.sha)
        dir_mode = scenario.state_dir.stat().st_mode & 0o777
        self.assertEqual(dir_mode, 0o700)

    def test_matching_resume_hint_restores_only_observability_evidence(self):
        scenario = Scenario(pushed=True)
        orch, *_ = make_orch(scenario)
        orch._preflight()
        hint = pr.ReleaseState(
            version=scenario.version,
            commit_sha=scenario.sha,
            tag=scenario.tag,
            phase="public_verified",
            ci_run_id=999,
            images={"base": "sha256:" + "f" * 64},
            phase_durations={"ci_passed": 12.5, "public_verified": 7.25},
            checks=[
                {
                    "name": "anonymous-docker",
                    "status": "failed",
                    "duration_seconds": 7.0,
                    "detail": "temporary failure",
                }
            ],
        )

        orch._restore_observability_hint(hint)

        self.assertEqual(orch.state.phase_durations["ci_passed"], 12.5)
        self.assertEqual(orch.state.checks[0]["detail"], "temporary failure")
        self.assertIsNone(orch.state.ci_run_id)
        self.assertEqual(orch.state.images, {})
        self.assertEqual(orch.state.phase, "local_ready")

    def test_mismatched_resume_hint_is_ignored(self):
        scenario = Scenario(pushed=True)
        orch, *_ = make_orch(scenario)
        orch._preflight()
        hint = pr.ReleaseState(
            version=scenario.version,
            commit_sha="f" * 40,
            tag=scenario.tag,
            phase_durations={"ci_passed": 99},
        )
        orch._restore_observability_hint(hint)
        self.assertEqual(orch.state.phase_durations, {})

    def test_failed_phase_time_is_accumulated_for_the_next_resume(self):
        scenario = Scenario(pushed=True)
        orch, clock, *_ = make_orch(scenario)
        orch._preflight()
        orch.state.phase_durations["public_verified"] = 2.5

        def fail_verification():
            clock.advance(3.0)
            raise pr.VerificationError("temporary public failure")

        orch._public_verify = fail_verification
        with self.assertRaises(pr.VerificationError):
            orch._execute("release_workflow_passed")
        self.assertEqual(orch.state.phase_durations["public_verified"], 5.5)

    def test_version_one_state_hint_remains_readable(self):
        scenario = Scenario()
        orch, *_ = make_orch(scenario)
        orch._acquire_lock()
        try:
            orch.state.version = scenario.version
            orch.state.commit_sha = scenario.sha
            orch.state.tag = scenario.tag
            state_file = scenario.state_dir / f"{scenario.tag}.json"
            state_file.write_text(
                json.dumps(
                    {
                        "schema": pr.STATE_SCHEMA,
                        "schema_version": 1,
                        "version": scenario.version,
                        "commit_sha": scenario.sha,
                        "tag": scenario.tag,
                        "phase": "release_workflow_passed",
                        "phase_durations": {"ci_passed": 4.0},
                    }
                ),
                encoding="utf-8",
            )
            hint = orch._load_state_hint()
        finally:
            orch._release_lock()
        self.assertIsNotNone(hint)
        self.assertEqual(hint.phase_durations["ci_passed"], 4.0)

    def test_non_object_state_hint_is_ignored(self):
        scenario = Scenario()
        orch, *_ = make_orch(scenario)
        orch._acquire_lock()
        try:
            orch.state.tag = scenario.tag
            state_file = scenario.state_dir / f"{scenario.tag}.json"
            state_file.write_text("[]", encoding="utf-8")
            self.assertIsNone(orch._load_state_hint())
        finally:
            orch._release_lock()


# --- Tests: redaction & logs -----------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_redact_scrubs_common_secret_shapes(self):
        samples = [
            "token=gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "Authorization: Bearer abcdef1234567890xyz",
            "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "-----BEGIN RSA PRIVATE KEY-----",
        ]
        for sample in samples:
            self.assertNotIn("ABCDEF", pr.redact(sample).replace("[REDACTED]", ""))
        self.assertIn("[REDACTED]", pr.redact("Authorization: Bearer abcdef1234567890xyz"))

    def test_bounded_truncates(self):
        text = "x" * 10000
        out = pr.bounded(text, limit=100)
        self.assertLessEqual(len(out), 200)
        self.assertIn("truncated", out)

    def test_log_file_redacts_secrets(self):
        scenario = Scenario()
        orch, *_ = make_orch(scenario)
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            orch._log_path = Path(td) / "log.txt"
            orch.log("using token gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 now")
            content = orch._log_path.read_text()
            self.assertNotIn("gho_ABCDEF", content)
            self.assertIn("[REDACTED]", content)

    def test_check_json_includes_bounded_redacted_detail(self):
        secret = "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        payload = pr.CheckResult(
            "anonymous-docker",
            "failed",
            1.25,
            f"pull failed with {secret} " + "x" * 2000,
        ).to_json()
        self.assertIn("detail", payload)
        self.assertIn("[REDACTED]", payload["detail"])
        self.assertNotIn(secret, payload["detail"])
        self.assertIn("truncated", payload["detail"])


# --- Tests: structured JSON output -----------------------------------------


class JsonOutputTests(PublishReleaseTestCase):
    def test_json_single_object_without_secrets(self):
        sha = "a" * 40
        scenario = Scenario(
            pushed=True,
            remote_tag="d" * 40,
            release_exists=True,
            ci_runs=[make_run(sha)],
            release_runs=[make_run(sha, databaseId=22, branch="v0.26.7")],
        )
        orch, *_ = make_orch(scenario, dry_run=True, json_output=True)
        orch.run()
        orch.render_json()
        stdout = "\n".join(orch._out.__self__ if False else [])  # placeholder
        # out_lines were captured by the factory; re-render into a fresh collector.
        captured = []
        orch._out = captured.append
        orch.render_json()
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertEqual(payload["schema"], pr.RESULT_SCHEMA)
        self.assertEqual(payload["schema_version"], pr.RESULT_SCHEMA_VERSION)
        self.assertEqual(payload["repository"], "Sindycate/cage")
        self.assertEqual(payload["version"], "0.26.7")
        self.assertEqual(payload["tag"], "v0.26.7")
        self.assertEqual(payload["commit_sha"], sha)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["phase"], "release_workflow_passed")
        self.assertEqual(payload["ci"]["conclusion"], "success")
        self.assertEqual(payload["release"]["conclusion"], "success")
        for name in pr.IMAGE_NAMES:
            self.assertTrue(payload["images"][name]["digest"].startswith("sha256:"))
        # No secret material may appear in the JSON object.
        self.assertNotIn("gho_", captured[0])
        self.assertNotIn("Authorization", captured[0])

    def test_error_json_contains_phase_timing_and_verification_diagnostics(self):
        scenario = Scenario(pushed=True)
        orch, *_ = make_orch(scenario, json_output=True)
        orch.context = pr.ReleaseContext(
            repository=pr.REPOSITORY,
            commit_sha=scenario.sha,
            version=scenario.version,
            tag=scenario.tag,
        )
        orch.state.version = scenario.version
        orch.state.commit_sha = scenario.sha
        orch.state.tag = scenario.tag
        orch.state.phase = "release_workflow_passed"
        orch.state.phase_durations = {"public_verified": 12.5}
        orch.verification_checks = [
            pr.CheckResult(
                "anonymous-docker",
                "failed",
                12.5,
                "pull timed out with gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            )
        ]
        captured = []
        orch._out = captured.append

        orch.render_error_json("public verification failed")

        payload = json.loads(captured[0])
        self.assertEqual(payload["phase_durations"]["public_verified"], 12.5)
        self.assertEqual(payload["checks"][0]["status"], "failed")
        self.assertIn("timed out", payload["checks"][0]["detail"])
        self.assertNotIn("gho_", captured[0])


# --- Tests: review findings (adversarial) ----------------------------------


class ReviewFindingTests(PublishReleaseTestCase):
    """Adversarial coverage for the issue #6 review findings."""

    def _prepared_orch(self, scenario):
        orch, *_ = make_orch(scenario)
        orch._preflight()
        orch.state.images = dict(scenario.digests)
        return orch

    # [P0] image attestation verification must use an oci:// image reference.
    def test_image_attestation_verification_uses_oci_reference(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        orch._verify_image_attestations()  # attestation_ok=True by default
        verify_calls = [
            c for c in scenario.runner.commands("gh")
            if c[:3] == ["gh", "attestation", "verify"]
        ]
        self.assertTrue(verify_calls, "no gh attestation verify calls recorded")
        refs = [c[3] for c in verify_calls]
        for name in pr.IMAGE_NAMES:
            digest = scenario.digests[name]
            self.assertIn(f"oci://ghcr.io/sindycate/cage/{name}@{digest}", refs)
        for ref in refs:
            self.assertFalse(
                ref.startswith("ghcr.io/"),
                f"image attestation reference missing oci:// prefix: {ref}",
            )

    def test_source_provenance_and_spdx_sbom_attestations_are_both_verified(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            archive = Path(td) / orch._archive_name()
            archive.write_bytes(scenario.archive_bytes)
            orch._verify_source_attestation()
            orch._verify_source_sbom_attestation()

        calls = [
            c
            for c in scenario.runner.commands("gh")
            if c[:3] == ["gh", "attestation", "verify"]
        ]
        self.assertEqual(len(calls), 2)
        provenance = next(c for c in calls if "--predicate-type" not in c)
        sbom = next(c for c in calls if "--predicate-type" in c)
        self.assertIn(str(archive), provenance)
        self.assertIn(str(archive), sbom)
        self.assertEqual(
            sbom[sbom.index("--predicate-type") + 1],
            "https://spdx.dev/Document/v2.3",
        )
        for call in calls:
            self.assertIn("--source-digest", call)
            self.assertIn(scenario.sha, call)
            self.assertIn("--source-ref", call)
            self.assertIn(f"refs/tags/{scenario.tag}", call)

    # [P1] the public installer must be fetched anonymously, not run from the
    # local checkout, and without any ambient credentials.
    def test_public_installer_is_fetched_anonymously_not_from_checkout(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            (Path(td) / orch._archive_name()).write_bytes(scenario.archive_bytes)
            detail = orch._verify_public_installer()
        self.assertIn("public installer", detail)
        self.assertFalse(
            scenario.install_dir_preexisted,
            "public installer destination must not be pre-created",
        )

        fetch_calls = [
            c for c in scenario.runner.commands("curl")
            if any(
                part.endswith("/install.sh") and "raw.githubusercontent.com" in part
                for part in c
            )
        ]
        self.assertTrue(fetch_calls, "installer not fetched from the public raw URL")
        fetch = fetch_calls[0]
        self.assertIn("-q", fetch)
        self.assertEqual(fetch[0], "curl")
        self.assertEqual(fetch[1], "-q", "-q must be the first curl option")
        self.assertIn(
            f"https://raw.githubusercontent.com/Sindycate/cage/v{scenario.version}/install.sh",
            fetch,
        )

        bash_calls = [
            c for c in scenario.runner.commands("/bin/bash")
            if any("install.sh" in part for part in c)
        ]
        self.assertTrue(bash_calls)
        installer_path = bash_calls[0][1]
        self.assertNotIn(
            str(scenario.repo_root), installer_path,
            "installer must not be executed from the local checkout",
        )

        env = scenario.runner.last_env
        for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                    "GITHUB_ENTERPRISE_TOKEN", "GEMFURY_TOKEN"):
            self.assertNotIn(var, env)
        self.assertIn("GH_CONFIG_DIR", env)
        self.assertIn("CURL_HOME", env)
        self.assertEqual(env.get("CAGE_VERSION"), scenario.version)

    def test_anonymous_asset_download_records_full_machine_readable_digests(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            orch._verify_anonymous_download()
        self.assertEqual(
            set(orch.state.assets),
            {orch._archive_name(), orch._checksum_name(), orch._spdx_name()},
        )
        for entry in orch.state.assets.values():
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(entry["size"], 0)

        captured = []
        orch._out = captured.append
        orch.render_json()
        payload = json.loads(captured[0])
        self.assertEqual(payload["assets"], orch.state.assets)

    def test_image_verification_does_not_require_docker_buildx(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        orch._verify_image_version_digests()
        orch._verify_image_latest_digests()
        orch._verify_image_platforms()

        self.assertEqual(len(scenario.image_inspect_calls), len(pr.IMAGE_NAMES) * 3)
        self.assertFalse(
            any(call[:2] == ["docker", "buildx"] for call in scenario.runner.calls),
            "public image verification must not require the optional buildx plugin",
        )

    def test_public_installer_failure_preserves_stdout_and_stderr(self):
        scenario = Scenario(pushed=True)
        scenario.runner.handlers.insert(
            0,
            (
                lambda c: c and c[0] == "/bin/bash" and any("install.sh" in p for p in c),
                R("installer stdout", code=1, err="installer stderr"),
            ),
        )
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            (Path(td) / orch._archive_name()).write_bytes(scenario.archive_bytes)
            with self.assertRaises(pr.VerificationError) as ctx:
                orch._verify_public_installer()
        self.assertIn("installer stdout", str(ctx.exception))
        self.assertIn("installer stderr", str(ctx.exception))

    # [P1] malformed public artifacts must become structured failed checks, not
    # tracebacks. A malformed SPDX delivered through an otherwise successful
    # download must fail the spdx-parses check (JSONDecodeError captured) and fail
    # the overall verification closed, naming the gate.
    def test_malformed_spdx_is_structured_failed_check_not_traceback(self):
        scenario = Scenario(pushed=True, release_exists=True)
        orch = self._prepared_orch(scenario)

        def curl(argv):
            if "-o" in argv:
                dest = Path(argv[argv.index("-o") + 1])
                url = argv[-1]
                if url.endswith(".tar.gz"):
                    dest.write_bytes(scenario.archive_bytes)
                elif url.endswith(".sha256"):
                    dest.write_text(
                        f"{scenario.archive_digest}  cage-{scenario.version}.tar.gz\n",
                        encoding="utf-8",
                    )
                elif url.endswith(".spdx.json"):
                    dest.write_text("{ this is not valid json", encoding="utf-8")
                elif url.endswith(".sh"):
                    dest.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
                else:
                    dest.write_bytes(b"")
            return pr.CommandResult(argv, 0, "", "")

        scenario.runner.handlers.insert(0, (lambda c: c[:1] == ["curl"], curl))

        with self.assertRaises(pr.VerificationError) as ctx:
            orch._public_verify()
        statuses = {c.name: c.status for c in orch.verification_checks}
        self.assertEqual(statuses["anonymous-download"], "passed")
        self.assertEqual(statuses["spdx-parses"], "failed")
        spdx_check = next(c for c in orch.verification_checks if c.name == "spdx-parses")
        self.assertIn("JSONDecodeError", spdx_check.detail)
        self.assertIn("spdx-parses", str(ctx.exception))

    # [P1] a failed prerequisite gates dependent checks (skipped) and the overall
    # verification still fails closed, naming the failed gate.
    def test_failed_download_skips_dependent_checks_and_fails_closed(self):
        scenario = Scenario(pushed=True, release_exists=True)
        orch = self._prepared_orch(scenario)

        def failing_curl(argv):
            return pr.CommandResult(argv, 22, "", "curl: (22) HTTP 404")

        scenario.runner.handlers.insert(0, (lambda c: c[:1] == ["curl"], failing_curl))

        with self.assertRaises(pr.VerificationError) as ctx:
            orch._public_verify()
        statuses = {c.name: c.status for c in orch.verification_checks}
        self.assertEqual(statuses["anonymous-download"], "failed")
        for dependent in ("archive-checksum", "spdx-parses", "archive-reproducible",
                          "source-attestation", "source-sbom-attestation",
                          "public-installer"):
            self.assertEqual(statuses[dependent], "skipped", dependent)
        self.assertIn("anonymous-download", str(ctx.exception))

    # [P2] anonymous GHCR verification must perform a real pull with fresh creds.
    def test_anonymous_docker_check_performs_real_pull_with_fresh_creds(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        detail = orch._verify_anonymous_docker()
        self.assertIn("anonymous pull ok", detail)
        docker_calls = scenario.runner.commands("docker")
        pulls = [c for c in docker_calls if c[:2] == ["docker", "pull"]]
        self.assertEqual(len(pulls), len(pr.IMAGE_NAMES))
        for name in pr.IMAGE_NAMES:
            self.assertIn(
                ["docker", "pull", f"ghcr.io/sindycate/cage/{name}:{scenario.version}"],
                pulls,
            )
        env = scenario.runner.last_env
        self.assertIn("DOCKER_CONFIG", env)
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(var, env)

    def test_anonymous_docker_pull_failure_fails_closed(self):
        scenario = Scenario(pushed=True, anonymous_pull_ok=False)
        orch = self._prepared_orch(scenario)
        with self.assertRaises(pr.VerificationError) as ctx:
            orch._verify_anonymous_docker()
        self.assertIn("anonymous pull failed", str(ctx.exception))

    # [P2] reproducibility must rebuild from the recorded commit, not the live
    # checkout.
    def test_archive_reproducibility_rebuilds_from_recorded_commit_not_checkout(self):
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            (Path(td) / orch._archive_name()).write_bytes(scenario.archive_bytes)
            detail = orch._verify_archive_reproducible()
        self.assertIn("byte-identical", detail)
        archive_calls = [
            c for c in scenario.runner.commands("git") if c[:2] == ["git", "archive"]
        ]
        self.assertTrue(archive_calls, "git archive was not used to materialize the commit")
        self.assertIn(scenario.sha, archive_calls[0])
        build_calls = [
            c for c in scenario.runner.calls
            if c and c[0] == sys.executable and any("build-release.py" in part for part in c)
        ]
        self.assertTrue(build_calls)
        packager_arg = build_calls[-1][1]
        self.assertIn("build-release.py", packager_arg)
        self.assertNotIn(
            str(scenario.repo_root), packager_arg,
            "packager must run from the materialized commit, not the live checkout",
        )

    # [P1] safe_extract_tar must preserve permission bits (minus special bits)
    # so executable files survive commit reconstruction.
    def test_safe_extract_tar_preserves_modes_and_strips_special_bits(self):
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tar = td / "a.tar"
            with tarfile.open(tar, "w") as t:
                for name, mode, kind in (
                    ("bin/run", 0o755, "f"),
                    ("readme", 0o644, "f"),
                    ("suid", 0o4755, "f"),
                    # umask noise that git archive can introduce must canonicalize
                    ("grpwrite", 0o664, "f"),
                    ("grpexec", 0o775, "f"),
                    ("d", 0o755, "d"),
                ):
                    info = tarfile.TarInfo(name)
                    info.mode = mode
                    if kind == "d":
                        info.type = tarfile.DIRTYPE
                        t.addfile(info)
                    else:
                        info.size = 1
                        t.addfile(info, io.BytesIO(b"x"))
            out = td / "out"
            out.mkdir()
            pr.safe_extract_tar(tar, out)
            self.assertEqual((out / "bin" / "run").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((out / "readme").stat().st_mode & 0o7777, 0o644)
            # setuid/setgid/sticky bits are stripped; only safe perms remain.
            self.assertEqual((out / "suid").stat().st_mode & 0o7777, 0o755)
            # group/other-write umask noise is canonicalized by the executable bit.
            self.assertEqual((out / "grpwrite").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((out / "grpexec").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((out / "d").stat().st_mode & 0o7777, 0o755)

    # [P1] end-to-end with REAL git and the REAL packager: a commit materialized
    # via git archive + safe_extract_tar and rebuilt by build-release.py must be
    # byte-identical to an archive built from a canonical-mode checkout of the
    # same commit. Without mode preservation the executable `cage`/`install.sh`
    # become 0644 and the rebuilt archive differs.
    def test_archive_reconstruction_is_byte_identical_with_real_git(self):
        import importlib.util as _ilu

        spec = _ilu.spec_from_file_location(
            "build_release_mod", ROOT / "scripts" / "build-release.py"
        )
        br = _ilu.module_from_spec(spec)
        spec.loader.exec_module(br)

        # Canonical modes from the source repo's git index (100755 / 100644).
        ls = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-s"],
            capture_output=True, text=True, check=True,
        ).stdout
        index_mode = {}
        for line in ls.splitlines():
            meta, path = line.split("	", 1)
            index_mode[path] = 0o755 if meta.split()[0] == "100755" else 0o644

        base = Path(tempfile.mkdtemp(prefix="cage-recon-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        repo = base / "repo"
        repo.mkdir()

        def place_file(rel):
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dst)
            os.chmod(dst, index_mode.get(rel, 0o644))

        for rel in br.PAYLOAD_FILES:
            place_file(rel)
        place_file("scripts/build-release.py")  # required to rebuild from the tree
        for directory in br.PAYLOAD_DIRS:
            for path in sorted((ROOT / directory).rglob("*")):
                if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                    continue
                rel = path.relative_to(ROOT).as_posix()
                if path.is_dir():
                    (repo / rel).mkdir(parents=True, exist_ok=True)
                else:
                    dst = repo / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dst)
                    os.chmod(dst, index_mode.get(rel, 0o644))
        # Release checkouts use umask 022; normalize every directory to 0755.
        for d in sorted([repo] + [x for x in repo.rglob("*") if x.is_dir()]):
            os.chmod(d, 0o755)

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "release@example.com")
        git("config", "user.name", "Release Bot")
        git("config", "commit.gpgsign", "false")
        git("add", "-A")
        git("commit", "-q", "-m", "payload")
        sha = git("rev-parse", "HEAD").stdout.strip()
        epoch = git("show", "-s", "--format=%ct", "HEAD").stdout.strip()
        version = (repo / "cage").read_text().split('CAGE_VERSION="')[1].split('"')[0]

        # Ground-truth archive from the canonical-mode working tree.
        gt_dir = base / "gt"
        gt_dir.mkdir()
        env = dict(os.environ, SOURCE_DATE_EPOCH=epoch)
        subprocess.run(
            [sys.executable, str(repo / "scripts" / "build-release.py"), version, str(gt_dir)],
            cwd=str(repo), env=env, check=True, capture_output=True, text=True,
        )
        published = (gt_dir / f"cage-{version}.tar.gz").read_bytes()

        # Reconstruct from the recorded commit through the real orchestrator path.
        clock = FakeClock()
        orch = pr.Orchestrator(
            pr.Options(repo_root=repo),
            runner=pr.SubprocessRunner(),
            clock=clock,
            sleeper=FakeSleeper(clock),
            prompter=FakePrompter(),
            out=lambda line: None,
            err=lambda line: None,
        )
        orch.context.version = version
        orch.context.commit_sha = sha
        with tempfile.TemporaryDirectory() as vd:
            orch._verify_dir = Path(vd)
            (Path(vd) / f"cage-{version}.tar.gz").write_bytes(published)
            detail = orch._verify_archive_reproducible()
        self.assertIn("byte-identical", detail)

    # [P1] a fake runner accepts any argv, so validate the actual curl commands
    # the code builds against the REAL curl argument parser. Regression guard for
    # the invalid `curl --no-config` option (curl requires first-position `-q`).
    def test_curl_invocations_use_options_accepted_by_real_curl(self):
        if shutil.which("curl") is None:
            self.skipTest("curl not installed")

        # Control: the parser must actually reject an invalid option, otherwise
        # this test would pass vacuously.
        control = subprocess.run(
            ["curl", "--no-config", "-fsS", "http://127.0.0.1:1/"],
            capture_output=True, text=True,
        )
        self.assertIn("curl: option", control.stderr)

        def options_accepted(argv):
            # Swap the URL (last arg) for an unroutable address so no real
            # download happens; assert only that curl's parser accepts the options.
            probe = list(argv)
            probe[-1] = "http://127.0.0.1:1/"
            r = subprocess.run(probe, capture_output=True, text=True, timeout=30)
            self.assertNotIn(
                "curl: option", r.stderr,
                f"curl rejected options in {argv}: {r.stderr}",
            )

        # Anonymous asset download (curl_download).
        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        with tempfile.TemporaryDirectory() as td:
            orch.curl_download("https://example.invalid/asset.tar.gz", Path(td) / "a")
        download_calls = scenario.runner.commands("curl")
        self.assertTrue(download_calls)
        for argv in download_calls:
            self.assertEqual(argv[0], "curl")
            self.assertEqual(argv[1], "-q", f"-q must be the first option: {argv}")
            options_accepted(argv)

        # Public installer fetch.
        scenario2 = Scenario(pushed=True)
        orch2 = self._prepared_orch(scenario2)
        with tempfile.TemporaryDirectory() as td:
            orch2._verify_dir = Path(td)
            (Path(td) / orch2._archive_name()).write_bytes(scenario2.archive_bytes)
            orch2._verify_public_installer()
        fetch_calls = [
            c for c in scenario2.runner.commands("curl")
            if any("raw.githubusercontent.com" in part for part in c)
        ]
        self.assertTrue(fetch_calls)
        for argv in fetch_calls:
            self.assertEqual(argv[1], "-q", f"-q must be the first option: {argv}")
            options_accepted(argv)

    # [P1] the payload comparison must reject malicious archives instead of
    # falling back to an unfiltered extraction.
    def test_payload_comparison_rejects_malicious_archive(self):
        import io
        import tarfile

        scenario = Scenario(pushed=True)
        orch = self._prepared_orch(scenario)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))
        with tempfile.TemporaryDirectory() as td:
            orch._verify_dir = Path(td)
            (Path(td) / orch._archive_name()).write_bytes(raw.getvalue())
            with tempfile.TemporaryDirectory() as inst:
                with self.assertRaises(pr.VerificationError):
                    orch._assert_installed_payload_matches_archive(Path(inst))


# --- Tests: end-to-end ordering with a real bare Git remote ----------------


def build_real_repo(testcase, version="0.26.7"):
    base = Path(tempfile.mkdtemp(prefix="cage-e2e-"))
    testcase.addCleanup(shutil.rmtree, base, ignore_errors=True)
    repo = base / "repo"
    # Place the bare remote at a github.com/Sindycate/cage.git-shaped path so the
    # origin URL still normalizes to Sindycate/cage during preflight.
    remote = base / "github.com" / "Sindycate" / "cage.git"
    remote.parent.mkdir(parents=True, exist_ok=True)

    def git(*args, cwd=repo, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
        )

    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git("config", "user.email", "release@example.com")
    git("config", "user.name", "Release Bot")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    (repo / "cage").write_text(f'#!/bin/bash\nCAGE_VERSION="{version}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n\n- base\n", encoding="utf-8")
    (repo / "install.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    # The reproducibility check materializes the recorded commit via git archive
    # and rebuilds from scripts/build-release.py, so the fixture must ship it.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build-release.py").write_text(
        "#!/usr/bin/env python3\n# release packager (e2e fixture stub)\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "main")
    git("branch", "--set-upstream-to=origin/main", "main")
    # Second (release) commit, left unpushed so local main is ahead by one.
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {version}\n\n- release change\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "release")
    sha = git("rev-parse", "HEAD").stdout.strip()
    return repo, remote, sha


class EndToEndTests(PublishReleaseTestCase):
    def test_end_to_end_command_ordering_with_bare_remote(self):
        version = "0.26.7"
        repo, remote, sha = build_real_repo(self, version=version)
        scenario = Scenario(
            version=version,
            sha=sha,
            real_git=True,
            repo_root=repo,
            ahead=1,
            release_exists=True,
            ci_runs=[make_run(sha)],
            release_runs=[make_run(sha, databaseId=22, branch=f"v{version}")],
        )
        answer = f"release v{version} from {sha[:12]}"
        orch, *_ = make_orch(scenario, answer=answer)
        orch.run()
        self.assertEqual(orch.state.phase, "public_verified")

        calls = scenario.runner.calls

        def index_of(predicate):
            for i, call in enumerate(calls):
                if predicate(call):
                    return i
            return -1

        push_main = index_of(lambda c: c == ["git", "push", "origin", f"{sha}:refs/heads/main"])
        ci_list = index_of(lambda c: c[:2] == ["gh", "run"] and "list" in c and "ci.yml" in c)
        manifest = index_of(lambda c: c[:3] == ["gh", "run", "download"])
        tag_create = index_of(lambda c: c[:2] == ["git", "tag"] and "-a" in c)
        tag_push = index_of(lambda c: c[:4] == ["git", "push", "origin", f"refs/tags/v{version}"])
        release_list = index_of(lambda c: c[:2] == ["gh", "run"] and "list" in c and "release.yml" in c)

        for name, value in {
            "push_main": push_main,
            "ci_list": ci_list,
            "manifest": manifest,
            "tag_create": tag_create,
            "tag_push": tag_push,
            "release_list": release_list,
        }.items():
            self.assertGreaterEqual(value, 0, f"missing command: {name}")

        self.assertLess(push_main, ci_list)
        self.assertLess(ci_list, manifest)
        self.assertLess(manifest, tag_create)
        self.assertLess(tag_create, tag_push)
        self.assertLess(tag_push, release_list)

        # The real bare remote received main and the immutable tag.
        ls = subprocess.run(
            ["git", "ls-remote", str(remote)], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn(f"{sha}\trefs/heads/main", ls)
        self.assertIn(f"refs/tags/v{version}", ls)
