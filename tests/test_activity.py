import asyncio
import time

import pytest

from cadnext.agent import Agent
from cadnext.provider import ProviderError
from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, reply, settle
from tests.test_app import api as api
from tests.test_app import profile, start, wait


async def test_model_wait_is_visible_before_response_without_extra_calls():
    entered, release = asyncio.Event(), asyncio.Event()

    class HoldingProvider(ScriptedProvider):
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return await super().complete(messages, tools)

    model = HoldingProvider([reply(content="Готово")])
    mcp = FakeMcp([])
    agent = Agent(mcp)
    before = time.time()
    run = agent.start(session_id="s", provider=model, message="Подумай")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        starts = [event for event in run.snapshot()["events"] if event["type"] == "model_start"]
        assert len(starts) == 1
        assert starts[0]["data"] == {"number": 1}
        assert before <= starts[0]["at"] <= time.time()
        assert not any(event["type"] == "model_end" for event in run.events)
        assert mcp.calls == []
    finally:
        release.set()
        await settle(agent, run)
    ends = [event for event in run.events if event["type"] == "model_end"]
    assert len(ends) == 1
    assert ends[0]["data"] == {"number": 1, "outcome": "received"}
    assert starts[0]["at"] <= ends[0]["at"] <= time.time()
    assert len(model.requests) == 1 and mcp.calls == []


async def test_model_rounds_and_cad_events_keep_time_and_real_order():
    model = ScriptedProvider([reply(("read", "get_dimension", {"name": "D1"})), reply(content="Готово")])
    mcp = FakeMcp([("get_dimension", ok({"value_mm": 62}))])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Прочитай")
    await settle(agent, run)
    relevant = [event for event in run.events if event["type"] in {
        "model_start", "model_end", "tool_start", "tool_result"}]
    assert [event["type"] for event in relevant] == [
        "model_start", "model_end", "tool_start", "tool_result", "model_start", "model_end"]
    assert [event["data"]["number"] for event in relevant if event["type"] == "model_start"] == [1, 2]
    assert all(isinstance(event["at"], (int, float)) for event in run.events)
    assert len(model.requests) == 2 and len(mcp.calls) == 1


@pytest.mark.parametrize("error", [ProviderError("Ошибка API"), RuntimeError("private details")])
async def test_failed_model_call_finishes_activity_without_leaking_error_details(error):
    model = ScriptedProvider([error])
    agent = Agent(FakeMcp([]))
    run = agent.start(session_id="s", provider=model, message="Ответь")
    await settle(agent, run)
    activity = [event for event in run.events if event["type"] in {"model_start", "model_end"}]
    assert [event["type"] for event in activity] == ["model_start", "model_end"]
    assert activity[-1]["data"] == {"number": 1, "outcome": "error"}
    assert "private details" not in str(activity)
    assert run.status == "failed" and len(model.requests) == 1


async def test_stop_during_model_wait_closes_activity_and_blocks_cad():
    entered, release = asyncio.Event(), asyncio.Event()

    class HoldingProvider(ScriptedProvider):
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return await super().complete(messages, tools)

    model = HoldingProvider([reply(("change", "set_dimension", {"name": "D1", "value_mm": 7}))])
    mcp = FakeMcp([])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=model, message="Измени")
    await asyncio.wait_for(entered.wait(), 2)
    agent.stop(run.id)
    release.set()
    await settle(agent, run)
    assert run.status == "stopped"
    assert [event["data"]["outcome"] for event in run.events if event["type"] == "model_end"] == ["received"]
    assert mcp.calls == [] and len(model.requests) == 1


def test_api_restores_activity_times_and_counters_without_model_requests(api):
    client, backend = api
    run_id = start(client, profile(client)["id"]).json()["id"]
    result = wait(client, run_id)
    starts = [event for event in result["events"] if event["type"] == "model_start"]
    assert [event["data"]["number"] for event in starts] == [1, 2]
    assert all(event["at"] > 0 for event in result["events"])
    requests = len(backend.requests)
    assert client.get(f"/api/runs/{run_id}").json() == result
    assert client.get("/api/runs?session_id=session-1").json() == [result]
    assert len(backend.requests) == requests
