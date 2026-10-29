"""Goal loop: server-owned decisions, fresh CAD evidence, bounded corrections."""

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from cadnext.agent import Agent
from cadnext.evaluation import EvaluationOptions
from cadnext.privacy import PrivacyContext
from cadnext.settings import Profile
from tests.fake_mcp import FakeMcp, ok, tool

GOAL_TOOLS = {"cadnext_define_goal", "cadnext_bind_requirements",
              "cadnext_report_visual_evaluation"}


def call(call_id, name, arguments=None):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def response(*calls, content=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return message


@dataclass
class ScriptedProvider:
    answers: list
    profile: Profile = field(default_factory=lambda: Profile(
        name="Vision", base_url="http://localhost/v1", model="fake", vision=True))
    privacy: PrivacyContext = field(default_factory=lambda: PrivacyContext(
        local=True, vision=True))
    sent: list = field(default_factory=list)
    last_metrics: dict | None = None

    async def complete(self, messages, tools, **_):
        self.sent.append((messages, tools))
        if not self.answers:
            raise AssertionError("unexpected model request")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def requirement(id="d1", expected=50.0, *, method="cad", required=True, weight=1):
    item = {"id": id, "description": f"Размер {id}", "source": "request",
            "required": required, "weight": weight, "method": method}
    if method == "cad":
        item.update({"expected": expected, "tolerance": 0.001, "unit": "mm"})
    return item


def define(*requirements, kind="cad", ambiguities=()):
    return call("define-1", "cadnext_define_goal", {
        "kind": kind, "requirements": list(requirements),
        "reference_ids": [], "ambiguities": list(ambiguities)})


def dimension(name, value, unit="mm"):
    return ok({"name": name, "value": value, "value_mm": value, "unit": unit})


async def run_agent(steps, answers, *, options=None, catalog=None, max_calls=80, language="ru"):
    mcp = FakeMcp(steps, catalog=catalog)
    agent = Agent(mcp)
    provider = ScriptedProvider(list(answers))
    run = agent.start(session_id="s", provider=provider, message="Сделай деталь",
                      evaluation=options or EvaluationOptions(), requested_profile_id="text",
                      max_calls=max_calls, language=language)
    await asyncio.wait_for(agent.wait(run.id), 3)
    return run, mcp, provider


CAD_CATALOG = [
    tool("get_dimension", {"name": {"type": "string"}}, ["name"]),
    tool("set_dimension", {"name": {"type": "string"}, "value_mm": {"type": "number"}},
         ["name", "value_mm"]),
    tool("get_feature_tree", {"limit": {"type": "integer"}}),
    tool("list_open_documents"), tool("connect_solidworks"), tool("screenshot"),
    tool("create_part"), tool("execute_python", {"code": {"type": "string"}}, ["code"]),
]


def doc_steps(*extra):
    return [("connect_solidworks", ok({"active_document": "Part1"})),
            ("list_open_documents", ok({"documents": [
                {"title": "Part1", "path": "C:/Part1.sldprt", "type": "part"}]})), *extra]


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_goal_tools_follow_current_run_contract(continuation):
    mcp = FakeMcp([
        ("get_dimension", dimension("D1", 50.0)),
        ("set_dimension", ok({"name": "D1", "unit": "mm"})),
        ("get_dimension", dimension("D1", 62.0)),
        *doc_steps(("get_dimension", dimension("D1", 62.0))),
    ], catalog=CAD_CATALOG)
    agent = Agent(mcp)
    if continuation:
        previous = agent.start(session_id="s", provider=ScriptedProvider([
            response(define(requirement("d1", 50.0), ambiguities=["Confirm the size"]))]),
            message="Set the size", evaluation=EvaluationOptions())
        await asyncio.wait_for(agent.wait(previous.id), 3)
        assert previous.goal["completion_reason"] == "clarification_required"

    provider = ScriptedProvider([
        response(call("read-current", "get_dimension", {"name": "D1"})),
        response(call("define-current", "cadnext_define_goal", {
            "kind": "cad", "requirements": [requirement("d1", 62.0)]})),
        response(call("bind-current", "cadnext_bind_requirements", {"bindings": [
            {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
        response(call("set-current", "set_dimension", {"name": "D1", "value_mm": 62.0})),
        response(content="Ready for verification."),
    ])
    run = agent.start(session_id="s", provider=provider, message="Set the size to 62 mm",
                      evaluation=EvaluationOptions())
    await asyncio.wait_for(agent.wait(run.id), 3)

    available = [{item["function"]["name"] for item in tools} for _, tools in provider.sent]
    preparation_tools = {"get_dimension", "get_feature_tree", "list_open_documents",
                         "connect_solidworks", "screenshot", "set_construction_plan",
                         "cadnext_define_goal"}
    assert available[:2] == [preparation_tools, preparation_tools]
    assert {"set_dimension", "create_part", "execute_python", "cadnext_bind_requirements"} <= available[2]
    assert run.status == "completed"
    assert run.goal["outcome"] == "achieved"
    assert run.goal["contract"]["requirements"][0]["expected"] == 62.0
    assert not mcp.steps
    if continuation:
        assert previous.goal["contract"]["requirements"][0]["expected"] == 50.0
        assert any(item.get("tool_call_id") == "define-1" for item in provider.sent[0][0])


@pytest.mark.asyncio
async def test_early_ready_text_does_not_complete_cad_goal():
    run, mcp, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 40.0))),
        [response(define(requirement("d1", 50.0))),
         response(content="Готово."),
         response(content="Готово.")])
    assert run.status == "limit_reached"
    assert run.goal["outcome"] == "unknown"
    assert run.goal["completion_reason"] == "no_progress"
    assert run.goal["evaluations"][0]["score"] == 0


@pytest.mark.asyncio
async def test_seventy_then_correction_reaches_hundred():
    mcp_steps = doc_steps(
        ("get_dimension", dimension("D1", 50.0)),
        ("get_dimension", dimension("D2", 30.0)),
        ("set_dimension", ok({"name": "D2", "unit": "mm"})),
        ("get_dimension", dimension("D2", 20.0)),
        ("connect_solidworks", ok({"active_document": "Part1"})),
        ("list_open_documents", ok({"documents": [
            {"title": "Part1", "path": "C:/Part1.sldprt", "type": "part"}]})),
        ("get_dimension", dimension("D1", 50.0)),
        ("get_dimension", dimension("D2", 20.0)),
    )
    bind = call("bind-1", "cadnext_bind_requirements", {"bindings": [
        {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"},
        {"requirement_id": "d2", "tool": "get_dimension", "name": "D2"}]})
    run, _, _ = await run_agent(
        mcp_steps,
        [response(define(requirement("d1", 50.0, weight=7),
                         requirement("d2", 20.0, weight=3))),
         response(bind),
         response(content="Проверяю."),
         response(call("c1", "set_dimension", {"name": "D2", "value_mm": 20.0})),
         response(content="Исправил.")])
    assert [item["score"] for item in run.goal["evaluations"]] == [70, 100]
    assert run.status == "completed"
    assert run.goal["outcome"] == "achieved"
    assert run.goal["completion_reason"] == "criteria_met"


@pytest.mark.asyncio
async def test_visual_silhouette_cannot_override_wrong_dimension():
    steps = doc_steps(("get_dimension", dimension("D1", 40.0)))
    run, _, provider = await run_agent(
        steps,
        [response(define(requirement("d1", 50.0),
                         requirement("v1", method="vision"))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово."), response(content="Готово.")],
        catalog=CAD_CATALOG)
    assert run.status in {"limit_reached", "completed"}
    assert run.goal["outcome"] == "not_achieved"


@pytest.mark.asyncio
async def test_two_texts_without_actions_are_finite():
    run, mcp, provider = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 40.0))),
        [response(define(requirement("d1", 50.0))),
         response(content="Готово."), response(content="Готово.")])
    assert len(run.goal["evaluations"]) == 1
    assert len(provider.sent) == 3
    assert run.status in {"limit_reached", "completed"}


@pytest.mark.asyncio
async def test_bindings_cannot_change_frozen_values():
    run, mcp, _ = await run_agent(
        [], [response(define(requirement("d1", 50.0))),
             response(call("bind", "cadnext_bind_requirements", {"bindings": [
                 {"requirement_id": "d1", "tool": "get_dimension", "name": "D1",
                  "expected": 40.0}]})),
             response(content="Готово.")])
    assert run.status == "failed"
    assert run.goal["contract"]["requirements"][0]["expected"] == 50.0
    assert not [name for name, _ in mcp.calls if name.startswith("get_")]


@pytest.mark.asyncio
async def test_informational_goal_blocks_cad_mutation():
    run, mcp, _ = await run_agent(
        [], [response(define(kind="informational")),
             response(call("c1", "create_part")),
             response(content="Ответ.")])
    assert run.status == "failed"
    assert [name for name, _ in mcp.calls] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("informational", [False, True])
async def test_blocked_goal_batch_closes_tool_calls_before_followup(informational):
    mcp = FakeMcp([])
    agent = Agent(mcp)
    answers = [response(define(kind="informational"))] if informational else []
    answers.append(response(call("blocked-create", "create_part"),
                            call("blocked-read", "get_dimension", {"name": "D1"})))
    first = agent.start(session_id="s", provider=ScriptedProvider(answers), message="Create a part",
                        evaluation=EvaluationOptions())
    await asyncio.wait_for(agent.wait(first.id), 3)
    assert first.status == "failed"
    assert mcp.calls == []

    provider = ScriptedProvider([
        response(call("define-followup", "cadnext_define_goal", {
            "kind": "informational", "requirements": []})),
        response(content="No CAD actions were performed."),
    ])
    followup = agent.start(session_id="s", provider=provider, message="What happened?",
                           evaluation=EvaluationOptions())
    await asyncio.wait_for(agent.wait(followup.id), 3)
    history = provider.sent[0][0]
    replies = {item["tool_call_id"]: json.loads(item["content"])
               for item in history if item["role"] == "tool"}
    assert {"blocked-create", "blocked-read"} <= replies.keys()
    for call_id in ("blocked-create", "blocked-read"):
        assert replies[call_id]["ok"] is False
        assert replies[call_id]["uncertain"] is False
    assert followup.status == "completed"
    assert mcp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language,ambiguity", [
    ("ru", "Укажите радиус скругления"), ("en", "Specify the fillet radius"),
])
async def test_clarification_finishes_without_mutation(language, ambiguity):
    run, mcp, _ = await run_agent(
        [], [response(define(requirement("d1", 50.0), ambiguities=[ambiguity]))],
        language=language)
    assert run.status == "completed"
    assert run.goal["outcome"] == "unknown"
    assert run.goal["completion_reason"] == "clarification_required"
    assert mcp.calls == []
    clarification = [event["data"]["content"] for event in run.events
                     if event["type"] == "message" and event["data"]["role"] == "assistant"]
    assert len(clarification) == 1
    assert ambiguity in clarification[0]
    assert ambiguity in run.goal["summary"]
    assert run.messages[-1] == {"role": "assistant", "content": clarification[0]}


@pytest.mark.asyncio
async def test_stop_before_start_and_during_model_turns():
    mcp = FakeMcp([])
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=ScriptedProvider([
        response(define(kind="informational")), response(content="Ответ.")]),
        message="x", evaluation=EvaluationOptions())
    agent.stop(run.id)
    await asyncio.wait_for(agent.wait(run.id), 3)
    assert run.status == "stopped"
    assert run.goal["outcome"] is None
    second = agent.start(session_id="s", provider=ScriptedProvider([]), message="y",
                         evaluation=EvaluationOptions())
    agent.stop(second.id)
    await asyncio.wait_for(agent.wait(second.id), 3)
    assert second.status == "stopped"


@pytest.mark.asyncio
async def test_evaluation_budget_is_reserved_before_mutation():
    options = EvaluationOptions(max_evaluations=1)
    run, mcp, _ = await run_agent(
        doc_steps(),
        [response(define(requirement("d1", 50.0))), response(content="Готово.")],
        options=options)
    assert run.status in {"limit_reached", "failed"}
    assert run.call_count <= run.max_calls

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [False, True])
async def test_followup_evaluation_retains_drawing_from_conversation(reopen):
    mcp = FakeMcp([
        ("screenshot", ok(None, [{"data": PNG, "mimeType": "image/png"}])),
        *doc_steps(("screenshot", ok(None, [{"data": PNG, "mimeType": "image/png"}]))),
    ])
    agent = Agent(mcp)
    first = agent.start(session_id="drawing", provider=ScriptedProvider([
        response(call("previous-view", "screenshot")), response(content="Drawing read.")]),
        message="Use this drawing", attachments=[{"name": "drawing.png", "mimeType": "image/png", "data": PNG}])
    await asyncio.wait_for(agent.wait(first.id), 3)
    original = first.events[0]["data"]["attachments"][0]
    if reopen:
        saved = agent.conversation_snapshot("drawing")
        agent = Agent(mcp)
        agent.load_conversation(saved)
    provider = ScriptedProvider([
        response(define(requirement("v1", method="vision"))), response(content="Check the result."),
        response(call("vision-report", "cadnext_report_visual_evaluation", {"evaluations": [
            {"requirement_id": "v1", "status": "pass", "explanation": "Matches the drawing",
             "image_ids": ["current-0"]}]})),
    ])
    followup = agent.start(session_id="drawing", provider=provider, message="Continue from the drawing",
                           evaluation=EvaluationOptions())
    await asyncio.wait_for(agent.wait(followup.id), 3)
    assert followup.status == "completed"
    assert followup.goal["outcome"] == "achieved"
    visual_messages, _ = provider.sent[-1]
    images = [block["image_url"]["url"] for block in visual_messages[-1]["content"]
              if block["type"] == "image_url"]
    assert images[0] == f'data:{original["mimeType"]};base64,{original["data"]}'
    assert len(images) == 2
    assert not mcp.steps


@pytest.mark.asyncio
async def test_visual_report_uses_current_images_and_is_marked_mixed():
    steps = doc_steps(("get_dimension", dimension("D1", 50.0)),
                      ("screenshot", ok(None, [{"data": PNG, "mimeType": "image/png"}])))
    report = call("vision-report", "cadnext_report_visual_evaluation", {"evaluations": [
        {"requirement_id": "v1", "status": "pass", "explanation": "Скругление видно",
         "image_ids": ["current-0"]}]})
    mcp = FakeMcp(steps)
    agent = Agent(mcp)
    provider = ScriptedProvider([
        response(define(requirement("d1", 50.0), requirement("v1", method="vision"))),
        response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
            {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
        response(content="Готово."), response(report)])
    run = agent.start(session_id="s", provider=provider, message="Сделай деталь",
                      evaluation=EvaluationOptions(),
                      attachments=[{"name": "ref.png", "mimeType": "image/png", "data": PNG}])
    await asyncio.wait_for(agent.wait(run.id), 3)
    assert run.status == "completed"
    assert run.goal["outcome"] == "achieved"
    assert run.goal["authority"] == "mixed"
    vision_sent = provider.sent[-1][0][-1]["content"]
    assert any(part.get("type") == "image_url" for part in vision_sent)
    assert run.goal["summary"]


@pytest.mark.asyncio
async def test_vision_without_reference_never_passes():
    steps = doc_steps(("get_dimension", dimension("D1", 50.0)))
    run, _, provider = await run_agent(
        steps,
        [response(define(requirement("v1", method="vision"))),
         response(content="Готово."), response(content="Готово.")])
    assert run.goal["outcome"] != "achieved"
    assert all(item["score"] == 0 for item in run.goal["evaluations"])

@pytest.mark.asyncio
async def test_snapshot_keeps_full_goal_and_state_stays_compact():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    snapshot = run.snapshot()
    assert snapshot["goal"]["evaluations"][0]["score"] == 100
    assert snapshot["goal"]["contract"]["requirements"][0]["id"] == "d1"
    state = run.state()
    assert state["goal"]["evaluation_count"] == 1
    assert "contract" not in state["goal"]
    assert "evaluations" not in state["goal"]
    assert state["profile_id"] == run.profile_id
    snapshot["goal"]["phase"] = "changed"
    assert run.goal["phase"] != "changed"


@pytest.mark.asyncio
async def test_sidecar_round_trip_and_legacy_without_goal():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    agent = Agent(FakeMcp([]))
    agent.runs[run.id] = run
    data = agent.conversation_snapshot(run.session_id)
    restored = Agent(FakeMcp([])).load_conversation(data)
    goal = restored["runs"][-1]["goal"]
    assert goal["version"] == 1
    assert goal["outcome"] == "achieved"
    legacy = json.loads(json.dumps(data))
    for item in legacy["runs"]:
        item.pop("goal")
    loaded = Agent(FakeMcp([])).load_conversation(legacy)
    assert loaded["runs"][-1]["goal"] is None


@pytest.mark.asyncio
async def test_sidecar_rejects_unknown_goal_version_and_false_achievement():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    agent = Agent(FakeMcp([]))
    agent.runs[run.id] = run
    data = agent.conversation_snapshot(run.session_id)
    unknown = json.loads(json.dumps(data))
    unknown["runs"][-1]["goal"]["version"] = 2
    with pytest.raises(ValueError):
        Agent(FakeMcp([])).load_conversation(unknown)
    false = json.loads(json.dumps(data))
    false["runs"][-1]["goal"]["outcome"] = "achieved"
    false["runs"][-1]["goal"]["evaluations"][-1]["outcome"] = "not_achieved"
    with pytest.raises(ValueError):
        Agent(FakeMcp([])).load_conversation(false)

@pytest.mark.asyncio
async def test_vision_pass_without_current_image_id_never_passes():
    steps = doc_steps(("screenshot", ok(None, [{"data": PNG, "mimeType": "image/png"}])))
    report = call("vision-report", "cadnext_report_visual_evaluation", {"evaluations": [
        {"requirement_id": "v1", "status": "pass", "explanation": "Похоже",
         "image_ids": []}]})
    mcp = FakeMcp(steps)
    agent = Agent(mcp)
    provider = ScriptedProvider([
        response(define(requirement("v1", method="vision"))),
        response(content="Готово."), response(report)])
    run = agent.start(session_id="s", provider=provider, message="Сделай деталь",
                      evaluation=EvaluationOptions(),
                      attachments=[{"name": "ref.png", "mimeType": "image/png", "data": PNG}])
    await asyncio.wait_for(agent.wait(run.id), 3)
    assert run.goal["outcome"] != "achieved"
    assert run.goal["evaluations"][0]["per_requirement"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_stop_during_vision_prevents_achievement():
    holder = {}

    class StopOnVision(ScriptedProvider):
        def __init__(self, answers):
            super().__init__(answers)
            self.holder = holder

        async def complete(self, messages, tools, **kwargs):
            if any((tool.get("function") or {}).get("name") == "cadnext_report_visual_evaluation"
                   for tool in tools):
                self.holder["agent"].stop(self.holder["run"].id)
            return await super().complete(messages, tools, **kwargs)

    steps = doc_steps(("screenshot", ok(None, [{"data": PNG, "mimeType": "image/png"}])))
    report = call("vision-report", "cadnext_report_visual_evaluation", {"evaluations": [
        {"requirement_id": "v1", "status": "pass", "explanation": "Совпало",
         "image_ids": ["current-0"]}]})
    agent = Agent(FakeMcp(steps))
    provider = StopOnVision([response(define(requirement("v1", method="vision"))),
                             response(content="Готово."), response(report)])
    run = agent.start(session_id="s", provider=provider, message="Сделай деталь",
                      evaluation=EvaluationOptions(),
                      attachments=[{"name": "ref.png", "mimeType": "image/png", "data": PNG}])
    holder.update(agent=agent, run=run)
    await asyncio.wait_for(agent.wait(run.id), 3)
    assert run.status == "stopped"
    assert run.goal["outcome"] != "achieved"
    assert run.goal["evaluations"] == []


@pytest.mark.asyncio
async def test_file_requirement_without_current_write_is_unknown():
    run, mcp, _ = await run_agent(
        doc_steps(),
        [response(define(requirement("f1", method="cad"))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "f1", "tool": "file", "name": "C:/Out/part.sldprt"}]})),
         response(content="Готово.")])
    assert run.goal["evaluations"][0]["per_requirement"][0]["status"] == "unknown"
    assert run.goal["outcome"] != "achieved"
    assert not [name for name, _ in mcp.calls if name in {"save_document", "export_flat_pattern_dxf"}]


@pytest.mark.asyncio
async def test_sidecar_rejects_achieved_without_contract_or_full_coverage():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    agent = Agent(FakeMcp([]))
    agent.runs[run.id] = run
    data = agent.conversation_snapshot(run.session_id)
    no_contract = json.loads(json.dumps(data))
    goal = no_contract["runs"][-1]["goal"]
    goal["contract"] = None
    goal["kind"] = None
    with pytest.raises(ValueError):
        Agent(FakeMcp([])).load_conversation(no_contract)
    uncovered = json.loads(json.dumps(data))
    uncovered["runs"][-1]["goal"]["evaluations"][-1]["per_requirement"] = []
    with pytest.raises(ValueError):
        Agent(FakeMcp([])).load_conversation(uncovered)


@pytest.mark.asyncio
async def test_binding_after_unknown_triggers_a_fresh_evaluation():
    steps = doc_steps(
        ("connect_solidworks", ok({"active_document": "Part1"})),
        ("list_open_documents", ok({"documents": [
            {"title": "Part1", "path": "C:/Part1.sldprt", "type": "part"}]})),
        ("get_dimension", dimension("D1", 50.0)))
    bind = call("bind-1", "cadnext_bind_requirements", {"bindings": [
        {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})
    run, _, _ = await run_agent(
        steps, [response(define(requirement("d1", 50.0))), response(content="Готово."),
                response(bind), response(content="Готово.")])
    assert run.status == "completed"
    assert run.goal["outcome"] == "achieved"
    assert [item["outcome"] for item in run.goal["evaluations"]] == ["unknown", "achieved"]


@pytest.mark.asyncio
async def test_insufficient_budget_finishes_before_any_evaluation_read():
    run, mcp, _ = await run_agent(
        [], [response(define(requirement("d1", 50.0))), response(content="Готово.")],
        max_calls=1)
    assert run.status == "limit_reached"
    assert run.goal["completion_reason"] == "call_limit"
    assert run.call_count == 0
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_sidecar_rejects_false_cad_evidence_in_achieved_goal():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    agent = Agent(FakeMcp([]))
    agent.runs[run.id] = run
    data = agent.conversation_snapshot(run.session_id)
    for change in ("source", "actual", "revision"):
        broken = json.loads(json.dumps(data))
        goal = broken["runs"][-1]["goal"]
        item = goal["evaluations"][-1]["per_requirement"][0]
        if change == "source":
            item["source"] = "vision"
        elif change == "actual":
            item["actual"] = 40.0
        else:
            goal["evaluations"][-1]["revision"] = goal["revision"] + 1
        with pytest.raises(ValueError):
            Agent(FakeMcp([])).load_conversation(broken)


@pytest.mark.asyncio
async def test_old_goal_snapshot_without_binding_counters_loads():
    run, _, _ = await run_agent(
        doc_steps(("get_dimension", dimension("D1", 50.0))),
        [response(define(requirement("d1", 50.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово.")])
    agent = Agent(FakeMcp([]))
    agent.runs[run.id] = run
    data = agent.conversation_snapshot(run.session_id)
    for item in data["runs"]:
        item["goal"].pop("bindings_revision", None)
        item["goal"].pop("last_evaluated_bindings", None)
    loaded = Agent(FakeMcp([])).load_conversation(data)
    assert loaded["runs"][-1]["goal"]["bindings_revision"] == 0


@pytest.mark.asyncio
async def test_document_change_clears_bindings_and_progress():
    steps = doc_steps(
        ("get_dimension", dimension("D1", 50.0)),
        ("set_dimension", ok({"name": "D1", "unit": "mm"})),
        ("get_dimension", dimension("D1", 60.0)),
        ("connect_solidworks", ok({"active_document": "Part2"})),
        ("list_open_documents", ok({"documents": [
            {"title": "Part2", "path": "C:/Part2.sldprt", "type": "part"}]})),
    )
    run, _, _ = await run_agent(
        steps,
        [response(define(requirement("d1", 60.0))),
         response(call("bind-1", "cadnext_bind_requirements", {"bindings": [
             {"requirement_id": "d1", "tool": "get_dimension", "name": "D1"}]})),
         response(content="Готово."),
         response(call("c1", "set_dimension", {"name": "D1", "value_mm": 60.0})),
         response(content="Готово."), response(content="Готово.")])
    assert run.status == "limit_reached"
    assert run.goal["outcome"] == "unknown"
    assert run.goal["document_identity"].startswith("Part2|")
    assert run.goal["bindings"] == {}
    assert run.goal["best_passed_weight"] == 0
    assert run.goal["no_progress"] == 2
