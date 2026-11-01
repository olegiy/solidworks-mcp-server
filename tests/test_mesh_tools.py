"""Local summaries expose bounded measurements and keep source ownership private."""

import asyncio
import json

import pytest

from tests.test_mesh_assets import chunks
from tests.test_mesh_loader import encoded_plate


async def ready_tools(tmp_path):
    from cadnext.mesh.assets import MeshAssetStore
    from cadnext.mesh.tools import MeshTools

    store = MeshAssetStore(tmp_path)
    asset = await store.receive("s", chunks(encoded_plate("obj")), format="obj", units="mm")
    store.accept("s", asset.asset_id)
    tools = MeshTools(store)
    return tools, asset


async def test_inspection_later_pages_preserve_global_count_and_offset(tmp_path):
    import trimesh

    tools, _ = await ready_tools(tmp_path)
    raw = trimesh.creation.icosphere(subdivisions=4).export(file_type='stl')
    asset = await tools.store.receive('s', chunks(raw), format='stl', units='mm')
    tools.store.accept('s', asset.asset_id)
    binding = tools.bind('s', {'asset_id': asset.asset_id, 'tolerance_mm': .1}, run_id='run')
    try:
        first = await tools.dispatch(binding, 'mesh_inspect', {}, stop=asyncio.Event())
        second = await tools.dispatch(binding, 'mesh_inspect', {'offset': 100}, stop=asyncio.Event())
        assert first['next_offset'] == 100 and second['next_offset'] == 200
        assert second['total'] == first['total'] > 1000
        assert len(second['regions']) == 100
        assert not ({r['region_id'] for r in first['regions']} & {r['region_id'] for r in second['regions']})
    finally:
        await tools.aclose()


async def test_tools_measure_without_leaking_paths_points_or_obj_metadata(tmp_path):
    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")
    try:
        summary = await tools.dispatch(binding, "mesh_inspect", {}, stop=asyncio.Event())
        assert summary["summary"]["watertight"]
        assert str(tmp_path) not in json.dumps(summary)
        assert "path" not in summary
        section = await tools.dispatch(binding, "mesh_section", {"frame": {
            "origin_mm": [0, 0, 2.5], "x_axis": [1, 0, 0], "y_axis": [0, 1, 0],
            "normal": [0, 0, 1]}}, stop=asyncio.Event())
        assert section["usable_for_sketch"]
        assert len(section["segments"]) == 6
        assert "points_mm" not in json.dumps(section)
        assert len(json.dumps(section).encode()) <= 65536
        assert binding.evidence and "chains" in binding.evidence[-1]["section"]
        unavailable = await tools.dispatch(binding, "mesh_compare", {}, stop=asyncio.Event())
        assert unavailable["status"] == "unavailable"
    finally:
        await tools.aclose()


async def test_reference_must_belong_to_current_session_and_be_ready(tmp_path):
    from cadnext.mesh.contracts import MeshError

    tools, asset = await ready_tools(tmp_path)
    try:
        for session, spec in (("other", {"asset_id": asset.asset_id, "tolerance_mm": .1}),
                              ("s", {"asset_id": asset.asset_id, "tolerance_mm": 0})):
            with pytest.raises(MeshError):
                tools.bind(session, spec, run_id="run")
        next_asset = await tools.store.receive("s", chunks(b"not validated"), format="stl", units="mm")
        with pytest.raises(MeshError):
            tools.bind("s", {"asset_id": next_asset.asset_id, "tolerance_mm": .1}, run_id="run")
    finally:
        await tools.aclose()


@pytest.mark.parametrize("field", ["sha256", "frame_id", "request_id"])
async def test_stale_worker_envelope_is_rejected_by_client(tmp_path, field):
    # The client validates these fields on the wire (worker tests); the dispatcher
    # independently protects bindings when a client is replaced or adapted.
    from cadnext.mesh.contracts import MeshError

    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")

    class WrongWorker:
        async def request(self, operation, data, *, stop):
            return {"sha256": data["sha256"], "frame_id": data["frame_id"],
                    "request_id": "1"*32, field: "wrong", "summary": {}}

        async def aclose(self):
            pass

    tools.worker = WrongWorker()
    with pytest.raises(MeshError):
        await tools.dispatch(binding, "mesh_inspect", {}, stop=asyncio.Event())
    assert not binding.evidence


async def test_summary_and_section_pages_obey_segment_and_byte_limits(tmp_path):
    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")
    try:
        first = await tools.dispatch(binding, "mesh_section", {"frame": {
            "origin_mm": [0, 0, 2.5], "x_axis": [1, 0, 0], "y_axis": [0, 1, 0],
            "normal": [0, 0, 1]}}, stop=asyncio.Event())
        again = await tools.dispatch(binding, "mesh_section", {
            "section_id": first["section_id"], "offset": 3}, stop=asyncio.Event())
        assert len(again["segments"]) == 3
        assert again["segments"][0] == first["segments"][3]
    finally:
        await tools.aclose()


async def test_local_dispatch_rejects_injected_internal_fields(tmp_path):
    from cadnext.mesh.contracts import MeshError

    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")
    other = tmp_path / "other.obj"
    other.write_bytes(encoded_plate("obj"))
    try:
        with pytest.raises(MeshError):
            await tools.dispatch(binding, "mesh_inspect", {"path": str(other)}, stop=asyncio.Event())
        assert not binding.evidence
    finally:
        await tools.aclose()


@pytest.mark.parametrize("matrix", [
    [[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]],
    [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
])
def test_reference_rejects_scaling_and_reflection(matrix):
    from cadnext.mesh.contracts import MeshError, MeshReference

    with pytest.raises(MeshError):
        MeshReference("a", "0"*64, "stl", "mm", .1, matrix)
