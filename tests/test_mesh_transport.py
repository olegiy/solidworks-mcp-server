"""Large results are atomic and verified across the real process boundary."""

import asyncio
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("corrupt", ["ok", "data", "index", "digest", "missing"])
async def test_large_result_is_reassembled_or_rejected_atomically(tmp_path, corrupt):
    from cadnext.mesh.contracts import MeshError
    from cadnext.mesh.worker_client import MeshWorkerClient

    script = tmp_path / "paged.py"
    script.write_text('import sys\nsys.path.insert(0, '+repr(str(Path(__file__).resolve().parents[1]))+')\n'+r'''import json, sys
from cadnext.mesh.transport import ResultTransport
t = ResultTransport()
for line in sys.stdin:
    q = json.loads(line)
    envelope = {"request_id":q["request_id"], "sha256":q["payload"]["sha256"], "frame_id":q["payload"]["frame_id"]}
    if q["operation"] == "result_page":
        result = t.page(q["payload"])
        if sys.argv[1] == "data": result["part"]["data"] = "AAAA"
        if sys.argv[1] == "index": result["part"]["index"] += 1
        if sys.argv[1] == "missing": sys.exit(0)
    else:
        result = {"points": list(range(300000))}
    encoded = t.encode({**envelope, **result})
    if sys.argv[1] == "digest" and "artifact" in json.loads(encoded):
        value = json.loads(encoded)
        value["artifact"]["sha256"] = "0"*64
        encoded = json.dumps(value).encode()+b"\n"
    sys.stdout.write(encoded.decode())
    sys.stdout.flush()
''', encoding="utf-8")
    client = MeshWorkerClient(command=[sys.executable, str(script), corrupt])
    try:
        call = client.request("inspect", {"sha256": "a"*64, "frame_id": "f"}, stop=asyncio.Event())
        if corrupt != "ok":
            with pytest.raises(MeshError) as error:
                await call
            assert error.value.code == ("worker_crash" if corrupt == "missing" else "worker_failure")
            assert client.process is None
        else:
            result = await call
            assert len(result["points"]) == 300000
            assert result["points"][0] == 0 and result["points"][-1] == 299999
            assert "artifact" not in result
    finally:
        await client.aclose()


async def test_real_large_section_retains_all_source_faces(tmp_path):
    import hashlib
    import json

    import trimesh

    from cadnext.mesh.worker_client import MeshWorkerClient

    path = tmp_path / "dense.stl"
    raw = trimesh.creation.cylinder(radius=10, height=5, sections=12000).export(file_type="stl")
    path.write_bytes(raw)
    data = {"path": str(path), "format": "stl", "units": "mm", "sha256": hashlib.sha256(raw).hexdigest(),
            "frame_id": "source-mm", "tolerance_mm": .1, "frame": {
                "origin_mm": [0, 0, 0], "x_axis": [1, 0, 0], "y_axis": [0, 1, 0], "normal": [0, 0, 1]}}
    client = MeshWorkerClient()
    try:
        result = await client.request("section", data, stop=asyncio.Event())
        section = result["section"]
        assert section["usable_for_sketch"]
        assert len(section["chains"]) == 1
        assert len(section["chains"][0]["source_face_ids"]) == 24000
        assert len(json.dumps(result).encode()) > 1024*1024
    finally:
        await client.aclose()
