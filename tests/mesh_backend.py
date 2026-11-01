"""Explicit CAD/LLM test doubles for mesh integration, never imported by production."""

import ast
import hashlib
import json

import httpx

from cadnext.provider import Provider
from tests.fake_mcp import ok, tool
from tests.mesh_fixtures import make_plate_with_hole, make_pocket_bracket, make_stepped_bushing
from tests.web_fixture import Backend

FIXTURES = {'plate': make_plate_with_hole, 'bushing': make_stepped_bushing, 'bracket': make_pocket_bracket}
DIMENSIONS = {'plate': {'Width@Profile': 40, 'Height@Profile': 30, 'Thickness@Boss': 5, 'Diameter@Hole': 10},
              'bushing': {'Height@Profile': 40, 'Flange@Profile': 30, 'Stem@Profile': 20, 'Bore@Profile': 10},
              'bracket': {'Width@Profile': 40, 'Height@Profile': 25, 'Wall@Boss': 5, 'Depth@Pocket': 2}}


class MeshBackend(Backend):
    def __init__(self, directory, **kwargs):
        super().__init__(directory, **kwargs)
        self.native_features = []
        self.native_dimensions = {}
        self.shape = None
        self.revision = 0

    async def tools(self):
        catalog = await super().tools()
        if self.profiles and self.profiles[-1].model == 'mesh-fixture':
            catalog += [tool('open_python_session'), tool('close_python_session'), tool('export_verification_mesh')]
        return catalog

    async def call(self, name, arguments):
        if not self.profiles or self.profiles[-1].model != 'mesh-fixture':
            return await super().call(name, arguments)
        self.calls.append((name, arguments))
        if name == 'open_python_session':
            return ok({'session_id': 'fixture-python'})
        if name == 'close_python_session':
            return ok({})
        if name == 'connect_solidworks':
            return ok({'active_document': self.active_document, 'document_id': 'fixture-document',
                       'document_update_stamp': self.revision})
        if name == 'execute_python':
            assignment = ast.parse(arguments['code']).body[0]
            command = ast.literal_eval(assignment.value)
            self.shape = command['shape']
            if command['stage'] == 'profile':
                self.native_features = [{'name': 'Profile', 'type': 'ProfileFeature', 'suppressed': False, 'error_code': 0}]
                self.native_dimensions = DIMENSIONS[self.shape].copy()
            elif command['stage'] == 'body':
                self.native_features += [{'name': 'Body', 'type': 'Revolution' if self.shape == 'bushing' else 'Boss',
                                          'suppressed': False, 'error_code': 0}]
                if self.shape != 'bushing':
                    self.native_features += [{'name': 'Pocket' if self.shape == 'bracket' else 'Hole',
                                              'type': 'Cut', 'suppressed': False, 'error_code': 0}]
            self.revision += 1
            return ok({'result': 'fixture operations applied'})
        if name == 'inspect_cad':
            return ok({'features': self.native_features, 'truncated': False,
                       'bodies': [{'name': 'Solid', 'type': 'solid'}] if len(self.native_features) > 1 else [],
                       'dimensions': [{'name': key, 'value': value, 'unit': 'mm', 'driving': True}
                                      for key, value in self.native_dimensions.items()]})
        if name == 'export_verification_mesh':
            import trimesh

            fixture = FIXTURES[self.shape]()
            # A different triangulation prevents exact face identity from masquerading as comparison.
            vertices, faces = trimesh.remesh.subdivide(fixture.vertices, fixture.faces)
            raw = json.dumps({'vertices_mm': vertices.tolist(), 'faces': faces.tolist()}).encode()
            path = self.settings.path.parent / 'fixture-cad.json'
            path.write_bytes(raw)
            return ok({'snapshot': {'snapshot_id': 'fixture-snapshot', 'path': str(path),
                       'sha256': hashlib.sha256(raw).hexdigest(), 'document_identity': 'fixture-document',
                       'revision': self.revision, 'precision_known': False, 'chord_tolerance_mm': .01}})
        self.calls.pop()
        return await super().call(name, arguments)

    def provider(self, profile, secret=None, *, privacy):
        if profile.model != 'mesh-fixture':
            return super().provider(profile, secret, privacy=privacy)
        self.profiles.append(profile)

        def respond(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            messages = payload['messages']
            original = next(message['content'] for message in reversed(messages)
                            if message['role'] == 'user' and isinstance(message.get('content'), str)
                            and message['content'].startswith('mesh-build:'))
            shape = original.split(':')[1]
            latest_user = max(i for i,m in enumerate(messages) if m.get('content') == original)
            count = sum(m['role'] == 'tool' for m in messages[latest_user+1:])
            dimension_checks = [{'kind': 'dimension', 'name': key, 'value': value, 'unit': 'mm'}
                                for key,value in DIMENSIONS[shape].items()]
            sequence = [('mesh_inspect', {}),
                        ('mesh_section', {'frame': {'origin_mm': [0,0,1], 'x_axis': [1,0,0],
                                                   'y_axis': [0,1,0], 'normal': [0,0,1]}}),
                        ('set_construction_plan', {'stages': [
                            {'id': 'profile', 'title': 'Размерный эскиз', 'expected_result': 'Управляющие размеры'},
                            {'id': 'body', 'title': 'Операции тела', 'expected_result': 'Одно тело и вырезы'}]}),
                        ('execute_python', {'code': f"result = {{'shape': '{shape}', 'stage': 'profile'}}",
                                            'stage_id': 'profile', 'checks': [
                                                {'kind': 'feature', 'name': 'Profile', 'type': 'ProfileFeature'},
                                                *dimension_checks]}),
                        ('execute_python', {'code': f"result = {{'shape': '{shape}', 'stage': 'body'}}",
                                            'stage_id': 'body', 'checks': [{'kind': 'body_count', 'expected': 1}]}),
                        ('mesh_compare', {})]
            if count < len(sequence):
                name, arguments = sequence[count]
                return self.tool_response(name, arguments)
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
                'content': 'Этапы и размеры проверены. Точность CAD-экспорта пока не подтверждена.'},
                'finish_reason': 'stop'}]})
        return Provider(profile, secret, privacy=privacy, transport=httpx.MockTransport(respond))
