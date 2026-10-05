"""Opt-in crash recovery on exact disposable containers, never user sessions.

CAGE_RUN_DOCKER_SMOKE=1 CAGE_ACCOUNTING_TEST_IMAGE=<existing image with sh>
python3 -m pytest -q tests/test_accounting_docker.py
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from cage_core.accounting import execution, runtime

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get("CAGE_RUN_DOCKER_SMOKE") != "1", reason="opt-in disposable Docker recovery")


@pytest.fixture
def tmp_path():
    # Colima shares the checkout but may not share macOS's /var/folders tmpdir.
    # This exact private fixture directory is removed even after test failure.
    with tempfile.TemporaryDirectory(prefix=".accounting-docker-", dir=ROOT) as name:
        yield Path(name)


@pytest.fixture
def owned(tmp_path):
    docker = shutil.which("docker")
    assert docker
    image = runtime.image_id(docker, os.environ.get("CAGE_ACCOUNTING_TEST_IMAGE", "cage-token-monitor:latest"))
    engine = runtime.transport(docker)["engine"]
    cache = tmp_path / "cache"
    cache.mkdir()
    yield docker, image, engine, cache
    # Only attempt journals created in this test's private root are eligible.
    for path in (tmp_path / "accounting" / "attempts").glob("*.json"):
        execution._finish(docker, json.loads(path.read_text()))


def command(docker, image, cache, script):
    return [docker, "run", "--rm", "--network", "none", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,src={cache},dst=/cache", "--entrypoint", "sh", image, "-c", script]


def test_owned_create_start_and_cleanup_match_real_docker_inspection(tmp_path, owned):
    docker, image, engine, cache = owned
    work = execution.Work(tmp_path, "a" * 64, nullcontext, engine)
    with execution.scope(work), execution.collector(tmp_path, docker, "poke:fixture", command(docker, image, cache, "printf owned > /cache/result"), image) as args:
        subprocess.run(args, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert (cache / "result").read_text() == "owned"
    assert not list((tmp_path / "accounting" / "attempts").glob("*.json"))


def test_crashed_owner_cannot_leave_a_writer_during_cache_reuse(tmp_path, owned):
    docker, image, engine, cache = owned
    code = """
import json,sys,subprocess
from pathlib import Path
from contextlib import nullcontext
from cage_core.accounting import execution
from cage_core.monitoring.locks import _wait_for_volume_lock
root, docker, image, engine, command = sys.argv[1:]
with _wait_for_volume_lock(Path(root), 'a' * 32), execution.scope(execution.Work(Path(root), 'a' * 64, nullcontext, engine)):
    with execution.collector(Path(root), docker, 'monitor:' + 'a' * 32, json.loads(command), image) as args:
        subprocess.run(args, check=True, timeout=60)
"""
    args = command(docker, image, cache, "printf started > /cache/started; while :; do printf x >> /cache/writes; sleep 0.05; done")
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), docker, image, engine, json.dumps(args)], cwd=ROOT,
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not (cache / "started").exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.02)
        assert (cache / "started").exists(), child.stderr.read() if child.poll() is not None else "fixture start timed out"
        os.killpg(child.pid, signal.SIGKILL)  # only this test's process group
        child.wait(timeout=5)
        journal = json.loads(next((tmp_path / "accounting" / "attempts").glob("*.json")).read_text())
        assert execution._inspect(docker, journal["name"])["State"]["Running"]
        from cage_core.monitoring.locks import _wait_for_volume_lock
        with _wait_for_volume_lock(tmp_path, "a" * 32):
            execution.recover(tmp_path, docker, "monitor:" + "a" * 32)
            assert execution._inspect(docker, journal["name"]) is None
            previous = (cache / "writes").read_bytes()
            time.sleep(0.15)
            assert (cache / "writes").read_bytes() == previous
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        child.stderr.close()
