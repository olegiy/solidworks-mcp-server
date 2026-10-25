import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from cadnext.privacy import PrivacyContext
from cadnext.provider import Provider
from cadnext.settings import Profile
from tests.fake_mcp import CATALOG, FakeMcp, ok, tool


def reply(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": [
        {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}} for call_id, name, arguments in calls]}


class ScriptedProvider:
    def __init__(self, responses, *, vision=False):
        self.profile = Profile(name="Тест", base_url="http://localhost/v1", model="fake",
                               vision=vision)
        self.privacy = PrivacyContext(local=True, vision=vision, profile_id=self.profile.id)
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else deepcopy(response)


async def settle(agent, run):
    await asyncio.wait_for(agent.wait(run.id), 2)
    return run


async def pending(run):
    async with asyncio.timeout(2):
        while run.confirmation is None and not run.done:
            await asyncio.sleep(0)
    assert run.confirmation is not None
    return deepcopy(run.confirmation)


async def test_tool_result_changes_next_model_decision():
    from cadnext.agent import Agent

    def decide(messages):
        result = json.loads(messages[-1]["content"])
        return reply(content=f'Размер {result["value"]["value_mm"]} мм')

    model = ScriptedProvider([reply(("read-62", "get_dimension", {"name": "D1@Эскиз1"})), decide])
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62, "unit": "mm"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай размер")
    await settle(agent, run)
    assert run.status == "completed"
    assert model.requests[1][-1]["tool_call_id"] == "read-62"
    assert run.messages[-1]["content"] == "Размер 62 мм"
    assert [event["seq"] for event in run.events] == list(range(1, len(run.events) + 1))


async def test_batch_is_sequential_and_preserves_call_ids():
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("a", "get_dimension", {"name": "D1"}),
                                    ("b", "get_dimension", {"name": "D2"})), reply(content="Готово")])
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62})),
                   ("get_dimension", ok({"value_mm": 18}))], hold="get_dimension")
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай два размера")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    assert len(mcp.calls) == 1
    mcp.release.set()
    await settle(agent, run)
    results = [m for m in model.requests[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["a", "b"]
    assert [json.loads(m["content"])["value"]["value_mm"] for m in results] == [62, 18]


@pytest.mark.parametrize("approved", [True, False])
async def test_python_requires_confirmation_of_exact_restored_code(approved):
    from cadnext.agent import Agent

    code = 'result = "C:\\\\Company\\\\part.sldprt"\nprint(result)'
    model = ScriptedProvider([reply(("py", "execute_python", {"code": code})),
                              reply(content="Геометрия проверена")])
    mcp = FakeMcp([("execute_python", ok({"stdout": "ok"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни код")
    confirmation = await pending(run)
    assert confirmation["kind"] == "python"
    assert confirmation["code"] == code
    assert mcp.calls == []
    with pytest.raises(ValueError):
        agent.confirm(run.id, "wrong-id", True)
    agent.confirm(run.id, confirmation["id"], approved)
    with pytest.raises(ValueError):
        agent.confirm(run.id, confirmation["id"], approved)
    await settle(agent, run)
    if approved:
        assert mcp.calls == [("execute_python", {"code": code})]
        assert run.status == "completed"
        assert run.verification == "unverified"
        assert any("требует проверки" in str(e["data"]) for e in run.events)
    else:
        assert run.status == "rejected"
        assert mcp.calls == []


@pytest.mark.parametrize("disconnect", [False, True])
async def test_stop_or_stream_disconnect_waits_for_current_com_and_blocks_followup(disconnect):
    from cadnext.agent import Agent, AgentBusy

    model = ScriptedProvider([reply(("a", "get_dimension", {"name": "D1"}),
                                    ("b", "get_dimension", {"name": "D2"}))])
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))], hold="get_dimension")
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    if disconnect:
        events = agent.events(run.id)
        await anext(events)
        await events.aclose()
    else:
        agent.stop(run.id)
    assert run.status == "stopping"
    assert not mcp.cancelled
    with pytest.raises(AgentBusy):
        agent.start(session_id="other", provider=model, message="Ещё")
    assert len(mcp.calls) == 1
    mcp.release.set()
    await settle(agent, run)
    assert run.status == "stopped"
    assert len(mcp.calls) == 1
    assert any(e["type"] == "tool_result" and e["data"]["result"]["ok"] for e in run.events)
    assert not mcp.cancelled


@pytest.mark.parametrize("result,status", [
    ({"ok": False, "value": None, "images": [], "error": "Ошибка CAD", "uncertain": False}, "failed"),
    ({"ok": True, "value": {"status": "error", "error": "Вложенная ошибка"},
      "images": [], "error": None, "uncertain": False}, "failed"),
    ({"ok": False, "value": None, "images": [], "error": "Таймаут", "uncertain": True}, "uncertain"),
])
async def test_error_stops_batch_and_uncertainty_is_latched(result, status):
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("a", "set_dimension", {"name": "D1", "value_mm": 62}),
                                    ("b", "get_dimension", {"name": "D1"}))])
    mcp = FakeMcp([("set_dimension", result)])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени размер")
    await settle(agent, run)
    assert run.status == status
    assert len(mcp.calls) == 1
    if status == "uncertain":
        with pytest.raises(RuntimeError):
            agent.start(session_id="s", provider=model, message="Повторить")


@pytest.mark.parametrize("readback,status", [(62, "completed"), (61, "failed")])
async def test_dimension_mutation_is_independently_read_back(readback, status):
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62})),
                              reply(content="Готово")])
    mcp = FakeMcp([("set_dimension", ok({"name": "D1", "value": 62, "unit": "mm"})),
                   ("get_dimension", ok({"name": "D1", "value": readback, "unit": "mm"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Сделай 62 мм")
    await settle(agent, run)
    assert run.status == status
    assert mcp.calls == [("set_dimension", {"name": "D1", "value_mm": 62}),
                         ("get_dimension", {"name": "D1"})]
    assert run.call_count == 2
    assert run.verification == ("verified" if readback == 62 else "failed")


@pytest.mark.parametrize("name,key", [("create_sketch", "sketch"), ("cut_through", "feature"),
                                     ("add_mate", "mate")])
@pytest.mark.parametrize("present,status", [(True, "completed"), (False, "failed")])
async def test_new_feature_is_checked_against_tree(name, key, present, status):
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("sk", name, {})), reply(content="Готово")])
    features = [{"name": "Эскиз1", "type": "ProfileFeature"}] if present else []
    mcp = FakeMcp([("get_feature_tree", ok({"features": []})),
                   (name, ok({key: "Эскиз1"})),
                   ("get_feature_tree", ok({"features": features}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай эскиз")
    await settle(agent, run)
    assert run.status == status
    assert run.call_count == 3


@pytest.mark.parametrize("write,status", [(True, "completed"), (False, "failed")])
async def test_save_checks_file_and_does_not_accept_stale_output(tmp_path, write, status):
    from cadnext.agent import Agent

    path = tmp_path / "part.sldprt"
    path.write_bytes(b"old")

    def save(arguments):
        if write:
            path.write_bytes(b"new-document-bytes")
        return ok({"saved": str(path)})

    model = ScriptedProvider([reply(("save", "save_document", {"save_as_path": str(path)})),
                              reply(content="Сохранено")])
    mcp = FakeMcp([("save_document", save)])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Сохрани")
    await settle(agent, run)
    assert run.status == status
    assert len(mcp.calls) == 1


async def test_default_limit_is_80_calls_and_checks_are_reserved_before_mutation():
    from cadnext.agent import Agent

    reads = [reply((f"r{i}", "get_dimension", {"name": "D1"})) for i in range(81)]
    model = ScriptedProvider(reads)
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))] * 80)
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await settle(agent, run)
    assert run.status == "limit_reached"
    assert len(mcp.calls) == 80

    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62}))])
    mcp = FakeMcp()
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени", max_calls=1)
    await settle(agent, run)
    assert run.status == "limit_reached"
    assert mcp.calls == []


@pytest.mark.parametrize("vision", [False, True])
async def test_real_provider_sends_without_data_prompt_and_keeps_vision_gate(vision):
    from cadnext.agent import Agent

    payloads = []
    path = r"C:\Company\part.sldprt"

    def respond(request):
        data = json.loads(request.content)
        payloads.append(data)
        if len(payloads) == 1:
            response = reply(("snap", "screenshot", {}))
        else:
            response = reply(content="Готово")
        return httpx.Response(200, json={"choices": [{"message": response,
            "finish_reason": "tool_calls" if response["tool_calls"] else "stop"}]})

    profile = Profile(name="Облако", base_url="https://example.test/v1", model="m", vision=vision)
    privacy = PrivacyContext(local=False, vision=vision, session_id="s", profile_id=profile.id)
    provider = Provider(profile, privacy=privacy, transport=httpx.MockTransport(respond))
    mcp = FakeMcp([("screenshot", ok({"path": path}, [{"data": "AAAA", "mimeType": "image/png"}]))])
    agent = Agent(mcp)
    try:
        run = agent.start(session_id="s", provider=provider, message=f'Покажи "{path}"')
        await settle(agent, run)
        assert run.status == "completed"
        assert not any(e["type"] == "confirmation" for e in run.events)
        assert "Company" not in str(payloads)
        assert ("data:image/png;base64,AAAA" in str(payloads)) is vision
        assert any(e["type"] == "image" and e["data"]["data"] == "AAAA" for e in run.events)
    finally:
        await provider.aclose()


@pytest.mark.parametrize("name,arguments", [("missing_tool", {}),
    ("set_dimension", {"name": "D1", "value_mm": "62"}),
    ("set_dimension", {"name": "D1", "value_mm": float("inf")})])
async def test_bad_model_call_never_reaches_mcp(name, arguments):
    from cadnext.agent import Agent

    mcp = FakeMcp()
    model = ScriptedProvider([reply(("bad", name, arguments))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Запрос")
    await settle(agent, run)
    assert run.status == "failed"
    assert mcp.calls == []


async def test_waiter_cancellation_does_not_cancel_cad_and_snapshots_are_detached():
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))], hold="get_dimension")
    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    waiter = asyncio.create_task(agent.wait(run.id))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    snapshot = run.snapshot()
    snapshot["events"].clear()
    assert run.events
    assert not mcp.cancelled
    mcp.release.set()
    await agent.aclose()
    assert run.status == "stopped"


async def test_failed_readback_is_recorded_in_history_and_never_retried():
    from cadnext.agent import Agent

    mcp = FakeMcp([("set_dimension", ok({"name": "D1", "value": 62, "unit": "mm"})),
                   ("get_dimension", ok({"name": "D1", "value": 61, "unit": "mm"}))])
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62}))])
    agent = Agent(mcp)
    first = agent.start(session_id="s", provider=model, message="Измени")
    await settle(agent, first)
    followup = ScriptedProvider([reply(content="Требуется проверка")])
    second = agent.start(session_id="s", provider=followup, message="Что получилось?")
    await settle(agent, second)
    result = next(json.loads(m["content"]) for m in followup.requests[0] if m["role"] == "tool")
    assert result["verification"]["status"] == "failed"
    assert result["verification"]["evidence"][-1]["value"]["value"] == 61
    assert [name for name, args in mcp.calls].count("set_dimension") == 1


@pytest.mark.parametrize("unit,name,status", [("deg", "D1", "completed"),
    ("mm", "D1", "failed"), ("deg", "D2", "failed")])
async def test_angular_readback_checks_units_and_dimension_identity(unit, name, status):
    from cadnext.agent import Agent

    mcp = FakeMcp([("set_dimension", ok({"name": "D1", "value": 90, "unit": "deg"})),
                   ("get_dimension", ok({"name": name, "value": 90, "unit": unit}))])
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 90})),
                              reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Угол 90 градусов")
    await settle(agent, run)
    assert run.status == status


async def test_missing_verification_tool_prevents_mutation():
    from cadnext.agent import Agent

    catalog = [t for t in CATALOG if t["function"]["name"] != "get_dimension"]
    mcp = FakeMcp([("set_dimension", ok({"value": 62, "unit": "mm"}))], catalog=catalog)
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени")
    await settle(agent, run)
    assert run.status == "failed"
    assert mcp.calls == []


@pytest.mark.parametrize("actual,status", [(1, "failed"), (2, "completed")])
async def test_sheet_metal_creation_checks_tree_and_actual_thickness(actual, status):
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_feature_tree", ok({"features": []})),
        ("create_base_flange", ok({"feature": "Кромка1", "thickness_mm": 2})),
        ("get_feature_tree", ok({"features": [{"name": "Кромка1", "type": "SMBaseFlange"}]})),
        ("get_sheet_metal_info", ok({"thickness_mm": actual, "bend_radius_mm": 1}))])
    model = ScriptedProvider([reply(("sheet", "create_base_flange", {"thickness_mm": 2})),
                              reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Лист 2 мм")
    await settle(agent, run)
    assert run.status == status
    assert run.call_count == 4
    assert run.verification == ("failed" if actual == 1 else "verified")


@pytest.mark.parametrize("create", ["create_part", "create_assembly"])
async def test_created_document_is_confirmed_by_documents_and_tree(create):
    from cadnext.agent import Agent

    mcp = FakeMcp([("list_open_documents", ok({"documents": []})),
        (create, ok({"created": "Новая деталь"})),
        ("list_open_documents", ok({"documents": [{"title": "Новая деталь", "path": ""}]})),
        ("get_feature_tree", ok({"features": [{"name": "Начало координат", "type": "Origin"}]}))])
    model = ScriptedProvider([reply(("create", create, {})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай документ")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "verified"
    assert run.call_count == 4


async def test_default_save_captures_existing_path_before_call(tmp_path):
    from cadnext.agent import Agent

    path = tmp_path / "part.sldprt"
    path.write_bytes(b"stale")
    mcp = FakeMcp([("connect_solidworks", ok({"active_document": "Деталь1"})),
        ("list_open_documents", ok({"documents": [{"title": "Деталь1", "path": str(path)}]})),
        ("save_document", ok({"saved": str(path)}))])
    model = ScriptedProvider([reply(("save", "save_document", {})), reply(content="Сохранено")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Сохрани")
    await settle(agent, run)
    assert run.status == "failed"
    assert run.call_count == 3


async def test_export_checks_new_nonempty_file(tmp_path):
    from cadnext.agent import Agent

    path = tmp_path / "part.dxf"

    def export(args):
        path.write_bytes(b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
        return ok({"exported": str(path)})

    mcp = FakeMcp([("export_flat_pattern_dxf", export)])
    model = ScriptedProvider([reply(("dxf", "export_flat_pattern_dxf", {"output_path": str(path)})),
                              reply(content="Экспортировано")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Экспортируй")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "verified"
    assert path.is_file()


async def test_stop_during_mutation_skips_readback_and_keeps_unverified_outcome():
    from cadnext.agent import Agent

    mcp = FakeMcp([("set_dimension", ok({"name": "D1", "value": 62, "unit": "mm"}))],
                  hold="set_dimension")
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    agent.stop(run.id)
    mcp.release.set()
    await settle(agent, run)
    assert run.status == "stopped"
    assert run.verification == "unverified"
    assert [name for name, args in mcp.calls] == ["set_dimension"]


async def test_stop_while_waiting_for_confirmation_never_executes_code():
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("py", "execute_python", {"code": "result = 62"}))])
    mcp = FakeMcp()
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни")
    confirmation = await pending(run)
    agent.stop(run.id)
    with pytest.raises(ValueError):
        agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "stopped"
    assert run.confirmation is None
    assert mcp.calls == []


async def test_next_turn_has_results_for_skipped_batch_calls():
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("a", "get_dimension", {"name": "D1"}),
        ("b", "get_dimension", {"name": "D2"}))])
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))], hold="get_dimension")
    agent = Agent(mcp)
    first = agent.start(session_id="s", provider=model, message="Читай")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    agent.stop(first.id)
    mcp.release.set()
    await settle(agent, first)
    followup = ScriptedProvider([reply(content="Остановлено")])
    second = agent.start(session_id="s", provider=followup, message="Продолжим диалог")
    await settle(agent, second)
    results = [m for m in followup.requests[0] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["a", "b"]
    assert not json.loads(results[1]["content"])["ok"]
    assert len(mcp.calls) == 1


@pytest.mark.parametrize("size", ["history", "catalog"])
async def test_context_and_catalog_limits_stop_before_model_or_cad(size):
    from cadnext.agent import Agent

    mcp = FakeMcp()
    model = ScriptedProvider([reply(content="Не должен вызываться")])
    agent = Agent(mcp, **({"max_history_bytes": 10} if size == "history"
                         else {"max_catalog_bytes": 10}))
    if size == "history":
        with pytest.raises(ValueError, match="История"):
            agent.start(session_id="s", provider=model, message="Прочитай")
        assert agent.runs == {}
        assert mcp.calls == [] and model.requests == []
        return
    run = agent.start(session_id="s", provider=model, message="Прочитай")
    await settle(agent, run)
    assert run.status == "failed"
    assert mcp.calls == []
    assert model.requests == []


async def test_event_resume_does_not_execute_tools_again():
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))])
    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"})), reply(content="62")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await settle(agent, run)
    events = [e async for e in agent.events(run.id, after=2)]
    assert [e["seq"] for e in events] == list(range(3, len(run.events) + 1))
    assert len(mcp.calls) == 1


async def test_python_approval_covers_different_code_and_followup_in_same_dialogue():
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("a", "execute_python", {"code": "result=1"}),
                                    ("b", "execute_python", {"code": "result=2"})),
                              reply(content="Готово"),
                              reply(("c", "execute_python", {"code": "result=3"})), reply(content="Готово")])
    mcp = FakeMcp([("execute_python", ok({"result": str(i)})) for i in (1, 2, 3)])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни дважды")
    first = await pending(run)
    agent.confirm(run.id, first["id"], True)
    await settle(agent, run)
    assert run.status == "completed"
    assert len([e for e in run.events if e["type"] == "confirmation"]) == 1
    followup = agent.start(session_id="s", provider=model, message="Продолжи")
    await settle(agent, followup)
    assert followup.status == "completed"
    assert not any(e["type"] == "confirmation" for e in followup.events)
    assert mcp.calls == [("execute_python", {"code": f"result={i}"}) for i in (1, 2, 3)]
    with pytest.raises(ValueError):
        agent.confirm(run.id, first["id"], True)


@pytest.mark.parametrize("change", ["dialogue", "profile", "endpoint", "model", "restart"])
async def test_python_approval_does_not_cross_scope_or_restart(change):
    from cadnext.agent import Agent

    first = ScriptedProvider([reply(("a", "execute_python", {"code": "result=1"})), reply(content="Готово")])
    mcp = FakeMcp([("execute_python", ok({"result": "1"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=first, message="Код")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    if change == "restart":
        agent = Agent(mcp)
    next_provider = ScriptedProvider([reply(("b", "execute_python", {"code": "result=2"}))])
    changes = {"profile": {"id": "different"}, "endpoint": {"base_url": "http://localhost/v2"},
               "model": {"model": "different"}}.get(change, {})
    next_provider.profile = replace(first.profile, **changes)
    next_provider.privacy = PrivacyContext(local=True, profile_id=next_provider.profile.id)
    next_run = agent.start(session_id="another" if change == "dialogue" else "s",
                           provider=next_provider, message="Другой код")
    confirmation = await pending(next_run)
    assert confirmation["code"] == "result=2"
    agent.confirm(next_run.id, confirmation["id"], False)
    await settle(agent, next_run)
    assert next_run.status == "rejected"
    assert len(mcp.calls) == 1


async def test_cad_transport_exception_poisoning_and_diagnostics_hide_private_values(caplog):
    import logging

    from cadnext.agent import Agent

    private = r"C:\Company\secret-part.sldprt"
    mcp = FakeMcp([("execute_python", RuntimeError(private + " secret-key"))])
    model = ScriptedProvider([reply(("py", "execute_python", {"code": f'result={private!r}'}))])
    agent = Agent(mcp)
    with caplog.at_level(logging.INFO, logger="cadnext.agent"):
        run = agent.start(session_id="s", provider=model, message="Выполни код")
        confirmation = await pending(run)
        agent.confirm(run.id, confirmation["id"], True)
        await settle(agent, run)
    assert run.status == "uncertain"
    assert "Company" not in caplog.text
    assert "result=" not in caplog.text
    assert "secret-key" not in str(run.events)
    assert "execute_python" in caplog.text


async def test_component_creation_checks_independent_component_list_and_tree():
    from cadnext.agent import Agent

    path = r"C:\Parts\part.sldprt"
    mcp = FakeMcp([("get_components", ok({"components": []})),
        ("add_component", ok({"component": "Деталь-1"})),
        ("get_components", ok({"components": [{"name": "Деталь-1", "path": path}]})),
        ("get_feature_tree", ok({"features": [{"name": "Деталь-1", "type": "Reference"}]}))])
    model = ScriptedProvider([reply(("add", "add_component", {"path": path})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Добавь компонент")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "verified"
    assert run.call_count == 4


async def test_sketch_geometry_gets_tree_feedback_but_is_not_claimed_verified():
    from cadnext.agent import Agent

    arguments = {"x1_mm": 0, "y1_mm": 0, "x2_mm": 20, "y2_mm": 0}
    mcp = FakeMcp([("sketch_line", ok({"created": "line"})),
                   ("get_feature_tree", ok({"features": [{"name": "Эскиз1", "type": "ProfileFeature"}]}))])
    model = ScriptedProvider([reply(("line", "sketch_line", arguments)), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Нарисуй отрезок")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "unverified"
    assert run.call_count == 2
    assert run.checks["line"]["evidence"][0]["name"] == "get_feature_tree"


async def test_replayed_call_id_cannot_overwrite_previous_verification():
    from cadnext.agent import Agent

    mcp = FakeMcp([("execute_python", ok({"result": "1"})),
        ("set_dimension", ok({"name": "D1", "value": 62, "unit": "mm"})),
        ("get_dimension", ok({"name": "D1", "value": 62, "unit": "mm"}))])
    model = ScriptedProvider([reply(("same", "execute_python", {"code": "result=1"})),
        reply(("same", "set_dimension", {"name": "D1", "value_mm": 62})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Выполни")
    confirmation = await pending(run)
    agent.confirm(run.id, confirmation["id"], True)
    await settle(agent, run)
    assert run.status == "failed"
    assert run.verification == "unverified"
    assert len(mcp.calls) == 1


@pytest.mark.parametrize("result", [None, {"value": None}, {"ok": "yes", "value": {}}])
async def test_malformed_mcp_response_preserves_unknown_outcome(result):
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_dimension", result)])
    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай")
    await settle(agent, run)
    assert run.status == "uncertain"
    with pytest.raises(RuntimeError):
        agent.start(session_id="s", provider=model, message="Снова")


async def test_tool_schema_cannot_make_network_requests(monkeypatch):
    import urllib.request

    from cadnext.agent import Agent

    requests = []

    def forbid_network(*args, **kwargs):
        requests.append(args)
        raise OSError("network forbidden")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    catalog = [tool("untrusted")]
    catalog[0]["function"]["parameters"] = {"$ref": "https://example.test/schema"}
    mcp = FakeMcp(catalog=catalog)
    model = ScriptedProvider([reply(("bad", "untrusted", {}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай")
    await settle(agent, run)
    assert run.status == "failed"
    assert requests == []
    assert mcp.calls == []


async def test_remote_python_confirmation_and_mcp_use_identical_restored_code():
    from cadnext.agent import Agent

    path = r"C:\Company\part.sldprt"
    payloads = []
    expected_code = []

    def respond(request):
        data = json.loads(request.content)
        payloads.append(data)
        if len(payloads) == 1:
            token = data["messages"][-1]["content"]
            expected_code.append(f'result = "{path}"')
            response = reply(("py", "execute_python", {"code": f'result = "{token}"'}))
        else:
            response = reply(content="Выполнено")
        return httpx.Response(200, json={"choices": [{"message": response,
            "finish_reason": "tool_calls" if response["tool_calls"] else "stop"}]})

    profile = Profile(name="Облако", base_url="https://example.test/v1", model="m")
    provider = Provider(profile, privacy=PrivacyContext(local=False, consent=True,
        session_id="s", profile_id=profile.id), transport=httpx.MockTransport(respond))
    mcp = FakeMcp([("execute_python", ok({"result": "done"}))])
    agent = Agent(mcp)
    try:
        run = agent.start(session_id="s", provider=provider, message=path)
        confirmation = await pending(run)
        assert confirmation["code"] == expected_code[0]
        agent.confirm(run.id, confirmation["id"], True)
        await settle(agent, run)
        assert run.status == "completed"
        assert mcp.calls == [("execute_python", {"code": expected_code[0]})]
        assert "Company" not in str(payloads)
    finally:
        await provider.aclose()


async def test_actual_stdio_catalog_can_be_used_without_cad_calls():
    from cadnext.agent import Agent
    from cadnext.mcp import SolidWorksMcp

    model = ScriptedProvider([reply(content="Готов к работе")])
    async with SolidWorksMcp() as mcp:
        agent = Agent(mcp)
        run = agent.start(session_id="s", provider=model, message="Покажи готовность")
        await asyncio.wait_for(agent.wait(run.id), 15)
        assert run.status == "completed"
        assert run.call_count == 0


async def test_slow_event_consumer_receives_events_added_while_paused():
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))], hold="get_dimension")
    model = ScriptedProvider([reply(("r", "get_dimension", {"name": "D1"})), reply(content="62")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    stream = agent.events(run.id)
    first = await anext(stream)
    mcp.release.set()
    await settle(agent, run)
    events = [first, *[event async for event in stream]]
    assert [e["seq"] for e in events] == [e["seq"] for e in run.events]
    assert events[-1]["data"]["status"] == "completed"


async def test_stop_while_model_is_pending_never_starts_its_tool_calls():
    from cadnext.agent import Agent

    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(ScriptedProvider):
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return reply(("late", "get_dimension", {"name": "D1"}))

    model = WaitingProvider([])
    mcp = FakeMcp()
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await asyncio.wait_for(entered.wait(), 2)
    agent.stop(run.id)
    release.set()
    await settle(agent, run)
    assert run.status == "stopped"
    assert mcp.calls == []


async def test_provider_error_does_not_retry_or_call_mcp():
    from cadnext.agent import Agent
    from cadnext.provider import ProviderError

    model = ScriptedProvider([ProviderError("API модели вернул HTTP 401.")])
    mcp = FakeMcp()
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Читай")
    await settle(agent, run)
    assert run.status == "failed"
    assert len(model.requests) == 1
    assert mcp.calls == []


async def test_remote_message_is_sent_without_data_confirmation_and_still_masked():
    from cadnext.agent import Agent

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Готово"}, "finish_reason": "stop"}]})

    profile = Profile(name="Облако", base_url="https://example.test/v1", model="m")
    provider = Provider(profile, transport=httpx.MockTransport(respond))
    agent = Agent(FakeMcp())
    try:
        run = agent.start(session_id="s", provider=provider, message=r"Прочитай C:\Company\part.sldprt")
        await settle(agent, run)
        assert run.status == "completed"
        assert len(requests) == 1
        assert "Company" not in requests[0].content.decode()
        assert not any(e["type"] == "confirmation" for e in run.events)
    finally:
        await provider.aclose()


async def test_privacy_context_cannot_reuse_consent_in_different_session():
    from cadnext.agent import Agent

    profile = Profile(name="Облако", base_url="https://example.test/v1", model="m")
    provider = Provider(profile, privacy=PrivacyContext(local=False, consent=True,
        session_id="s1", profile_id=profile.id), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [{"message": {
                "content": "Готово"}, "finish_reason": "stop"}]})))
    agent = Agent(FakeMcp())
    try:
        run = agent.start(session_id="s1", provider=provider, message="Привет")
        await settle(agent, run)
        with pytest.raises(ValueError):
            agent.start(session_id="s2", provider=provider, message="Другой диалог")
    finally:
        await provider.aclose()


async def test_stop_preserves_uncertainty_when_current_call_times_out():
    from cadnext.agent import Agent

    mcp = FakeMcp([("set_dimension", {"ok": False, "value": None, "images": [],
        "error": "Таймаут COM", "uncertain": True})], hold="set_dimension")
    model = ScriptedProvider([reply(("set", "set_dimension", {"name": "D1", "value_mm": 62}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    agent.stop(run.id)
    mcp.release.set()
    await settle(agent, run)
    assert run.status == "uncertain"
    assert len(mcp.calls) == 1


async def test_no_verification_budget_prevents_feature_preflight_and_mutation():
    from cadnext.agent import Agent

    mcp = FakeMcp()
    model = ScriptedProvider([reply(("sk", "create_sketch", {}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай эскиз", max_calls=2)
    await settle(agent, run)
    assert run.status == "limit_reached"
    assert run.call_count == 0
    assert mcp.calls == []


async def test_sheet_readback_accepts_server_four_decimal_rounding():
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_feature_tree", ok({"features": []})),
        ("create_base_flange", ok({"feature": "Кромка1", "thickness_mm": 1.23456})),
        ("get_feature_tree", ok({"features": [{"name": "Кромка1", "type": "SMBaseFlange"}]})),
        ("get_sheet_metal_info", ok({"thickness_mm": 1.2346, "bend_radius_mm": 1.2346}))])
    model = ScriptedProvider([reply(("sheet", "create_base_flange", {
        "thickness_mm": 1.23456, "bend_radius_mm": 1.23456})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай лист")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "verified"


async def test_tree_checks_request_more_than_default_100_features():
    from cadnext.agent import Agent

    features = [{"name": f"Feature{i}", "type": "ProfileFeature"} for i in range(100)]

    def tree_before(arguments):
        return ok({"features": features[:arguments.get("limit", 100)]})

    def tree_after(arguments):
        return ok({"features": [*features, {"name": "Sketch101", "type": "ProfileFeature"}][
            :arguments.get("limit", 100)]})

    mcp = FakeMcp([("get_feature_tree", tree_before),
                   ("create_sketch", ok({"sketch": "Sketch101"})), ("get_feature_tree", tree_after)])
    model = ScriptedProvider([reply(("sk", "create_sketch", {})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай эскиз")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "verified"


async def test_possibly_truncated_tree_prevents_creation():
    from cadnext.agent import Agent

    mcp = FakeMcp([("get_feature_tree", ok({"features": [
        {"name": f"Feature{i}", "type": "ProfileFeature"} for i in range(1000)]})),
        ("create_sketch", ok({"sketch": "Sketch1001"}))])
    model = ScriptedProvider([reply(("sk", "create_sketch", {})), reply(content="Готово")])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Создай эскиз")
    await settle(agent, run)
    assert run.status == "failed"
    assert [name for name, args in mcp.calls] == ["get_feature_tree"]


async def test_partial_batch_keeps_successful_image_in_followup_history():
    from cadnext.agent import Agent

    mcp = FakeMcp([("screenshot", ok(None, [{"data": "AAAA", "mimeType": "image/png"}]))])
    model = ScriptedProvider([reply(("snap", "screenshot", {}),
        ("py", "execute_python", {"code": "result=1"}))], vision=True)
    agent = Agent(mcp)
    first = agent.start(session_id="s", provider=model, message="Снимок и код")
    confirmation = await pending(first)
    agent.confirm(first.id, confirmation["id"], False)
    await settle(agent, first)
    followup = ScriptedProvider([reply(content="Вижу снимок")], vision=True)
    second = agent.start(session_id="s", provider=followup, message="Что на снимке?")
    await settle(agent, second)
    assert "data:image/png;base64,AAAA" in str(followup.requests[0])
    messages = followup.requests[0]
    result_positions = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    image_position = next(i for i, m in enumerate(messages) if "data:image/png" in str(m))
    assert image_position > max(result_positions)
    assert len(mcp.calls) == 1


async def test_oversized_model_reply_stops_before_its_tool_call():
    from cadnext.agent import Agent

    model = ScriptedProvider([reply(("py", "execute_python", {"code": "x" * 20000}))])
    mcp = FakeMcp()
    agent = Agent(mcp, max_history_bytes=10000)
    run = agent.start(session_id="s", provider=model, message="Выполни")
    async with asyncio.timeout(2):
        while not run.done and run.confirmation is None:
            await asyncio.sleep(0)
    snapshot = run.snapshot()
    if run.confirmation is not None:
        agent.stop(run.id)
    await settle(agent, run)
    assert snapshot["status"] == "failed"
    assert mcp.calls == []
