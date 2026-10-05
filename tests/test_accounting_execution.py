"""Owned container cleanup cannot cross a job, engine or mount boundary."""

from contextlib import nullcontext
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from cage_core.accounting import execution, grants, runtime, store

IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64
ENGINE = "engine-test-1234"


def attempt():
    nonce = "c" * 32
    return dict(version=1, source="poke:codex-state-test", name="cage-accounting-" + nonce,
                nonce=nonce, job="d" * 64, image=IMAGE, mount_digest=grants.digest([]), engine=ENGINE, container_id=CONTAINER)


def observed(value):
    return {"Id": CONTAINER, "Name": "/" + value["name"], "Image": IMAGE,
            "Config": {"Labels": {"io.cage.accounting.job": value["job"], "io.cage.accounting.attempt": value["nonce"]}},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Mounts": []}}


@pytest.mark.parametrize("field", ["name", "image", "job", "nonce", "id", "mount", "network", "readonly"])
def test_recovery_refuses_a_container_with_changed_identity(tmp_path, field):
    journal = attempt()
    actual = observed(journal)
    if field == "name":
        actual["Name"] = "/other"
    elif field == "image":
        actual["Image"] = "sha256:" + "e" * 64
    elif field in {"job", "nonce"}:
        actual["Config"]["Labels"]["io.cage.accounting." + ("attempt" if field == "nonce" else field)] = "different"
    elif field == "id":
        actual["Id"] = "e" * 64
    elif field == "mount":
        actual["HostConfig"]["Mounts"] = [{"Type": "bind", "Source": "/other", "Target": "/state"}]
    elif field == "network":
        actual["HostConfig"]["NetworkMode"] = "host"
    else:
        actual["HostConfig"]["ReadonlyRootfs"] = False
    with patch.object(execution, "_inspect", return_value=actual), patch.object(execution.subprocess, "run") as run:
        with pytest.raises(store.AccountingError, match="identity changed"):
            execution._finish("docker", journal)
        run.assert_not_called()


def test_recovery_uses_exact_id_and_preserves_journal_on_wrong_engine(tmp_path):
    journal = attempt()
    with store.root(tmp_path) as fd, store.directory(fd, "attempts") as directory:
        store.write(directory, journal["nonce"] + ".json", journal)
    with patch.object(runtime, "_output", return_value="other-engine"), patch.object(execution, "_inspect") as inspect:
        with pytest.raises(store.AccountingError, match="original Docker engine"):
            execution.recover(tmp_path, "docker", journal["source"])
        inspect.assert_not_called()
    assert list((tmp_path / "accounting" / "attempts").glob("*.json"))
    with patch.object(runtime, "_output", return_value=ENGINE), patch.object(
        execution, "_inspect", side_effect=[observed(journal), None]
    ), patch.object(execution.subprocess, "run") as run:
        execution.recover(tmp_path, "docker", journal["source"])
        assert run.call_args.args[0] == ["docker", "rm", "-f", CONTAINER]
    assert not list((tmp_path / "accounting" / "attempts").glob("*.json"))


def test_create_is_journaled_before_any_start_and_unknown_create_is_retained(tmp_path):
    work = execution.Work(tmp_path, "d" * 64, nullcontext, ENGINE)
    command = ["docker", "run", "--rm", "--name", "previous-name", "--network", "none", "--read-only", "-i", "example", "-I", "-"]

    def create(args, **kwargs):
        journal = json.loads(next((tmp_path / "accounting" / "attempts").glob("*.json")).read_text())
        assert journal["engine"] == ENGINE
        assert args[1] == "create" and args.count("--name") == 1
        assert "--rm" in args and IMAGE in args
        raise subprocess.TimeoutExpired(args, 30)

    with execution.scope(work), patch.object(runtime, "image_id", return_value=IMAGE), patch.object(execution.subprocess, "run", side_effect=create):
        with pytest.raises(subprocess.TimeoutExpired):
            with execution.collector(tmp_path, "docker", "poke:codex-state-test", command, "example"):
                pytest.fail("start was admitted after an uncertain create")
    assert len(list((tmp_path / "accounting" / "attempts").glob("*.json"))) == 1


def test_start_command_keeps_stdin_and_journaled_id(tmp_path):
    work = execution.Work(tmp_path, "d" * 64, nullcontext, ENGINE)
    command = ["docker", "run", "--rm", "--network", "none", "--read-only", "-i", "example", "-I", "-"]
    made = subprocess.CompletedProcess([], 0, CONTAINER + "\n")
    with execution.scope(work), patch.object(runtime, "image_id", return_value=IMAGE), patch.object(
        execution.subprocess, "run", return_value=made
    ), patch.object(execution, "_finish"):
        with execution.collector(tmp_path, "docker", "poke:codex-state-test", command, "example") as start:
            assert start == ["docker", "start", "--attach", "--interactive", CONTAINER]
            journal = json.loads(next((tmp_path / "accounting" / "attempts").glob("*.json")).read_text())
            assert journal["container_id"] == CONTAINER
    assert not list((tmp_path / "accounting" / "attempts").glob("*.json"))


def test_inspect_unavailable_is_not_absence():
    failed = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
    with patch.object(execution.subprocess, "run", return_value=failed):
        with pytest.raises(store.AccountingError, match="reconcile"):
            execution._inspect("docker", "cage-accounting-test")


def test_endpoint_rebinds_and_restores_environment(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "ambient")
    monkeypatch.setenv("DOCKER_HOST", "unix:///ambient.sock")
    binding = {"environment": {"DOCKER_CONTEXT": "selected"}, "engine": ENGINE}
    with patch.object(runtime, "_output", return_value=ENGINE):
        with runtime.endpoint("docker", binding):
            assert runtime.os.environ.get("DOCKER_CONTEXT") == "selected"
            assert "DOCKER_HOST" not in runtime.os.environ
    assert runtime.os.environ["DOCKER_CONTEXT"] == "ambient"
    assert runtime.os.environ["DOCKER_HOST"] == "unix:///ambient.sock"
    binding["environment"] = {"DOCKER_HOST": "ssh://user:password@host"}
    with pytest.raises(store.AccountingError):
        runtime.validate_transport(binding)
