"""Rejected local requests return to the model before any dependent CAD call."""

import json
from copy import deepcopy

import pytest

from cadnext.agent import Agent
from cadnext.evaluation import EvaluationOptions
from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, reply
from tests.test_goal_loop import define, requirement, response
from tests.test_mesh_tools import ready_tools
from tests.test_staged_agent import STAGES, staged_mcp, trusted_agent


def tool_result(messages, call_id):
    return json.loads(next(message['content'] for message in messages
                           if message.get('tool_call_id') == call_id))


@pytest.mark.parametrize('evaluate', [False, True])
async def test_rejected_plan_preserves_completed_work_and_requests_a_corrected_plan(evaluate):
    changed = deepcopy(STAGES)
    changed[0]['title'] = 'Rewritten profile'

    def correct(messages):
        failure = tool_result(messages, 'rewrite')
        assert failure['error_code'] == 'invalid_construction_plan'
        assert failure['recoverable'] and failure['changes_started'] is False
        assert failure['current_plan'][0]['title'] == STAGES[0]['title']
        assert failure['current_plan'][0]['status'] == 'done'
        assert tool_result(messages, 'queued')['ok'] is False
        assert len(run.construction_plan_history) == 1
        assert [name for name, _ in mcp.calls] == ['execute_python']
        return reply(('correct', 'set_construction_plan', {'stages': STAGES}))

    def finish(messages):
        assert tool_result(messages, 'body')['ok'] is True
        agent.stop(run.id)
        return reply(content='Stopped after checking the repaired sequence')

    answers = [
        reply(('plan', 'set_construction_plan', {'stages': STAGES})),
        reply(('profile', 'execute_python', {'code': 'profile()', 'stage_id': 'profile'})),
        reply(('rewrite', 'set_construction_plan', {'stages': changed}),
              ('queued', 'execute_python', {'code': 'must_not_run()'})),
        correct,
        reply(('body', 'execute_python', {'code': 'body()', 'stage_id': 'body'})),
        finish,
    ]
    if evaluate:
        answers.insert(0, response(define(requirement())))
    provider = ScriptedProvider(answers)
    mcp = staged_mcp([('execute_python', ok({})), ('execute_python', ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id='s', provider=provider, message='Build',
                      evaluation=EvaluationOptions() if evaluate else None)
    try:
        await agent.wait(run.id)
        assert run.status == 'stopped'
        assert [args['code'] for _, args in mcp.calls] == ['profile()', 'body()']
        assert [stage['status'] for stage in run.construction_plan] == ['done', 'done']
        assert len(run.construction_plan_history) == 2
        assert run.call_count == 5 and not agent.uncertain
    finally:
        await agent.aclose()


@pytest.mark.parametrize('evaluate', [False, True])
async def test_missing_mesh_inspection_returns_to_model_before_python_permission(tmp_path, evaluate):
    tools, asset = await ready_tools(tmp_path)
    mcp = FakeMcp()

    def inspect(messages):
        failure = tool_result(messages, 'early')
        assert failure['error_code'] == 'mesh_inspection_required'
        assert failure['recoverable'] and failure['changes_started'] is False
        assert 'mesh_inspect' in failure['recovery_hint']
        assert tool_result(messages, 'queued')['ok'] is False
        assert run.confirmation is None and not mcp.calls
        assert not run.construction_plan and not run.checks and run._mesh_revision == 0
        return reply(('inspect', 'mesh_inspect', {}))

    def finish(messages):
        assert tool_result(messages, 'inspect')['ok'] is True
        assert run._mesh_binding.inspected
        agent.stop(run.id)
        return reply(content='Inspection completed')

    answers = [reply(('early', 'execute_python', {'code': 'must_not_run()'}),
                     ('queued', 'get_dimension', {'name': 'D1'})), inspect, finish]
    if evaluate:
        answers.insert(0, response(define(requirement())))
    provider = ScriptedProvider(answers)
    agent = Agent(mcp, mesh_tools=tools)
    run = agent.start(session_id='s', provider=provider, message='Build',
                      mesh_reference={'asset_id': asset.asset_id, 'tolerance_mm': .1},
                      evaluation=EvaluationOptions() if evaluate else None)
    try:
        await agent.wait(run.id)
        assert run.status == 'stopped'
        assert run.call_count == 2 and not mcp.calls and not agent.uncertain
        assert not any(e['type'] == 'confirmation' for e in run.events)
    finally:
        await agent.aclose()


@pytest.mark.parametrize('evaluate', [False, True])
@pytest.mark.parametrize('requested', ['missing', 'profile'])
async def test_invalid_stage_returns_current_plan_without_repeating_completed_work(evaluate, requested):
    def correct(messages):
        failure = tool_result(messages, 'bad-stage')
        assert failure['error_code'] == 'invalid_construction_stage'
        assert failure['recoverable'] and failure['changes_started'] is False
        assert failure['current_plan'][0]['status'] == 'done'
        assert failure['current_plan'][0]['tool_call_ids'] == ['profile']
        assert tool_result(messages, 'queued')['ok'] is False
        assert run._mesh_revision == 0
        assert set(run.checks) == {'profile'}
        assert [args['code'] for _, args in mcp.calls] == ['profile()']
        return reply(('body', 'execute_python', {'code': 'body()', 'stage_id': 'body'}))

    def finish(messages):
        assert tool_result(messages, 'body')['ok'] is True
        agent.stop(run.id)
        return reply(content='Checked stage correction')

    answers = [reply(('plan', 'set_construction_plan', {'stages': STAGES})),
               reply(('profile', 'execute_python', {'code': 'profile()', 'stage_id': 'profile'})),
               reply(('bad-stage', 'execute_python', {'code': 'never()', 'stage_id': requested}),
                     ('queued', 'execute_python', {'code': 'also_never()'})), correct, finish]
    if evaluate:
        answers.insert(0, response(define(requirement())))
    provider = ScriptedProvider(answers)
    mcp = staged_mcp([('execute_python', ok({})), ('execute_python', ok({}))])
    agent = trusted_agent(mcp, provider)
    run = agent.start(session_id='s', provider=provider, message='Build',
                      evaluation=EvaluationOptions() if evaluate else None)
    try:
        await agent.wait(run.id)
        assert run.status == 'stopped' and run.call_count == 4
        assert [args['code'] for _, args in mcp.calls] == ['profile()', 'body()']
        assert [stage['status'] for stage in run.construction_plan] == ['done', 'done']
        assert not agent.uncertain
    finally:
        await agent.aclose()


@pytest.mark.parametrize('kind', ['plan', 'mesh', 'stage'])
async def test_repeated_local_rejections_exhaust_the_call_budget(tmp_path, kind):
    tools, asset = await ready_tools(tmp_path)
    if kind == 'plan':
        invalid = [STAGES[0], STAGES[0]]
        name, args = 'set_construction_plan', {'stages': invalid}
    elif kind == 'stage':
        name, args = 'execute_python', {'code': 'must_not_run()', 'stage_id': 'missing'}
    else:
        name, args = 'execute_python', {'code': 'must_not_run()'}
    provider = ScriptedProvider([reply((f'bad-{i}', name, args)) for i in range(3)])
    mcp = FakeMcp()
    agent = Agent(mcp, mesh_tools=tools)
    run = agent.start(session_id='s', provider=provider, message='Build', max_calls=2,
                      mesh_reference=({'asset_id': asset.asset_id, 'tolerance_mm': .1}
                                      if kind == 'mesh' else None))
    try:
        await agent.wait(run.id)
        assert run.status == 'limit_reached' and run.call_count == 2
        assert not mcp.calls and run.confirmation is None and not agent.uncertain
    finally:
        await agent.aclose()


@pytest.mark.parametrize('kind', ['plan', 'mesh', 'stage'])
async def test_stop_after_local_rejection_prevents_model_correction(tmp_path, kind):
    tools, asset = await ready_tools(tmp_path)
    mcp = FakeMcp()
    name = 'set_construction_plan' if kind == 'plan' else 'execute_python'
    args = {'stages': [STAGES[0], STAGES[0]]} if kind == 'plan' else {'code': 'never()'}
    if kind == 'stage':
        args['stage_id'] = 'missing'
    provider = ScriptedProvider([reply(('reject', name, args),
                                      ('queued', 'get_dimension', {'name': 'D1'}))])
    agent = Agent(mcp, mesh_tools=tools)
    original_emit = None

    def stop_on_result(event, **data):
        original_emit(event, **data)
        if event == 'tool_result' and data.get('tool_call_id') == 'reject':
            agent.stop(run.id)

    run = agent.start(session_id='s', provider=provider, message='Build',
                      mesh_reference=({'asset_id': asset.asset_id, 'tolerance_mm': .1}
                                      if kind == 'mesh' else None))
    original_emit = run._emit
    run._emit = stop_on_result
    try:
        await agent.wait(run.id)
        assert run.status == 'stopped'
        assert len(provider.requests) == 1 and run.call_count == 1
        assert not mcp.calls and not agent.uncertain
    finally:
        await agent.aclose()
