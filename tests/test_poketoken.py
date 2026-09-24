from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from cage_core import cli, config, poketoken
from cage_core import poketoken_records as records
from cage_core.lifecycle import LifecycleCoordinator
from cage_core.models import LaunchRequest
from cage_core.planning import PlanError, build_launch_plan
from cage_core.targets import container


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-09-24T10:00:00.000Z"
FINGERPRINT = {
    "name": "codex-state-example", "driver": "local", "scope": "local",
    "created_at": STAMP, "label_identity": "",
}


def event(kind, payload, timestamp=STAMP):
    return {"type": kind, "timestamp": timestamp, "payload": payload}


def rollout(session="session-one"):
    return [
        event("session_meta", {
            "id": session, "cwd": "/private/company/repository",
            "base_instructions": {"text": "private instructions"},
        }),
        event("turn_context", {"model": "gpt-5.4", "developer_instructions": "secret"}),
        event("response_item", {"type": "message", "content": "private conversation"}),
        event("event_msg", {"type": "token_count", "rate_limits": {"secret": "secret"}, "info": {
            "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                 "output_tokens": 50, "reasoning_output_tokens": 10, "total_tokens": 150},
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                  "output_tokens": 50, "reasoning_output_tokens": 10, "total_tokens": 150},
        }}),
    ]


def envelope(values=None, name="rollout-session-one.jsonl"):
    return records.encode({"name": name, "records": [
        clean for item in (rollout() if values is None else values)
        if (clean := records.sanitize_record(item)) is not None
    ]})


@pytest.fixture
def prepared(tmp_path):
    configuration = tmp_path / "config"
    configuration.mkdir(mode=0o700)
    repository = tmp_path / "repo"
    repository.mkdir()
    path = configuration / "config.toml"
    path.write_text('version=1\ndefault_preset="main"\n[presets.main]\ntool="codex"\nnet="open"\npoketoken=true\n')
    resolved = config.resolve_config(
        config.load_config(path), path, str(repository), mcp_inventory=False,
    )
    return configuration, build_launch_plan(
        LaunchRequest(repo_operand=str(repository)), resolved, cage_version="dev",
        config_root=configuration, install_root=ROOT,
    )


def run_sync(prepared, data=None):
    root, launch = prepared
    with patch.object(poketoken.volumes, "volume_fingerprint", return_value=FINGERPRINT), patch.object(
        poketoken, "_collect", return_value=envelope() if data is None else data,
    ):
        return poketoken.sync(root, "docker", ROOT, launch.plan)


def test_opt_in_is_visible_in_validated_plan_and_diagnostics(prepared, capsys):
    root, launch = prepared
    assert poketoken.CAPABILITY in launch.plan.public_dict()["selected_capabilities"]["runtime"]
    assert not any("poketoken" in mount.path for mount in launch.plan.mounts)
    assert poketoken.export_path(root) == root / "poketoken" / "exports"
    cli._run_poketoken(["status"], config_root=root, install_root=ROOT, cage_version="dev")
    assert str(poketoken.export_path(root)) in capsys.readouterr().out
    assert not (root / "poketoken").exists()


@pytest.mark.parametrize("tool,target", [("claude", "container"), ("opencode", "container"), ("codex", "host"), ("codex", "desktop")])
def test_only_codex_cli_container_presets_can_opt_in(tmp_path, tool, target):
    path = tmp_path / "config.toml"
    data = {"presets": {"main": {"tool": tool, "target": target, "poketoken": True, "net": "open"}}}
    with pytest.raises(config.ConfigError, match="poketoken"):
        config.resolve_config(data, path, str(tmp_path), preset_name="main", mcp_inventory=False)


@pytest.mark.parametrize("value", ['"true"', "1", "[]"])
def test_config_rejects_non_boolean_opt_in(value):
    with pytest.raises(config.ConfigError, match="poketoken must be true or false"):
        config.parse_config_text(f'[presets.main]\ntool="codex"\npoketoken={value}')


def test_target_override_fails_before_effects(prepared):
    root, launch = prepared
    path = root / "config.toml"
    resolved = config.resolve_config(config.load_config(path), path, launch.plan.repository, mcp_inventory=False)
    with pytest.raises(PlanError, match="poketoken"):
        build_launch_plan(
            replace(launch.request, target="host"), resolved, cage_version="dev",
            config_root=root, install_root=ROOT,
        )


def test_disabled_plan_cannot_export_or_start_worker(prepared):
    root, launch = prepared
    plan = replace(launch.plan, capabilities=())
    with patch.object(poketoken, "_store") as store, pytest.raises(poketoken.ExportError, match="requires"):
        poketoken.sync(root, "docker", ROOT, plan)
    store.assert_not_called()
    with patch.object(poketoken, "ActiveExport") as worker:
        container._start_poketoken_export(SimpleNamespace(plan=plan))
    worker.assert_not_called()


def test_worker_is_registered_and_final_scan_runs(prepared):
    root, launch = prepared
    lifecycle = LifecycleCoordinator()
    runtime = SimpleNamespace(plan=launch.plan, config_root=root, docker="docker", install_root=ROOT, lifecycle=lifecycle)
    with patch.object(poketoken, "sync") as sync:
        container._start_poketoken_export(runtime)
        assert lifecycle.cleanup() == 0
        assert sync.call_count == 2
        lifecycle.cleanup()
        assert sync.call_count == 2


def test_periodic_worker_retries_and_reports_final_errors(monkeypatch, capsys):
    scan = Mock(side_effect=poketoken.ExportError("accounting failed"))
    monkeypatch.setattr(poketoken, "INTERVAL_SECONDS", 0.01)
    worker = poketoken.ActiveExport(scan)
    deadline = poketoken.time.monotonic() + 1
    while scan.call_count < 2 and poketoken.time.monotonic() < deadline:
        poketoken.time.sleep(0.01)
    worker.stop()
    assert scan.call_count >= 3
    assert "cage poketoken status" in capsys.readouterr().err


def test_export_is_private_sanitized_stable_and_idempotent(prepared):
    root, _ = prepared
    destination = run_sync(prepared)
    file = next(destination.rglob("*.jsonl"))
    data = file.read_bytes()
    for secret in (b"private", b"secret", b"session-one", b"company", b"instructions", b"rate_limits"):
        assert secret not in data
    values = [json.loads(line) for line in data.splitlines()]
    assert values[-1]["payload"]["info"]["last_token_usage"]["total_tokens"] == 150
    assert len(values[0]["payload"]["id"]) == 64
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    mtime = file.stat().st_mtime_ns
    run_sync(prepared)
    assert file.stat().st_mtime_ns == mtime
    assert file.read_bytes() == data
    state = poketoken.status(root)
    assert len(state["sources"]) == 1
    assert state["sources"][0]["error"] is None


def test_multiple_volumes_share_one_scan_root_and_stable_session_ids(prepared):
    root, launch = prepared
    first = run_sync(prepared)
    other = root, replace(launch, plan=replace(launch.plan, volume_name="codex-state-other"))
    assert run_sync(other) == first
    files = list(first.rglob("*.jsonl"))
    assert len(files) == 2
    assert files[0].read_bytes() == files[1].read_bytes()


def test_active_archive_duplicates_and_fork_metadata_are_preserved():
    parent = rollout()
    child = rollout("child")
    child[0]["payload"].update(forked_from_id="session-one", source={"subagent": {"private": "secret"}})
    values = poketoken._prepare(envelope(parent) * 2 + envelope(child, "child.jsonl"), b"x" * 32)
    assert len(values) == 2
    metas = [json.loads(value.splitlines()[0])["payload"] for value in values.values()]
    assert metas[1]["forked_from_id"] == metas[0]["id"]
    assert metas[1]["thread_source"] == "subagent"
    assert "source" not in metas[1]


def test_truncation_or_invalid_output_retains_last_good_and_records_error(prepared):
    root, _ = prepared
    file = next(run_sync(prepared).rglob("*.jsonl"))
    good = file.read_bytes()
    with pytest.raises(poketoken.ExportError, match="shrank"):
        run_sync(prepared, envelope(rollout()[:2]))
    assert file.read_bytes() == good
    assert "shrank" in poketoken.status(root)["sources"][0]["error"]
    with pytest.raises(poketoken.ExportError):
        run_sync(prepared, b'{"name":"../../bad","records":[{"type":"event_msg","payload":"secret"}]}\n')
    assert file.read_bytes() == good
    run_sync(prepared, b"")
    assert file.read_bytes() == good


@pytest.mark.parametrize("component", ["poketoken", "exports", "source", "file", "identity", "lock"])
def test_rejects_symlink_destinations(prepared, tmp_path, component):
    root, _ = prepared
    file = next(run_sync(prepared).rglob("*.jsonl"))
    targets = {"poketoken": root / "poketoken", "exports": file.parent.parent,
               "source": file.parent, "file": file, "identity": root / "poketoken" / "identity",
               "lock": root / "poketoken" / "lock"}
    target = targets[component]
    original = target.with_name(target.name + ".original")
    target.rename(original)
    target.symlink_to(original, target_is_directory=original.is_dir())
    before = file.read_bytes()
    with pytest.raises(poketoken.ExportError):
        run_sync(prepared)
    assert file.read_bytes() == before


def test_rejects_hardlinked_export(prepared):
    file = next(run_sync(prepared).rglob("*.jsonl"))
    os.link(file, file.with_suffix(".copy"))
    with pytest.raises(poketoken.ExportError, match="unsafe"):
        run_sync(prepared)


def test_fingerprint_recheck_precedes_publication(prepared):
    root, launch = prepared
    changed = {**FINGERPRINT, "created_at": "later"}
    with patch.object(poketoken.volumes, "volume_fingerprint", side_effect=[FINGERPRINT, changed]), patch.object(
        poketoken, "_collect", return_value=envelope(),
    ), pytest.raises(poketoken.ExportError, match="changed"):
        poketoken.sync(root, "docker", ROOT, launch.plan)
    assert not list(poketoken.export_path(root).rglob("*.jsonl"))


def test_collector_is_network_free_and_never_mounts_host(prepared):
    _, launch = prepared
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write(envelope())
        assert b"def sanitize_record" in kwargs["input"]
        return SimpleNamespace(returncode=0)

    with patch.object(poketoken.collector, "_subpath_available", side_effect=[True, False]), patch.object(
        poketoken.subprocess, "run", side_effect=run,
    ):
        assert poketoken._collect("docker", launch.plan, ROOT) == envelope()
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
    assert len(mounts) == 1
    assert "readonly,volume-subpath=sessions,volume-nocopy" in mounts[0]
    assert "type=bind" not in str(command)
    assert not any(value in command for value in ("-v", "--privileged", "/var/run/docker.sock"))


def test_collection_timeout_removes_only_its_exact_container(prepared):
    _, launch = prepared
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 120)
        return SimpleNamespace(returncode=0)

    with patch.object(poketoken.collector, "_subpath_available", return_value=True), patch.object(
        poketoken.subprocess, "run", side_effect=run,
    ), pytest.raises(poketoken.ExportError, match="timed out"):
        poketoken._collect("docker", launch.plan, ROOT)
    assert calls[1] == ["docker", "rm", "-f", calls[0][calls[0].index("--name") + 1]]


def test_streaming_reader_strips_content_and_defers_partial_tail(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(records.encode(item) for item in rollout()) + b'{"type":"event_msg"')
    values = records.read_rollout(os.open(path, os.O_RDONLY), [0])
    assert len(values) == 3
    assert b"private" not in records.encode(values)


def test_model_only_metadata_nested_models_and_rate_only_events():
    meta = rollout()[0]
    meta["payload"]["model"] = "gpt-5.4"
    assert records.sanitize_record(meta)["payload"]["model"] == "gpt-5.4"
    context = event("turn_context", {"turn_context": {"model": "gpt-5.4", "cwd": "/private"}})
    assert records.sanitize_record(context)["payload"] == {"model": "gpt-5.4"}
    assert records.sanitize_record(event("event_msg", {"type": "token_count", "info": None})) is None
    nanos = "2026-09-24T10:00:00.123456789Z"
    namespaced = event("turn_context", {"model": "openai/gpt-5.4"}, nanos)
    assert records.sanitize_record(namespaced) == namespaced


def test_collector_rejects_source_links_and_fifos(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"secret")
    scan = tmp_path / "scan"
    scan.mkdir()
    link = scan / "rollout.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        records.collect([str(scan)])
    link.unlink()
    os.mkfifo(link)
    with pytest.raises(records.RecordError, match="unsafe"):
        records.collect([str(scan)])
    assert not capsys.readouterr().out


def test_invalid_timestamp_and_model_cannot_smuggle_text():
    with pytest.raises(records.RecordError, match="timestamp"):
        records.sanitize_record(event("turn_context", {"model": "gpt-5.4"}, "private data"))
    with pytest.raises(records.RecordError, match="model"):
        records.sanitize_record(event("turn_context", {"model": "/private/config"}))


@pytest.mark.parametrize("count", [-1, True, 1.2, records.MAX_COUNTER + 1])
def test_rejects_invalid_counters(count):
    item = rollout()[-1]
    item["payload"]["info"]["last_token_usage"]["input_tokens"] = count
    with pytest.raises(records.RecordError):
        records.sanitize_record(item)


def test_limits_fail_explicitly(tmp_path, monkeypatch):
    path = tmp_path / "large.jsonl"
    path.write_bytes(b"x" * 64 + b"\n")
    monkeypatch.setattr(records, "MAX_LINE", 32)
    with pytest.raises(records.RecordError, match="line exceeds"):
        records.read_rollout(os.open(path, os.O_RDONLY), [0])
    monkeypatch.setattr(poketoken.records, "MAX_OUTPUT", 4)
    with pytest.raises(poketoken.ExportError, match="output limit"):
        poketoken._prepare(b"large data", b"x" * 32)


def test_sync_cli_requires_config_opt_in_and_does_not_connect_hub(prepared, capsys):
    root, launch = prepared
    with patch.object(poketoken, "sync", return_value=poketoken.export_path(root)) as sync, patch.object(
        cli.storage, "docker_command", return_value="docker",
    ):
        assert cli._run_poketoken(
            ["sync", launch.plan.repository, "--preset", "main"],
            config_root=root, install_root=ROOT, cage_version="dev",
        ) == 0
    assert poketoken.CAPABILITY in sync.call_args.args[3].capabilities
    assert "Exported Codex accounting" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("CAGE_RUN_DOCKER_SMOKE") != "1", reason="opt-in Docker fixture")
def test_real_docker_export_preserves_source_and_counts(prepared, tmp_path):
    root, launch = prepared
    volume = "codex-state-poketoken-test-" + poketoken.secrets.token_hex(10)
    image = os.environ.get("CAGE_POKETOKEN_TEST_IMAGE", "codex:latest")
    plan = replace(launch.plan, volume_name=volume, image=image)
    subprocess.run(["docker", "volume", "create", volume], check=True, stdout=subprocess.DEVNULL)
    script = (
        "import os,pathlib,json\n"
        "p=pathlib.Path('/fixture/sessions');p.mkdir()\n"
        f"p.chmod(0o700);os.chown(p,{os.getuid()},{os.getgid()})\n"
        f"f=p/'rollout-test.jsonl';f.write_bytes({b''.join(records.encode(item) for item in rollout())!r})\n"
        f"f.chmod(0o600);os.chown(f,{os.getuid()},{os.getgid()})\n"
        "pathlib.Path('/fixture/auth.json').write_text('must-not-export')\n"
    )
    inspection = (
        "import hashlib,json,pathlib\n"
        "p=pathlib.Path('/fixture/sessions/rollout-test.jsonl');s=p.stat()\n"
        "print(json.dumps([hashlib.sha256(p.read_bytes()).hexdigest(),s.st_uid,s.st_gid,s.st_mode,s.st_mtime_ns,"
        "pathlib.Path('/fixture/auth.json').read_text(),sorted(x.name for x in p.parent.parent.iterdir())]))"
    )

    def inspect_source():
        return subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--user", "0:0",
             "--mount", f"type=volume,src={volume},dst=/fixture,readonly,volume-nocopy",
             "--entrypoint", "python3", image, "-c", inspection],
            check=True, capture_output=True,
        ).stdout

    try:
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--user", "0:0",
             "--mount", f"type=volume,src={volume},dst=/fixture", "--entrypoint", "python3", image, "-c", script],
            check=True,
        )
        before = inspect_source()
        destination = poketoken.sync(root, "docker", ROOT, plan)
        files = list(destination.rglob("*.jsonl"))
        assert len(files) == 1
        values = [json.loads(line) for line in files[0].read_bytes().splitlines()]
        assert values[-1]["payload"]["info"]["last_token_usage"]["total_tokens"] == 150
        assert "must-not-export" not in files[0].read_text()
        snapshot = files[0].read_bytes()
        poketoken.sync(root, "docker", ROOT, plan)
        assert files[0].read_bytes() == snapshot
        assert inspect_source() == before
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True, stdout=subprocess.DEVNULL)
