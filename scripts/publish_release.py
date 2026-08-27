#!/usr/bin/env python3
"""Deterministic, resumable Cage release automation (maintainer-only).

This command validates a prepared release commit, asks for one explicit
confirmation, pushes ``main`` if needed, waits for the exact commit's CI run,
pushes an immutable annotated version tag, waits for publication, and
independently verifies the public release.

It is maintainer tooling only. It is intentionally not part of the end-user
``cage`` CLI and is excluded from the release archive (``scripts/`` is not in
the archive payload).

Standard library only; requires Python 3.12+.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# --- Constants -------------------------------------------------------------

REPOSITORY = "Sindycate/cage"
GHCR_ROOT = "ghcr.io/sindycate/cage"
IMAGE_NAMES: tuple[str, ...] = ("base", "claude-code", "codex", "opencode", "token-monitor")
IMAGE_DOCKERFILE = {
    "base": "Dockerfile.base",
    "claude-code": "Dockerfile",
    "codex": "Dockerfile.codex",
    "opencode": "Dockerfile.opencode",
    "token-monitor": "Dockerfile.monitor",
}
PHASES: tuple[str, ...] = (
    "local_ready",
    "main_pushed",
    "ci_passed",
    "tag_pushed",
    "release_workflow_passed",
    "public_verified",
)
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
CI_WORKFLOW_FILE = "ci.yml"
RELEASE_WORKFLOW_FILE = "release.yml"
CI_SIGNER_WORKFLOW = f"{REPOSITORY}/{CI_WORKFLOW_PATH}"
RELEASE_SIGNER_WORKFLOW = f"{REPOSITORY}/{RELEASE_WORKFLOW_PATH}"
CANDIDATE_ARTIFACT_PREFIX = "release-candidate-"
CANDIDATE_SCHEMA = "cage.release-candidate"
RESULT_SCHEMA = "cage.release-result"
RESULT_SCHEMA_VERSION = 2
STATE_SCHEMA = "cage.release-state"
STATE_SCHEMA_VERSION = 2
REQUIRED_EXECUTABLES = ("git", "gh", "docker", "curl")
REQUIRED_BASH = "/bin/bash"
CONFIRMATION_HINT = "release v{version} from {short_sha}"
DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_WORKFLOW_TIMEOUT = 3600.0  # 60 minutes, accommodates cold CI
DEFAULT_EXTERNAL_COMMAND_TIMEOUT = 120.0
PUBLIC_RETRY_ATTEMPTS = 5
PUBLIC_RETRY_DELAY_SECONDS = 5.0
ANONYMOUS_PULL_ATTEMPTS = 2
ANONYMOUS_PULL_ATTEMPT_TIMEOUT = 300.0
ANONYMOUS_PULL_TOTAL_TIMEOUT = 600.0
ANONYMOUS_PULL_CLEANUP_TIMEOUT = 60.0
MAX_LOG_TAIL_CHARS = 4000
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GHCR_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
MAX_REGISTRY_RESPONSE_BYTES = 4 * 1024 * 1024

_SECRET_PATTERNS = (
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"ghu_[A-Za-z0-9]{20,}"),
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    re.compile(r"ghr_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)authorization\s*[:=].*"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)token\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class _RejectRegistryRedirects(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding the short-lived bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise VerificationError(f"GHCR registry request redirected with HTTP {code}")


def redact(text: str) -> str:
    """Scrub obvious credential material from text destined for logs/output."""
    if not text:
        return ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def bounded(text: str, limit: int = MAX_LOG_TAIL_CHARS) -> str:
    text = redact(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


# --- Errors ----------------------------------------------------------------


class ReleaseError(Exception):
    """A controlled, user-facing failure. Message must be secret-free."""


class PreflightError(ReleaseError):
    pass


class MutationError(ReleaseError):
    pass


class VerificationError(ReleaseError):
    pass


# --- Anonymous-operation helpers -------------------------------------------

# Environment variables that could grant privileged access to GitHub/GHCR or
# otherwise leak ambient credentials into checks that must prove anonymous
# public access. They are stripped before running anonymous curl/docker/installer
# verification. ``GH_CONFIG_DIR`` is stripped so ``gh`` cannot fall back to a
# maintainer's authenticated configuration.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GEMFURY_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
)


def anonymous_env(base_env: dict, **overrides: str) -> dict:
    """Return ``base_env`` minus credential variables, then apply ``overrides``.

    Used for verification that must demonstrate anonymous public access. The
    overrides typically pin ``HOME``/``CURL_HOME``/``DOCKER_CONFIG`` to fresh
    temporary directories so no ambient curlrc, netrc, Docker credentials, or gh
    configuration can influence the result.
    """
    env = {key: value for key, value in base_env.items() if key not in CREDENTIAL_ENV_VARS}
    env.update(overrides)
    return env


def inspect_public_ghcr_manifest(ref: str, *, opener=None) -> dict:
    """Fetch a public GHCR manifest index without Docker or ambient credentials.

    GHCR's response header is the authoritative digest of the requested tag; the
    response body supplies the platform descriptors. The registry token is
    requested anonymously and retained only in memory for this call.
    """
    prefix = "ghcr.io/"
    if not ref.startswith(prefix):
        raise VerificationError(f"unsupported registry reference: {ref}")
    remainder = ref[len(prefix) :]
    if "@" in remainder:
        repository, reference = remainder.rsplit("@", 1)
    else:
        repository, separator, reference = remainder.rpartition(":")
        if not separator:
            raise VerificationError(f"registry reference has no tag or digest: {ref}")
    if (
        not repository
        or not reference
        or any(part in ("", ".", "..") for part in repository.split("/"))
    ):
        raise VerificationError(f"invalid registry reference: {ref}")

    client = opener or urllib.request.build_opener(_RejectRegistryRedirects())

    def read_json(request: urllib.request.Request, label: str) -> tuple[dict, object]:
        try:
            response = client.open(request, timeout=30)
            with response:
                payload = response.read(MAX_REGISTRY_RESPONSE_BYTES + 1)
                if len(payload) > MAX_REGISTRY_RESPONSE_BYTES:
                    raise VerificationError(f"{label} response exceeds size limit")
                try:
                    value = json.loads(payload)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise VerificationError(f"could not parse {label} JSON") from exc
                if not isinstance(value, dict):
                    raise VerificationError(f"invalid {label} JSON object")
                return value, response.headers
        except VerificationError:
            raise
        except urllib.error.HTTPError as exc:
            raise VerificationError(f"{label} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise VerificationError(f"{label} request failed: {bounded(str(reason))}") from exc

    token_query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repository}:pull"}
    )
    token_request = urllib.request.Request(
        f"https://ghcr.io/token?{token_query}",
        headers={"Accept": "application/json"},
    )
    token_document, _ = read_json(token_request, "GHCR token")
    token = token_document.get("token") or token_document.get("access_token")
    if not isinstance(token, str) or not token:
        raise VerificationError("GHCR token response did not contain a token")

    encoded_reference = urllib.parse.quote(reference, safe=":")
    manifest_request = urllib.request.Request(
        f"https://ghcr.io/v2/{repository}/manifests/{encoded_reference}",
        headers={
            "Accept": GHCR_MANIFEST_ACCEPT,
            "Authorization": f"Bearer {token}",
        },
    )
    manifest, headers = read_json(manifest_request, f"GHCR manifest for {ref}")
    digest = headers.get("Docker-Content-Digest")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise VerificationError(f"GHCR manifest for {ref} omitted a valid content digest")
    manifest["digest"] = digest
    return manifest


def safe_extract_tar(archive_path: Path, dest: Path) -> None:
    """Extract ``archive_path`` into ``dest`` with explicit per-member validation.

    Every member is validated before anything is written: absolute paths, parent
    traversal, symlinks/hardlinks, and special files are rejected; only regular
    files and directories are permitted, and each resolved target must stay within
    ``dest``. This replaces reliance on ``tarfile``'s ``filter`` argument so the
    safety behavior is identical on every supported Python 3.12+ interpreter and
    never falls back to an unfiltered extraction.
    """
    import tarfile

    dest = dest.resolve()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            if member.name.startswith(("/", "\\")):
                raise VerificationError(f"unsafe absolute tar member: {member.name}")
            normalized = os.path.normpath(member.name)
            if normalized == ".." or normalized.startswith(".." + os.sep):
                raise VerificationError(f"unsafe tar member path: {member.name}")
            if normalized.startswith(os.sep):
                raise VerificationError(f"unsafe absolute tar member: {member.name}")
            target = (dest / normalized).resolve()
            try:
                target.relative_to(dest)
            except ValueError as exc:
                raise VerificationError(
                    f"tar member escapes destination: {member.name}"
                ) from exc
            if member.issym() or member.islnk():
                raise VerificationError(f"tar member is a link (rejected): {member.name}")
            if not (member.isfile() or member.isdir()):
                raise VerificationError(
                    f"tar member is not a regular file or directory: {member.name}"
                )
    # Re-open and extract only after every member has been validated. Restore
    # canonical permission bits so executable bits survive reconstruction.
    # build-release.py derives archive modes from the filesystem, so the modes
    # written here must match the release workflow's canonical checkout
    # (directories 0755, executables 0755, other files 0644). ``git archive``
    # writes tar modes as ``(0666|0777) & ~umask`` rather than the tracked index
    # mode, so preserving ``member.mode`` verbatim would make the reconstruction
    # depend on the maintainer's umask and rebuild a byte-different archive.
    # Canonicalizing from the executable bit is umask-independent, strips
    # setuid/setgid/sticky and group/other-write noise, and matches the release.
    extracted: list[tuple[Path, int]] = []
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            normalized = os.path.normpath(member.name)
            target = dest / normalized
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                extracted.append((target, 0o755))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise VerificationError(f"tar member has no readable content: {member.name}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append((target, 0o755 if (member.mode & 0o111) else 0o644))
    # Apply modes only after every path exists, so a read-only directory can never
    # block creation of its children during extraction.
    for target, mode in extracted:
        os.chmod(target, mode)


# Verification checks that depend on an earlier check's downloaded artifacts. A
# check whose prerequisite did not pass is recorded as ``skipped`` rather than
# running against missing/partial state (which would raise uncontrolled I/O or
# parse errors). A failed prerequisite is itself a failed check, so the overall
# verification still fails closed.
VERIFICATION_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "anonymous-download": ("release-assets",),
    "archive-checksum": ("anonymous-download",),
    "spdx-parses": ("anonymous-download",),
    "archive-reproducible": ("archive-checksum",),
    "source-attestation": ("anonymous-download",),
    "source-sbom-attestation": ("spdx-parses",),
    "public-installer": ("archive-checksum",),
}


# --- Data model ------------------------------------------------------------


@dataclass
class CommandResult:
    argv: list
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class CheckResult:
    name: str
    status: str  # passed | failed | skipped
    duration_seconds: float = 0.0
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 3),
            "detail": bounded(self.detail, 1000),
        }


@dataclass
class ReleaseState:
    version: str = ""
    commit_sha: str = ""
    tag: str = ""
    phase: str = "local_ready"
    resumed_from: Optional[str] = None
    ci_run_id: Optional[int] = None
    ci_url: Optional[str] = None
    release_run_id: Optional[int] = None
    release_url: Optional[str] = None
    assets: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    phase_durations: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    updated_at: str = ""

    def to_json(self) -> dict:
        return {
            "schema": STATE_SCHEMA,
            "schema_version": STATE_SCHEMA_VERSION,
            **dataclasses.asdict(self),
        }

    @classmethod
    def from_json(cls, payload: dict) -> "ReleaseState":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class Options:
    dry_run: bool = False
    json_output: bool = False
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL
    workflow_timeout_seconds: float = DEFAULT_WORKFLOW_TIMEOUT
    run_local_gates: bool = True
    repo_root: Optional[Path] = None  # constructor-only; never a CLI flag
    assume_yes: bool = False  # constructor-only test hook; never a CLI flag


# --- Dependency-injection primitives ---------------------------------------


class SubprocessRunner:
    """Runs non-interactive commands without a shell and captures output."""

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> tuple[str, str]:
        """Stop a detached command and its descendants, then drain diagnostics."""
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return stdout or "", stderr or ""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        argv = [str(item) for item in argv]
        # A closed stdin alone is insufficient: Cage and some credential tools
        # deliberately fall back to /dev/tty. A fresh session removes the
        # controlling terminal, and process-group ownership lets timeout or
        # interruption clean up every descendant rather than orphaning it.
        stdin = subprocess.DEVNULL if input_text is None else subprocess.PIPE
        process = None
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except FileNotFoundError as exc:
            return CommandResult(argv, 127, "", f"command not found: {exc.filename}")
        except subprocess.TimeoutExpired as exc:
            assert process is not None
            stdout, stderr = self._terminate_process_group(process)

            def decode_timeout_output(value) -> str:
                if isinstance(value, str):
                    return value
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return ""

            stdout = stdout or decode_timeout_output(exc.stdout)
            stderr = stderr or decode_timeout_output(exc.stderr)
            timeout_diagnostic = (
                f"command timed out after {timeout:g}s"
                if isinstance(timeout, (int, float))
                else "command timed out"
            )
            diagnostic = "\n".join(
                part for part in (stderr.strip(), timeout_diagnostic) if part
            )
            return CommandResult(argv, 124, stdout, diagnostic)
        except BaseException:
            if process is not None:
                self._terminate_process_group(process)
            raise
        return CommandResult(argv, process.returncode, stdout or "", stderr or "")


class RealClock:
    def now(self) -> float:
        return time.time()


class RealSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class StdinPrompter:
    def prompt(self, message: str) -> str:
        sys.stderr.write(message)
        sys.stderr.flush()
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return ""
        return line.rstrip("\n")


# --- Orchestrator ----------------------------------------------------------


@dataclass
class ReleaseContext:
    repository: str = ""
    commit_sha: str = ""
    version: str = ""
    tag: str = ""
    ahead: int = 0
    behind: int = 0
    origin_main_sha: str = ""
    local_tag_sha: Optional[str] = None
    local_tag_annotated: bool = False
    remote_tag_sha: Optional[str] = None
    remote_tag_annotated: bool = False
    release_exists: bool = False


class Orchestrator:
    def __init__(
        self,
        options: Options,
        runner=None,
        clock=None,
        sleeper=None,
        prompter=None,
        image_inspector: Optional[Callable[[str], dict]] = None,
        out: Optional[Callable[[str], None]] = None,
        err: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.options = options
        self.runner = runner or SubprocessRunner()
        self.clock = clock or RealClock()
        self.sleeper = sleeper or RealSleeper()
        self.prompter = prompter or StdinPrompter()
        self.image_inspector = image_inspector or inspect_public_ghcr_manifest
        self._out = out or (lambda line: sys.stdout.write(line + "\n"))
        self._err = err or (lambda line: sys.stderr.write(line + "\n"))
        self.repo_root = Path(options.repo_root).resolve() if options.repo_root else Path.cwd()
        self.state = ReleaseState()
        self.context = ReleaseContext()
        self.preflight_checks: list = []
        self.verification_checks: list = []
        self._verify_dir: Optional[Path] = None
        self._lock_fd: Optional[int] = None
        self._state_dir: Optional[Path] = None
        self._state_file: Optional[Path] = None
        self._log_path: Optional[Path] = None
        self._interrupted = False
        self._started = self.clock.now()
        self._last_transition: dict = {}
        self._previous_signal_handlers: dict = {}

    # -- output / logging ---------------------------------------------------

    def progress(self, message: str) -> None:
        """Progress always goes to stderr so --json keeps stdout clean."""
        self.log(message)
        self._err(message)

    def log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.now()))
        line = f"{stamp} {redact(message)}"
        try:
            if self._log_path is not None:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass

    def emit(self, message: str) -> None:
        """Human-facing summary line (stdout unless --json)."""
        if not self.options.json_output:
            self._out(message)

    # -- signal handling ----------------------------------------------------

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_signal_handlers[sig] = signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Not on the main thread (e.g. under test); skip.
                pass

    def _on_signal(self, signum, frame) -> None:  # pragma: no cover - async
        self._interrupted = True
        try:
            self._save_state()
        except Exception:
            pass
        self._err("\nInterrupted. State flushed. Resume with: python3 scripts/publish_release.py")
        sys.exit(128 + int(signum))

    def _check_interrupt(self) -> None:
        if self._interrupted:
            self._save_state()
            raise ReleaseError(
                "interrupted; state flushed. Resume with: python3 scripts/publish_release.py"
            )

    # -- locking & state ----------------------------------------------------

    def _resolve_state_dir(self) -> Path:
        result = self.runner.run(
            ["git", "rev-parse", "--git-path", "cage-release"], cwd=self.repo_root
        )
        if not result.ok:
            raise ReleaseError(f"could not resolve git path: {bounded(result.stderr)}")
        path = Path(result.stdout.strip())
        if not path.is_absolute():
            path = self.repo_root / path
        return path

    def _acquire_lock(self) -> None:
        state_dir = self._resolve_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        self._state_dir = state_dir
        lock_path = state_dir / "lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as exc:
            os.close(fd)
            raise ReleaseError(
                "another publish_release process holds the release lock; resume later"
            ) from exc
        self._lock_fd = fd
        logs_dir = state_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(logs_dir, 0o700)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock.now()))
        self._log_path = logs_dir / f"{stamp}.log"

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _state_file_path(self) -> Path:
        assert self._state_dir is not None
        return self._state_dir / f"{self.state.tag}.json"

    def _save_state(self) -> None:
        if self._state_dir is None or not self.state.tag:
            return
        self._state_file = self._state_file_path()
        self.state.updated_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.now())
        )
        payload = json.dumps(self.state.to_json(), indent=2, sort_keys=True) + "\n"
        tmp = self._state_file.with_name(self._state_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._state_file)

    def _load_state_hint(self) -> Optional[ReleaseState]:
        if self._state_dir is None or not self.state.tag:
            return None
        path = self._state_file_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if data.get("schema") != STATE_SCHEMA:
                return None
            if data.get("schema_version") not in (1, STATE_SCHEMA_VERSION):
                return None
            return ReleaseState.from_json(data)
        except (TypeError, ValueError, OSError):
            return None

    def _restore_observability_hint(self, hint: Optional[ReleaseState]) -> None:
        """Restore only local evidence; remote state still determines the phase.

        A matching private journal carries cumulative phase timings and the most
        recent verification results across retries. It never restores refs,
        workflow conclusions, image digests, or a claimed phase.
        """
        if hint is None or (
            hint.version,
            hint.commit_sha,
            hint.tag,
        ) != (
            self.state.version,
            self.state.commit_sha,
            self.state.tag,
        ):
            return
        durations = {}
        hint_durations = (
            hint.phase_durations if isinstance(hint.phase_durations, dict) else {}
        )
        for phase, value in hint_durations.items():
            if phase not in PHASES or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and 0 <= value < float("inf"):
                durations[phase] = round(float(value), 3)
        checks = []
        if isinstance(hint.checks, list):
            for entry in hint.checks:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                status = entry.get("status")
                duration = entry.get("duration_seconds", 0.0)
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[a-z0-9-]{1,80}", name)
                    or status not in ("passed", "failed", "skipped")
                ):
                    continue
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    duration = 0.0
                if duration != duration or duration < 0 or duration == float("inf"):
                    duration = 0.0
                checks.append(
                    CheckResult(
                        name=name,
                        status=status,
                        duration_seconds=max(0.0, float(duration)),
                        detail=str(entry.get("detail") or ""),
                    ).to_json()
                )
        self.state.phase_durations = durations
        self.state.checks = checks
        assets = {}
        if isinstance(hint.assets, dict):
            expected_names = {
                self._archive_name(),
                self._checksum_name(),
                self._spdx_name(),
            }
            for name, entry in hint.assets.items():
                if name not in expected_names or not isinstance(entry, dict):
                    continue
                digest = entry.get("sha256")
                size = entry.get("size")
                if (
                    isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", digest)
                    and isinstance(size, int)
                    and not isinstance(size, bool)
                    and size >= 0
                ):
                    assets[name] = {"sha256": digest, "size": size}
        self.state.assets = assets

    # -- command helpers ----------------------------------------------------

    def git(
        self,
        *args: str,
        check: bool = True,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = DEFAULT_EXTERNAL_COMMAND_TIMEOUT,
    ) -> CommandResult:
        result = self.runner.run(
            ["git", *args], cwd=cwd or self.repo_root, timeout=timeout
        )
        if check and not result.ok:
            raise ReleaseError(f"git {args[0]} failed: {bounded(result.stderr or result.stdout)}")
        return result

    def git_out(self, *args: str, cwd: Optional[Path] = None) -> str:
        return self.git(*args, cwd=cwd).stdout.strip()

    def gh(
        self,
        *args: str,
        check: bool = True,
        timeout: Optional[float] = DEFAULT_EXTERNAL_COMMAND_TIMEOUT,
    ) -> CommandResult:
        result = self.runner.run(
            ["gh", *args], cwd=self.repo_root, timeout=timeout
        )
        if check and not result.ok:
            label = " ".join(args[:2]) if len(args) >= 2 else (args[0] if args else "")
            raise ReleaseError(f"gh {label} failed: {bounded(result.stderr or result.stdout)}")
        return result

    def gh_json(
        self,
        *args: str,
        timeout: Optional[float] = DEFAULT_EXTERNAL_COMMAND_TIMEOUT,
    ):
        result = self.gh(*args, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise ReleaseError(f"could not parse gh output: {bounded(result.stdout)}") from exc

    def docker(
        self,
        *args: str,
        env: Optional[dict] = None,
        check: bool = True,
        timeout: Optional[float] = DEFAULT_EXTERNAL_COMMAND_TIMEOUT,
    ) -> CommandResult:
        result = self.runner.run(
            ["docker", *args], cwd=self.repo_root, env=env, timeout=timeout
        )
        if check and not result.ok:
            raise ReleaseError(f"docker {args[0]} failed: {bounded(result.stderr or result.stdout)}")
        return result

    def curl_download(self, url: str, dest: Path) -> Path:
        # ``-q`` (a.k.a. ``--disable``) ignores any ambient curlrc so public-release
        # asset downloads are genuinely anonymous (no injected credentials/proxy).
        # It must be the first option; ``curl --no-config`` is not a valid option.
        result = self.runner.run(
            [
                "curl",
                "-q",
                "-fsSL",
                "--retry",
                "3",
                "--retry-delay",
                "2",
                "--connect-timeout",
                "15",
                "--max-time",
                "120",
                "-o",
                str(dest),
                url,
            ],
            cwd=self.repo_root,
            timeout=150,
        )
        if not result.ok:
            raise VerificationError(
                f"anonymous download failed for {url}: {bounded(result.stderr)}"
            )
        return dest

    # -- parsing helpers ----------------------------------------------------

    @staticmethod
    def normalize_remote(url: str) -> Optional[str]:
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
        if not match:
            return None
        return f"{match.group(1)}/{match.group(2)}"

    def read_version(self) -> str:
        cage = self.repo_root / "cage"
        if not cage.is_file():
            raise PreflightError("cage launcher not found at repository root")
        text = cage.read_text(encoding="utf-8")
        match = re.search(r'^CAGE_VERSION="([^"]+)"', text, re.MULTILINE)
        if not match:
            raise PreflightError("could not read CAGE_VERSION from cage script")
        version = match.group(1)
        if not VERSION_RE.fullmatch(version):
            raise PreflightError(f"invalid CAGE_VERSION: {version!r}")
        return version

    # -- preflight ----------------------------------------------------------

    def _record_check(self, name: str, fn: Callable[[], object]) -> CheckResult:
        start = self.clock.now()
        try:
            detail = fn() or ""
            result = CheckResult(name, "passed", self.clock.now() - start, str(detail))
        except ReleaseError as exc:
            result = CheckResult(name, "failed", self.clock.now() - start, str(exc))
        self.preflight_checks.append(result)
        return result

    def _abort_if_failed(self) -> None:
        failed = [c for c in self.preflight_checks if c.status == "failed"]
        if failed:
            details = "\n".join(f"  - {c.name}: {bounded(c.detail, 800)}" for c in failed)
            raise PreflightError(f"preflight failed:\n{details}")

    def _preflight(self) -> None:
        self.preflight_checks = []
        self._record_check("python-version", self._check_python)
        self._record_check("executables", self._check_executables)
        self._abort_if_failed()
        self._establish_context()
        self._record_check("worktree-clean", self._check_clean_worktree)
        self._record_check("git-diff-check", self._check_diff_check)
        self._record_check("upstream", self._check_upstream)
        self._record_check("divergence", self._check_divergence)
        self._record_check("changelog", self._check_changelog)
        self._record_check("local-tag", self._check_local_tag)
        self._record_check("remote-tag", self._check_remote_tag)
        self._record_check("gh-auth", self._check_gh_auth)
        self._record_check("docker-usable", self._check_docker)
        self._record_check("github-release", self._check_existing_release)
        if self.options.run_local_gates:
            self._record_check("unit-tests", self._gate_unit_tests)
            self._record_check("python-compile", self._gate_compileall)
            self._record_check("shell-syntax", self._gate_shell_syntax)
            self._record_check("compose-config", self._gate_compose)
            self._record_check("reproducible-archive", self._gate_archive)
        self._abort_if_failed()

    def _check_python(self) -> str:
        if sys.version_info < (3, 12):
            raise PreflightError(
                f"Python 3.12+ is required (have {sys.version.split()[0]})"
            )
        return sys.version.split()[0]

    def _check_executables(self) -> str:
        missing = [name for name in REQUIRED_EXECUTABLES if shutil.which(name) is None]
        if not os.access(REQUIRED_BASH, os.X_OK):
            missing.append(REQUIRED_BASH)
        if missing:
            raise PreflightError(f"missing required executables: {', '.join(missing)}")
        return ", ".join(REQUIRED_EXECUTABLES)

    def _establish_context(self) -> None:
        top = self.git_out("rev-parse", "--show-toplevel")
        if Path(top).resolve() != self.repo_root:
            raise PreflightError(
                f"must run from the Cage repository root (root is {top}, cwd is {self.repo_root})"
            )
        branch = self.git_out("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise PreflightError(f"must run on the main branch (currently {branch!r})")
        url = self.git_out("remote", "get-url", "origin")
        repo = self.normalize_remote(url)
        if repo is None or repo.lower() != REPOSITORY.lower():
            raise PreflightError(
                f"origin must resolve to {REPOSITORY} (got {repo or url!r})"
            )
        sha = self.git_out("rev-parse", "HEAD")
        if not FULL_SHA_RE.fullmatch(sha):
            raise PreflightError(f"invalid HEAD sha: {sha!r}")
        version = self.read_version()
        tag = f"v{version}"
        # Read-only fetch of origin/main into the remote-tracking ref.
        self.git(
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
            "--quiet",
        )
        origin_main = self.git_out("rev-parse", "origin/main")
        counts = self.git_out("rev-list", "--left-right", "--count", "origin/main...HEAD")
        parts = counts.split()
        if len(parts) != 2:
            raise PreflightError(f"could not compare histories: {counts!r}")
        behind, ahead = int(parts[0]), int(parts[1])
        self.context = ReleaseContext(
            repository=REPOSITORY,
            commit_sha=sha,
            version=version,
            tag=tag,
            ahead=ahead,
            behind=behind,
            origin_main_sha=origin_main,
        )
        self.state.version = version
        self.state.commit_sha = sha
        self.state.tag = tag

    def _check_clean_worktree(self) -> str:
        status = self.git_out("status", "--porcelain")
        if status.strip():
            raise PreflightError(f"working tree is not clean:\n{bounded(status)}")
        return "clean"

    def _check_diff_check(self) -> str:
        result = self.git("diff", "--check", check=False)
        if not result.ok:
            raise PreflightError(
                f"git diff --check reported problems:\n{bounded(result.stdout or result.stderr)}"
            )
        return "ok"

    def _check_upstream(self) -> str:
        result = self.git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
        )
        upstream = result.stdout.strip() if result.ok else ""
        if upstream != "origin/main":
            raise PreflightError(f"main must track origin/main (got {upstream or 'none'})")
        return upstream

    def _check_divergence(self) -> str:
        if self.context.behind != 0:
            raise PreflightError(
                f"local main is behind origin/main by {self.context.behind}; "
                "fast-forward first (never force-push)"
            )
        if self.context.ahead > 1:
            raise PreflightError(
                f"local main is ahead by {self.context.ahead} commits; "
                "only one release commit may be unpublished"
            )
        return f"ahead={self.context.ahead} behind={self.context.behind}"

    def _check_changelog(self) -> str:
        changelog = self.repo_root / "CHANGELOG.md"
        if not changelog.is_file():
            raise PreflightError("CHANGELOG.md not found")
        text = changelog.read_text(encoding="utf-8")
        pattern = rf"^##\s+{re.escape(self.context.version)}\b"
        if not re.search(pattern, text, re.MULTILINE):
            raise PreflightError(
                f"CHANGELOG.md has no section for {self.context.version}"
            )
        return "section present"

    def _check_local_tag(self) -> str:
        result = self.git(
            "rev-parse", "-q", "--verify", f"refs/tags/{self.context.tag}", check=False
        )
        if not result.ok:
            self.context.local_tag_sha = None
            return "absent"
        obj_type = self.git_out("cat-file", "-t", self.context.tag)
        target = self.git_out("rev-list", "-n1", self.context.tag)
        self.context.local_tag_sha = target
        self.context.local_tag_annotated = obj_type == "tag"
        if target != self.context.commit_sha:
            raise PreflightError(
                f"local tag {self.context.tag} points to {target}, "
                f"expected {self.context.commit_sha}"
            )
        if obj_type != "tag":
            raise PreflightError(f"local tag {self.context.tag} must be annotated")
        return f"annotated -> {target[:12]}"

    def _check_remote_tag(self) -> str:
        result = self.git(
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{self.context.tag}",
            f"refs/tags/{self.context.tag}^{{}}",
        )
        peeled = None
        unpeeled = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                peeled = sha
            else:
                unpeeled = sha
        target = peeled or unpeeled
        self.context.remote_tag_sha = target
        self.context.remote_tag_annotated = peeled is not None
        if target is None:
            return "absent"
        if target != self.context.commit_sha:
            raise PreflightError(
                f"remote tag {self.context.tag} points to {target}, "
                f"expected {self.context.commit_sha}"
            )
        if peeled is None:
            raise PreflightError(f"remote tag {self.context.tag} must be annotated")
        return f"annotated -> {target[:12]}"

    def _check_gh_auth(self) -> str:
        result = self.gh("auth", "status", check=False)
        if not result.ok:
            raise PreflightError("gh is not authenticated")
        return "authenticated"

    def _check_docker(self) -> str:
        result = self.docker("info", check=False)
        if not result.ok:
            raise PreflightError("docker is not usable")
        return "usable"

    def _check_existing_release(self) -> str:
        result = self.gh(
            "release",
            "view",
            self.context.tag,
            "--repo",
            REPOSITORY,
            "--json",
            "tagName,isDraft,isPrerelease",
            check=False,
        )
        if not result.ok:
            self.context.release_exists = False
            return "absent"
        self.context.release_exists = True
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise PreflightError("could not parse existing release") from exc
        if data.get("tagName") != self.context.tag:
            raise PreflightError("existing release tag mismatch")
        return "present (will verify)"

    # -- local gates --------------------------------------------------------

    def _gate_unit_tests(self) -> str:
        result = self.runner.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=self.repo_root
        )
        if not result.ok:
            raise PreflightError(
                f"unit tests failed:\n{bounded((result.stdout or '')[-2000:] or (result.stderr or '')[-2000:])}"
            )
        return "passed"

    def _gate_compileall(self) -> str:
        targets = [
            "cage-main.py",
            "cage-config.py",
            "cage_core",
            "cage-desktop.py",
            "cage-tui.py",
            "codex-remote.py",
            "cage-user-remap.py",
            "netgate-proxy.py",
            "mcp-bridge.py",
            "mcp-relay",
            "host-cmd-bridge.py",
            "host-cmd-relay",
            "scripts",
        ]
        result = self.runner.run(
            [sys.executable, "-m", "compileall", "-q", *targets], cwd=self.repo_root
        )
        if not result.ok:
            raise PreflightError(
                f"compileall failed:\n{bounded(result.stderr or result.stdout)}"
            )
        return "passed"

    def _gate_shell_syntax(self) -> str:
        files = [
            "cage",
            "cage-netgate.sh",
            "entrypoint.sh",
            "entrypoint-codex.sh",
            "entrypoint-opencode.sh",
            "install.sh",
            ".github/scripts/install-gitleaks.sh",
        ]
        for name in files:
            result = self.runner.run([REQUIRED_BASH, "-n", name], cwd=self.repo_root)
            if not result.ok:
                raise PreflightError(f"bash -n {name} failed:\n{bounded(result.stderr)}")
        node = shutil.which("node")
        if node:
            result = self.runner.run([node, "--check", "token-monitor-collector.js"], cwd=self.repo_root)
            if not result.ok:
                raise PreflightError(
                    f"node --check token-monitor-collector.js failed:\n{bounded(result.stderr)}"
                )
        return "passed"

    def _gate_compose(self) -> str:
        result = self.docker("compose", "config", check=False)
        if not result.ok:
            raise PreflightError(
                f"docker compose config failed:\n{bounded(result.stderr or result.stdout)}"
            )
        return "passed"

    def _gate_archive(self) -> str:
        epoch = self.git_out("show", "-s", "--format=%ct", "HEAD")
        packager = self.repo_root / "scripts" / "build-release.py"
        digests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                env = dict(os.environ, SOURCE_DATE_EPOCH=epoch)
                result = self.runner.run(
                    [sys.executable, str(packager), self.context.version, temporary],
                    cwd=self.repo_root,
                    env=env,
                )
                if not result.ok:
                    raise PreflightError(
                        f"archive build failed:\n{bounded(result.stderr or result.stdout)}"
                    )
                archive = Path(temporary) / f"cage-{self.context.version}.tar.gz"
                if not archive.is_file():
                    raise PreflightError("archive build produced no file")
                digests.append(hashlib.sha256(archive.read_bytes()).hexdigest())
        if digests[0] != digests[1]:
            raise PreflightError("source archive is not reproducible")
        return f"reproducible sha256:{digests[0][:12]}"

    # -- resume detection ---------------------------------------------------

    def _find_workflow_run(
        self, workflow_file: str, event: str, branch: str, sha: str
    ) -> Optional[dict]:
        fields = "databaseId,headSha,headBranch,event,conclusion,status,url,displayTitle"
        runs = self._retry_idempotent_operation(
            f"{workflow_file} run discovery",
            lambda: self.gh_json(
                "run",
                "list",
                "--repo",
                REPOSITORY,
                "--workflow",
                workflow_file,
                "--event",
                event,
                "--branch",
                branch,
                "--json",
                fields,
                "--limit",
                "50",
            ),
            attempts=3,
        )
        matches = [
            run
            for run in runs
            if run.get("headSha") == sha and run.get("event") == event
        ]
        if not matches:
            return None

        def rank(run: dict):
            conclusion = run.get("conclusion") or ""
            return (conclusion == "success", run.get("databaseId") or 0)

        matches.sort(key=rank, reverse=True)
        return matches[0]

    def _candidate_artifact_name(self) -> str:
        return f"{CANDIDATE_ARTIFACT_PREFIX}{self.context.commit_sha}"

    def _validate_candidate_manifest(self, data: dict) -> None:
        if data.get("schema") != CANDIDATE_SCHEMA:
            raise ReleaseError("candidate manifest schema mismatch")
        if data.get("schema_version") != 3:
            raise ReleaseError("candidate manifest schema version mismatch")
        if data.get("source_sha") != self.context.commit_sha:
            raise ReleaseError("candidate manifest source_sha mismatch")
        if data.get("version") != self.context.version:
            raise ReleaseError("candidate manifest version mismatch")
        if data.get("ci_run_id") != self.state.ci_run_id:
            raise ReleaseError("candidate manifest CI run mismatch")
        if data.get("platforms") != ["linux/amd64", "linux/arm64"]:
            raise ReleaseError("candidate manifest platform set mismatch")
        images = data.get("images") or {}
        if set(images) != set(IMAGE_NAMES):
            raise ReleaseError("candidate manifest image set mismatch")
        for name in IMAGE_NAMES:
            entry = images.get(name) or {}
            if entry.get("name") != f"{GHCR_ROOT}/{name}":
                raise ReleaseError(f"candidate manifest image name mismatch for {name}")
            if entry.get("tag") != f"candidate-{self.context.commit_sha}":
                raise ReleaseError(f"candidate manifest tag mismatch for {name}")
            if not DIGEST_RE.fullmatch(entry.get("digest") or ""):
                raise ReleaseError(f"candidate manifest missing valid digest for {name}")
        self.state.images = {name: images[name]["digest"] for name in IMAGE_NAMES}

    def _load_candidate_manifest(self) -> dict:
        if self.state.ci_run_id is None:
            raise ReleaseError("no CI run id recorded; cannot load candidate manifest")
        with tempfile.TemporaryDirectory() as temporary:
            result = self.gh(
                "run",
                "download",
                str(self.state.ci_run_id),
                "--repo",
                REPOSITORY,
                "--name",
                self._candidate_artifact_name(),
                "--dir",
                temporary,
                check=False,
            )
            if not result.ok:
                raise ReleaseError(
                    f"could not download candidate manifest: {bounded(result.stderr)}"
                )
            files = sorted(Path(temporary).glob("*.json"))
            if not files:
                raise ReleaseError("candidate manifest artifact contained no JSON")
            data = json.loads(files[0].read_text(encoding="utf-8"))
        self._validate_candidate_manifest(data)
        return data

    def _detect_phase(self) -> str:
        sha = self.context.commit_sha
        tag = self.context.tag
        if self.context.origin_main_sha != sha:
            return "local_ready"
        ci = self._find_workflow_run(CI_WORKFLOW_FILE, "push", "main", sha)
        if ci is not None:
            self.state.ci_run_id = ci.get("databaseId")
            self.state.ci_url = ci.get("url")
        if ci is None or ci.get("conclusion") != "success":
            return "main_pushed"
        if self.context.remote_tag_sha != sha:
            return "ci_passed"
        release = self._find_workflow_run(RELEASE_WORKFLOW_FILE, "push", tag, sha)
        if release is not None:
            self.state.release_run_id = release.get("databaseId")
            self.state.release_url = release.get("url")
        if release is None or release.get("conclusion") != "success":
            return "tag_pushed"
        return "release_workflow_passed"

    # -- confirmation -------------------------------------------------------

    def _remaining_mutations(self, phase: str) -> list:
        mutations = []
        if phase == "local_ready":
            mutations.append(
                f"push main: git push origin {self.context.commit_sha}:refs/heads/main"
            )
        if phase in ("local_ready", "main_pushed"):
            mutations.append(
                "wait for the exact CI run (a transient failure may be rerun once)"
            )
        if phase in ("local_ready", "main_pushed", "ci_passed"):
            mutations.append(
                f"create and push immutable annotated tag {self.context.tag}"
            )
        if phase in ("local_ready", "main_pushed", "ci_passed", "tag_pushed"):
            mutations.append("wait for the tag-triggered release workflow")
        return mutations

    def _confirm(self, phase: str) -> None:
        mutations = self._remaining_mutations(phase)
        if not mutations:
            self.progress("only public verification remains; no confirmation required")
            return
        short = self.context.commit_sha[:12]
        self._err("")
        self._err("=== Cage release confirmation ===")
        self._err(f"repository : {self.context.repository}")
        self._err(f"commit     : {self.context.commit_sha}")
        self._err(f"version    : {self.context.version}")
        self._err(f"tag        : {self.context.tag}")
        self._err(f"resumed    : {self.state.resumed_from or phase}")
        self._err("remaining remote mutations:")
        for mutation in mutations:
            self._err(f"  - {mutation}")
        self._err("notes:")
        self._err(
            "  - pushing main causes CI to publish public SHA-only GHCR candidate images"
        )
        self._err("  - the version tag and versioned image digests are immutable")
        self._err("")
        expected = CONFIRMATION_HINT.format(version=self.context.version, short_sha=short)
        answer = self.prompter.prompt(f"Type exactly '{expected}' to proceed: ")
        if answer != expected:
            raise ReleaseError("confirmation did not match; aborting with no remote mutation")

    # -- push main ----------------------------------------------------------

    def _remote_main_sha(self) -> str:
        result = self.git("ls-remote", "origin", "refs/heads/main")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[0].partition("\t")[0]

    def _verify_remote_main(self, sha: str) -> None:
        remote = self._remote_main_sha()
        if remote != sha:
            raise MutationError(f"remote main is {remote or 'absent'}, expected {sha}")

    def _push_main(self) -> None:
        sha = self.context.commit_sha
        self.progress(f"pushing main ({sha[:12]}) to origin")
        result = self.git("push", "origin", f"{sha}:refs/heads/main", check=False)
        if result.ok:
            self._verify_remote_main(sha)
            return
        self.progress("push reported failure; inspecting remote ref before retry")
        if self._remote_main_sha() == sha:
            self.progress("remote main already at recorded SHA; treating push as succeeded")
            return
        retry = self.git("push", "origin", f"{sha}:refs/heads/main", check=False)
        if retry.ok:
            self._verify_remote_main(sha)
            return
        if self._remote_main_sha() == sha:
            return
        raise MutationError(
            f"failed to push main:\n{bounded(retry.stderr or result.stderr)}"
        )

    # -- workflow waiting ---------------------------------------------------

    def _save_run_diagnostics(self, run: Optional[dict]) -> None:
        if not run:
            return
        run_id = run.get("databaseId")
        result = self.gh(
            "run", "view", str(run_id), "--repo", REPOSITORY, "--log", check=False
        )
        if self._log_path is not None:
            try:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"--- diagnostics run {run_id} ---\n")
                    handle.write(bounded(result.stdout or result.stderr, 20000) + "\n")
            except OSError:
                pass
        tail = "\n".join((result.stdout or result.stderr or "").splitlines()[-15:])
        self.progress(f"diagnostics tail:\n{bounded(tail)}")

    def _wait_for_run(
        self, workflow_file: str, event: str, branch: str, sha: str, purpose: str
    ) -> dict:
        deadline = self.clock.now() + self.options.workflow_timeout_seconds
        last_transition = None
        rerun_attempted = False
        while True:
            self._check_interrupt()
            run = self._find_workflow_run(workflow_file, event, branch, sha)
            status = run.get("status") if run else "not-started"
            conclusion = run.get("conclusion") if run else None
            run_id = run.get("databaseId") if run else None
            transition = (status, conclusion)
            if transition != last_transition:
                self.progress(
                    f"{purpose}: status={status} conclusion={conclusion or '-'} run={run_id or '-'}"
                )
                last_transition = transition
            if conclusion == "success":
                return run
            if conclusion in ("failure", "cancelled", "timed_out", "action_required"):
                if conclusion == "failure" and not rerun_attempted and run is not None:
                    rerun_attempted = True
                    self.progress(
                        f"{purpose}: failed; rerunning failed jobs once (run {run_id})"
                    )
                    self.gh(
                        "run",
                        "rerun",
                        str(run_id),
                        "--failed",
                        "--repo",
                        REPOSITORY,
                        check=False,
                    )
                    self.sleeper.sleep(self.options.poll_interval_seconds)
                    continue
                self._save_run_diagnostics(run)
                raise MutationError(
                    f"{purpose} run {run_id} ended with conclusion={conclusion}"
                )
            if self.clock.now() > deadline:
                self._save_run_diagnostics(run)
                raise MutationError(
                    f"{purpose} timed out after {self.options.workflow_timeout_seconds:.0f}s"
                )
            self.sleeper.sleep(self.options.poll_interval_seconds)

    # -- tag ----------------------------------------------------------------

    def _remote_tag_sha(self) -> Optional[str]:
        result = self.git(
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{self.context.tag}",
            f"refs/tags/{self.context.tag}^{{}}",
        )
        peeled = None
        unpeeled = None
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                peeled = sha
            else:
                unpeeled = sha
        return peeled or unpeeled

    def _create_and_push_tag(self) -> None:
        tag = self.context.tag
        sha = self.context.commit_sha
        if self.context.local_tag_sha == sha and self.context.local_tag_annotated:
            self.progress(f"local tag {tag} already present and correct")
        else:
            if self.context.local_tag_sha is not None:
                raise MutationError(
                    f"local tag {tag} exists but does not match; refusing to recreate"
                )
            self.progress(f"creating annotated tag {tag} -> {sha[:12]}")
            self.git("tag", "-a", tag, "-m", f"Cage {tag}", sha)
            target = self.git_out("rev-list", "-n1", tag)
            if target != sha:
                raise MutationError(f"created tag derefs to {target}, expected {sha}")
        if self.context.remote_tag_sha == sha and self.context.remote_tag_annotated:
            self.progress(f"remote tag {tag} already present and correct")
            return
        if self.context.remote_tag_sha is not None:
            raise MutationError(
                f"remote tag {tag} exists but does not match; refusing to move/overwrite"
            )
        self.progress(f"pushing tag {tag}")
        result = self.git("push", "origin", f"refs/tags/{tag}", check=False)
        if not result.ok and self._remote_tag_sha() != sha:
            raise MutationError(f"failed to push tag {tag}: {bounded(result.stderr)}")
        remote = self._remote_tag_sha()
        if remote != sha:
            raise MutationError(f"remote tag {tag} is {remote}, expected {sha}")

    # -- public verification ------------------------------------------------

    def _archive_name(self) -> str:
        return f"cage-{self.context.version}.tar.gz"

    def _checksum_name(self) -> str:
        return f"cage-{self.context.version}.tar.gz.sha256"

    def _spdx_name(self) -> str:
        return f"cage-{self.context.version}.spdx.json"

    def _retry_idempotent_operation(
        self,
        label: str,
        operation: Callable[[], object],
        *,
        attempts: int = PUBLIC_RETRY_ATTEMPTS,
    ):
        """Retry an idempotent public read with bounded, visible backoff."""
        if attempts < 1:
            raise ValueError("attempts must be positive")
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            self._check_interrupt()
            try:
                return operation()
            except (ReleaseError, OSError, ValueError) as exc:
                last_error = exc
                if attempt == attempts:
                    break
                diagnostic = bounded(str(exc), 240).replace("\n", " ")
                self.progress(
                    f"retry {label}: attempt {attempt}/{attempts} failed"
                    f" ({diagnostic}); retrying in {PUBLIC_RETRY_DELAY_SECONDS:g}s"
                )
                self.sleeper.sleep(PUBLIC_RETRY_DELAY_SECONDS)
        raise VerificationError(
            f"{label} failed after {attempts} attempts: "
            f"{bounded(str(last_error or 'unknown error'), 800)}"
        ) from last_error

    def _inspect_image_once(self, ref: str, env: Optional[dict] = None) -> dict:
        # The Registry API provides the top-level digest and platform index
        # portably. Do not require the optional Docker Buildx CLI plugin here.
        del env  # retained in the private signature for compatibility
        try:
            manifest = self.image_inspector(ref)
        except VerificationError:
            raise
        except Exception as exc:
            raise VerificationError(f"could not inspect {ref}: {bounded(str(exc))}") from exc
        if not isinstance(manifest, dict):
            raise VerificationError(f"could not inspect {ref}: invalid manifest object")
        return manifest

    def _inspect_image(self, ref: str, env: Optional[dict] = None) -> dict:
        return self._retry_idempotent_operation(
            f"registry inspect {ref}",
            lambda: self._inspect_image_once(ref, env=env),
        )

    def _release_json(self) -> dict:
        return self._retry_idempotent_operation(
            "public release metadata",
            lambda: self.gh_json(
                "release",
                "view",
                self.context.tag,
                "--repo",
                REPOSITORY,
                "--json",
                "tagName,isDraft,isPrerelease,assets,targetCommitish,url",
            ),
        )

    def _run_verification_check(self, name: str, fn: Callable[[], object]) -> CheckResult:
        start = self.clock.now()
        statuses = {check.name: check.status for check in self.verification_checks}
        unmet = [
            dep
            for dep in VERIFICATION_DEPENDENCIES.get(name, ())
            if statuses.get(dep) != "passed"
        ]
        if unmet:
            # A prerequisite failed or was skipped; do not run against missing or
            # partial artifacts (which would raise uncontrolled I/O or parse
            # errors). The failed prerequisite already fails the overall
            # verification, so this dependent check is recorded as skipped.
            result = CheckResult(
                name,
                "skipped",
                self.clock.now() - start,
                "skipped: prerequisite not passed (" + ", ".join(unmet) + ")",
            )
        else:
            try:
                detail = fn() or ""
                result = CheckResult(name, "passed", self.clock.now() - start, str(detail))
            except ReleaseError as exc:
                result = CheckResult(name, "failed", self.clock.now() - start, str(exc))
            except Exception as exc:  # malformed JSON, tar errors, I/O, missing files
                # Convert any uncontrolled exception into a structured, redacted,
                # bounded failed check instead of letting a traceback escape.
                detail = f"{type(exc).__name__}: {bounded(str(exc))}"
                result = CheckResult(name, "failed", self.clock.now() - start, detail)
        self.verification_checks.append(result)
        self.state.checks.append(result.to_json())
        summary = f"verify {name}: {result.status}"
        if result.status != "passed" and result.detail:
            summary += ": " + bounded(result.detail, 240).replace("\n", " ")
        self.progress(summary)
        self._save_state()
        return result

    def _public_verify(self) -> None:
        self.verification_checks = []
        self.state.checks = []
        self.state.assets = {}
        self._save_state()
        if not self.state.images:
            self._load_candidate_manifest()
        temporary = tempfile.TemporaryDirectory()
        self._verify_dir = Path(temporary.name)
        try:
            checks = (
                ("release-public", self._verify_release_public),
                ("release-assets", self._verify_release_assets),
                ("anonymous-download", self._verify_anonymous_download),
                ("archive-checksum", self._verify_archive_checksum),
                ("spdx-parses", self._verify_spdx),
                ("archive-reproducible", self._verify_archive_reproducible),
                ("source-attestation", self._verify_source_attestation),
                ("source-sbom-attestation", self._verify_source_sbom_attestation),
                ("image-version-digests", self._verify_image_version_digests),
                ("image-latest-digests", self._verify_image_latest_digests),
                ("image-attestations", self._verify_image_attestations),
                ("image-platforms", self._verify_image_platforms),
                ("anonymous-docker", self._verify_anonymous_docker),
                ("public-installer", self._verify_public_installer),
            )
            for name, fn in checks:
                self._run_verification_check(name, fn)
        finally:
            temporary.cleanup()
            self._verify_dir = None
        failed = [c for c in self.verification_checks if c.status == "failed"]
        if failed:
            raise VerificationError(
                "public verification failed: " + ", ".join(c.name for c in failed)
            )

    def _verify_release_public(self) -> str:
        data = self._release_json()
        if data.get("isDraft"):
            raise VerificationError("release is a draft")
        if data.get("isPrerelease"):
            raise VerificationError("release is a prerelease")
        if data.get("tagName") != self.context.tag:
            raise VerificationError("release tag mismatch")
        return str(data.get("url") or "")

    def _verify_release_assets(self) -> str:
        data = self._release_json()
        assets = {asset["name"]: asset for asset in data.get("assets", [])}
        expected = {self._archive_name(), self._checksum_name(), self._spdx_name()}
        if set(assets) != expected:
            raise VerificationError(
                f"asset set mismatch: got {sorted(assets)}, expected {sorted(expected)}"
            )
        for name, asset in assets.items():
            if not asset.get("size"):
                raise VerificationError(f"asset {name} is empty")
        return ", ".join(sorted(expected))

    def _verify_anonymous_download(self) -> str:
        base = f"https://github.com/{REPOSITORY}/releases/download/{self.context.tag}"
        for name in (self._archive_name(), self._checksum_name(), self._spdx_name()):
            path = self.curl_download(f"{base}/{name}", self._verify_dir / name)
            payload = path.read_bytes()
            self.state.assets[name] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        return "downloaded 3 assets anonymously"

    def _verify_archive_checksum(self) -> str:
        archive = (self._verify_dir / self._archive_name()).read_bytes()
        digest = hashlib.sha256(archive).hexdigest()
        expected = (self._verify_dir / self._checksum_name()).read_text().split()[0]
        if digest != expected:
            raise VerificationError("archive checksum mismatch")
        return f"sha256:{digest[:12]}"

    def _verify_spdx(self) -> str:
        data = json.loads((self._verify_dir / self._spdx_name()).read_text(encoding="utf-8"))
        if self._archive_name() not in json.dumps(data):
            raise VerificationError("SPDX document does not identify the archive")
        return "spdx-json ok"

    def _materialize_commit_tree(self, sha: str, dest: Path) -> Path:
        """Materialize the exact committed tree for ``sha`` under ``dest/src``.

        Uses ``git archive`` (a read-only operation that touches neither the
        worktree nor the index) so the reproducibility rebuild reconstructs the
        released commit even if ``HEAD`` or the live worktree changes during the
        long workflow wait.
        """
        tar_path = dest / "tree.tar"
        result = self.git("archive", "--format=tar", "-o", str(tar_path), sha, check=False)
        if not result.ok or not tar_path.is_file():
            raise VerificationError(
                f"could not materialize commit {sha[:12]}: {bounded(result.stderr)}"
            )
        src = dest / "src"
        src.mkdir()
        safe_extract_tar(tar_path, src)
        return src

    def _verify_archive_reproducible(self) -> str:
        epoch = self.git_out("show", "-s", "--format=%ct", self.context.commit_sha)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            src = self._materialize_commit_tree(self.context.commit_sha, temp)
            packager = src / "scripts" / "build-release.py"
            if not packager.is_file():
                raise VerificationError(
                    "materialized commit is missing scripts/build-release.py"
                )
            out_dir = temp / "out"
            out_dir.mkdir()
            env = dict(os.environ, SOURCE_DATE_EPOCH=epoch)
            result = self.runner.run(
                [sys.executable, str(packager), self.context.version, str(out_dir)],
                cwd=src,
                env=env,
            )
            if not result.ok:
                raise VerificationError(
                    f"archive rebuild failed: {bounded(result.stderr or result.stdout)}"
                )
            rebuilt = (out_dir / self._archive_name()).read_bytes()
        published = (self._verify_dir / self._archive_name()).read_bytes()
        if rebuilt != published:
            raise VerificationError("rebuilt archive differs from published archive")
        return "byte-identical (rebuilt from recorded commit)"

    def _verify_source_attestation(self) -> str:
        archive = self._verify_dir / self._archive_name()

        def verify_attestation():
            result = self.gh(
                "attestation",
                "verify",
                str(archive),
                "--repo",
                REPOSITORY,
                "--signer-workflow",
                RELEASE_SIGNER_WORKFLOW,
                "--source-digest",
                self.context.commit_sha,
                "--source-ref",
                f"refs/tags/{self.context.tag}",
                check=False,
            )
            if not result.ok:
                raise VerificationError(
                    "source attestation failed: "
                    f"{bounded(result.stderr or result.stdout)}"
                )
            return result

        self._retry_idempotent_operation("source attestation", verify_attestation)
        return "verified"

    def _verify_source_sbom_attestation(self) -> str:
        archive = self._verify_dir / self._archive_name()

        def verify_attestation():
            result = self.gh(
                "attestation",
                "verify",
                str(archive),
                "--repo",
                REPOSITORY,
                "--signer-workflow",
                RELEASE_SIGNER_WORKFLOW,
                "--source-digest",
                self.context.commit_sha,
                "--source-ref",
                f"refs/tags/{self.context.tag}",
                "--predicate-type",
                "https://spdx.dev/Document/v2.3",
                check=False,
            )
            if not result.ok:
                raise VerificationError(
                    "source SPDX SBOM attestation failed: "
                    f"{bounded(result.stderr or result.stdout)}"
                )
            return result

        self._retry_idempotent_operation("source SPDX SBOM attestation", verify_attestation)
        return "verified"

    def _verify_image_version_digests(self) -> str:
        for name in IMAGE_NAMES:
            ref = f"{GHCR_ROOT}/{name}:{self.context.version}"
            manifest = self._inspect_image(ref)
            digest = manifest.get("digest")
            if digest != self.state.images.get(name):
                raise VerificationError(
                    f"{name} version digest {digest} != candidate {self.state.images.get(name)}"
                )
        return "version tags match candidate digests"

    def _verify_image_latest_digests(self) -> str:
        for name in IMAGE_NAMES:
            ref = f"{GHCR_ROOT}/{name}:latest"
            expected = self.state.images.get(name)

            def inspect_latest():
                manifest = self._inspect_image_once(ref)
                digest = manifest.get("digest")
                if digest != expected:
                    raise VerificationError(
                        f"{name} latest digest {digest} != candidate {expected}"
                    )
                return manifest

            # ``latest`` is intentionally mutable and can lag the completed
            # workflow briefly at registry edges. Retry that propagation read;
            # immutable version-tag conflicts still fail immediately below.
            self._retry_idempotent_operation(f"{name} latest propagation", inspect_latest)
        return "latest tags match candidate digests"

    def _verify_image_attestations(self) -> str:
        for name in IMAGE_NAMES:
            digest = self.state.images.get(name)
            ref = f"oci://{GHCR_ROOT}/{name}@{digest}"

            def verify_attestation():
                result = self.gh(
                    "attestation",
                    "verify",
                    ref,
                    "--repo",
                    REPOSITORY,
                    "--signer-workflow",
                    RELEASE_SIGNER_WORKFLOW,
                    "--source-digest",
                    self.context.commit_sha,
                    check=False,
                )
                if not result.ok:
                    raise VerificationError(
                        f"{name} image attestation failed: "
                        f"{bounded(result.stderr or result.stdout)}"
                    )
                return result

            self._retry_idempotent_operation(f"{name} image attestation", verify_attestation)
        return "all image attestations verified"

    def _verify_image_platforms(self) -> str:
        for name in IMAGE_NAMES:
            ref = f"{GHCR_ROOT}/{name}:{self.context.version}"

            def inspect_platforms():
                manifest = self._inspect_image_once(ref)
                arches = {
                    (entry.get("platform") or {}).get("architecture")
                    for entry in manifest.get("manifests", [])
                }
                if not {"amd64", "arm64"} <= arches:
                    raise VerificationError(
                        f"{name} index missing amd64/arm64 "
                        f"(got {sorted(a for a in arches if a)})"
                    )
                return manifest

            self._retry_idempotent_operation(f"{name} platform index", inspect_platforms)
        return "amd64+arm64 present"

    def _verify_anonymous_docker(self) -> str:
        # A real pull (not merely a manifest inspect) exercises native-platform
        # layer downloads anonymously. A fresh, empty Docker credential directory
        # plus stripped credential env vars guarantee no maintainer credentials.
        with tempfile.TemporaryDirectory() as credential_dir:
            env = anonymous_env(os.environ, DOCKER_CONFIG=credential_dir)
            deadline = self.clock.now() + ANONYMOUS_PULL_TOTAL_TIMEOUT
            for name in IMAGE_NAMES:
                ref = f"{GHCR_ROOT}/{name}:{self.context.version}"
                result = None
                for attempt in range(1, ANONYMOUS_PULL_ATTEMPTS + 1):
                    self._check_interrupt()
                    remaining = deadline - self.clock.now()
                    if remaining <= 0:
                        raise VerificationError(
                            "anonymous Docker verification exceeded its "
                            f"{ANONYMOUS_PULL_TOTAL_TIMEOUT:g}s total budget"
                        )
                    attempt_timeout = min(ANONYMOUS_PULL_ATTEMPT_TIMEOUT, remaining)
                    result = self.docker(
                        "pull",
                        ref,
                        env=env,
                        check=False,
                        timeout=attempt_timeout,
                    )
                    if result.ok:
                        break
                    if attempt < ANONYMOUS_PULL_ATTEMPTS:
                        diagnostic = bounded(result.stderr or result.stdout, 240).replace(
                            "\n", " "
                        )
                        self.progress(
                            f"verify retry anonymous pull {name}: attempt "
                            f"{attempt}/{ANONYMOUS_PULL_ATTEMPTS} failed "
                            f"({diagnostic}); retrying in "
                            f"{PUBLIC_RETRY_DELAY_SECONDS:g}s"
                        )
                        self.sleeper.sleep(PUBLIC_RETRY_DELAY_SECONDS)
                if result is None or not result.ok:
                    raise VerificationError(
                        f"anonymous pull failed for {ref} after "
                        f"{ANONYMOUS_PULL_ATTEMPTS} attempts: "
                        f"{bounded((result.stderr or result.stdout) if result else '')}"
                    )
                # Best-effort cleanup so verification does not fill local disk;
                # shared base layers mean later pulls reuse present blobs.
                self.docker(
                    "rmi",
                    ref,
                    env=env,
                    check=False,
                    timeout=ANONYMOUS_PULL_CLEANUP_TIMEOUT,
                )
        return "anonymous pull ok (native platform)"

    def _verify_public_installer(self) -> str:
        # Prove the PUBLIC installer works anonymously: fetch install.sh from the
        # published tag (not the local checkout), with curl configuration disabled
        # and all credential/gh variables stripped so the install cannot fall back
        # to a maintainer token or authenticated gh configuration.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            install_dir = root / "install"
            bin_dir = root / "bin"
            curl_home = root / "curlhome"
            gh_config = root / "ghconfig"
            # A first-time install requires CAGE_INSTALL_DIR not to exist. The
            # installer creates it transactionally and correctly refuses an
            # existing unrecognized directory.
            for path in (home, bin_dir, curl_home, gh_config):
                path.mkdir()
            installer_url = (
                f"https://raw.githubusercontent.com/{REPOSITORY}/"
                f"{self.context.tag}/install.sh"
            )
            installer = root / "install.sh"
            fetch_env = anonymous_env(os.environ, HOME=str(home), CURL_HOME=str(curl_home))
            fetch = self.runner.run(
                [
                    "curl",
                    "-q",
                    "-fsSL",
                    "--retry",
                    "3",
                    "--retry-delay",
                    "2",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "120",
                    "-o",
                    str(installer),
                    installer_url,
                ],
                cwd=root,
                env=fetch_env,
                timeout=150,
            )
            if not fetch.ok or not installer.is_file():
                raise VerificationError(
                    f"could not fetch public installer anonymously: {bounded(fetch.stderr)}"
                )
            env = anonymous_env(
                os.environ,
                HOME=str(home),
                CURL_HOME=str(curl_home),
                GH_CONFIG_DIR=str(gh_config),
                CAGE_INSTALL_DIR=str(install_dir),
                CAGE_BIN_DIR=str(bin_dir),
                CAGE_VERSION=self.context.version,
            )
            result = self.runner.run(
                [REQUIRED_BASH, str(installer)], cwd=root, env=env, timeout=300
            )
            if not result.ok:
                diagnostic = "\n".join(
                    part for part in (result.stdout.strip(), result.stderr.strip()) if part
                )
                raise VerificationError(
                    f"public installer failed:\n{bounded(diagnostic)}"
                )
            version_result = self.runner.run(
                [str(install_dir / "cage"), "--version"], env=env, timeout=30
            )
            reported = version_result.stdout.strip()
            if reported != f"cage {self.context.version}":
                raise VerificationError(f"installed launcher reports {reported!r}")
            self._assert_installed_payload_matches_archive(install_dir)
        return f"installed cage {self.context.version} from public installer"

    def _assert_installed_payload_matches_archive(self, install_dir: Path) -> None:
        archive_path = self._verify_dir / self._archive_name()
        if not archive_path.is_file():
            raise VerificationError("published archive not downloaded; cannot compare payload")
        with tempfile.TemporaryDirectory() as temporary:
            extract = Path(temporary)
            safe_extract_tar(archive_path, extract)
            payload_root = extract / f"cage-{self.context.version}"
            compared = 0
            for source in sorted(payload_root.rglob("*")):
                if source.is_dir():
                    continue
                relative = source.relative_to(payload_root)
                installed = install_dir / relative
                if not installed.is_file():
                    raise VerificationError(f"installed payload missing: {relative}")
                if installed.read_bytes() != source.read_bytes():
                    raise VerificationError(f"installed payload differs: {relative}")
                compared += 1
        if compared == 0:
            raise VerificationError("no payload files compared")

    # -- revalidation & execution ------------------------------------------

    def _revalidate(self) -> None:
        sha = self.git_out("rev-parse", "HEAD")
        if sha != self.context.commit_sha:
            raise MutationError(f"HEAD changed ({sha[:12]}); aborting before mutation")
        status = self.git_out("status", "--porcelain")
        if status.strip():
            raise MutationError("worktree became dirty; aborting before mutation")
        version = self.read_version()
        if version != self.context.version:
            raise MutationError(f"version changed ({version}); aborting before mutation")

    def _execute(self, phase: str) -> str:
        def timed(name: str, fn: Callable[[], None]) -> None:
            start = self.clock.now()
            try:
                fn()
            finally:
                elapsed = max(0.0, self.clock.now() - start)
                previous = self.state.phase_durations.get(name, 0.0)
                if isinstance(previous, bool) or not isinstance(previous, (int, float)):
                    previous = 0.0
                self.state.phase_durations[name] = round(previous + elapsed, 3)
                # Preserve timing evidence even when a phase fails and the next
                # invocation must resume it.
                self._save_state()

        if phase == "local_ready":
            self._revalidate()
            timed("main_pushed", self._push_main)
            phase = "main_pushed"
            self.state.phase = phase
            self._save_state()
        if phase == "main_pushed":
            def wait_ci() -> None:
                run = self._wait_for_run(
                    CI_WORKFLOW_FILE, "push", "main", self.context.commit_sha, "ci"
                )
                self.state.ci_run_id = run.get("databaseId")
                self.state.ci_url = run.get("url")
                self._load_candidate_manifest()

            timed("ci_passed", wait_ci)
            phase = "ci_passed"
            self.state.phase = phase
            self._save_state()
        if phase == "ci_passed":
            self._revalidate()

            def tag() -> None:
                self._create_and_push_tag()

            timed("tag_pushed", tag)
            phase = "tag_pushed"
            self.state.phase = phase
            self._save_state()
        if phase == "tag_pushed":
            def wait_release() -> None:
                run = self._wait_for_run(
                    RELEASE_WORKFLOW_FILE,
                    "push",
                    self.context.tag,
                    self.context.commit_sha,
                    "release",
                )
                self.state.release_run_id = run.get("databaseId")
                self.state.release_url = run.get("url")

            timed("release_workflow_passed", wait_release)
            phase = "release_workflow_passed"
            self.state.phase = phase
            self._save_state()
        if phase == "release_workflow_passed":
            timed("public_verified", self._public_verify)
            phase = "public_verified"
            self.state.phase = phase
            self._save_state()
        return phase

    def _dry_run_report(self, phase: str) -> None:
        self.planned_mutations = self._remaining_mutations(phase)
        self.progress("dry-run: read-only discovery complete; no remote mutations performed")
        if self.planned_mutations:
            for mutation in self.planned_mutations:
                self.progress(f"planned mutation: {mutation}")
        else:
            self.progress("planned mutations: none (only public verification remains)")

    # -- orchestration ------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        self.planned_mutations = []
        try:
            self._acquire_lock()
            self.progress("running preflight checks")
            self._preflight()
            self._state_file = self._state_file_path()
            hint = self._load_state_hint()
            self._restore_observability_hint(hint)
            phase = self._detect_phase()
            if phase != "local_ready":
                self.state.resumed_from = phase
            self.state.phase = phase
            self.progress(f"release phase detected: {phase}")
            if PHASES.index(phase) >= PHASES.index("ci_passed"):
                self._load_candidate_manifest()
            self._save_state()
            if self.options.dry_run:
                self._dry_run_report(phase)
                return
            self._confirm(phase)
            phase = self._execute(phase)
            self.state.phase = phase
            self._save_state()
        finally:
            self._save_state()
            self._release_lock()

    # -- rendering ----------------------------------------------------------

    def render_human(self) -> None:
        self._out("")
        self._out("=== Cage release summary ===")
        self._out(f"repository : {self.context.repository}")
        self._out(f"version    : {self.context.version}")
        self._out(f"tag        : {self.context.tag}")
        self._out(f"commit     : {self.context.commit_sha}")
        self._out(f"phase      : {self.state.phase}")
        if self.options.dry_run:
            self._out("dry-run    : true (no remote mutations performed)")
        if self.state.resumed_from:
            self._out(f"resumed    : {self.state.resumed_from}")
        self._out("")
        self._out("phases:")
        reached = PHASES.index(self.state.phase) if self.state.phase in PHASES else -1
        for index, phase in enumerate(PHASES):
            marker = "done" if index <= reached else "pending"
            duration = self.state.phase_durations.get(phase)
            duration_text = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "-"
            self._out(f"  [{marker:>7}] {phase:<28} {duration_text}")
        if self.options.dry_run and getattr(self, "planned_mutations", None):
            self._out("")
            self._out("planned remote mutations:")
            for mutation in self.planned_mutations:
                self._out(f"  - {mutation}")
        if self.state.ci_url:
            self._out(f"ci run     : {self.state.ci_url}")
        if self.state.release_url:
            self._out(f"release run: {self.state.release_url}")
        self._out(
            f"release    : https://github.com/{REPOSITORY}/releases/tag/{self.context.tag}"
        )
        if self.state.assets:
            self._out("asset digests:")
            for name in (self._archive_name(), self._checksum_name(), self._spdx_name()):
                entry = self.state.assets.get(name) or {}
                digest = entry.get("sha256", "-")
                size = entry.get("size", "-")
                self._out(f"  {name:<34} sha256:{digest} ({size} bytes)")
        if self.state.images:
            self._out("image digests:")
            for name in IMAGE_NAMES:
                self._out(f"  {name:<12} {self.state.images.get(name, '-')}")
        if self.verification_checks:
            self._out("verification:")
            for check in self.verification_checks:
                self._out(
                    f"  [{check.status:>7}] {check.name} ({check.duration_seconds:.1f}s)"
                )
                if check.status != "passed" and check.detail:
                    self._out(f"             {bounded(check.detail, 500)}")
        self._out(f"duration   : {self.clock.now() - self._started:.1f}s")
        if self._log_path:
            self._out(f"log        : {self._log_path}")

    def render_json(self) -> None:
        reached = PHASES.index(self.state.phase) if self.state.phase in PHASES else -1
        result = {
            "schema": RESULT_SCHEMA,
            "schema_version": RESULT_SCHEMA_VERSION,
            "repository": self.context.repository,
            "version": self.context.version,
            "tag": self.context.tag,
            "commit_sha": self.context.commit_sha,
            "dry_run": self.options.dry_run,
            "resumed_from": self.state.resumed_from,
            "phase": self.state.phase,
            "ci": {
                "run_id": self.state.ci_run_id,
                "url": self.state.ci_url,
                "conclusion": "success" if reached >= PHASES.index("ci_passed") else None,
            },
            "release": {
                "run_id": self.state.release_run_id,
                "url": self.state.release_url,
                "conclusion": "success"
                if reached >= PHASES.index("release_workflow_passed")
                else None,
            },
            "assets": dict(self.state.assets),
            "images": {
                name: {"digest": self.state.images.get(name)} for name in IMAGE_NAMES
            },
            "phase_durations": dict(self.state.phase_durations),
            "checks": (
                [check.to_json() for check in self.verification_checks]
                if self.verification_checks
                else list(self.state.checks)
            ),
            "duration_seconds": round(self.clock.now() - self._started, 3),
            "log_path": str(self._log_path) if self._log_path else None,
        }
        self._out(json.dumps(result, indent=2, sort_keys=True))

    def render_error_json(self, message: str) -> None:
        result = {
            "schema": RESULT_SCHEMA,
            "schema_version": RESULT_SCHEMA_VERSION,
            "repository": self.context.repository,
            "version": self.context.version,
            "tag": self.context.tag,
            "commit_sha": self.context.commit_sha,
            "dry_run": self.options.dry_run,
            "resumed_from": self.state.resumed_from,
            "phase": self.state.phase,
            "error": bounded(message, 1000),
            "ci": {
                "run_id": self.state.ci_run_id,
                "url": self.state.ci_url,
            },
            "release": {
                "run_id": self.state.release_run_id,
                "url": self.state.release_url,
            },
            "assets": dict(self.state.assets),
            "images": {
                name: {"digest": self.state.images.get(name)} for name in IMAGE_NAMES
            },
            "phase_durations": dict(self.state.phase_durations),
            "checks": (
                [check.to_json() for check in self.verification_checks]
                if self.verification_checks
                else list(self.state.checks)
            ),
            "duration_seconds": round(self.clock.now() - self._started, 3),
            "log_path": str(self._log_path) if self._log_path else None,
        }
        self._out(json.dumps(result, indent=2, sort_keys=True))


# --- CLI entry point -------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_release.py",
        description="Deterministic, resumable Cage release automation (maintainer-only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read-only discovery and validation; display planned mutations; change nothing",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit one final JSON result object on stdout; progress and errors go to stderr",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    options = Options(dry_run=args.dry_run, json_output=args.json_output)
    orchestrator = Orchestrator(options)
    try:
        orchestrator.run()
    except ReleaseError as exc:
        orchestrator._err(f"ERROR: {bounded(str(exc))}")
        orchestrator._err(
            "Resume with: python3 scripts/publish_release.py"
            + (" --json" if options.json_output else "")
        )
        if options.json_output:
            orchestrator.render_error_json(str(exc))
        return 1
    if options.json_output:
        orchestrator.render_json()
    else:
        orchestrator.render_human()
    return 0


if __name__ == "__main__":
    sys.exit(main())
