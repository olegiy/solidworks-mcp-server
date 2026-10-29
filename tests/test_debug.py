import json

import pytest

from cadnext.settings import SettingsStore
from tests.test_settings import MemorySecrets


def test_debug_setting_is_persistent_atomic_and_preserved_by_other_settings(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path, MemorySecrets())
    assert store.debug_logging_enabled is False
    store.set_debug_logging_enabled(True)
    store.set_python_execution_allowed(True)
    store.save({'name': 'P', 'base_url': 'http://localhost/v1', 'model': 'm'})
    assert SettingsStore(tmp_path, MemorySecrets()).debug_logging_enabled is True
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError('Disk unavailable')

    monkeypatch.setattr('cadnext.settings.os.replace', fail)
    with pytest.raises(OSError):
        store.set_debug_logging_enabled(False)
    assert store.debug_logging_enabled is True
    assert store.path.read_bytes() == before


def test_debug_log_is_opt_in_redacts_secrets_and_images_and_stops_immediately(tmp_path):
    from cadnext.debug import DebugLog

    store = SettingsStore(tmp_path, MemorySecrets())
    log = DebugLog(store)
    log.write('disabled', payload='ignored')
    assert not log.path.exists()
    log.prepare()
    store.set_debug_logging_enabled(True)
    log.register_secret('private-test-value')
    try:
        raise AttributeError('private-test-value: SolidWorks member missing')
    except AttributeError as error:
        log.exception('cad_exception', error, run_id='r1')
    log.write('payload', payload={'api_key': 'hidden', 'Authorization': 'Bearer hidden',
        'code': 'result = 62 # private-test-value',
        'image': {'mimeType': 'image/png', 'data': 'c2VjcmV0LWltYWdl'},
        'url': 'data:image/png;base64,c2VjcmV0LWltYWdl'})
    text = log.path.read_text(encoding='utf-8')
    assert all(secret not in text for secret in ['private-test-value', 'hidden', 'c2VjcmV0LWltYWdl'])
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]['exception']['type'] == 'AttributeError'
    assert records[0]['exception']['frames'][-1]['function'].startswith('test_debug_log')
    assert records[0]['run_id'] == 'r1'
    assert 'result = 62' in text and '[REDACTED]' in text
    store.set_debug_logging_enabled(False)
    before = log.path.read_bytes()
    log.write('after_disable', payload='ignored')
    assert log.path.read_bytes() == before


def test_debug_rotation_preserves_valid_json_and_write_failure_does_not_break_cad(tmp_path, monkeypatch):
    from cadnext.debug import DebugLog

    store = SettingsStore(tmp_path, MemorySecrets())
    store.set_debug_logging_enabled(True)
    log = DebugLog(store, max_bytes=600, backups=2)
    for i in range(20):
        log.write('entry', index=i, value='x' * 160)
    paths = list(log.path.parent.glob('debug*.jsonl'))
    assert 1 < len(paths) <= 3
    records = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
    assert max(record['index'] for record in records) == 19
    monkeypatch.setattr(log, '_append', lambda _: (_ for _ in ()).throw(OSError('Full disk')))
    log.write('cad_result', value=62)
    assert log.error is not None


def test_cad_error_contains_frame_locations_without_source_or_locals(monkeypatch):
    import solidworks_mcp_server as server

    monkeypatch.setattr(server, 'com_call', lambda job: job())

    def failing_cad():
        local_secret = 'not-in-diagnostics'
        assert local_secret
        raise AttributeError('Missing member')

    result = json.loads(server._run(failing_cad))
    assert result['error_type'] == 'AttributeError'
    assert result['diagnostic_frames'][-1]['function'] == 'failing_cad'
    assert 'not-in-diagnostics' not in json.dumps(result)


def test_raw_provider_error_redacts_json_escaped_secrets(tmp_path):
    from cadnext.debug import DebugLog

    store = SettingsStore(tmp_path, MemorySecrets())
    store.set_debug_logging_enabled(True)
    log = DebugLog(store)
    secret = 'private"key\\value'
    log.register_secret(secret)
    log.write('provider_http_error', body=json.dumps({'error': secret}))
    record = json.loads(log.path.read_text())
    assert secret not in record['body']
    assert json.dumps(secret)[1:-1] not in record['body']
    assert '[REDACTED]' in record['body']


async def test_provider_debug_keeps_error_status_and_trace_without_exposing_key(tmp_path):
    import httpx

    from cadnext.debug import DebugLog, debug_scope
    from cadnext.provider import Provider, ProviderError
    from cadnext.settings import Profile

    store = SettingsStore(tmp_path, MemorySecrets())
    store.set_debug_logging_enabled(True)
    log = DebugLog(store)
    secret = 'private-provider-test-key'
    log.register_secret(secret)
    profile = Profile(name='P', base_url='http://localhost/v1', model='m')
    provider = Provider(profile, secret, transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={'error': f'unsupported tools: {secret}'})))
    provider.debug = log
    try:
        with debug_scope(run_id='r-error', session_id='s-error'):
            with pytest.raises(ProviderError):
                await provider.complete([{'role': 'user', 'content': 'Read size'}], [])
    finally:
        await provider.aclose()
    text = log.path.read_text()
    assert secret not in text
    records = [json.loads(line) for line in text.splitlines()]
    error = next(r for r in records if r['event'] == 'provider_http_error')
    assert error['status'] == 400 and error['run_id'] == 'r-error'
    assert 'unsupported tools' in error['body']
    assert any(r['event'] == 'model_exception' and r['exception']['frames'] for r in records)
