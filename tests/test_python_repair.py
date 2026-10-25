from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, pending, reply, settle


def cad_error(message="<unknown>.GetFeature", *, line=14):
    return {"ok": False, "value": {"status": "error", "error": message,
        "error_code": "CAD_ERROR", "python_line": line, "error_type": "AttributeError"},
        "images": [], "error": None, "uncertain": False}


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
