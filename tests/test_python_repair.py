import json
from types import SimpleNamespace

from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, pending, reply, settle


def cad_error(message="<unknown>.GetFeature", *, line=14):
    return {"ok": False, "value": {"status": "error", "error": message,
        "error_code": "CAD_ERROR", "python_line": line, "error_type": "AttributeError"},
        "images": [], "error": None, "uncertain": False}


async def test_repair_model_receives_preflight_phase_and_actionable_hint():
    error = cad_error("Invalid endpoint", line=2)
    error["value"].update(phase="preflight", changes_started=False,
                          hint="Use endpoints 1 and 2", error_type="PythonPreflightError")
    mcp = FakeMcp([("execute_python", error), ("execute_python", ok({"result": "done"}))])
    model = ScriptedProvider([
        reply(("broken", "execute_python", {"code": "dim.SetArcEndCondition(0, 3)"})),
        reply(("fixed", "execute_python", {"code": "dim.SetArcEndCondition(1, 3)"})),
        reply(content="Требуется проверка размеров"),
    ])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Оформи размер")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    diagnostic = json.loads(model.requests[1][-1]["content"].split("Диагностика: ", 1)[1])
    assert diagnostic["phase"] == "preflight"
    assert diagnostic["changes_started"] is False
    assert diagnostic["hint"] == "Use endpoints 1 and 2"
    assert diagnostic["python_line"] == 2
    assert run.status == "completed"
    assert [args["code"] for _, args in mcp.calls] == [
        "dim.SetArcEndCondition(0, 3)", "dim.SetArcEndCondition(1, 3)"]
    assert run.verification == "unverified"


async def test_preflight_repair_initializes_objects_from_the_unexecuted_fragment(monkeypatch):
    import solidworks_mcp_server as server

    preparations = []
    conditions = {}

    def prepare():
        preparations.append(True)
        return SimpleNamespace(SetArcEndCondition=lambda index, value: conditions.update({index: value}))

    model_doc = SimpleNamespace(prepare_dimension=prepare)
    monkeypatch.setattr(server, "_app", lambda: SimpleNamespace(ActiveDoc=model_doc))
    monkeypatch.setattr(server, "_nothing", lambda: None)
    monkeypatch.setattr(server, "com_call", lambda job: job())
    broken = "dim = model.prepare_dimension()\ndim.SetArcEndCondition(0, 3)"
    corrected = "dim = model.prepare_dimension()\ndim.SetArcEndCondition(1, 3)"

    def execute(arguments):
        value = json.loads(server.execute_python(arguments["code"]))
        return {"ok": value["status"] == "ok", "value": value, "images": [],
                "error": value.get("error"), "uncertain": False}

    mcp = FakeMcp([("execute_python", execute), ("execute_python", execute)])
    model = ScriptedProvider([
        reply(("broken", "execute_python", {"code": broken})),
        reply(("fixed", "execute_python", {"code": corrected})),
        reply(content="Требуется проверка"),
    ])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Оформи размер")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    repair_prompt = model.requests[1][-1]["content"]
    assert "Исправь отклонённый фрагмент целиком" in repair_prompt
    assert "не запускай исходный скрипт целиком" not in repair_prompt
    assert preparations == [True]
    assert conditions == {1: 3}
    assert run.status == "completed"
    assert run.verification == "unverified"


async def test_known_python_error_requests_repair_and_executes_only_replacement_script():
    mcp = FakeMcp([("execute_python", cad_error()), ("execute_python", ok({"result": "done"}))])
    model = ScriptedProvider([
        reply(("broken", "execute_python", {"code": "sketch = GET(sm, 'GetFeature')"})),
        reply(("fixed", "execute_python", {"code": "sketch = GET(sm, 'ActiveSketch')"})),
        reply(content="Деталь построена и проверена."),
    ])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Построй деталь")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)

    assert run.status == "completed"
    assert mcp.calls == [
        ("execute_python", {"code": "sketch = GET(sm, 'GetFeature')"}),
        ("execute_python", {"code": "sketch = GET(sm, 'ActiveSketch')"}),
    ]
    assert len(model.requests) == 3
    repair_prompt = model.requests[1][-1]
    assert repair_prompt["role"] == "user"
    assert "исправ" in repair_prompt["content"].lower()
    assert "не повторяй" in repair_prompt["content"].lower()
    assert any(event["type"] == "repair" and event["data"]["attempt"] == 1 for event in run.events)
    assert run.checks["broken"]["status"] == "repaired"
    assert run.verification == "unverified"


async def test_repair_can_inspect_state_before_replacement_script():
    mcp = FakeMcp([
        ("execute_python", cad_error()),
        ("get_feature_tree", ok({"features": [{"name": "Эскиз1", "type": "ProfileFeature"}]})),
        ("execute_python", ok({"result": "finished"})),
    ])
    model = ScriptedProvider([
        reply(("broken", "execute_python", {"code": "bad()"})),
        reply(("state", "get_feature_tree", {"limit": 1000})),
        reply(("fixed", "execute_python", {"code": "continue_from_existing_sketch()"})),
        reply(content="Готово"),
    ])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Продолжи")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "completed"
    assert [name for name, _ in mcp.calls] == ["execute_python", "get_feature_tree", "execute_python"]
    assert "Эскиз1" in str(model.requests[2])


async def test_image_from_successful_batch_is_available_to_repair_model():
    mcp = FakeMcp([
        ("screenshot", ok(None, [{"data": "AAAA", "mimeType": "image/png"}])),
        ("execute_python", cad_error()),
        ("execute_python", ok({"result": "finished"})),
    ])
    model = ScriptedProvider([
        reply(("snap", "screenshot", {}), ("broken", "execute_python", {"code": "bad()"})),
        reply(("fixed", "execute_python", {"code": "continue()"})),
        reply(content="Продолжено"),
    ], vision=True)
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Проверь")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "completed"
    assert "data:image/png;base64,AAAA" in str(model.requests[1])
    assert "data:image/png;base64,AAAA" in str(model.requests[2])


async def test_known_python_error_is_retried_at_most_twice_then_fails():
    mcp = FakeMcp([("execute_python", cad_error("first")),
                   ("execute_python", cad_error("second")),
                   ("execute_python", cad_error("third"))])
    model = ScriptedProvider([
        reply(("one", "execute_python", {"code": "one()"})),
        reply(("two", "execute_python", {"code": "two()"})),
        reply(("three", "execute_python", {"code": "three()"})),
    ])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Исправь")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "failed"
    assert len(mcp.calls) == 3
    assert len([event for event in run.events if event["type"] == "repair"]) == 2
    assert "двух попыток" in run.events[-1]["data"]["message"]


async def test_unknown_python_outcome_never_requests_repair():
    mcp = FakeMcp([("execute_python", {"ok": False, "value": None, "images": [],
                                      "error": "MCP disconnected", "uncertain": True})])
    model = ScriptedProvider([reply(("py", "execute_python", {"code": "mutate()"}))])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "uncertain"
    assert not any(event["type"] == "repair" for event in run.events)
    assert len(model.requests) == 1


async def test_non_cad_python_error_never_requests_repair():
    mcp = FakeMcp([("execute_python", {"ok": False, "value": {"status": "error",
        "error_code": "PYTHON_ERROR", "error": "SyntaxError"}, "images": [],
        "error": None, "uncertain": False})])
    model = ScriptedProvider([reply(("py", "execute_python", {"code": "broken("}))])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "failed"
    assert not any(event["type"] == "repair" for event in run.events)
    assert len(model.requests) == 1


async def test_successful_result_with_cad_error_payload_never_requests_repair():
    mcp = FakeMcp([("execute_python", {"ok": True, "value": {"error_code": "CAD_ERROR"},
                                      "images": [], "error": None, "uncertain": False})])
    model = ScriptedProvider([reply(("py", "execute_python", {"code": "inspect()"})),
                              reply(content="Проверка завершена")])
    agent = __import__("cadnext.agent", fromlist=["Agent"]).Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Проверь")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "completed"
    assert not any(event["type"] == "repair" for event in run.events)
    assert len(model.requests) == 2
