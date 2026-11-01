"""Exercise owned geometry processes; no SolidWorks or MCP process is started."""

import asyncio
import sys

import pytest

from tests.test_mesh_loader import encoded_plate


def payload(tmp_path):
    import hashlib

    raw = encoded_plate("obj")
    path = tmp_path / "reference.obj"
    path.write_bytes(raw)
    return {"path": str(path), "format": "obj", "units": "mm",
            "sha256": hashlib.sha256(raw).hexdigest(), "frame_id": "source-mm"}


async def test_real_worker_inspects_without_blocking_event_loop_and_closes(tmp_path):
    from cadnext.mesh.worker_client import MeshWorkerClient

    client = MeshWorkerClient()
    task = asyncio.create_task(client.request("inspect", payload(tmp_path), stop=asyncio.Event()))
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(.01)
    try:
        response = await task
        assert response["summary"]["watertight"]
        assert response["sha256"] == payload(tmp_path)["sha256"]
        assert response["frame_id"] == "source-mm"
        assert ticks > 2
        process = client.process
    finally:
        await client.aclose()
    assert process.returncode == 0


async def test_worker_rejects_changed_hash_and_arbitrary_operation(tmp_path):
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    client = MeshWorkerClient()
    data = payload(tmp_path)
    try:
        with pytest.raises(MeshError):
            await client.request("execute_python", data, stop=asyncio.Event())
        data["sha256"] = "0"*64
        with pytest.raises(MeshError) as exc:
            await client.request("inspect", data, stop=asyncio.Event())
        assert exc.value.code == "hash_mismatch"
    finally:
        await client.aclose()


@pytest.mark.parametrize("mode", ["timeout", "stop", "cancel", "crash", "stale", "oversize"])
async def test_worker_failure_reaps_owned_child_before_reuse(tmp_path, mode):
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    script = tmp_path / "fake.py"
    script.write_text('''import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    mode = sys.argv[1]
    if mode == "crash": sys.exit(7)
    if mode == "stale":
        print(json.dumps({"request_id":"old", "sha256":"old", "frame_id":"old"}), flush=True)
    elif mode == "oversize": print("x"*2000000, flush=True)
    else: time.sleep(30)
''', encoding="utf-8")
    client = MeshWorkerClient(command=[sys.executable, str(script), mode], timeout_s=.5)
    stop = asyncio.Event()
    task = asyncio.create_task(client.request("inspect", payload(tmp_path), stop=stop))
    while client.process is None:
        await asyncio.sleep(.01)
    process = client.process
    if mode == "stop":
        stop.set()
    elif mode == "cancel":
        task.cancel()
    try:
        with pytest.raises((MeshError, asyncio.CancelledError)):
            await task
        assert process.returncode is not None
    finally:
        await client.aclose()


async def test_preexisting_stop_never_launches_worker(tmp_path):
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    stop = asyncio.Event()
    stop.set()
    client = MeshWorkerClient()
    with pytest.raises(MeshError):
        await client.request("inspect", payload(tmp_path), stop=stop)
    assert client.process is None
    await client.aclose()


async def test_windows_job_rejects_excess_allocation_in_owned_child(tmp_path):
    from cadnext.mesh.worker_client import MeshWorkerClient

    script = tmp_path / "allocation.py"
    script.write_text('''import json,sys
for line in sys.stdin:
    req=json.loads(line)
    try:
        block=bytearray(128*1024*1024)
        limited=False
    except MemoryError:
        limited=True
    print(json.dumps({"request_id":req["request_id"], "sha256":req["payload"]["sha256"],
          "frame_id":req["payload"]["frame_id"], "memory_limited":limited}), flush=True)
''', encoding="utf-8")
    client = MeshWorkerClient(command=[sys.executable, str(script)], memory_bytes=64*1024*1024)
    try:
        result = await client.request("inspect", payload(tmp_path), stop=asyncio.Event())
        assert result["memory_limited"]
    finally:
        await client.aclose()


async def test_stopped_request_can_be_followed_by_fresh_process(tmp_path):
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    client = MeshWorkerClient()
    stop = asyncio.Event()
    task = asyncio.create_task(client.request("inspect", payload(tmp_path), stop=stop))
    while client.process is None:
        await asyncio.sleep(.001)
    first = client.process
    stop.set()
    try:
        with pytest.raises(MeshError):
            await task
        assert first.returncode is not None
        result = await client.request("inspect", payload(tmp_path), stop=asyncio.Event())
        assert result["summary"]["watertight"]
        assert client.process is not first
    finally:
        await client.aclose()
