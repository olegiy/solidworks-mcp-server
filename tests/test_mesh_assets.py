"""Asset lifetime and failures must not lose a previously accepted reference."""

import asyncio
import hashlib

import pytest


async def chunks(raw):
    for i in range(0, len(raw), 7):
        yield raw[i:i+7]


async def test_received_asset_is_hashed_owned_and_waits_for_geometry_validation(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore

    store = MeshAssetStore(tmp_path)
    asset = await store.receive("dialogue", chunks(b"mesh bytes"), format="stl", units="mm")
    assert asset.state == "processing"
    assert asset.sha256 == hashlib.sha256(b"mesh bytes").hexdigest()
    assert store.path_for("dialogue", asset.asset_id).read_bytes() == b"mesh bytes"
    assert store.current("dialogue") is None
    store.accept("dialogue", asset.asset_id)
    assert store.current("dialogue").asset_id == asset.asset_id


async def test_wrong_owner_cannot_read_or_remove_asset(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore
    from cadnext.mesh.contracts import MeshError

    store = MeshAssetStore(tmp_path)
    asset = await store.receive("first", chunks(b"mesh"), format="obj", units="in")
    for operation in (store.path_for, store.reject):
        with pytest.raises(MeshError):
            operation("other", asset.asset_id)
    with pytest.raises(MeshError):
        await store.receive("../escape", chunks(b"mesh"), format="stl", units="mm")


async def test_rejected_replacement_preserves_accepted_asset(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore

    store = MeshAssetStore(tmp_path)
    first = await store.receive("session", chunks(b"first"), format="stl", units="mm")
    store.accept("session", first.asset_id)
    second = await store.receive("session", chunks(b"invalid"), format="stl", units="mm")
    store.reject("session", second.asset_id)
    assert store.current("session").asset_id == first.asset_id
    assert store.path_for("session", first.asset_id).read_bytes() == b"first"
    assert len(list(tmp_path.rglob("*.*"))) == 1


async def test_interrupted_upload_cleans_only_its_temporary_file(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore

    store = MeshAssetStore(tmp_path)

    async def interrupted():
        yield b"part"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await store.receive("session", interrupted(), format="stl", units="mm")
    assert not list(tmp_path.rglob("*.upload"))
    assert store.current("session") is None
    assert (await store.receive("session", chunks(b"next"), format="obj", units="mm")).state == "processing"


async def test_stream_limit_and_pending_reference_block_unbounded_storage(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore
    from cadnext.mesh.contracts import MeshError, MeshLimits

    store = MeshAssetStore(tmp_path, limits=MeshLimits(max_bytes=8))
    with pytest.raises(MeshError):
        await store.receive("session", chunks(b"123456789"), format="stl", units="mm")
    assert not list(tmp_path.rglob("*.upload"))
    await store.receive("session", chunks(b"1234"), format="stl", units="mm")
    with pytest.raises(MeshError):
        await store.receive("session", chunks(b"5678"), format="stl", units="mm")


async def test_asset_path_is_never_accepted_from_caller(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore
    from cadnext.mesh.contracts import MeshError

    store = MeshAssetStore(tmp_path)
    with pytest.raises(MeshError):
        store.path_for("session", "../../sensitive")
