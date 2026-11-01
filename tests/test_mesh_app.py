"""Streamed mesh bytes are prepared without CAD and owned by one dialogue."""

import time
from dataclasses import replace

import pytest

from tests.test_app import api as api  # Shared real application fixture.
from tests.test_app import profile, start, wait
from tests.test_mesh_loader import encoded_plate


def upload(client, raw=None, *, session="session-1", **kwargs):
    return client.post(f"/api/projects/{session}/mesh?format=obj&units=mm",
                       content=encoded_plate("obj") if raw is None else raw, **kwargs)


def prepared(client, session="session-1"):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{session}/mesh")
        assert response.status_code == 200, response.text
        data = response.json()
        if data["state"] not in {"uploading", "processing"}:
            return data
        time.sleep(.01)
    pytest.fail("Mesh processing did not finish")


def test_upload_is_streamed_prepared_and_never_calls_cad(api):
    client, backend = api
    raw = encoded_plate("obj")
    response = upload(client, (raw[i:i+31] for i in range(0, len(raw), 31)))
    assert response.status_code == 202, response.text
    data = prepared(client)
    assert data["state"] == "ready" and data["summary"]["watertight"]
    assert data["size_bytes"] == len(raw)
    assert not backend.calls and not client.app.state.agent.runs
    assert "path" not in data


def test_false_content_length_cannot_replace_real_stream_limit(api):
    client, _ = api
    store = client.app.state.agent.mesh_tools.store
    store.limits = replace(store.limits, max_bytes=8)
    response = upload(client, b"123456789", headers={"Content-Length": "1"})
    assert response.status_code == 413
    assert store.current("session-1") is None


def test_failed_replacement_keeps_previous_reference(api):
    client, _ = api
    assert upload(client).status_code == 202
    original = prepared(client)
    assert upload(client, b"v 0 0 0\nf 1 2 3\n").status_code == 202
    rejected = prepared(client)
    assert rejected["state"] == "error"
    assert rejected["current"]["asset_id"] == original["asset_id"]
    assert client.app.state.agent.mesh_tools.store.current("session-1").asset_id == original["asset_id"]


def test_foreign_reference_and_bad_tolerance_are_rejected_before_cad(api):
    client, backend = api
    saved = profile(client)
    assert upload(client).status_code == 202
    asset = prepared(client)["asset_id"]
    for changes in ({"session_id": "other", "mesh_reference": {"asset_id": asset, "tolerance_mm": .1}},
                    {"mesh_reference": {"asset_id": asset, "tolerance_mm": 0}}):
        assert start(client, saved["id"], **changes).status_code == 422
    assert not backend.calls


def test_mesh_run_without_vision_measures_and_reports_unknown(api):
    client, _ = api
    saved = profile(client, vision=False)
    assert upload(client).status_code == 202
    data = prepared(client)
    response = start(client, saved["id"], "mesh-inspect", mesh_reference={
        "asset_id": data["asset_id"], "tolerance_mm": .1})
    assert response.status_code == 202, response.text
    run = wait(client, response.json()["id"])
    assert run["mesh_reference"]["sha256"] == data["sha256"]
    assert any(e["type"] == "tool_result" and e["data"]["name"] == "mesh_inspect" for e in run["events"])
    assert run["verification"] == "unverified"


def test_active_run_blocks_replacement_and_deletion(api):
    client, backend = api
    saved = profile(client)
    assert upload(client).status_code == 202
    data = prepared(client)
    response = start(client, saved["id"], "Прочитай долго", mesh_reference={
        "asset_id": data["asset_id"], "tolerance_mm": .1})
    run_id = response.json()["id"]
    import asyncio

    client.portal.call(asyncio.wait_for, backend.entered.wait(), 3)
    try:
        assert upload(client).status_code == 409
        assert client.delete("/api/projects/session-1/mesh").status_code == 409
    finally:
        client.post(f"/api/runs/{run_id}/stop")
        client.portal.call(backend.release.set)
        wait(client, run_id)


def test_delete_unbound_reference_and_explicit_units_validation(api):
    client, _ = api
    assert client.post("/api/projects/session-1/mesh?format=obj&units=auto", content=b"abc").status_code == 422
    assert upload(client).status_code == 202
    prepared(client)
    assert client.delete("/api/projects/session-1/mesh").status_code == 204
    assert client.get("/api/projects/session-1/mesh").json()["state"] == "empty"


def test_close_project_releases_mesh_bytes(api):
    client, _ = api
    assert upload(client).status_code == 202
    prepared(client)
    store = client.app.state.agent.mesh_tools.store
    asset = store.current("session-1")
    path = store.path_for("session-1", asset.asset_id)
    assert client.post("/api/projects/session-1/close").status_code == 204
    assert not path.exists()
    assert store.current("session-1") is None


