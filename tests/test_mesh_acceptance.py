"""Fixture acceptance exercises real API/agent/worker, with explicit CAD/LLM doubles."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.mesh_fixtures import make_plate_with_hole, make_stepped_bushing
from tests.test_app import profile, start
from tests.test_mesh_app import prepared


@pytest.mark.parametrize("format", ["stl", "obj"])
def test_three_analytic_references_keep_nominal_dimensions(tmp_path, format):
    from cadnext.mesh.analysis import inspect_mesh
    from cadnext.mesh.loader import load_mesh
    from tests.mesh_fixtures import encode_fixture, make_pocket_bracket

    for fixture, extents, volume in [(make_plate_with_hole(), [40,30,5], None),
                                    (make_stepped_bushing(), [30,30,40], None),
                                    (make_pocket_bracket(), [40,30,25], 8200)]:
        source = tmp_path / f'reference.{format}'
        source.write_bytes(encode_fixture(fixture, format))
        geometry = load_mesh(source, format=format, units='mm')
        summary = inspect_mesh(geometry)
        assert summary.watertight and summary.winding_consistent
        np.testing.assert_allclose(np.ptp(geometry.vertices, axis=0), extents, atol=1e-5)
        if volume is not None:
            assert summary.volume_mm3 == pytest.approx(volume)


@pytest.mark.parametrize("shape", ["plate", "bushing", "bracket"])
def test_api_constructs_stages_compares_and_transfers_source(tmp_path, shape):
    from cadnext.app import create_app
    from tests.mesh_backend import MeshBackend
    from tests.mesh_fixtures import encode_fixture, make_pocket_bracket

    fixtures = {'plate': make_plate_with_hole, 'bushing': make_stepped_bushing, 'bracket': make_pocket_bracket}
    raw = encode_fixture(fixtures[shape](), 'obj')
    backend = MeshBackend(tmp_path)
    backend.settings.set_python_execution_allowed(True)
    app = create_app(settings=backend.settings, mcp_factory=backend.mcp, provider_factory=backend.provider)
    with TestClient(app, base_url='http://127.0.0.1:8876') as client:
        saved = profile(client, model='mesh-fixture')
        assert client.post('/api/projects/session-1/mesh?format=obj&units=mm', content=raw).status_code == 202
        source = prepared(client)
        response = start(client, saved['id'], f'mesh-build:{shape}', mesh_reference={
            'asset_id': source['asset_id'], 'tolerance_mm': .1})
        assert response.status_code == 202, response.text
        async def finish():
            await asyncio.wait_for(app.state.agent.wait(response.json()['id']), 65)
        client.portal.call(finish)
        run = client.get(f'/api/runs/{response.json()["id"]}').json()
        assert run['status'] == 'completed', run['messages'][-1:]
        assert run['call_count'] < 80
        assert all(stage['status'] == 'done' and all(check['passed'] for check in stage['checks'])
                   for stage in run['construction_plan'])
        comparison = run['mesh_verification']['comparison']
        assert comparison['reference_to_cad']['samples'] == 50000
        assert comparison['cad_to_reference']['sampled_max_mm'] < .01
        # This double deliberately reports unknown export precision, just like production.
        assert run['mesh_verification']['status'] == 'unknown' and run['verification'] == 'unverified'
        tree = backend.native_features
        assert any(item['type'] == 'ProfileFeature' for item in tree)
        assert any(item['type'] in {'Boss', 'Revolution'} for item in tree)
        assert backend.native_dimensions and not any(item['type'] == 'Imported' for item in tree)
        assert len([name for name, _ in backend.calls if name == 'inspect_cad']) >= 2
        result = client.post('/api/cad/save', json={'session_id': 'session-1'}).json()
        sidecar = json.loads(Path(result['conversation']).read_text(encoding='utf-8'))
        assert sidecar['version'] == 2 and sidecar['runs'][-1]['mesh_evidence']
        staged = client.post('/api/conversation-imports', json={'mesh': sidecar['mesh']}).json()
        assert client.put(f'/api/conversation-imports/{staged["import_id"]}/{source["sha256"]}',
                          content=raw).status_code == 204
        opened = client.post('/api/conversations/open', json={'conversation': sidecar,
                             'mesh_import_id': staged['import_id']})
        assert opened.status_code == 200 and not opened.json()['mesh_missing']


def test_incomplete_scan_remains_unknown_and_missing_hole_fails(tmp_path):
    from cadnext.mesh.compare import compare_surfaces
    from tests.test_mesh_analysis import mesh_data
    from tests.test_mesh_compare import box, options

    fixture = make_plate_with_hole()
    full = mesh_data(fixture.vertices, fixture.faces)
    opened = mesh_data(fixture.vertices, fixture.faces[:-2])
    assert compare_surfaces(opened, full, options=options()).status == 'unknown'
    assert compare_surfaces(full, box(), options=options()).status == 'fail'


def test_smoke_defaults_to_numeric_read_only_and_never_calls_cad(tmp_path):
    from scripts.mesh_smoke import main
    from tests.mesh_fixtures import encode_fixture

    source = tmp_path / 'plate.obj'
    source.write_bytes(encode_fixture(make_plate_with_hole(), 'obj'))
    output = tmp_path / 'report.json'
    assert main([str(source), '--units', 'mm', '--output', str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['mode'] == 'numeric-read-only' and report['cad_checked'] is False
    assert report['summary']['watertight']
