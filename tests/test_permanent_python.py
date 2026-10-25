import json

import pytest

from cadnext.settings import SettingsStore
from tests.fake_mcp import FakeMcp, ok
from tests.test_agent import ScriptedProvider, pending, reply, settle
from tests.test_settings import MemorySecrets


def test_python_permission_survives_restart_and_profile_changes(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'version': 1, 'profiles': []}))
    store = SettingsStore(tmp_path, MemorySecrets())
    assert store.python_execution_allowed is False
    store.set_python_execution_allowed(True)
    profile = store.save({'name': 'P', 'base_url': 'http://localhost/v1', 'model': 'm'})
    store.delete(profile.id)
    restored = SettingsStore(tmp_path, MemorySecrets())
    assert restored.python_execution_allowed is True
    restored.set_python_execution_allowed(False)
    assert SettingsStore(tmp_path, MemorySecrets()).python_execution_allowed is False


def test_permission_write_failure_preserves_disk_and_memory(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path, MemorySecrets())
    store.set_python_execution_allowed(False)
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError('write failed')

    monkeypatch.setattr('cadnext.settings.os.replace', fail)
    with pytest.raises(OSError):
        store.set_python_execution_allowed(True)
    assert store.python_execution_allowed is False
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('value', ['true', 1, None])
def test_permission_requires_boolean(tmp_path, value):
    store = SettingsStore(tmp_path, MemorySecrets())
    with pytest.raises(ValueError):
        store.set_python_execution_allowed(value)


async def test_python_confirmation_is_permanent_and_revocable(tmp_path):
    from cadnext.agent import Agent

    store = SettingsStore(tmp_path, MemorySecrets())
    mcp = FakeMcp([('execute_python', ok({'result': '1'}))])
    agent = Agent(mcp, settings=store)
    model = ScriptedProvider([reply(('a', 'execute_python', {'code': 'result=1'})), reply(content='Готово')])
    run = agent.start(session_id='first', provider=model, message='Код')
    question = await pending(run)
    agent.confirm(run.id, question['id'], True)
    await settle(agent, run)
    assert run.status == 'completed'
    assert SettingsStore(tmp_path, MemorySecrets()).python_execution_allowed is True
    assert 'Разрешение Python ещё не выдано' in model.requests[0][0]['content']
    assert 'Постоянное разрешение Python уже включено' in model.requests[1][0]['content']
    await agent.aclose()

    restored = SettingsStore(tmp_path, MemorySecrets())
    mcp = FakeMcp([('execute_python', ok({'result': '2'}))])
    agent = Agent(mcp, settings=restored)
    model = ScriptedProvider([reply(('b', 'execute_python', {'code': 'result=2'})), reply(content='Готово')])
    run = agent.start(session_id='different', provider=model, message='Новый диалог и подключение')
    await settle(agent, run)
    assert run.status == 'completed'
    assert not any(e['type'] == 'confirmation' for e in run.events)
    restored.set_python_execution_allowed(False)
    model = ScriptedProvider([reply(('c', 'execute_python', {'code': 'result=3'}))])
    run = agent.start(session_id='different', provider=model, message='После отзыва')
    question = await pending(run)
    agent.confirm(run.id, question['id'], False)
    await settle(agent, run)
    assert run.status == 'rejected'
    assert len(mcp.calls) == 1
    assert not restored.python_execution_allowed


async def test_failed_permission_save_does_not_execute_python(tmp_path, monkeypatch):
    from cadnext.agent import Agent

    store = SettingsStore(tmp_path, MemorySecrets())
    mcp = FakeMcp([])
    agent = Agent(mcp, settings=store)
    model = ScriptedProvider([reply(('a', 'execute_python', {'code': 'result=1'}))])
    run = agent.start(session_id='s', provider=model, message='Код')
    question = await pending(run)

    def fail(*args):
        raise OSError('write failed')

    monkeypatch.setattr('cadnext.settings.os.replace', fail)
    agent.confirm(run.id, question['id'], True)
    await settle(agent, run)
    assert run.status == 'failed'
    assert not mcp.calls
    assert not store.python_execution_allowed


@pytest.mark.parametrize('allowed', [False, True])
async def test_model_receives_actual_permission_without_textual_approval_requests(tmp_path, allowed):
    from cadnext.agent import Agent

    store = SettingsStore(tmp_path, MemorySecrets())
    store.set_python_execution_allowed(allowed)
    agent = Agent(FakeMcp([]), settings=store)
    # Old history must not reintroduce obsolete approval instructions.
    agent._history['s'] = [{'role': 'system', 'content': 'Старая инструкция: спроси разрешение'},
                           {'role': 'user', 'content': 'Начни'},
                           {'role': 'assistant', 'content': 'Уточни разрешение'}]
    model = ScriptedProvider([reply(content='Продолжаю')])
    run = agent.start(session_id='s', provider=model, message='Продолжи')
    await settle(agent, run)
    prompt = model.requests[0][0]['content']
    expected = ('Постоянное разрешение Python уже включено' if allowed
                else 'Разрешение Python ещё не выдано')
    assert expected in prompt
    assert 'Не проси разрешение на Python обычным сообщением' in prompt
    assert 'Старая инструкция' not in prompt
    assert 'Python-код требует подтверждения один раз для диалога' not in prompt
