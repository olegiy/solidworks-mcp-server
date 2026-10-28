import gc
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import solidworks_mcp_server as server


@pytest.fixture
def cad(monkeypatch):
    document = SimpleNamespace(GetTitle="Part", changed=0)
    app = SimpleNamespace(ActiveDoc=document, GetDocuments=[document])
    monkeypatch.setattr(server, "_app", lambda: app)
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "com_call", lambda job, **kwargs: job())
    return app, document


def execute(code, session):
    return json.loads(server.execute_python(code, session_id=session))


def test_session_preserves_variables_and_partial_changes_but_not_last_result(cad):
    app, document = cad
    session = json.loads(server.open_python_session())["session_id"]
    try:
        assert execute("length = 80\ndef size(): return length\nresult = size()", session)["result"] == "80"
        assert execute("model.changed += 1\nlength = 100\nraise ValueError('partial')", session)["status"] == "error"
        assert document.changed == 1
        assert execute("result = size()", session)["result"] == "100"
        assert execute("length += 1", session)["result"] is None
        assert json.loads(server.execute_python("result = length"))["error_type"] == "NameError"
    finally:
        server.close_python_session(session)
    assert execute("result = length", session)["status"] == "error"


def test_sessions_do_not_share_state_or_replace_a_closed_document(cad):
    app, document = cad
    first = json.loads(server.open_python_session())["session_id"]
    second = json.loads(server.open_python_session())["session_id"]
    try:
        assert execute("marker = 9", first)["status"] == "ok"
        assert execute("result = marker", second)["error_type"] == "NameError"
        replacement = SimpleNamespace(GetTitle="Other", changed=0)
        app.ActiveDoc, app.GetDocuments = replacement, [replacement]
        assert execute("model.changed += 1", first)["status"] == "error"
        assert replacement.changed == 0
    finally:
        server.close_python_session(first)
        server.close_python_session(second)


def test_dimension_context_suppresses_modal_input_and_restores_on_error(cad):
    app, document = cad
    enabled = {10: True}
    app.GetUserPreferenceToggle = enabled.get
    app.SetUserPreferenceToggle = lambda key, value: enabled.update({key: value}) or True
    session = json.loads(server.open_python_session())["session_id"]
    try:
        result = execute("with DIMENSIONS():\n    result = swApp.GetUserPreferenceToggle(10)", session)
        assert result["result"] == "False"
        assert enabled[10] is True
        execute("with DIMENSIONS():\n    raise ValueError('partial')", session)
        assert enabled[10] is True
    finally:
        server.close_python_session(session)


def test_switching_active_document_cannot_silently_redirect_session_mutations(cad):
    app, original = cad
    session = json.loads(server.open_python_session())["session_id"]
    try:
        assert execute("saved_model = model", session)["status"] == "ok"
        other = SimpleNamespace(GetTitle="Other", changed=0)
        app.GetDocuments, app.ActiveDoc = [original, other], other
        response = execute("model.changed += 1", session)
        assert response["status"] == "error"
        assert original.changed == other.changed == 0
    finally:
        server.close_python_session(session)


def test_session_cleanup_waits_for_timed_out_worker_and_runs_on_same_thread(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    released = []
    worker_ids = []
    monkeypatch.setattr(server, "_ensure_executor", lambda: executor)
    monkeypatch.setattr(server, "_pending", {"fut": None, "started": 0, "what": ""})
    monkeypatch.setattr(server, "_poisoned", False)
    monkeypatch.setattr(server, "COM_TIMEOUT", .02)

    class Reference:
        def __del__(self):
            released.append(threading.get_ident())

    session = json.loads(server.open_python_session())["session_id"]

    def work():
        worker_ids.append(threading.get_ident())
        server._python_sessions[session]["env"]["ref"] = Reference()
        gate.wait(2)

    try:
        with pytest.raises(server.ComOutcomeUnknown):
            server.com_call(work)
        closed = json.loads(server.close_python_session(session))
        assert closed["status"] == "ok"
        assert not released
        gate.set()
    finally:
        gate.set()
        executor.shutdown(wait=True)
    assert released == worker_ids
    assert session not in server._python_sessions


def test_abandoned_sessions_are_released_on_worker_at_server_shutdown(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(server, "_ensure_executor", lambda: executor)
    monkeypatch.setattr(server, "_pending", {"fut": None, "started": 0, "what": ""})
    monkeypatch.setattr(server, "_poisoned", False)
    threads = []

    class Reference:
        def __del__(self):
            threads.append(threading.get_ident())

    try:
        session = json.loads(server.open_python_session())["session_id"]
        server.com_call(lambda: server._python_sessions[session]["env"].update(ref=Reference()))
        worker = server.com_call(threading.get_ident)
        server.com_call(server._release_python_sessions, _cleanup=True)
    finally:
        executor.shutdown(wait=True)
    assert threads == [worker]
    assert not server._python_sessions


def test_cyclic_com_references_are_collected_on_sta_not_later_on_main(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(server, "_ensure_executor", lambda: executor)
    monkeypatch.setattr(server, "_pending", {"fut": None, "started": 0, "what": ""})
    monkeypatch.setattr(server, "_poisoned", False)
    released = []

    class Reference:
        def __del__(self):
            released.append(threading.get_ident())

    def attach(session):
        cycle = [Reference()]
        cycle.append(cycle)
        server._python_sessions[session]["env"]["cycle"] = cycle

    gc.disable()
    try:
        session = json.loads(server.open_python_session())["session_id"]
        server.com_call(lambda: attach(session))
        worker = server.com_call(threading.get_ident)
        server.close_python_session(session)
        assert released == [worker]
    finally:
        executor.shutdown(wait=True)
        gc.enable()
        gc.collect()
