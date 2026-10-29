import json
from types import SimpleNamespace

import pytest

import solidworks_mcp_server as server


@pytest.mark.parametrize("failure", [None, "ambiguous", "wrong_active", "unknown_session"])
def test_open_activates_exact_document_and_rebinds_only_after_success(tmp_path, monkeypatch, failure):
    path = tmp_path / "bushing.sldprt"
    path.touch()
    old = SimpleNamespace(GetTitle="Assembly", GetPathName="", changed=0)
    target = SimpleNamespace(GetTitle="bushing.sldprt", GetPathName=str(path), changed=0)
    app = SimpleNamespace(ActiveDoc=old, GetDocuments=[old, target])
    calls = []
    def open_doc(*args):
        calls.append("open")
        return target
    def activate(title, use_preferences, option, errors):
        calls.append((title, use_preferences, option))
        app.ActiveDoc = old if failure == "wrong_active" else target
        return target
    app.OpenDoc6, app.ActivateDoc3 = open_doc, activate
    monkeypatch.setattr(server, "_app", lambda: app)
    monkeypatch.setattr(server, "com_call", lambda job, **kwargs: job())
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "_byref_i4", lambda: SimpleNamespace(value=0))
    session = json.loads(server.open_python_session())["session_id"]
    try:
        server.execute_python("marker = 42", session_id=session)
        if failure == "ambiguous":
            app.GetDocuments.append(SimpleNamespace(GetTitle=target.GetTitle))
        result = json.loads(server.open_document(str(path), session_id=("missing" if failure == "unknown_session" else session)))
        assert result["status"] == ("ok" if failure is None else "error")
        if failure is None:
            assert calls == ["open", ("bushing.sldprt", False, 1)]
            assert json.loads(server.execute_python("model.changed += 1\nresult = marker", session_id=session))["result"] == "42"
            assert target.changed == 1 and old.changed == 0
        else:
            assert server._python_sessions[session]["document"] is old
            if failure != "wrong_active":
                assert ("bushing.sldprt", False, 1) not in calls
        assert len([c for c in calls if c == "open"]) <= 1
    finally:
        server.close_python_session(session)
