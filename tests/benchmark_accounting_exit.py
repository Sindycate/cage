"""Manual accounting-exit benchmark with synthetic histories and a local hub.

Requires an already installed versioned collector image (also contains Python).
Creates exact disposable volumes, containers and a private checkout directory.
No real Codex sessions, host configuration or external hub are used.

python3 tests/benchmark_accounting_exit.py --version 0.38.10 --samples 20
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cage_core import monitor, poketoken
from cage_core.accounting import backends, execution, lifecycle, queue, runtime, worker
from cage_core.lifecycle import LifecycleCoordinator
from cage_core.models import StoragePolicy


class Hub(BaseHTTPRequestHandler):
    observations = []

    def do_POST(self):
        assert self.path == "/api/ingest"
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.observations.append(data)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def run(docker, *args, **kwargs):
    return subprocess.run([docker, *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, **kwargs)


def history(docker, image, volume, size):
    stamp = datetime.now(timezone.utc).isoformat()
    script = r'''
import json,os
from pathlib import Path
root=Path('/fixture')
(root/'sessions').mkdir(exist_ok=True)
(root/'archived_sessions').mkdir(exist_ok=True)
stamp=STAMP
def event(kind,payload):
    return json.dumps(dict(type=kind,timestamp=stamp,payload=payload))+'\n'
with (root/'sessions'/'rollout-fixture.jsonl').open('w') as f:
    f.write(event('session_meta',dict(id='12345678-1234-1234-1234-123456789abc',cwd='/work/fixture',model_provider='openai')))
    f.write(event('turn_context',dict(model='gpt-5.4')))
    padding=event('response_item',dict(type='message',role='user',content=[dict(type='input_text',text='x'*8192)]))
    for _ in range(SIZE*1024*1024//len(padding)):
        f.write(padding)
    usage=dict(input_tokens=100,cached_input_tokens=20,output_tokens=50,reasoning_output_tokens=10,total_tokens=150)
    f.write(event('event_msg',dict(type='token_count',info=dict(last_token_usage=usage,total_token_usage=usage))))
'''.replace("STAMP", repr(stamp)).replace("SIZE", str(size))
    run(docker, "run", "--rm", "--network", "none", "--read-only", "-i", "--entrypoint", "python3",
        "--mount", f"type=volume,src={volume},dst=/fixture,volume-nocopy", image, "-I", "-", input=script.encode())


def wait_idle(root, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = queue.jobs(root)
        if any(j["phase"] in {"blocked", "retry_wait", "cancelled"} for j in jobs):
            raise RuntimeError(json.dumps(queue.status(root, "monitor") + queue.status(root, "poke")))
        if jobs and all(j["delivered"] == j["requested"] for j in jobs) and not worker.status(root)["running"]:
            return
        time.sleep(0.02)
    raise RuntimeError("fixture accounting did not complete within its deadline")


def close_pair(root, docker, pinned, sources):
    before = {j["id"]: j["requested"] for j in queue.jobs(root)}
    lifecycle_owner = LifecycleCoordinator()
    producers = []
    for source, permissions in sources:
        producer = lifecycle.Producer(root, docker, pinned, source, permissions, 3600)
        producers.append(producer)
        lifecycle_owner.register("fixture accounting", producer.stop, quiesce=producer.request_stop)
    started = time.perf_counter()
    assert lifecycle_owner.cleanup(23) == 23
    elapsed = time.perf_counter() - started
    for producer in producers:
        producer._thread.join(2)
    for job in queue.jobs(root):
        assert job["final"] == job["requested"] > before.get(job["id"], 0), "final handoff was not committed"
    return elapsed


def distribution(values):
    return {"n": len(values), "median_ms": round(statistics.median(values)*1000, 3),
            "p95_ms": round(sorted(values)[math.ceil(len(values)*0.95)-1]*1000, 3),
            "max_ms": round(max(values)*1000, 3)}


def benchmark(args):
    docker = shutil.which("docker")
    assert docker
    image = runtime.image_id(docker, "cage-token-monitor:" + args.version)
    transport_started = time.perf_counter()
    transport = runtime.transport(docker)
    transport_ms = (time.perf_counter() - transport_started) * 1000
    hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    server = threading.Thread(target=hub.serve_forever, daemon=True)
    server.start()
    results = {"image_version": args.version, "docker_binding_ms": round(transport_ms, 3), "fixtures": []}
    try:
        with tempfile.TemporaryDirectory(prefix=".accounting-benchmark-", dir=ROOT) as temporary:
            for size in args.sizes:
                root = Path(temporary) / str(size)
                root.mkdir(mode=0o700)
                volume = "codex-state-accounting-fixture-" + secrets.token_hex(12)
                nonce = secrets.token_hex(16)
                run(docker, "volume", "create", "--label", "io.cage.accounting.fixture=" + nonce, volume)
                try:
                    history(docker, image, volume, size)
                    record = monitor.VolumeRegistration(
                        secrets.token_hex(16), monitor.host_device_id(root), volume, "container", "/work/fixture", "Cage: fixture (Container)",
                        monitor.volume_fingerprint(docker, volume),
                    )
                    monitor.save_registry(root, [record])
                    monitor.save_connection(root, monitor.MonitorConnection(f"http://127.0.0.1:{hub.server_port}", "fixture-only"))
                    monitor.save_split_status(root, {"complete": True, "device_ids": []})
                    plan = SimpleNamespace(tool="codex", target="container", capabilities=(poketoken.CAPABILITY,),
                                           volume_name=volume, image=image, cage_version=args.version)
                    started = time.perf_counter()
                    pinned = runtime.snapshot(root, ROOT)
                    pin_cold = time.perf_counter() - started
                    started = time.perf_counter()
                    assert runtime.snapshot(root, ROOT) == pinned
                    pin_warm = time.perf_counter() - started
                    sources = [backends.monitor_source(root, record, transport, args.version, StoragePolicy()),
                               backends.poke_source(root, docker, plan, transport)]
                    synchronous = []
                    for _ in range(3):
                        started = time.perf_counter()
                        monitor.scan_registration(root, docker, ROOT, record, version=args.version, storage_policy=StoragePolicy(),
                                                  allow_build=False, force=True, final=True)
                        poketoken.sync(root, docker, ROOT, plan)
                        synchronous.append(time.perf_counter() - started)
                    cold = []
                    delivery = []
                    for _ in range(args.samples):
                        delivery_started = time.perf_counter()
                        cold.append(close_pair(root, docker, pinned, sources))
                        wait_idle(root)
                        delivery.append(time.perf_counter() - delivery_started)
                    # A final request starts genuine collection on the same
                    # frozen history. Measure other exits while it is active.
                    first_source, permissions = sources[0]
                    queue.enqueue(root, first_source, permissions, final=True)
                    worker.wake(root, docker, pinned)
                    deadline = time.monotonic() + 10
                    while not worker.status(root)["running"] and time.monotonic() < deadline:
                        time.sleep(0.005)
                    warm = [close_pair(root, docker, pinned, sources) for _ in range(args.burst_samples or args.samples)]
                    wait_idle(root)
                    aggregate = monitor.load_aggregate_status(root)
                    assert aggregate["total_tokens"] == 150, aggregate.get("total_tokens")
                    exports = list(poketoken.export_path(root).rglob("*.jsonl"))
                    assert len(exports) == 1 and "private conversation" not in exports[0].read_text()
                    for job in queue.jobs(root):
                        assert job["final"] <= job["delivered"] == job["requested"]
                    result = {"history_mib": size, "synchronous_first_ms": round(synchronous[0]*1000, 3),
                              "synchronous_repeated_ms": [round(x*1000, 3) for x in synchronous[1:]],
                              "cold_worker_handoff": distribution(cold), "running_worker_handoff": distribution(warm),
                              "final_delivery": distribution(delivery),
                              "runtime_pin_cold_ms": round(pin_cold*1000, 3), "runtime_pin_repeated_ms": round(pin_warm*1000, 3),
                              "verified_tokens": aggregate["total_tokens"], "verified_export_files": len(exports)}
                    results["fixtures"].append(result)
                    print(json.dumps(result, sort_keys=True), flush=True)
                finally:
                    # Revoke this fixture and reconcile only its owned attempts.
                    monitor.disable_connection(root)
                    from cage_core.accounting import grants
                    with grants.effects(root):
                        grants.revoke(root, ["poke"])
                    deadline = time.monotonic() + 30
                    while worker.status(root)["running"] and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert not worker.status(root)["running"], "fixture worker must stop before cleanup"
                    for path in (root / "accounting" / "attempts").glob("*.json"):
                        execution._finish(docker, json.loads(path.read_text()))
                    observed = json.loads(run(docker, "volume", "inspect", volume).stdout)[0]
                    assert observed["Name"] == volume and observed["Labels"]["io.cage.accounting.fixture"] == nonce
                    run(docker, "volume", "rm", volume)
    finally:
        hub.shutdown()
        hub.server_close()
        server.join(2)
    return results


def checked_benchmark(args):
    failures = []
    submit = lifecycle.Producer._submit

    def checked_submit(self, **kwargs):
        try:
            return submit(self, **kwargs)
        except Exception as exc:
            failures.append((self.source["backend"], kwargs, type(exc).__name__, str(exc)))
            raise

    with patch.object(lifecycle.Producer, "_submit", checked_submit):
        result = benchmark(args)
    assert not failures, failures
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.38.10")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--burst-samples", type=int, default=0)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1, 64])
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 <= args.burst_samples <= 1000 or any(not 1 <= size <= 256 for size in args.sizes):
        parser.error("use 1..100 samples and 1..256 MiB synthetic fixtures")
    print(json.dumps(checked_benchmark(args), indent=2, sort_keys=True))
