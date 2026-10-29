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
        catalog.append(tool("open_document", {"path": {"type": "string"},
                                             "session_id": {"type": "string"}}, ["path"]))
        for item in catalog:
            if item["function"]["name"] in {"create_part", "create_assembly"}:
                item["function"]["parameters"]["properties"]["session_id"] = {"type": "string"}
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
            for item in tools:
                if item["function"]["name"] in {"create_part", "create_assembly", "open_document"}:
                    assert "session_id" not in item["function"]["parameters"]["properties"]
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


@pytest.mark.parametrize("evaluate", [False, True])
async def test_staged_run_publishes_terminal_status_only_after_session_cleanup(evaluate):
    from cadnext.evaluation import EvaluationOptions
    from tests.test_goal_loop import define, doc_steps, requirement, response

    class ClosingMcp(SessionMcp):
        async def call(self, name, arguments):
            if name == "close_python_session":
                self.entered.set()
                await self.release.wait()
            return await super().call(name, arguments)

    answers = [reply(("plan", "set_construction_plan", {"stages": STAGES[:1]})),
               reply(("build", "execute_python", {"code": "result = 80", "stage_id": "profile",
                                                       "checks": CHECKS})),
               reply(content="Готово")]
    steps = [("execute_python", ok({})),
             ("inspect_cad", ok({"dimensions": [{"name": "Length@Profile", "value": 80,
                                                "unit": "mm", "driving": True}]}))]
    if evaluate:
        answers.insert(0, response(define(requirement("length", 80))))
        answers.insert(3, reply(("bind", "cadnext_bind_requirements", {"bindings": [
            {"requirement_id": "length", "tool": "get_dimension", "name": "Length@Profile"}]})))
        steps.extend(doc_steps(("get_dimension", ok({"name": "Length@Profile", "value": 80,
                                                     "unit": "mm"}))))
    provider = ScriptedProvider(answers)
    mcp = ClosingMcp(steps)
    mcp.catalog.extend(staged_mcp([]).catalog[-1:])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй",
                      evaluation=EvaluationOptions() if evaluate else None)
    await asyncio.wait_for(mcp.entered.wait(), 2)
    try:
        assert run.status == "running"
        assert not any(event["type"] == "status" and event["data"]["status"] == "completed"
                       for event in run.events)
    finally:
        mcp.release.set()
        await settle(agent, run)
    assert run.status == "completed"
    assert run.construction_plan[0]["status"] == "done"
    assert run.construction_plan[0]["checks"][0]["passed"] is True
    if evaluate:
        assert run.goal["outcome"] == "achieved"
        assert run.goal["last_score"] == 100
    restored = Agent(FakeMcp()).load_conversation(agent.conversation_snapshot("s"))["runs"][0]
    assert restored["construction_plan"] == run.construction_plan
    assert restored["goal"] == run.snapshot()["goal"]


@pytest.mark.parametrize("active", ["bushing.sldprt", "Other"])
async def test_explicit_open_rebinds_hidden_session_and_checks_active_path_before_python(active):
    provider = ScriptedProvider([
        reply(("before", "execute_python", {"code": "marker = 42"})),
        reply(("open", "open_document", {"path": "C:/test/bushing.sldprt"})),
        reply(("after", "execute_python", {"code": "result = marker"})),
        reply(content="Готово")])
    mcp = SessionMcp([("execute_python", ok({})),
        ("open_document", ok({"opened": "bushing.sldprt"})),
        ("connect_solidworks", ok({"active_document": active})),
        ("list_open_documents", ok({"documents": [{"title": "bushing.sldprt", "path": "C:/test/bushing.sldprt"}]})),
        ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Открой тест")
    await settle(agent, run)
    assert run.status == ("completed" if active == "bushing.sldprt" else "failed")
    assert ("open_document", {"path": "C:/test/bushing.sldprt", "session_id": "private-env"}) in mcp.calls
    assert sum(name == "execute_python" for name, _ in mcp.calls) == (2 if active == "bushing.sldprt" else 1)
    assert "private-env" not in json.dumps(run.snapshot())


async def test_failed_new_document_inspection_prevents_python_after_internal_binding():
    provider = ScriptedProvider([
        reply(("part", "execute_python", {"code": "part_ref = model"})),
        reply(("new", "create_assembly", {}), ("must-not-run", "execute_python", {"code": "mutate()"}))])
    mcp = SessionMcp([("execute_python", ok({})),
        ("list_open_documents", ok({"documents": []})),
        ("create_assembly", ok({"created": "Assembly1"})),
        ("list_open_documents", ok({"documents": []}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "failed"
    assert sum(name == "execute_python" for name, _ in mcp.calls) == 1
    assert ("create_assembly", {"session_id": "private-env"}) in mcp.calls
    assert mcp.lifecycle[-1][0] == "close_python_session"


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


async def test_native_created_assembly_explicitly_rebinds_existing_python_session_after_checks():
    provider = ScriptedProvider([
        reply(("part", "execute_python", {"code": "part_ref = model"})),
        reply(("new", "create_assembly", {})),
        reply(("assembly", "execute_python", {"code": "insert(part_ref)"})),
        reply(content="Готово")])
    mcp = SessionMcp([
        ("execute_python", ok({})),
        ("list_open_documents", ok({"documents": [{"title": "Part1", "type": "part"}]})),
        ("create_assembly", ok({"created": "Assembly1"})),
        ("list_open_documents", ok({"documents": [{"title": "Part1", "type": "part"},
                                                   {"title": "Assembly1", "type": "assembly"}]})),
        ("get_feature_tree", ok({"features": []})), ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Создай сборку")
    await settle(agent, run)
    assert run.status == "completed"
    assert mcp.lifecycle == [("open_python_session", {}),
        ("close_python_session", {"session_id": "private-env"})]
    assert ("create_assembly", {"session_id": "private-env"}) in mcp.calls
    assert "private-env" not in json.dumps(run.snapshot())


def staged_mcp(steps, **kwargs):
    catalog = deepcopy(CATALOG)
    catalog += [tool("inspect_cad", {"sections": {"type": "array", "items": {"type": "string"}},
                                   "dimension_names": {"type": "array", "items": {"type": "string"}}},
                     ["sections"])]
    return FakeMcp(steps, catalog=catalog, **kwargs)


@pytest.mark.parametrize("defect", [None, "suppressed", "wrong_type", "old", "truncated"])
async def test_native_mate_verification_reads_subfeature_mates_and_rejects_unhealthy_result(defect):
    mate = {"name": "Concentric1", "type": 1, "suppressed": defect == "suppressed", "error_code": 0}
    if defect == "wrong_type":
        mate["type"] = 0
    provider = ScriptedProvider([reply(("mate", "add_mate", {})), reply(content="Готово")])
    mcp = staged_mcp([
        ("inspect_cad", ok({"mates": [mate] if defect == "old" else [], "truncated": False})),
        ("add_mate", ok({"mate": "Concentric1"})),
        ("inspect_cad", ok({"mates": [mate], "truncated": defect == "truncated"}))])
    # Native default is coincident; choose concentric explicitly in the real schema.
    for entry in mcp.catalog:
        if entry["function"]["name"] == "add_mate":
            entry["function"]["parameters"]["properties"] = {"mate_type": {"type": "string"}}
    provider.responses[0] = reply(("mate", "add_mate", {"mate_type": "concentric"}))
    agent = Agent(mcp)
    run = agent.start(session_id="s", provider=provider, message="Добавь сопряжение")
    await settle(agent, run)
    assert run.status == ("completed" if defect is None else "failed")
    assert [name for name, _ in mcp.calls] == ["inspect_cad", "add_mate", "inspect_cad"]


STAGES = [{"id": "profile", "title": "Профиль", "expected_result": "Длина 80 мм"},
          {"id": "body", "title": "Тело", "expected_result": "Одно тело"}]
CHECKS = [{"kind": "dimension", "name": "Length@Profile", "value": 80, "unit": "mm"}]


async def test_supplemental_python_does_not_complete_a_pending_construction_stage():
    observed = []

    def build_profile(messages):
        observed.append(deepcopy(run.construction_plan))
        return reply(("profile", "execute_python", {"code": "build_profile()", "stage_id": "profile"}))

    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        reply(("diagnostics", "execute_python", {"code": "result = read_existing_profile()"})),
        build_profile,
        reply(("body", "execute_python", {"code": "build_body()", "stage_id": "body"})),
        reply(content="Готово")])
    mcp = staged_mcp([("execute_python", ok({})) for _ in range(3)])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Продолжи деталь")
    await settle(agent, run)
    assert run.status == "completed"
    assert [stage["status"] for stage in observed[0][:2]] == ["pending", "pending"]
    diagnostic_stage = observed[0][-1]
    assert diagnostic_stage["application_owned"] is True
    assert diagnostic_stage["status"] == "done"
    assert diagnostic_stage["tool_call_ids"] == ["diagnostics"]
    assert run.construction_plan[0]["tool_call_ids"] == ["profile"]
    assert run.construction_plan[1]["tool_call_ids"] == ["body"]
    assert len(mcp.calls) == 3


@pytest.mark.parametrize("failures", [1, 2])
async def test_python_diagnostics_preserve_failed_stage_and_repair_until_explicit_continuation(failures):
    from tests.test_python_repair import cad_error

    observed = []
    resumed = []

    def continue_profile(messages):
        observed.append((run.repairing, deepcopy(run.construction_plan), deepcopy(run.checks)))
        return reply(("fixed", "execute_python", {
            "code": "continue_existing_profile()", "stage_id": "profile", "checks": CHECKS}))

    def build_body(messages):
        resumed.append((run.status, run.repairing))
        return reply(("body", "execute_python", {"code": "create_body()", "stage_id": "body"}))

    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        *(reply((f"broken-{i}", "execute_python", {
            "code": f"partial_step_{i}()", "stage_id": "profile"})) for i in range(failures)),
        reply(("diagnostics", "execute_python", {"code": "result = inspect_existing_features()"})),
        continue_profile,
        build_body,
        reply(content="Готово")])
    mcp = staged_mcp([
        *(("execute_python", cad_error()) for _ in range(failures)),
        ("execute_python", ok({"result": "existing profile"})),
        ("execute_python", ok({"result": "continued"})),
        ("inspect_cad", ok({"dimensions": [
            {"name": "Length@Profile", "value": 80, "unit": "mm", "driving": True}]})),
        ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)

    repairing, stages, checks = observed[0]
    assert repairing is True
    assert [(s["id"], s["status"]) for s in stages[:2]] == [("profile", "error"), ("body", "pending")]
    assert checks[f"broken-{failures - 1}"]["status"] == "failed"
    assert stages[-1]["application_owned"] is True
    assert stages[-1]["tool_call_ids"] == ["diagnostics"]
    assert run.status == "completed"
    assert resumed == [("running", False)]
    assert run.repairing is False
    assert run.repair_attempts == failures
    assert all(run.checks[f"broken-{i}"]["status"] == "repaired" for i in range(failures))
    assert run.checks["fixed"]["status"] == "unverified"
    assert run.construction_plan[0]["checks"][0]["passed"] is True
    assert run.checks["diagnostics"]["status"] == "unverified"
    assert run.verification == "unverified"
    assert run.construction_plan[1]["tool_call_ids"] == ["body"]
    assert [args["code"] for name, args in mcp.calls if name == "execute_python"] == [
        *(f"partial_step_{i}()" for i in range(failures)),
        "result = inspect_existing_features()", "continue_existing_profile()", "create_body()"]


async def test_pending_plan_revision_retains_application_owned_native_stages():
    extra = {"id": "edges", "title": "Кромки", "expected_result": "Выбраны торцы"}
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES})),
        reply(("new", "create_part", {})),
        reply(("profile", "execute_python", {"code": "profile()", "stage_id": "profile"})),
        reply(("revise", "set_construction_plan", {"stages": [STAGES[0], extra, STAGES[1]]})),
        reply(("edges", "execute_python", {"code": "edges()", "stage_id": "edges"})),
        reply(content="Готово")])
    mcp = staged_mcp([("list_open_documents", ok({"documents": []})),
                     ("create_part", ok({"created": "Part1"})),
                     ("list_open_documents", ok({"documents": [{"title": "Part1", "type": "part"}]})),
                     ("get_feature_tree", ok({"features": [], "truncated": False})),
                     ("execute_python", ok({})), ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    assert [s["id"] for s in run.construction_plan] == ["profile", "edges", "body", "step-1"]
    automatic = run.construction_plan[-1]
    assert automatic["application_owned"] is True
    assert automatic["status"] == "done"
    assert automatic["tool_call_ids"] == ["new"]
    saved = agent.conversation_snapshot("s")
    assert Agent(FakeMcp()).load_conversation(saved)["runs"][0]["construction_plan"][-1] == automatic


def test_plan_revision_cannot_rewrite_application_stage_or_exceed_combined_limit():
    from cadnext.construction import set_plan

    native = {"id": "step-1", "title": "Создание", "expected_result": "create_part",
              "status": "done", "application_owned": True, "checks": [], "tool_call_ids": ["new"]}
    declaration = {k: native[k] for k in ("id", "title", "expected_result")}
    assert set_plan([native], [declaration]) == [native]
    with pytest.raises(ValueError, match="Служебный"):
        set_plan([native], [{**declaration, "title": "Изменено"}])
    with pytest.raises(ValueError, match="лимит"):
        set_plan([native], [{"id": f"planned-{i}", "title": "Этап", "expected_result": "Тело"}
                            for i in range(32)])


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


async def test_native_document_creation_does_not_complete_planned_python_profile():
    provider = ScriptedProvider([
        reply(("plan", "set_construction_plan", {"stages": STAGES[:1]})),
        reply(("new", "create_part", {})),
        reply(("profile", "execute_python", {"code": "build_profile()", "stage_id": "profile"})),
        reply(content="Готово")])
    mcp = staged_mcp([
        ("list_open_documents", ok({"documents": []})),
        ("create_part", ok({"created": "NewPart"})),
        ("list_open_documents", ok({"documents": [{"title": "NewPart", "type": "part"}]})),
        ("get_feature_tree", ok({"features": [], "truncated": False})),
        ("execute_python", ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id="s", provider=provider, message="Построй")
    await settle(agent, run)
    assert run.status == "completed"
    profile = next(s for s in run.construction_plan if s["id"] == "profile")
    assert profile["tool_call_ids"] == ["profile"]
    assert profile["status"] == "done"
    native = next(s for s in run.construction_plan if s["id"] != "profile")
    assert native["tool_call_ids"] == ["new"]
    result = next(m for m in run.messages if m.get("tool_call_id") == "new")
    assert json.loads(result["content"])["construction_stage"]["id"] == native["id"]


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
