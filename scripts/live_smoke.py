"""Explicit live checks; mutations are confined to a newly created test part."""

import argparse
import asyncio
import base64
import json
import math
import tempfile
from pathlib import Path

from cadnext.mcp import SolidWorksMcp


class SmokeFailure(RuntimeError):
    pass


async def smoke(mcp, folder: Path, *, mutate=False):
    report = {'status': 'failed', 'mode': 'new-part' if mutate else 'read-only', 'steps': []}

    async def call(tool_name, **arguments):
        result = await mcp.call(tool_name, arguments)
        report['steps'].append({'tool': tool_name, 'ok': result['ok'],
                                'uncertain': result.get('uncertain', False)})
        if not result['ok']:
            raise SmokeFailure(result.get('error') or 'Ошибка CAD-инструмента.')
        return result

    async def value(tool_name, **arguments):
        return (await call(tool_name, **arguments))['value']

    async def active(expected):
        connected = await value('connect_solidworks')
        if connected.get('active_document') != expected:
            raise SmokeFailure('Активный документ изменён. Проверка остановлена без следующих изменений.')

    async def picture():
        result = await call('screenshot', width=800, height=600, zoom_to_fit=False)
        if not result.get('images'):
            raise SmokeFailure('Инструмент не вернул изображение.')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'view.png').write_bytes(base64.b64decode(result['images'][0]['data'], validate=True))
        report['screenshot'] = 'view.png'

    try:
        connected = await value('connect_solidworks')
        report['revision'] = connected.get('revision')
        before = (await value('list_open_documents'))['documents']
        if not mutate:
            report['document_count'] = len(before)
            if connected.get('active_document'):
                await value('get_feature_tree', limit=1000)
                await picture()
            else:
                report['limitation'] = 'Нет активного документа: дерево и снимок не проверены.'
        else:
            # Use a fresh folder and never overwrite a previous smoke result.
            folder.mkdir(parents=True, exist_ok=True)
            part, dxf = folder / 'smoke.SLDPRT', folder / 'smoke.dxf'
            if part.exists() or dxf.exists():
                raise SmokeFailure('Тестовые файлы уже существуют; выберите новый каталог.')
            created = (await value('create_part'))['created']
            if created in {doc['title'] for doc in before}:
                raise SmokeFailure('Создание отдельного тестового документа не подтверждено.')
            report['created_document'] = created
            await active(created)
            tree = await value('get_feature_tree', limit=1000)
            planes = [item['name'] for item in tree['features'] if item['type'] == 'RefPlane']
            if not planes:
                raise SmokeFailure('Не найдена опорная плоскость в новом документе.')
            await active(created)
            await value('create_sketch', plane_name=planes[0])
            await active(created)
            await value('sketch_line', x1_mm=0, y1_mm=0, x2_mm=60, y2_mm=0)
            await active(created)
            await value('sketch_line', x1_mm=60, y1_mm=0, x2_mm=60, y2_mm=40)
            await active(created)
            sketch = (await value('close_sketch'))['closed']
            await active(created)
            await value('create_base_flange', thickness_mm=2, bend_radius_mm=1,
                        depth_mm=50, sketch_name=sketch)
            await active(created)
            sheet = await value('get_sheet_metal_info')
            if not math.isclose(sheet['thickness_mm'], 2, abs_tol=0.0001):
                raise SmokeFailure('Толщина новой детали не соответствует 2 мм.')
            built = await value('get_feature_tree', limit=1000)
            flanges = [item['name'] for item in built['features'] if item['type'] == 'SMBaseFlange']
            if len(flanges) != 1:
                raise SmokeFailure('Базовая кромка новой детали не идентифицирована.')
            # SW2025 exposes extrusion depth as D2 on the newly created base flange.
            # Require its known initial value before writing; never try another dimension.
            dimension = 'D2@' + flanges[0]
            original = await value('get_dimension', name=dimension)
            if original.get('unit') != 'mm' or not math.isclose(original['value'], 50, abs_tol=0.0001):
                raise SmokeFailure('Размер глубины не идентифицирован; изменение не выполнялось.')
            await active(created)
            await value('set_dimension', name=dimension, value_mm=62)
            await active(created)
            readback = await value('get_dimension', name=dimension)
            independent = await value('get_sheet_metal_info')
            if not (math.isclose(readback['value'], 62, abs_tol=0.0001) and
                    math.isclose(independent['thickness_mm'], 2, abs_tol=0.0001)):
                raise SmokeFailure('Глубина 62 мм и толщина 2 мм не подтверждены.')
            report['thickness_mm'] = independent['thickness_mm']
            report['dimension'] = dimension
            report['depth_mm'] = readback['value']
            await active(created)
            await picture()
            await value('get_feature_tree', limit=1000)
            await active(created)
            await value('save_document', save_as_path=str(part.resolve()))
            if not part.is_file() or not part.stat().st_size:
                raise SmokeFailure('Сохранённый SLDPRT не найден или пуст.')
            documents = (await value('list_open_documents'))['documents']
            saved = [doc for doc in documents if doc.get('path') and
                     Path(doc['path']).resolve() == part.resolve()]
            if len(saved) != 1:
                raise SmokeFailure('Сохранённый тестовый документ не идентифицирован.')
            await active(saved[0]['title'])
            await value('export_flat_pattern_dxf', output_path=str(dxf.resolve()))
            if not dxf.is_file() or not dxf.stat().st_size:
                raise SmokeFailure('Экспортированный DXF не найден или пуст.')
            report['files'] = {path.name: path.stat().st_size for path in (part, dxf)}
            report['limitation'] = 'Тестовая деталь оставлена открытой для осмотра; размеры контуров DXF требуют проверки.'
        report['status'] = 'passed'
    except SmokeFailure as error:
        report['status'] = 'unavailable' if len(report['steps']) == 1 else 'failed'
        report['error'] = str(error)
    return report


async def main():
    parser = argparse.ArgumentParser(description='Живая проверка уже запущенного SolidWorks.')
    parser.add_argument('--mutate-new-part', action='store_true',
                        help='Создать отдельный кронштейн, изменить глубину 50→62 мм, сохранить SLDPRT/DXF.')
    parser.add_argument('--output', type=Path, help='Новый каталог результатов (по умолчанию временный).')
    args = parser.parse_args()
    folder = args.output or Path(tempfile.mkdtemp(prefix='cadnext-live-'))
    async with SolidWorksMcp() as mcp:
        report = await smoke(mcp, folder, mutate=args.mutate_new_part)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'каталог': str(folder), **report}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
