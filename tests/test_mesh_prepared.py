"""Preparation is reused only for the same verified source and context."""

import hashlib

import pytest

from tests.test_mesh_loader import encoded_plate


def source(tmp_path):
    raw = encoded_plate("obj")
    path = tmp_path / "plate.obj"
    path.write_bytes(raw)
    return {"path": str(path), "format": "obj", "units": "mm",
            "sha256": hashlib.sha256(raw).hexdigest(), "frame_id": "source-mm", "owner": "s"}


def test_preparation_reuses_verified_geometry_and_topology(tmp_path, monkeypatch):
    from cadnext.mesh import loader
    from cadnext.mesh.analysis import adjacency, face_geometry
    from cadnext.mesh.prepared import MeshCache

    data = source(tmp_path)
    calls = []
    original = loader._obj

    def parse(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(loader, "_obj", parse)
    cache = MeshCache()
    first = cache.get(data)
    second = cache.get(data)
    assert first is second
    assert len(calls) == 1
    assert face_geometry(first) is face_geometry(second)
    assert adjacency(first) is adjacency(second)
    assert not first.vertices.flags.writeable
    assert not first.faces.flags.writeable


async def test_invalidation_does_not_restart_an_exited_child():
    from types import SimpleNamespace

    from cadnext.mesh.worker_client import MeshWorkerClient

    client = MeshWorkerClient()
    client.process = SimpleNamespace(returncode=0)

    async def unexpected(*args, **kwargs):
        pytest.fail("Invalidation started a request for an exited worker")

    client.request = unexpected
    await client.forget("owner")


@pytest.mark.parametrize("field,value", [("owner", "other"), ("units", "in"),
                                        ("frame_id", "other-frame")])
def test_context_change_evicts_preparation(tmp_path, field, value):
    from cadnext.mesh.prepared import MeshCache

    cache = MeshCache()
    data = source(tmp_path)
    first = cache.get(data)
    assert cache.get({**data, field: value}) is not first


def test_cached_file_is_rehashed_even_with_same_size_and_timestamp(tmp_path):
    import os
    from pathlib import Path

    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.prepared import MeshCache

    data = source(tmp_path)
    cache = MeshCache()
    cache.get(data)
    path = Path(data["path"])
    stat = path.stat()
    raw = path.read_bytes().replace(b"20", b"21", 1)
    path.write_bytes(raw)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(MeshError) as error:
        cache.get(data)
    assert error.value.code == "hash_mismatch"


def test_transform_changes_coordinates_and_owner_forget_is_scoped(tmp_path):
    import numpy as np

    from cadnext.mesh.prepared import MeshCache

    data = source(tmp_path)
    cache = MeshCache()
    first = cache.get(data)
    moved = {**data, "source_to_cad": [[1, 0, 0, 7], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
    second = cache.get(moved)
    assert np.allclose(second.vertices, first.vertices+[7, 0, 0])
    cache.clear("another")
    assert cache.get(moved) is second
    cache.clear("s")
    assert cache.get(moved) is not second


async def test_recoverable_selection_keeps_worker_alive(tmp_path):
    import asyncio

    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    data = source(tmp_path)
    client = MeshWorkerClient()
    try:
        await client.request("inspect", data, stop=asyncio.Event())
        process = client.process
        with pytest.raises(MeshError) as error:
            await client.request("fit", {**data, "region": {"bounds_mm": [[0, 0, 1], [0, 0, 2]]},
                                         "kind": "plane", "tolerance_mm": .1}, stop=asyncio.Event())
        assert error.value.code == "empty_region"
        assert client.process is process and process.returncode is None
        assert (await client.request("inspect", data, stop=asyncio.Event()))["summary"]["watertight"]
    finally:
        await client.aclose()
