from scripts.staged_provider_acceptance import StopOnCadError, prompts


async def test_acceptance_can_resume_saved_research_in_a_new_run(tmp_path):
    import json

    from cadnext.agent import Agent
    from scripts.staged_provider_acceptance import run_cases
    from tests.fake_mcp import FakeMcp
    from tests.test_agent import ScriptedProvider, reply

    provider = ScriptedProvider([reply(content="Ранее прочитана сигнатура FeatureRevolve2")])
    previous = Agent(FakeMcp())
    run = previous.start(session_id="previous", provider=provider, message="Изучи API")
    await previous.wait(run.id)
    saved = previous.conversation_snapshot("previous")

    def resumed(messages):
        assert any(m.get("content") == "Ранее прочитана сигнатура FeatureRevolve2" for m in messages)
        return reply(content="Готово; геометрия требует проверки")

    provider = ScriptedProvider([resumed])
    report = await run_cases(FakeMcp(), provider, None, tmp_path, ["bushing"], resume=saved)
    assert report["runs"][0]["status"] == "completed"
    persisted = json.loads((tmp_path / "conversation.json").read_text(encoding="utf-8"))
    assert len(persisted["runs"]) == 2
    Agent(FakeMcp()).load_conversation(persisted)


def test_independent_assembly_case_can_reference_earlier_test_bushing(tmp_path):
    source = tmp_path / "earlier-test-bushing.sldprt"
    source.touch()
    text = prompts(tmp_path / "assembly-run", source_bushing=source)["assembly"]
    assert str(source) in text
    assert "assembly-run-agent-assembly.sldasm" in text


def test_test_document_names_are_unique_between_acceptance_folders(tmp_path):
    assert "first-agent-bushing.sldprt" in prompts(tmp_path / "first")["bushing"]
    assert "second-agent-bushing.sldprt" in prompts(tmp_path / "second")["bushing"]


async def test_provider_acceptance_stops_on_known_error_before_agent_repairs():
    stopped = []

    class Cad:
        async def call(self, name, arguments):
            return {"ok": False, "uncertain": False, "error": "known CAD error"}

    cad = StopOnCadError(Cad(), lambda: stopped.append(True))
    result = await cad.call("execute_python", {"code": "build()"})
    assert result["ok"] is False
    assert stopped == [True]


async def test_provider_acceptance_leaves_successful_calls_unchanged():
    class Cad:
        async def call(self, name, arguments):
            return {"ok": True, "value": {"feature": "BracketBase"}}

    cad = StopOnCadError(Cad(), lambda: (_ for _ in ()).throw(AssertionError("stop")))
    assert await cad.call("execute_python", {}) == {"ok": True, "value": {"feature": "BracketBase"}}
