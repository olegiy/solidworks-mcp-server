"""Numeric mesh smoke test: no SolidWorks connection or external provider."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from cadnext.mesh.contracts import MeshError, MeshLimits
from cadnext.mesh.worker_client import MeshWorkerClient


async def inspect_source(path, units):
    if path.stat().st_size > MeshLimits().max_bytes:
        raise MeshError('byte_limit', 'Файл сетки превышает 100 МиБ.')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    worker = MeshWorkerClient()
    try:
        result = await worker.request('inspect', {'path': str(path.absolute()),
            'format': path.suffix[1:].lower(), 'units': units, 'sha256': digest,
            'frame_id': 'source-mm'}, stop=asyncio.Event())
        return {'mode': 'numeric-read-only', 'cad_checked': False, 'summary': result['summary'],
                'limitation': 'Проверены только данные сетки. SolidWorks и внешний LLM не запускались.'}
    finally:
        await worker.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Численная проверка STL/OBJ без изменения CAD.')
    parser.add_argument('source', type=Path, help='Файл STL или треугольный OBJ')
    parser.add_argument('--units', choices=['mm', 'in'], required=True, help='Единицы исходника')
    parser.add_argument('--output', type=Path, required=True, help='Локальный JSON-отчёт')
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(inspect_source(args.source, args.units))
        code = 0
    except (MeshError, OSError) as error:
        report, code = {'status': 'error', 'message': str(error)}, 1
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
