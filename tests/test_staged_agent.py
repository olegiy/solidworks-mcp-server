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
