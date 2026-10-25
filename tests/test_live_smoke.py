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


async def test_new_part_smoke_passes_named_dimension_argument(tmp_path):
    from scripts.live_smoke import smoke

    class Cad:
        created = False
        saved = False
        depth = 50

        async def call(self, name, arguments):
            if name == 'connect_solidworks':
                value = {'active_document': 'Test' if self.created else None}
            elif name == 'list_open_documents':
                value = {'documents': [{'title': 'Test', 'path': str(tmp_path / 'smoke.SLDPRT')}]
                         if self.saved else []}
            elif name == 'create_part':
                self.created = True
                value = {'created': 'Test'}
            elif name == 'get_feature_tree':
                value = {'features': [{'name': 'Front', 'type': 'RefPlane'},
                                      {'name': 'BaseFlange', 'type': 'SMBaseFlange'}]}
            elif name == 'close_sketch':
                value = {'closed': 'Sketch'}
            elif name == 'get_sheet_metal_info':
                value = {'thickness_mm': 2, 'feature': 'SheetMetal'}
            elif name == 'get_dimension':
                assert arguments == {'name': 'D2@BaseFlange'}
                value = {'unit': 'mm', 'value': self.depth}
            elif name == 'set_dimension':
                assert arguments['name'] == 'D2@BaseFlange'
                self.depth = arguments['value_mm']
                value = {}
            elif name == 'save_document':
                self.saved = True
                (tmp_path / 'smoke.SLDPRT').write_bytes(b'part')
                value = {}
            elif name == 'export_flat_pattern_dxf':
                (tmp_path / 'smoke.dxf').write_bytes(b'dxf')
                value = {}
            else:
                value = {}
            return {'ok': True, 'value': value, 'images': [{'data': 'AAAA'}]}

    result = await smoke(Cad(), tmp_path, mutate=True)
    assert result['status'] == 'passed'
    assert result['thickness_mm'] == 2
    assert result['depth_mm'] == 62


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
