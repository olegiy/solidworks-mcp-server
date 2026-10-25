import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.web_fixture import Backend


@pytest.fixture
def api(tmp_path):
    from cadnext.app import create_app

    backend = Backend(tmp_path)
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp,
                     provider_factory=backend.provider)
    with TestClient(app, base_url="http://127.0.0.1:8876", raise_server_exceptions=False) as client:
        yield client, backend


def profile(client, **changes):
    response = client.post("/api/profiles", json={"name": "Тест", "base_url": "http://localhost/v1",
        "model": "fake-model", **changes})
    assert response.status_code == 201, response.text
    return response.json()


def start(client, profile_id, message="Прочитай размер", **changes):
    return client.post("/api/runs", json={"session_id": "session-1", "profile_id": profile_id,
                                          "message": message, **changes})


def wait(client, run_id):
    async def finish():
        await asyncio.wait_for(client.app.state.agent.wait(run_id), 3)
    client.portal.call(finish)
    return client.get(f"/api/runs/{run_id}").json()


def test_health_profiles_crud_and_secret_never_returned(api):
    client, backend = api
    assert backend.mcp_entered
    assert client.get("/api/health").json()["ok"]
    saved = profile(client, api_key="private-test-key")
    assert saved["has_key"]
    assert "private-test-key" not in str(saved)
    assert "api_key" not in saved
    assert client.get("/api/profiles").json() == [saved]
    updated = client.put(f'/api/profiles/{saved["id"]}', json={"model": "manual"})
    assert updated.json()["model"] == "manual"
    assert updated.json()["has_key"]
    assert client.delete(f'/api/profiles/{saved["id"]}').status_code == 204
    assert client.get("/api/profiles").json() == []
    assert backend.settings.secret(saved["id"]) is None


def test_profile_api_saves_reasoning_and_probe_uses_it(api):
    client, backend = api
    saved = profile(client, reasoning_effort="max")
    assert saved["reasoning_effort"] == "max"
    assert client.get(f'/api/profiles/{saved["id"]}').json()["reasoning_effort"] == "max"
    assert client.post(f'/api/profiles/{saved["id"]}/test').json()["ok"]
    assert backend.requests[-1]["reasoning_effort"] == "max"
    assert client.put(f'/api/profiles/{saved["id"]}', json={"model": "changed"}).json()["reasoning_effort"] == "max"
    invalid = client.put(f'/api/profiles/{saved["id"]}', json={"reasoning_effort": "unknown"})
    assert invalid.status_code == 422
    assert client.get(f'/api/profiles/{saved["id"]}').json()["reasoning_effort"] == "max"
    assert client.put(f'/api/profiles/{saved["id"]}', json={"reasoning_effort": None}).json()["reasoning_effort"] is None


def test_only_one_run_and_status_get_never_reexecutes(api):
    client, backend = api
    saved = profile(client)
    started = start(client, saved["id"], "Читай долго")
    assert started.status_code == 202
    run_id = started.json()["id"]
    client.portal.call(backend.entered.wait)
    assert start(client, saved["id"]).status_code == 409
    for _ in range(3):
        assert client.get(f"/api/runs/{run_id}").json()["id"] == run_id
    assert len(backend.calls) == 1
    assert client.get("/api/health").json()["active_run_id"] == run_id
    assert len(client.get("/api/runs?session_id=session-1").json()) == 1
    client.post(f"/api/runs/{run_id}/stop")
    client.portal.call(backend.release.set)
    assert wait(client, run_id)["status"] == "stopped"
    assert len(backend.calls) == 1


def test_bad_confirmation_id_is_rejected_and_exact_code_is_shown(api):
    client, backend = api
    run_id = start(client, profile(client)["id"], "Выполни код").json()["id"]

    async def pending():
        async with asyncio.timeout(2):
            while client.app.state.agent.get(run_id).confirmation is None:
                await asyncio.sleep(0)
    client.portal.call(pending)
    snapshot = client.get(f"/api/runs/{run_id}").json()
    assert snapshot["confirmation"]["code"] == "result = 62\nprint(result)"
    wrong = client.post(f"/api/runs/{run_id}/confirm", json={"confirmation_id": "wrong", "approved": True})
    assert wrong.status_code == 409
    assert backend.calls == []
    good = client.post(f"/api/runs/{run_id}/confirm", json={
        "confirmation_id": snapshot["confirmation"]["id"], "approved": False})
    assert good.status_code == 200
    assert wait(client, run_id)["status"] == "rejected"
    assert backend.calls == []


def test_run_keeps_original_profile_while_settings_change(api):
    client, backend = api
    saved = profile(client)
    run_id = start(client, saved["id"], "Читай долго").json()["id"]
    client.portal.call(backend.entered.wait)
    client.put(f'/api/profiles/{saved["id"]}', json={"model": "changed"})
    client.portal.call(backend.release.set)
    assert wait(client, run_id)["status"] == "completed"
    assert [request["model"] for request in backend.requests] == ["fake-model", "fake-model"]


def test_profile_edit_between_snapshot_and_provider_creation_keeps_snapshot(api, monkeypatch):
    client, backend = api
    saved = profile(client)
    original_get = backend.settings.get
    edit_pending = True

    def concurrent_edit(profile_id):
        nonlocal edit_pending
        snapshot = original_get(profile_id)
        if edit_pending:
            edit_pending = False
            backend.settings.save({"model": "changed"}, profile_id)
        return snapshot

    monkeypatch.setattr(backend.settings, "get", concurrent_edit)
    run_id = start(client, saved["id"]).json()["id"]
    assert wait(client, run_id)["status"] == "completed"
    assert backend.requests[0]["model"] == "fake-model"


@pytest.mark.parametrize("vision", [False, True])
def test_external_requests_do_not_prompt_and_images_follow_profile_vision(api, vision):
    client, backend = api
    saved = profile(client, base_url="https://example.test/v1", vision=vision)
    for session, changes in [("session-1", {}), ("another", {}), ("session-1", {"model": "changed"})]:
        if changes:
            client.put(f'/api/profiles/{saved["id"]}', json=changes)
        run_id = start(client, saved["id"], r"Покажи снимок C:\Company\part.sldprt private-project",
                       session_id=session, sensitive=["private-project"]).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "completed"
        assert not any(event["type"] == "confirmation" for event in result["events"])
    outgoing = json.dumps(backend.requests)
    assert ("image_url" in outgoing) is vision
    assert "Company" not in outgoing
    assert "private-project" not in outgoing


def test_invalid_payload_and_internal_errors_do_not_expose_secrets(api, monkeypatch):
    client, backend = api
    response = client.post("/api/profiles", json={"api_key": {"secret": "private-test-key"}})
    assert response.status_code == 422
    assert "private-test-key" not in response.text

    def fail(*args, **kwargs):
        raise RuntimeError("private-test-key C:\\Company\\secret")

    monkeypatch.setattr(backend.settings, "save", fail)
    response = client.post("/api/profiles", json={"name": "P", "model": "m", "base_url": "http://localhost/v1"})
    assert response.status_code == 500
    assert "private-test-key" not in response.text
    assert "Company" not in response.text


def test_models_probe_and_static_page_do_not_touch_cad(api):
    client, backend = api
    saved = profile(client)
    assert client.get("/api/models", params={"profile_id": saved["id"]}).json() == {
        "models": [{"id": "fake-model", "reasoning": None}]}
    assert client.post(f'/api/profiles/{saved["id"]}/test').json()["ok"]
    assert backend.calls == []
    assert client.get("/api/nonexistent").status_code == 404


def test_models_for_unsaved_connection_use_current_url_and_key_without_saving(api):
    client, backend = api
    response = client.post("/api/models", json={
        "base_url": "https://catalog.test/v1/", "api_key": "draft-key"})
    assert response.status_code == 200, response.text
    assert response.json() == {"models": [{"id": "fake-model", "reasoning": None}]}
    request, = backend.model_requests
    assert str(request.url) == "https://catalog.test/v1/models"
    assert request.headers["authorization"] == "Bearer draft-key"
    assert backend.settings.list() == []
    assert not backend.settings.path.exists()
    assert backend.closed == 1
    assert backend.calls == []
    assert "draft-key" not in response.text


def test_openrouter_model_catalog_returns_reasoning_metadata_without_secret(api):
    client, backend = api
    saved = profile(client, base_url="https://openrouter.ai/api/v1", model="deepseek/test",
                    api_key="openrouter-secret")
    response = client.get("/api/models", params={"profile_id": saved["id"]})
    assert response.status_code == 200
    assert response.json() == {"models": [{"id": "fake-model", "reasoning": {
        "mandatory": False, "default_effort": "high",
        "supported_efforts": ["max", "high", "low", "none"]}}]}
    assert "openrouter-secret" not in response.text


def test_reasoning_lookup_uses_public_openrouter_without_saved_provider_key(api):
    client, backend = api
    profile(client, base_url='https://another.test/v1', api_key='only-for-other-provider')
    response = client.get('/api/reasoning', params={'model': 'fake-model'})
    assert response.status_code == 200
    assert response.json()['reasoning']['supported_efforts'] == ['max', 'high', 'low', 'none']
    request, = backend.model_requests
    assert str(request.url) == 'https://openrouter.ai/api/v1/models'
    assert 'authorization' not in request.headers
    assert not request.content
    assert 'only-for-other-provider' not in response.text
    assert backend.calls == []


@pytest.mark.parametrize(("changes", "expected_url", "authorization"), [
    ({}, "https://saved.test/v1/models", "Bearer saved-key"),
    ({"api_key": "replacement-key"}, "https://saved.test/v1/models", "Bearer replacement-key"),
    ({"api_key": ""}, "https://saved.test/v1/models", None),
    ({"base_url": "https://another.test/v1"}, "https://another.test/v1/models", None),
])
def test_models_draft_key_is_scoped_to_current_endpoint(api, changes, expected_url, authorization):
    client, backend = api
    saved = profile(client, base_url="https://saved.test/v1", api_key="saved-key")
    response = client.post("/api/models", json={"profile_id": saved["id"],
        "base_url": "https://saved.test/v1/", **changes})
    assert response.status_code == 200, response.text
    request, = backend.model_requests
    assert str(request.url) == expected_url
    assert request.headers.get("authorization") == authorization
    assert client.get(f'/api/profiles/{saved["id"]}').json() == saved
    assert backend.settings.secret(saved["id"]) == "saved-key"


def test_models_draft_rejects_invalid_url_and_secret_payload_without_network(api):
    client, backend = api
    for body in ({"base_url": "javascript:alert(1)", "api_key": "private-key"},
                 {"base_url": "https://catalog.test/v1", "api_key": {"secret": "private-key"}}):
        response = client.post("/api/models", json=body)
        assert response.status_code == 422
        assert "private-key" not in response.text
    assert backend.model_requests == []


def test_sse_replay_uses_ids_and_last_event_id_without_new_calls(api):
    client, backend = api
    run_id = start(client, profile(client)["id"]).json()["id"]
    snapshot = wait(client, run_id)
    response = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "2"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    data = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
            if line.startswith("data: ")]
    assert [event["seq"] for event in data] == list(range(3, len(snapshot["events"]) + 1))
    assert len(backend.calls) == 1
    assert client.get(f"/api/runs/{run_id}/events?after=9999").status_code == 422


def test_foreign_origin_and_host_are_blocked(api):
    client, _ = api
    response = client.post("/api/profiles", json={}, headers={"Origin": "https://foreign.test"})
    assert response.status_code == 403
    assert client.get("/api/health", headers={"Host": "foreign.test:8876"}).status_code == 400


def test_pending_request_stop_covers_both_http_arrival_orders(api):
    client, backend = api
    saved = profile(client)
    assert client.post('/api/requests/closed-first/stop').status_code == 204
    assert start(client, saved['id'], request_id='closed-first').status_code == 409
    assert backend.calls == []
    run_id = start(client, saved['id'], 'Читай долго', request_id='started-first').json()['id']
    client.portal.call(backend.entered.wait)
    assert client.post('/api/requests/started-first/stop').status_code == 204
    client.portal.call(backend.release.set)
    assert wait(client, run_id)['status'] == 'stopped'
    assert len(backend.calls) == 1
    assert start(client, saved['id'], request_id='started-first').status_code == 409


def test_provider_is_closed_after_run_and_mcp_lifespan_is_owned(tmp_path):
    from cadnext.app import create_app

    backend = Backend(tmp_path)
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp, provider_factory=backend.provider)
    with TestClient(app, base_url="http://127.0.0.1:8876") as client:
        run_id = start(client, profile(client)["id"]).json()["id"]
        wait(client, run_id)
    assert backend.closed == 1
    assert backend.mcp_closed


def test_main_refuses_occupied_port_before_constructing_application(monkeypatch):
    import socket

    from cadnext.__main__ import main

    created = []
    monkeypatch.setattr("cadnext.__main__.create_app", lambda: created.append(True))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8876))
        listener.listen()
        with pytest.raises(SystemExit):
            main()
    assert created == []


async def test_sse_disconnect_without_beacon_waits_for_cad_and_blocks_next_call(tmp_path):
    import httpx

    from cadnext.app import create_app

    backend = Backend(tmp_path)
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp, provider_factory=backend.provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8876") as client:
            saved = backend.settings.save({"name": "P", "model": "m", "base_url": "http://localhost/v1"})
            response = await client.post("/api/runs", json={"session_id": "s", "profile_id": saved.id,
                                                           "message": "Читай долго"})
            run_id = response.json()["id"]
            await asyncio.wait_for(backend.entered.wait(), 2)
            disconnect = asyncio.Event()
            streamed = asyncio.Event()
            sent_request = False

            async def receive():
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    streamed.set()

            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                     "http_version": "1.1", "method": "GET", "scheme": "http", "root_path": "",
                     "path": f"/api/runs/{run_id}/events", "query_string": b"",
                     "headers": [(b"host", b"127.0.0.1:8876")],
                     "client": ("127.0.0.1", 43210), "server": ("127.0.0.1", 8876)}
            stream = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(streamed.wait(), 2)
                disconnect.set()
                await asyncio.wait_for(stream, 2)
                run = app.state.agent.get(run_id)
                assert run.status == "stopping"
                assert not run._task.done()
                assert len(backend.calls) == 1
            finally:
                backend.release.set()
                await asyncio.wait_for(app.state.agent.wait(run_id), 2)
                if not stream.done():
                    disconnect.set()
                    await asyncio.wait_for(stream, 2)
            assert run.status == "stopped"
            assert len(backend.calls) == 1
