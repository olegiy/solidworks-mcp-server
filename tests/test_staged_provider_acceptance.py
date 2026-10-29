from scripts.staged_provider_acceptance import StopOnCadError


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
