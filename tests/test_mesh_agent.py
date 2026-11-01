"""Both agent loops use local mesh tools with the existing budget and stop rules."""

import asyncio
import json

import pytest

from cadnext.agent import Agent
from tests.fake_mcp import FakeMcp
from tests.test_agent import ScriptedProvider, reply
from tests.test_mesh_tools import ready_tools


@pytest.mark.parametrize("evaluate", [False, True])
async def test_mesh_measurements_are_local_budgeted_and_work_without_vision(tmp_path, evaluate):
    from cadnext.evaluation import EvaluationOptions

    tools, asset = await ready_tools(tmp_path)

    class Provider(ScriptedProvider):
        async def complete(self, messages, catalog):
            names = {t["function"]["name"] for t in catalog}
            assert {"mesh_inspect", "mesh_fit", "mesh_section", "mesh_compare"} <= names
            assert str(tmp_path) not in json.dumps(messages)
            return await super().complete(messages, catalog)

    answers = [reply(("mesh", "mesh_inspect", {})), reply(content="Измерения готовы")]
    if evaluate:
        answers.insert(0, reply(("goal", "cadnext_define_goal", {
            "kind": "informational", "requirements": [], "reference_ids": [], "ambiguities": []})))
    provider = Provider(answers)
    mcp = FakeMcp()
    agent = Agent(mcp, mesh_tools=tools)
    try:
        run = agent.start(session_id="s", provider=provider, message="Измерь сетку",
                          mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1},
                          evaluation=EvaluationOptions() if evaluate else None)
        await asyncio.wait_for(agent.wait(run.id), 5)
        assert run.status == "completed"
        assert run.call_count == 1
        assert not mcp.calls
        assert run.snapshot()["mesh_reference"]["sha256"] == asset.sha256
        assert run.mesh_evidence
        assert [e["seq"] for e in run.events] == list(range(1, len(run.events)+1))
    finally:
        await agent.aclose()


async def test_non_mesh_run_keeps_original_catalog(tmp_path):
    tools, _ = await ready_tools(tmp_path)

    class Provider(ScriptedProvider):
        async def complete(self, messages, catalog):
            assert not any(t["function"]["name"].startswith("mesh_") for t in catalog)
            return reply(content="Ответ")

    agent = Agent(FakeMcp(), mesh_tools=tools)
    run = agent.start(session_id="s", provider=Provider([]), message="Ответь")
    await agent.wait(run.id)
    await agent.aclose()
    assert run.status == "completed"


async def test_mesh_budget_prevents_following_measurement_and_cad(tmp_path):
    tools, asset = await ready_tools(tmp_path)
    agent = Agent(FakeMcp(), mesh_tools=tools)
    run = agent.start(session_id="s", provider=ScriptedProvider([
        reply(("one", "mesh_inspect", {}), ("two", "mesh_inspect", {}))]),
        message="Измерь", max_calls=1,
        mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
    try:
        await agent.wait(run.id)
        assert run.status == "limit_reached" and run.call_count == 1
        assert not agent.mcp.calls
    finally:
        await agent.aclose()


async def test_bad_mesh_reference_fails_synchronously_before_any_cad(tmp_path):
    tools, asset = await ready_tools(tmp_path)
    agent = Agent(FakeMcp(), mesh_tools=tools)
    try:
        with pytest.raises(ValueError):
            agent.start(session_id="other", provider=ScriptedProvider([]), message="Построй",
                        mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
        assert not agent.runs and not agent.mcp.calls
    finally:
        await agent.aclose()


async def test_stop_in_mesh_analysis_prevents_queued_cad_without_poisoning_mcp(tmp_path):
    from cadnext.mesh.contracts import MeshError

    tools, asset = await ready_tools(tmp_path)
    entered = asyncio.Event()

    class WaitingWorker:
        async def request(self, operation, payload, *, stop):
            entered.set()
            await stop.wait()
            raise MeshError("stopped", "Остановлено")

        async def aclose(self):
            pass

    tools.worker = WaitingWorker()
    agent = Agent(FakeMcp(), mesh_tools=tools)
    run = agent.start(session_id="s", provider=ScriptedProvider([
        reply(("mesh", "mesh_inspect", {}), ("cad", "get_dimension", {"name": "D1"}))]),
        message="Измерь", mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
    await entered.wait()
    agent.stop(run.id)
    await agent.wait(run.id)
    assert run.status == "stopped" and not agent.uncertain and not agent.mcp.calls
    await agent.aclose()


async def test_real_provider_sends_only_sanitized_numeric_mesh_summary(tmp_path):
    import httpx

    from cadnext.privacy import PrivacyContext
    from cadnext.provider import Provider
    from cadnext.settings import Profile

    tools, asset = await ready_tools(tmp_path)
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        message = reply(("mesh", "mesh_inspect", {})) if len(captured) == 1 else reply(content="Готово")
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})

    provider = Provider(Profile(name="P", base_url="https://example.test/v1", model="m"),
                        privacy=PrivacyContext(local=False, sensitive=["Company"]),
                        transport=httpx.MockTransport(respond))
    agent = Agent(FakeMcp(), mesh_tools=tools)
    try:
        run = agent.start(session_id="s", provider=provider, message=r"Измерь C:\Company\ref.obj",
                          mesh_reference={"asset_id": asset.asset_id, "tolerance_mm": .1})
        await agent.wait(run.id)
        assert run.status == "completed" and len(captured) == 2
        outgoing = json.dumps(captured)
        assert "Company" not in outgoing and str(tmp_path) not in outgoing
        assert "source_face_ids" not in outgoing and "points_mm" not in outgoing
        messages = captured[1]["messages"]
        tool_result = next(m for m in messages if m["role"] == "tool")
        assert tool_result["tool_call_id"] == "mesh"
        assert json.loads(tool_result["content"])["value"]["summary"]["watertight"]
    finally:
        await agent.aclose()
        await provider.aclose()
