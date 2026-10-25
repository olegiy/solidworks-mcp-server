import pytest


async def test_unavailable_solidworks_stops_before_any_mutation(tmp_path):
    from scripts.live_smoke import smoke

    class Missing:
        calls = []

        async def call(self, name, arguments):
            self.calls.append(name)
            return {'ok': False, 'error': 'SolidWorks не запущен', 'uncertain': False}

    mcp = Missing()
    report = await smoke(mcp, tmp_path, mutate=True)
    assert report['status'] == 'unavailable'
    assert mcp.calls == ['connect_solidworks']


async def test_failed_creation_is_not_retried_or_followed_by_mutation(tmp_path):
    from scripts.live_smoke import smoke

    class Failed:
        calls = []

        async def call(self, name, arguments):
            self.calls.append(name)
            if name == 'create_part':
                return {'ok': False, 'error': 'Ошибка создания', 'uncertain': True}
            value = {'active_document': None} if name == 'connect_solidworks' else {'documents': []}
            return {'ok': True, 'value': value, 'images': []}

    mcp = Failed()
    report = await smoke(mcp, tmp_path, mutate=True)
    assert report['status'] == 'failed'
    assert mcp.calls == ['connect_solidworks', 'list_open_documents', 'create_part']


@pytest.mark.live
async def test_live_read_only(tmp_path):
    import os

    if os.environ.get('CADNEXT_LIVE') != '1':
        pytest.skip('Живая проверка включается явно: CADNEXT_LIVE=1.')
    from cadnext.mcp import SolidWorksMcp
    from scripts.live_smoke import smoke

    async with SolidWorksMcp() as mcp:
        report = await smoke(mcp, tmp_path)
    assert report['status'] == 'passed', report
