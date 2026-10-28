import asyncio
import json
from copy import deepcopy

import pytest

from cadnext.agent import Agent
from tests.fake_mcp import CATALOG, FakeMcp, ok, tool
from tests.test_agent import ScriptedProvider, reply, settle


class SessionMcp(FakeMcp):
    def __init__(self, steps=(), **kwargs):
        catalog = deepcopy(CATALOG)
        catalog += [tool("open_python_session"), tool("close_python_session", {
            "session_id": {"type": "string"}}, ["session_id"])]
        super().__init__(steps, catalog=catalog, **kwargs)
        self.lifecycle = []

    async def call(self, name, arguments):
        if name == "open_python_session":
            self.lifecycle.append((name, arguments))
            return ok({"session_id": "private-env"})
        if name == "close_python_session":
            self.lifecycle.append((name, arguments))
            return ok({})
        return await super().call(name, arguments)


def trusted_agent(mcp, provider):
    agent = Agent(mcp)
    agent._python_approved.add(("s", provider.profile))
    return agent


async def test_run_owns_one_lazy_session_and_model_never_sees_its_id():
    def respond(messages):
        assert "private-env" not in json.dumps(messages)
        return reply(content="Готово")

    class Provider(ScriptedProvider):
        async def complete(self, messages, tools):
            names = {t["function"]["name"] for t in tools}
            assert not names & {"open_python_session", "close_python_session"}
            python = next(t["function"]["parameters"] for t in tools
                          if t["function"]["name"] == "execute_python")
            assert "session_id" not in python["properties"]
            return await super().complete(messages, tools)

    provider = Provider([reply(("a", "execute_python", {"code": "a=1"}),
                               ("b", "execute_python", {"code": "result=a"})), respond])
    mcp = SessionMcp([("execute_python", ok({})), ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    assert mcp.lifecycle == [("open_python_session", {}),
                             ("close_python_session", {"session_id": "private-env"})]
    assert [args["session_id"] for _, args in mcp.calls] == ["private-env", "private-env"]
    assert "private-env" not in json.dumps(run.snapshot())


async def test_stop_waits_for_python_and_releases_session_without_another_cad_step():
    provider = ScriptedProvider([reply(("a", "execute_python", {"code": "work()"}),
                                      ("b", "execute_python", {"code": "later()"}))])
    mcp = SessionMcp([("execute_python", ok({}))], hold="execute_python")
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await asyncio.wait_for(mcp.entered.wait(), 2)
    agent.stop(run.id)
    assert len(mcp.lifecycle) == 1
    mcp.release.set()
    await settle(agent, run)
    assert run.status == "stopped"
    assert len(mcp.calls) == 1
    assert mcp.lifecycle[-1][0] == "close_python_session"


def staged_mcp(steps, **kwargs):
    catalog = deepcopy(CATALOG)
    catalog += [tool("inspect_cad", {"sections": {"type": "array", "items": {"type": "string"}},
                                   "dimension_names": {"type": "array", "items": {"type": "string"}}},
                     ["sections"])]
    return FakeMcp(steps, catalog=catalog, **kwargs)


STAGES = [{"id": "profile", "title": "Профиль", "expected_result": "Длина 80 мм"},
          {"id": "body", "title": "Тело", "expected_result": "Одно тело"}]
CHECKS = [{"kind": "dimension", "name": "Length@Profile", "value": 80, "unit": "mm"}]


async def test_plan_stages_and_declared_checks_survive_snapshot_and_sidecar():
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        reply(("py", "execute_python", {"code": "build()", "stage_id": "profile", "checks": CHECKS})),
        reply(("py2", "execute_python", {"code": "finish()", "stage_id": "body"})),
        reply(content="Построено; остальные свойства требуют проверки")])
    mcp = staged_mcp([("execute_python", ok({})), ("inspect_cad", ok({"dimensions": [
        {"name": "Length@Profile", "value": 80, "unit": "mm", "driving": True}]})),
        ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    assert run.verification == "unverified"
    plan = run.snapshot()["construction_plan"]
    assert [stage["status"] for stage in plan] == ["done", "done"]
    assert plan[0]["checks"][0]["passed"] is True
    assert mcp.calls[0] == ("execute_python", {"code": "build()"})
    assert mcp.calls[1] == ("inspect_cad", {"sections": ["dimensions"],
                                         "dimension_names": ["Length@Profile"]})
    assert run.call_count == 4  # plan, mutation, verification, mutation
    assert any(event["type"] == "construction_plan" for event in run.events)
    saved = agent.conversation_snapshot("s")
    restored = Agent(FakeMcp()).load_conversation(saved)["runs"][0]
    assert restored["construction_plan"] == plan
    assert restored["construction_plan_history"] == run.snapshot()["construction_plan_history"]


async def test_plan_cannot_rewrite_started_stage_and_no_followup_is_sent():
    changed = deepcopy(STAGES)
    changed[0]["title"] = "Переписанный профиль"
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        reply(("py", "execute_python", {"code": "build()", "stage_id": "profile"})),
        reply(("rewrite", "set_construction_plan", {"stages": changed}),
              ("next", "execute_python", {"code": "must_not_run()"}))])
    mcp = staged_mcp([("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "failed"
    assert mcp.calls == [("execute_python", {"code": "build()"})]
    assert run.snapshot()["construction_plan"][0]["title"] == "Профиль"
    assert len(run.snapshot()["construction_plan_history"]) == 1


@pytest.mark.parametrize("max_calls,value,expected_status,expected_calls", [
    (2, 80, "limit_reached", []),
    (3, 81, "failed", ["execute_python", "inspect_cad"]),
])
async def test_verification_budget_reserved_before_mutation_and_failure_stops(max_calls, value,
                                                                            expected_status, expected_calls):
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES[:1]})),
        reply(("py", "execute_python", {"code": "build()", "stage_id": "profile", "checks": CHECKS}))])
    mcp = staged_mcp([("execute_python", ok({})), ("inspect_cad", ok({"dimensions": [
        {"name": "Length@Profile", "value": value, "unit": "mm", "driving": True}]}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй", max_calls=max_calls)
    await settle(agent, run)
    assert run.status == expected_status
    assert [name for name, _ in mcp.calls] == expected_calls
    if expected_calls:
        assert run.snapshot()["construction_plan"][0]["status"] == "error"
        assert run.snapshot()["construction_plan"][0]["checks"][0]["passed"] is False


@pytest.mark.parametrize("malformed", [
    [{"id": None, "title": "X", "expected_result": "Y", "status": "done", "checks": [], "tool_call_ids": []}],
    [{"id": "x", "title": "X", "expected_result": "Y", "status": "done", "checks": [None], "tool_call_ids": []}],
    [{"id": "x", "title": "X", "expected_result": "Y", "status": "running", "checks": [], "tool_call_ids": []}],
    [{"id": "x", "title": "X", "expected_result": "Y", "status": {}, "checks": [], "tool_call_ids": []}],
])
async def test_sidecar_rejects_malformed_or_incomplete_stage_state(malformed):
    provider = ScriptedProvider([reply(content="Ответ")])
    agent = Agent(FakeMcp())
    run = agent.start(session_id="s", provider=provider, message="Проверка")
    await settle(agent, run)
    saved = agent.conversation_snapshot("s")
    saved["runs"][0]["construction_plan"] = malformed
    with pytest.raises(ValueError):
        Agent(FakeMcp()).load_conversation(saved)


async def test_pending_stages_can_change_and_revisions_remain_immutable():
    updated = deepcopy(STAGES)
    updated[1]["expected_result"] = "Тело с фаской"
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        reply(("py", "execute_python", {"code": "build()", "stage_id": "profile"})),
        reply(("revision", "set_construction_plan", {"stages": updated})),
        reply(("body", "execute_python", {"code": "finish()", "stage_id": "body"})),
        reply(content="Готово")])
    agent = trusted_agent(staged_mcp([("execute_python", ok({})), ("execute_python", ok({}))]), provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    history = run.snapshot()["construction_plan_history"]
    assert history[0]["stages"][1]["expected_result"] == "Одно тело"
    assert history[1]["stages"][1]["expected_result"] == "Тело с фаской"
    assert history[0]["stages"][0]["status"] == "pending"
    assert history[1]["stages"][0]["status"] == "done"


async def test_automatic_stage_id_cannot_collide_with_model_stage_id():
    stages = [{"id": "step-2", "title": "Первый", "expected_result": "Профиль"}]
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": stages})),
        reply(("a", "execute_python", {"code": "first()", "stage_id": "step-2"})),
        reply(("b", "execute_python", {"code": "second()"})), reply(content="Готово")])
    agent = trusted_agent(staged_mcp([("execute_python", ok({})), ("execute_python", ok({}))]), provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    ids = [stage["id"] for stage in run.construction_plan]
    assert len(set(ids)) == len(ids)
    Agent(FakeMcp()).load_conversation(agent.conversation_snapshot("s"))


@pytest.mark.parametrize("failure", [False, True])
async def test_session_released_after_model_failure_or_unknown_com(failure):
    from cadnext.provider import ProviderError

    result = ok({}) if failure else {"ok": False, "value": None, "images": [],
                                     "error": "timeout", "uncertain": True}
    provider = ScriptedProvider([reply(("a", "execute_python", {"code": "work()"})),
                                 ProviderError("offline")])
    mcp = SessionMcp([("execute_python", result)])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == ("failed" if failure else "uncertain")
    assert mcp.lifecycle[-1][0] == "close_python_session"
