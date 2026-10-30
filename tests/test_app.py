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
    payload = {"session_id": "session-1", "profile_id": profile_id, "message": message,
               "evaluation": {"enabled": False}, **changes}
    return client.post("/api/runs", json=payload)


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


def test_debug_settings_capture_model_and_cad_and_survive_project_closure(api):
    import io
    import zipfile

    from cadnext.settings import SettingsStore
    from tests.test_settings import MemorySecrets

    client, backend = api
    assert client.get('/api/settings').json()['debug_logging_enabled'] is False
    saved = profile(client, api_key='debug-private-key')
    enabled = client.put('/api/settings', json={'debug_logging_enabled': True})
    assert enabled.status_code == 200
    assert enabled.json()['debug_logging_enabled'] is True
    assert SettingsStore(backend.settings.path.parent, MemorySecrets()).debug_logging_enabled is True
    run = start(client, saved['id'], 'Прочитай размер').json()
    assert wait(client, run['id'])['status'] == 'completed'
    assert client.post('/api/projects/session-1/close').status_code == 204
    response = client.get('/api/debug/logs')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        text = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
    records = [json.loads(line) for line in text.splitlines()]
    assert any(r['event'] == 'model_request' for r in records)
    assert any(r['event'] == 'model_response' for r in records)
    assert any(r['event'] == 'model_request' and r['run_id'] == run['id'] for r in records)
    assert any(r['event'] == 'mcp_result' and r['run_id'] == run['id'] for r in records)
    events = [r for r in records if r['event'] == 'run_event' and r['run_id'] == run['id']]
    assert any(r['event_type'] == 'tool_start' and r['data']['name'] == 'get_dimension' for r in events)
    assert any(r['event_type'] == 'tool_result' and r['data']['result']['ok'] for r in events)
    assert any(r['event'] == 'project_closed' for r in records)
    assert 'debug-private-key' not in text
    assert client.put('/api/settings', json={'debug_logging_enabled': False}).status_code == 200
    size = client.app.state.debug.path.stat().st_size
    client.get('/api/profiles')
    assert client.app.state.debug.path.stat().st_size == size
    assert client.put('/api/settings', json={'debug_logging_enabled': 'true'}).status_code == 422
    assert client.put('/api/settings', json={'debug_logging_enabled': None}).status_code == 422


def test_debug_enable_failure_keeps_setting_disabled(api, monkeypatch):
    client, backend = api

    def fail():
        raise OSError('Log directory unavailable')

    monkeypatch.setattr(client.app.state.debug, 'prepare', fail)
    response = client.put('/api/settings', json={'debug_logging_enabled': True})
    assert response.status_code == 500
    assert 'отладки' in response.json()['detail']
    assert backend.settings.debug_logging_enabled is False
    assert client.get('/api/settings').json()['debug_logging_enabled'] is False


def test_global_python_permission_is_persisted_and_validated(api):
    from cadnext.settings import SettingsStore
    from tests.test_settings import MemorySecrets

    client, backend = api
    settings = client.get('/api/settings').json()
    assert settings['python_execution_allowed'] is False
    assert settings['vision_profile_id'] is None
    response = client.put('/api/settings', json={'python_execution_allowed': True})
    assert response.status_code == 200
    assert response.json()['python_execution_allowed'] is True
    restored = SettingsStore(backend.settings.path.parent, MemorySecrets())
    assert restored.python_execution_allowed is True
    saved = profile(client)
    run = start(client, saved['id'], message='Выполни код').json()
    assert wait(client, run['id'])['status'] == 'completed'
    assert backend.calls[0][0] == 'execute_python'
    assert client.put('/api/settings', json={'python_execution_allowed': 'true'}).status_code == 422
    assert client.put('/api/settings', json={'python_execution_allowed': False}).status_code == 200
    settings = client.get('/api/settings').json()
    assert settings['python_execution_allowed'] is False
    assert settings['vision_profile_id'] is None
    assert client.put('/api/settings', json={'python_execution_allowed': True, 'extra': 1}).status_code == 422


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


def test_profile_api_persists_streaming_flag_and_rejects_non_booleans(api):
    client, _ = api
    saved = profile(client, stream_responses=True)
    assert saved["stream_responses"] is True
    endpoint = f'/api/profiles/{saved["id"]}'
    assert client.put(endpoint, json={"model": "changed"}).json()["stream_responses"] is True
    for invalid in (None, 1, "true"):
        assert client.put(endpoint, json={"stream_responses": invalid}).status_code == 422
    assert client.get(endpoint).json()["stream_responses"] is True
    assert client.put(endpoint, json={"stream_responses": False}).json()["stream_responses"] is False


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
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        reserved = listener.getsockname()
        original_bind = socket.socket.bind

        def bind_reserved(sock, address):
            return original_bind(sock, reserved if address == ("127.0.0.1", 8876) else address)

        monkeypatch.setattr(socket.socket, "bind", bind_reserved)
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
            response = await client.post("/api/runs", json={
                "session_id": "s", "profile_id": saved.id, "message": "Читай долго",
                "evaluation": {"enabled": False}})
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


def test_language_header_localizes_api_errors(api):
    client, _ = api
    response = client.post('/api/runs', headers={'Accept-Language': 'en-US,en;q=0.9'}, json={})
    assert response.status_code == 422
    assert 'Invalid request fields' in response.json()['detail']

    response = client.post('/api/runs', headers={'Accept-Language': 'ru-RU,ru;q=0.9'}, json={})
    assert response.status_code == 422
    assert 'Некорректные поля запроса' in response.json()['detail']

    response = client.post('/api/profiles', headers={'Accept-Language': 'en-US,en;q=0.9'},
                           json={'name': '', 'base_url': 'not-a-url', 'model': ''})
    assert response.status_code == 422
    assert response.json()['detail'] == 'Invalid settings or request parameters.'


def test_run_language_selects_english_prompt_and_messages(api):
    client, backend = api
    saved = profile(client)
    response = client.post('/api/runs', headers={'Accept-Language': 'en-US'},
                           json={'session_id': 'english-session', 'profile_id': saved['id'],
                                 'message': 'Read size', 'language': 'en', 'evaluation': {'enabled': False}})
    assert response.status_code == 202, response.text
    run = response.json()
    assert run['language'] == 'en'
    finished = wait(client, run['id'])
    assert finished['status'] == 'completed'
    statuses = [event['data']['message'] for event in finished['events']
                if event['type'] == 'status']
    assert 'The request completed.' in statuses
    assert all('Запрос' not in message for message in statuses)
    assert 'Respond in English' in backend.requests[0]['messages'][0]['content']


def test_run_language_defaults_to_russian(api):
    client, backend = api
    saved = profile(client)
    run = start(client, saved['id']).json()
    assert run['language'] == 'ru'
    finished = wait(client, run['id'])
    assert finished['status'] == 'completed'
    assert 'Запрос завершён.' in [event['data']['message'] for event in finished['events']
                                   if event['type'] == 'status']
    assert 'Отвечай по-русски' in backend.requests[0]['messages'][0]['content']


def test_save_project_saves_active_document_and_conversation(api, tmp_path):
    client, backend = api
    saved = profile(client)
    run = start(client, saved['id']).json()
    assert wait(client, run['id'])['status'] == 'completed'
    response = client.post('/api/cad/save', json={'session_id': run['session_id']})
    assert response.status_code == 200, response.text
    body = response.json()
    sidecar = tmp_path / 'Деталь1.cadnext.json'
    assert body['saved'] == backend.document_path
    assert body['conversation'] == str(sidecar)
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    assert data['schema'] == 'cadnext.conversation'
    assert data['version'] == 1
    assert data['session_id'] == run['session_id']
    assert data['document'] == {'path': backend.document_path}
    assert data['messages'][-1]['role'] == 'assistant'
    assert data['runs'][-1]['status'] == 'completed'
    assert data['runs'][-1]['messages'] == []
    assert [(name, arguments) for name, arguments in backend.calls[-3:]] == [
        ('connect_solidworks', {}), ('list_open_documents', {}),
        ('save_document', {'save_as_path': ''})]


def test_save_project_chooses_default_path_for_unsaved_document(api, tmp_path, monkeypatch):
    import cadnext.agent as agent_module
    monkeypatch.setattr(agent_module, '_documents_folder', lambda: tmp_path)
    client, backend = api
    saved = profile(client)
    run = start(client, saved['id']).json()
    assert wait(client, run['id'])['status'] == 'completed'
    backend.document_path = ''
    response = client.post('/api/cad/save', json={'session_id': run['session_id']})
    assert response.status_code == 200, response.text
    path = backend.calls[-1][1]['save_as_path']
    assert path.endswith('.sldprt')
    assert 'Деталь1' in path
    body = response.json()
    assert body['saved'] == path
    assert body['conversation'] == str(tmp_path / 'Деталь1.cadnext.json')
    assert (tmp_path / 'Деталь1.cadnext.json').is_file()


def test_save_project_requires_an_active_document(api):
    client, backend = api
    backend.active_document = None
    response = client.post('/api/cad/save', headers={'Accept-Language': 'en-US'},
                           json={'session_id': 'session-1'})
    assert response.status_code == 409
    assert 'no active SolidWorks document' in response.json()['detail']


def test_saved_conversation_opens_and_continues(api, tmp_path):
    client, backend = api
    saved = profile(client)
    run = start(client, saved['id']).json()
    assert wait(client, run['id'])['status'] == 'completed'
    response = client.post('/api/cad/save', json={'session_id': run['session_id']})
    assert response.status_code == 200, response.text
    data = json.loads((tmp_path / 'Деталь1.cadnext.json').read_text(encoding='utf-8'))
    opened = client.post('/api/conversations/open', json={'conversation': data})
    assert opened.status_code == 200, opened.text
    loaded = opened.json()
    assert loaded['session_id'] != run['session_id']
    assert loaded['document'] == {'path': backend.document_path}
    assert loaded['runs'][-1]['status'] == 'completed'
    history = client.get(f"/api/runs?session_id={loaded['session_id']}").json()
    assert history
    assert any(event['type'] == 'message' and event['data']['content'] == 'Прочитай размер'
               for event in history[0]['events'])
    response = client.post('/api/runs', json={'session_id': loaded['session_id'],
                                              'profile_id': saved['id'],
                                              'message': 'Прочитай размер',
                                              'evaluation': {'enabled': False}})
    assert response.status_code == 202, response.text
    wait(client, response.json()['id'])
    sent = backend.requests[-1]['messages']
    assert any(message.get('role') == 'user' and message.get('content') == 'Прочитай размер'
               for message in sent)
    assert any(message.get('role') == 'assistant'
               and 'Размер 62' in str(message.get('content')) for message in sent)


def test_conversation_open_rejects_invalid_file(api):
    client, _ = api
    response = client.post('/api/conversations/open', headers={'Accept-Language': 'en-US'},
                           json={'conversation': {'schema': 'wrong'}})
    assert response.status_code == 422
    assert 'Unsupported conversation format' in response.json()['detail']


def test_close_project_ends_only_its_history_and_rejects_old_session(api):
    client, _ = api
    saved = profile(client)
    first = start(client, saved['id'], 'First project').json()
    wait(client, first['id'])
    other = start(client, saved['id'], 'Other project', session_id='other-project').json()
    wait(client, other['id'])
    response = client.post('/api/projects/session-1/close')
    assert response.status_code == 204
    assert client.get('/api/runs?session_id=session-1').json() == []
    assert len(client.get('/api/runs?session_id=other-project').json()) == 1
    assert start(client, saved['id'], 'Stale request').status_code == 409
    fresh = start(client, saved['id'], 'New project', session_id='fresh-project').json()
    assert wait(client, fresh['id'])['status'] == 'completed'


def test_closed_solidworks_document_ends_project_but_refresh_keeps_history(api):
    client, backend = api
    saved = profile(client, vision=True)
    first = start(client, saved['id'], 'оценка-100', evaluation={'enabled': True}).json()
    completed = wait(client, first['id'])
    state = client.get('/api/projects/session-1/state')
    assert state.status_code == 200
    assert state.json()['closed'] is False
    assert client.get('/api/runs?session_id=session-1').json()[0] == completed
    backend.active_document = None
    state = client.get('/api/projects/session-1/state')
    assert state.json()['closed'] is True
    assert client.get('/api/runs?session_id=session-1').json() == []
    assert start(client, saved['id'], 'Do not continue the closed document').status_code == 409


def test_reopened_project_can_be_closed_without_losing_other_history(api, tmp_path):
    client, _ = api
    saved = profile(client)
    first = start(client, saved['id']).json()
    wait(client, first['id'])
    assert client.post('/api/cad/save', json={'session_id': 'session-1'}).status_code == 200
    data = json.loads((tmp_path / 'Деталь1.cadnext.json').read_text(encoding='utf-8'))
    opened = client.post('/api/conversations/open', json={'conversation': data}).json()
    response = client.post(f'/api/projects/{opened["session_id"]}/close')
    assert response.status_code == 204
    assert len(client.get('/api/runs?session_id=session-1').json()) == 1


def test_opened_history_waits_for_its_document_before_detecting_closure(api, tmp_path):
    client, backend = api
    saved = profile(client, vision=True)
    first = start(client, saved['id'], 'оценка-100', evaluation={'enabled': True}).json()
    wait(client, first['id'])
    assert client.post('/api/cad/save', json={'session_id': 'session-1'}).status_code == 200
    data = json.loads((tmp_path / 'Деталь1.cadnext.json').read_text(encoding='utf-8'))
    backend.active_document = None
    opened = client.post('/api/conversations/open', json={'conversation': data}).json()
    session = opened['session_id']
    assert client.get(f'/api/projects/{session}/state').json() == {'closed': False}
    assert len(client.get(f'/api/runs?session_id={session}').json()) == 1
    backend.active_document = 'Деталь1'
    assert client.get(f'/api/projects/{session}/state').json() == {'closed': False}
    backend.active_document = None
    assert client.get(f'/api/projects/{session}/state').json() == {'closed': True}


def test_project_inspection_keeps_history_on_switch_and_known_cad_error(api):
    from tests.fake_mcp import ok

    client, backend = api
    saved = profile(client, vision=True)
    first = start(client, saved['id'], 'оценка-100', evaluation={'enabled': True}).json()
    completed = wait(client, first['id'])
    original = backend.call

    async def switched(name, arguments):
        if name == 'list_open_documents':
            return ok({'documents': [
                {'title': 'Other', 'path': 'C:/Other.sldprt'},
                {'title': 'Деталь1', 'path': backend.document_path.upper()}]})
        return await original(name, arguments)

    backend.call = switched
    assert client.get('/api/projects/session-1/state').json() == {'closed': False}

    async def error(name, arguments):
        return {'ok': False, 'value': {'status': 'error'}, 'error': 'CAD_ERROR',
                'uncertain': False, 'images': []}

    backend.call = error
    assert client.get('/api/projects/session-1/state').json() == {'closed': False}
    assert client.get('/api/runs?session_id=session-1').json() == [completed]


def test_project_inspection_unknown_outcome_survives_project_close(api):
    client, backend = api
    saved = profile(client, vision=True)
    first = start(client, saved['id'], 'оценка-100', evaluation={'enabled': True}).json()
    completed = wait(client, first['id'])

    async def unknown(name, arguments):
        return {'ok': False, 'value': None, 'error': 'Unknown outcome',
                'uncertain': True, 'images': []}

    backend.call = unknown
    assert client.get('/api/projects/session-1/state').json() == {'closed': False}
    assert client.get('/api/runs?session_id=session-1').json() == [completed]
    assert client.post('/api/projects/session-1/close').status_code == 204
    assert client.get('/api/health').json()['uncertain'] is True
    assert start(client, saved['id'], session_id='fresh').status_code == 409


def test_project_poll_does_not_touch_cad_while_run_is_active(api):
    client, backend = api
    saved = profile(client)
    first = start(client, saved['id'], 'Читай долго').json()
    client.portal.call(backend.entered.wait)
    calls = list(backend.calls)
    assert client.get('/api/projects/session-1/state').json() == {'closed': False}
    assert backend.calls == calls
    client.post(f'/api/runs/{first["id"]}/stop')
    client.portal.call(backend.release.set)
    assert wait(client, first['id'])['status'] == 'stopped'


def test_opening_uncertain_conversation_blocks_new_runs(api, tmp_path):
    client, backend = api
    saved = profile(client)
    run = start(client, saved['id']).json()
    assert wait(client, run['id'])['status'] == 'completed'
    assert client.post('/api/cad/save', json={'session_id': run['session_id']}).status_code == 200
    data = json.loads((tmp_path / 'Деталь1.cadnext.json').read_text(encoding='utf-8'))
    data['runs'][-1]['status'] = 'uncertain'
    opened = client.post('/api/conversations/open', json={'conversation': data})
    assert opened.status_code == 200, opened.text
    assert client.get('/api/health').json()['uncertain'] is True
    response = client.post('/api/runs', headers={'Accept-Language': 'en-US'},
                           json={'session_id': opened.json()['session_id'],
                                 'profile_id': saved['id'], 'message': 'Read size'})
    assert response.status_code == 409
    assert 'outcome is unknown' in response.json()['detail']

def vision_profile(client, **changes):
    return profile(client, name="Vision", vision=True,
                   base_url="https://vision.example.test/v1", **changes)


def test_settings_vision_routing_round_trip_and_partial_updates(api):
    client, backend = api
    text = profile(client)
    vision = vision_profile(client)
    initial = client.get("/api/settings").json()
    assert initial["vision_profile_id"] is None
    assert initial["vision_routing_approved"] is False
    response = client.put("/api/settings", json={
        "vision_profile_id": vision["id"], "vision_routing_approved": True})
    assert response.status_code == 200, response.text
    routing = response.json()
    assert routing["vision_profile_id"] == vision["id"]
    assert routing["vision_routing_approved"] is True
    assert len(routing["vision_routing_fingerprint"]) == 64
    assert client.put("/api/settings", json={"python_execution_allowed": True}).json()[
        "vision_profile_id"] == vision["id"]
    assert client.put("/api/settings", json={"vision_routing_approved": False}).json()[
        "vision_profile_id"] is None
    assert client.put("/api/settings", json={"vision_profile_id": text["id"],
                                             "vision_routing_approved": True}).status_code == 422
    assert client.put("/api/settings", json={"vision_routing_approved": True}).status_code == 422


def test_evaluation_requires_approved_vision_routing_before_any_request(api):
    client, backend = api
    text = profile(client)
    response = start(client, text["id"], "Прочитай размер", evaluation={"enabled": True})
    assert response.status_code == 422
    assert "Vision" in response.json()["detail"]
    assert backend.requests == []
    assert client.get("/api/health").json()["active_run_id"] is None


def test_auto_vision_uses_effective_profile_and_reports_both_ids(api):
    client, backend = api
    text = profile(client)
    vision = vision_profile(client)
    client.put("/api/settings", json={"vision_profile_id": vision["id"],
                                     "vision_routing_approved": True})
    run = start(client, text["id"], "Прочитай размер", evaluation={"enabled": True}).json()
    assert run["profile_id"] == vision["id"]
    assert run["requested_profile_id"] == text["id"]
    selected = [event for event in run["events"] if event["type"] == "profile_selected"]
    assert selected and selected[-1]["data"]["profile_id"] == vision["id"]
    finished = wait(client, run["id"])
    assert finished["profile_id"] == vision["id"]
    assert [item.id for item in backend.profiles] == [vision["id"]]


def test_evaluation_disabled_keeps_requested_profile(api):
    client, backend = api
    text = profile(client)
    run = start(client, text["id"], "Прочитай размер",
                evaluation={"enabled": False}).json()
    assert run["profile_id"] == text["id"]
    wait(client, run["id"])
    assert [item.id for item in backend.profiles] == [text["id"]]


def test_revoked_or_stale_routing_never_opens_provider(api):
    client, backend = api
    text = profile(client)
    vision = vision_profile(client)
    client.put("/api/settings", json={"vision_profile_id": vision["id"],
                                     "vision_routing_approved": True})
    client.put(f"/api/profiles/{vision['id']}", json={"model": "changed"})
    assert start(client, text["id"], "Прочитай размер", evaluation={"enabled": True}).status_code == 422
    assert backend.profiles == []


def test_attachments_follow_effective_vision_profile(api):
    client, backend = api
    text = profile(client)
    vision = vision_profile(client)
    client.put("/api/settings", json={"vision_profile_id": vision["id"],
                                     "vision_routing_approved": True})
    response = start(client, text["id"], "Посмотри", evaluation={"enabled": True}, attachments=[{
        "name": "a.png", "mimeType": "image/png",
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}])
    assert response.status_code == 202, response.text
    wait(client, response.json()["id"])
    assert backend.profiles[-1].vision is True

def test_goal_run_exposes_evaluation_and_events(api):
    client, backend = api
    saved = vision_profile(client)
    run = start(client, saved["id"], "оценка-100", evaluation={"enabled": True}).json()
    finished = wait(client, run["id"])
    assert finished["status"] == "completed"
    assert finished["goal"]["outcome"] == "achieved"
    assert finished["goal"]["evaluations"][-1]["score"] == 100
    types = [event["type"] for event in finished["events"]]
    assert "goal_defined" in types and "goal_evaluation" in types and "goal_finished" in types
    state = client.app.state.agent.get(run["id"]).state()
    assert state["goal"]["evaluation_count"] == 1
    assert "evaluations" not in state["goal"]
    assert "contract" not in state["goal"]


def test_goal_survives_sidecar_open(api, tmp_path):
    client, backend = api
    saved = vision_profile(client)
    run = start(client, saved["id"], "оценка-100", evaluation={"enabled": True}).json()
    assert wait(client, run["id"])["goal"]["outcome"] == "achieved"
    assert client.post("/api/cad/save", json={"session_id": run["session_id"]}).status_code == 200
    data = json.loads((tmp_path / "Деталь1.cadnext.json").read_text(encoding="utf-8"))
    opened = client.post("/api/conversations/open", json={"conversation": data})
    assert opened.status_code == 200, opened.text
    goal = opened.json()["runs"][-1]["goal"]
    assert goal["version"] == 1 and goal["outcome"] == "achieved"
    assert goal["evaluations"][-1]["score"] == 100


def test_invalid_goal_options_are_rejected_before_start(api):
    client, backend = api
    saved = vision_profile(client)
    for evaluation in ({"target_similarity": 0}, {"target_similarity": 101},
                       {"max_evaluations": 0}, {"max_no_progress": 11},
                       {"unknown": True}):
        response = start(client, saved["id"], "x", evaluation=evaluation)
        assert response.status_code == 422
    assert backend.requests == []
    assert client.get("/api/health").json()["active_run_id"] is None


def test_route_to_vision_keeps_requested_and_effective_ids_with_goal(api):
    client, backend = api
    text = profile(client)
    vision = vision_profile(client)
    client.put("/api/settings", json={"vision_profile_id": vision["id"],
                                     "vision_routing_approved": True})
    run = start(client, text["id"], "оценка-100", evaluation={"enabled": True}).json()
    finished = wait(client, run["id"])
    assert finished["requested_profile_id"] == text["id"]
    assert finished["profile_id"] == vision["id"]
    assert finished["goal"]["outcome"] == "achieved"
    assert [item.id for item in backend.profiles] == [vision["id"]]
