"""Process-boundary checks use protocol fixtures, not fake semantic evaluation."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import AppError
from app.retrieval_process import LocalInferenceProcess


@pytest.fixture
def client(tmp_path, monkeypatch):
    worker = tmp_path / "worker.py"
    marker = tmp_path / "crashed"
    worker.write_text(
        """import json, os, pathlib, sys, time
def send(v):
    print(json.dumps({"bge_rpc":1, **v}), flush=True)
send({"kind":"ready", "profile_id":"protocol-test"})
for line in sys.stdin:
    r=json.loads(line)
    if r["operation"]=="pid":
        send({"id":r["id"], "ok":True, "value":os.getpid()})
        continue
    if r["operation"]=="crash": os._exit(17)
    if r["operation"]=="once":
        p=pathlib.Path(sys.argv[1])
        if not p.exists():
            p.touch()
            os._exit(17)
    if r["operation"]=="timeout": time.sleep(10)
    send({"id":r["id"], "ok":True, "value":r["text"]})
""",
        encoding="utf-8",
    )
    instance = LocalInferenceProcess(tmp_path, "protocol-test", timeout=3)
    monkeypatch.setattr(
        instance, "_command", lambda: [sys.executable, "-u", str(worker), str(marker)]
    )
    yield instance
    instance.close()


def test_concurrent_responses_stay_with_their_request(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: client.call("echo", {"text": str(n)}), range(12)))
    assert results == [str(n) for n in range(12)]


def test_native_exit_restarts_only_the_pure_inference_worker(client):
    assert client.call("once", {"text": "original input"}) == "original input"
    assert client.restarts == 1
    assert client.call("echo", {"text": "host still alive"}) == "host still alive"


def test_repeated_native_exit_returns_controlled_failure(client):
    with pytest.raises(AppError) as error:
        client.call("crash", {"text": "input"})
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert client.restarts == 2
    assert client.process is None
    assert client.call("echo", {"text": "can recover later"}) == "can recover later"


def test_timeout_terminates_owned_worker_and_preserves_host(client):
    worker_pid = client.call("pid", {"text": "input"})
    handle = None
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, worker_pid)
        assert handle, "Must track the real interpreter, not only the venv launcher"
    client.timeout = 0.5
    with pytest.raises(AppError) as error:
        client.call("timeout", {"text": "input"})
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert client.process is None
    if handle:
        try:
            assert ctypes.windll.kernel32.WaitForSingleObject(handle, 1000) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    client.timeout = 3
    assert client.call("echo", {"text": "host alive"}) == "host alive"
