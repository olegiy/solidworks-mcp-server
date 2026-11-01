"""Portable mesh assets never authorize local reads from uploaded JSON."""

import hashlib
import json
from pathlib import Path

import pytest

from cadnext.mesh.contracts import MeshError, MeshReference
from tests.test_app import api as api
from tests.test_app import profile, start, wait
from tests.test_mesh_app import prepared, upload
from tests.test_mesh_loader import encoded_plate


def test_publish_uses_hash_and_survives_directory_move(tmp_path):
    from cadnext.mesh.persistence import publish_asset, validate_manifest

    raw = encoded_plate("obj")
    source = tmp_path / "input.obj"
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    reference = MeshReference("asset", digest, "obj", "mm", .1).snapshot()
    manifest = publish_asset(str(tmp_path / "part.sldprt"), source, reference)
    assert manifest["relative_path"] == f"part.sldprt.cadnext.assets/{digest}.obj"
    moved = tmp_path.with_name(tmp_path.name+"-moved")
    tmp_path.rename(moved)
    try:
        assert (moved / manifest["relative_path"]).read_bytes() == raw
        assert validate_manifest(manifest)["reference"]["sha256"] == digest
    finally:
        moved.rename(tmp_path)


@pytest.mark.parametrize("path", ["../secret.obj", "C:/secret.obj", "x.cadnext.assets/../secret.obj",
                                    "x.cadnext.assets\\secret.obj", "x/secret.obj"])
def test_manifest_rejects_unsafe_paths(path):
    from cadnext.mesh.persistence import validate_manifest

    ref = MeshReference("a", "a"*64, "obj", "mm", .1).snapshot()
    with pytest.raises(MeshError):
        validate_manifest({"reference": ref, "relative_path": path, "size_bytes": 5})


def test_publish_rejects_changed_source_and_reparse_directory(tmp_path, monkeypatch):
    from cadnext.mesh.persistence import publish_asset

    source = tmp_path / "input.obj"
    source.write_bytes(b"changed")
    ref = MeshReference("a", "a"*64, "obj", "mm", .1).snapshot()
    with pytest.raises(MeshError):
        publish_asset(str(tmp_path / "part.sldprt"), source, ref)
    ref["sha256"] = hashlib.sha256(b"changed").hexdigest()
    folder = tmp_path / "part.sldprt.cadnext.assets"
    folder.mkdir(exist_ok=True)
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == folder or original(path))
    with pytest.raises(MeshError):
        publish_asset(str(tmp_path / "part.sldprt"), source, ref)


def saved_mesh(client):
    saved = profile(client)
    assert upload(client).status_code == 202
    data = prepared(client)
    run = start(client, saved["id"], "mesh-inspect", mesh_reference={
        "asset_id": data["asset_id"], "tolerance_mm": .1}).json()
    wait(client, run["id"])
    response = client.post("/api/cad/save", json={"session_id": "session-1"})
    assert response.status_code == 200, response.text
    sidecar = json.loads(Path(response.json()["conversation"]).read_text(encoding="utf-8"))
    return saved, sidecar, response.json()


def test_save_open_remaps_import_and_rejects_reuse(api):
    client, _ = api
    saved, sidecar, response = saved_mesh(client)
    assert sidecar["version"] == 2
    manifest = sidecar["mesh"]
    path = Path(response["saved"]).parent / manifest["relative_path"]
    assert path.read_bytes() == encoded_plate("obj")
    assert len(json.dumps(sidecar)) < 100000
    staged = client.post("/api/conversation-imports", json={"mesh": manifest})
    assert staged.status_code == 201, staged.text
    import_id = staged.json()["import_id"]
    endpoint = f'/api/conversation-imports/{import_id}/{manifest["reference"]["sha256"]}'
    assert client.put(endpoint, content=path.read_bytes()).status_code == 204
    opened = client.post("/api/conversations/open", json={"conversation": sidecar, "mesh_import_id": import_id})
    assert opened.status_code == 200, opened.text
    result = opened.json()
    assert result["session_id"] != "session-1"
    assert result["runs"][-1]["mesh_verification"]["status"] == "unknown"
    asset = client.app.state.agent.mesh_tools.store.current(result["session_id"])
    assert asset.sha256 == manifest["reference"]["sha256"]
    again = client.post("/api/cad/save", json={"session_id": result["session_id"]})
    assert again.status_code == 200, again.text
    restored = json.loads(Path(again.json()["conversation"]).read_text(encoding="utf-8"))
    assert restored["mesh"]["relative_path"] == manifest["relative_path"]
    assert client.post("/api/conversations/open", json={"conversation": sidecar,
                      "mesh_import_id": import_id}).status_code == 422
    response = start(client, saved["id"], "mesh-inspect", session_id=result["session_id"],
                     mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
    assert response.status_code == 202, response.text
    wait(client, response.json()["id"])


def test_missing_asset_allows_history_but_blocks_continuation_before_cad(api):
    client, backend = api
    saved, sidecar, _ = saved_mesh(client)
    sidecar["runs"][-1]["mesh_verification"] = {"status": "pass", "reasons": []}
    response = client.post("/api/conversations/open", json={"conversation": sidecar})
    assert response.status_code == 200, response.text
    opened = response.json()
    assert opened["mesh_missing"]
    assert opened["runs"][-1]["verification"] == "unverified"
    before = len(backend.calls)
    assert start(client, saved["id"], session_id=opened["session_id"]).status_code == 422
    assert len(backend.calls) == before


def test_import_hash_substitution_and_wrong_manifest_are_rejected(api):
    client, _ = api
    _, sidecar, _ = saved_mesh(client)
    staged = client.post("/api/conversation-imports", json={"mesh": sidecar["mesh"]}).json()
    digest = sidecar["mesh"]["reference"]["sha256"]
    assert client.put(f'/api/conversation-imports/{staged["import_id"]}/{"0"*64}', content=b"bad").status_code == 422
    assert client.put(f'/api/conversation-imports/{staged["import_id"]}/{digest}', content=b"bad").status_code == 422
    assert not (client.app.state.mesh_import_store.root / staged["import_id"]).exists()
    assert client.post("/api/conversations/open", json={"conversation": sidecar,
                       "mesh_import_id": "unknown"}).status_code == 422


def test_asset_failure_reports_partial_save_without_retry(api, monkeypatch):
    import cadnext.app as app_module

    client, backend = api
    saved_mesh(client)
    before = len([call for call in backend.calls if call[0] == "save_document"])
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(app_module, "publish_asset", fail)
    response = client.post("/api/cad/save", json={"session_id": "session-1"})
    assert response.status_code == 200 and response.json()["warning"]
    assert "conversation" not in response.json()
    assert len([call for call in backend.calls if call[0] == "save_document"]) == before+1


def test_import_preserves_reference_frame_and_tolerance(api):
    client, _ = api
    saved, sidecar, _ = saved_mesh(client)
    ref = sidecar["mesh"]["reference"]
    transformed = MeshReference(ref["asset_id"], ref["sha256"], "obj", "mm", .25,
        ((1,0,0,20), (0,1,0,0), (0,0,1,0), (0,0,0,1))).snapshot()
    sidecar["mesh"]["reference"] = transformed
    staged = client.post("/api/conversation-imports", json={"mesh": sidecar["mesh"]}).json()
    assert client.put(f'/api/conversation-imports/{staged["import_id"]}/{ref["sha256"]}',
                      content=encoded_plate("obj")).status_code == 204
    opened = client.post("/api/conversations/open", json={"conversation": sidecar,
                         "mesh_import_id": staged["import_id"]}).json()
    state = client.get(f'/api/projects/{opened["session_id"]}/mesh').json()
    assert state["tolerance_mm"] == .25
    response = start(client, saved["id"], "mesh-inspect", session_id=opened["session_id"],
                     mesh_reference={"asset_id": state["asset_id"], "tolerance_mm": .25})
    assert response.status_code == 202, response.text
    run = wait(client, response.json()["id"])
    assert run["mesh_reference"]["frame_id"] == transformed["frame_id"]


def test_truncated_asset_does_not_replace_previous_publication(tmp_path):
    from cadnext.mesh.persistence import publish_asset

    raw = encoded_plate("obj")
    source = tmp_path / "input.obj"
    source.write_bytes(raw)
    reference = MeshReference("a", hashlib.sha256(raw).hexdigest(), "obj", "mm", .1).snapshot()
    manifest = publish_asset(str(tmp_path / "part.sldprt"), source, reference)
    source.write_bytes(raw[:100])
    with pytest.raises(MeshError):
        publish_asset(str(tmp_path / "part.sldprt"), source, reference)
    assert (tmp_path / manifest["relative_path"]).read_bytes() == raw
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("matrix", [[], [1], [[1]], None, "bad"])
def test_malformed_import_frame_is_a_validation_error(api, matrix):
    client, _ = api
    reference = MeshReference("a", "a"*64, "obj", "mm", .1).snapshot()
    reference["source_to_cad"] = matrix
    response = client.post('/api/conversation-imports', json={"mesh": {
        "reference": reference, "relative_path": f'x.cadnext.assets/{"a"*64}.obj', "size_bytes": 1}})
    assert response.status_code == 422


def test_portable_asset_can_be_opened_after_application_restart(api):
    from fastapi.testclient import TestClient

    from cadnext.app import create_app

    client, backend = api
    _, data, result = saved_mesh(client)
    source = Path(result["saved"]).parent / data["mesh"]["relative_path"]
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp, provider_factory=backend.provider)
    with TestClient(app, base_url='http://127.0.0.1:8876') as restarted:
        staged = restarted.post('/api/conversation-imports', json={"mesh": data["mesh"]}).json()
        digest = data["mesh"]["reference"]["sha256"]
        assert restarted.put(f'/api/conversation-imports/{staged["import_id"]}/{digest}',
                             content=source.read_bytes()).status_code == 204
        opened = restarted.post('/api/conversations/open', json={"conversation": data,
                                "mesh_import_id": staged["import_id"]})
        assert opened.status_code == 200 and not opened.json()["mesh_missing"]
        assert opened.json()["session_id"] == "session-1"
        root = app.state.agent.mesh_tools.store.root
    assert not root.exists()


def test_replacement_with_same_bytes_keeps_new_units_in_sidecar(api):
    client, _ = api
    saved_mesh(client)
    assert client.post('/api/projects/session-1/mesh?format=obj&units=in',
                       content=encoded_plate('obj')).status_code == 202
    prepared(client)
    result = client.post('/api/cad/save', json={'session_id': 'session-1'}).json()
    data = json.loads(Path(result['conversation']).read_text(encoding='utf-8'))
    assert data['mesh']['reference']['units'] == 'in'
    assert data['mesh']['reference']['scale_to_mm'] == 25.4


def test_current_source_is_saved_even_before_first_mesh_run(api):
    client, _ = api
    saved = profile(client)
    wait(client, start(client, saved['id']).json()['id'])
    assert upload(client).status_code == 202
    current = prepared(client)
    result = client.post('/api/cad/save', json={'session_id': 'session-1'}).json()
    data = json.loads(Path(result['conversation']).read_text(encoding='utf-8'))
    assert data['version'] == 2
    assert data['mesh']['reference']['sha256'] == current['sha256']
    assert data['mesh']['relative_path']
