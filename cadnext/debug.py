"""Opt-in, bounded local diagnostics, independent of conversation lifetime."""

import io
import json
import re
import time
import traceback
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import RLock
from urllib.parse import quote
from uuid import uuid4

_SCOPE = ContextVar('cadnext_debug_scope', default={})


@contextmanager
def debug_scope(**fields):
    token = _SCOPE.set({**_SCOPE.get(), **fields})
    try:
        yield
    finally:
        _SCOPE.reset(token)


class DebugLog:
    def __init__(self, settings, *, max_bytes=5 * 1024 * 1024, backups=3):
        self.settings = settings
        self.path = settings.path.parent / 'logs' / 'debug.jsonl'
        self.max_bytes = max_bytes
        self.backups = backups
        self.error = None
        self._secrets = set()
        self._lock = RLock()

    def register_secret(self, value):
        if isinstance(value, str) and value:
            with self._lock:
                self._secrets.update({value, json.dumps(value, ensure_ascii=False)[1:-1],
                                      json.dumps(value, ensure_ascii=True)[1:-1], quote(value, safe='')})

    def prepare(self):
        """Check writability before persisting an enabled setting."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8'):
                pass
            self.error = None

    def _clean(self, value, key='', depth=0):
        if key.casefold() in {'api_key', 'authorization', 'proxy-authorization', 'cookie',
                              'set-cookie', 'password', 'secret', 'access_token', 'refresh_token'}:
            return '[REDACTED]'
        if depth > 18:
            return '[TRUNCATED: depth]'
        if isinstance(value, dict):
            image = 'mimeType' in value and 'data' in value
            return {self._clean(str(k), depth=depth + 1): ('[OMITTED: image]' if image and k == 'data'
                            else self._clean(v, str(k), depth + 1)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            items = [self._clean(v, depth=depth + 1) for v in value[:200]]
            return items + ([f'[TRUNCATED: {len(value) - 200} items]'] if len(value) > 200 else [])
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
            value = re.sub(r'data:image/[^\s"\']+', '[OMITTED: image]', value, flags=re.I)
            value = re.sub(r'(?i)\bBearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
            return value[:32768] + ('[TRUNCATED]' if len(value) > 32768 else '')
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f'<{type(value).__name__}>'

    def _paths(self):
        return [self.path, *(self.path.with_name(f'debug.{i}.jsonl')
                             for i in range(1, self.backups + 1))]

    def _append(self, line):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line.encode('utf-8')) > self.max_bytes:
            paths = self._paths()
            paths[-1].unlink(missing_ok=True)
            for source, destination in reversed(list(zip(paths, paths[1:]))):
                if source.exists():
                    source.replace(destination)
        with self.path.open('a', encoding='utf-8', newline='\n') as output:
            output.write(line)

    def write(self, event, **data):
        if not self.settings.debug_logging_enabled:
            return
        try:
            with self._lock:
                if not self.settings.debug_logging_enabled:
                    return
                record = {'at': datetime.now(UTC).isoformat(),
                          'event': event, **self._clean({**_SCOPE.get(), **data})}
                line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
                if len(line.encode('utf-8')) > 256 * 1024:
                    record = {'at': record['at'], 'event': event,
                              'run_id': record.get('run_id'), 'session_id': record.get('session_id'),
                              'truncated': True, 'preview': line[:32768]}
                    line = json.dumps(record, ensure_ascii=False) + '\n'
                self._append(line)
                self.error = None
        except Exception as error:
            # Diagnostics must never change CAD execution, retries, or its outcome.
            self.error = type(error).__name__

    def exception(self, event, error, **data):
        if not self.settings.debug_logging_enabled:
            return
        frames = [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        self.write(event, **data, exception={'type': type(error).__name__,
                                            'message': str(error), 'frames': frames})

    def archive(self):
        with self._lock:
            paths = [path for path in self._paths() if path.is_file()]
            if not paths:
                raise FileNotFoundError
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in paths:
                    archive.write(path, path.name)
            return output.getvalue()


class DebugMcp:
    """Trace the existing transport, including saves and idle document inspections."""

    def __init__(self, mcp, debug):
        self.mcp = mcp
        self.debug = debug

    @property
    def uncertain(self):
        return bool(getattr(self.mcp, 'uncertain', False))

    async def tools(self):
        return await self.mcp.tools()

    async def call(self, name, arguments):
        call_id = uuid4().hex
        started = time.perf_counter()
        self.debug.write('mcp_start', call_id=call_id, name=name, arguments=arguments)
        try:
            result = await self.mcp.call(name, arguments)
        except Exception as error:
            self.debug.exception('mcp_exception', error, call_id=call_id, name=name)
            raise
        self.debug.write('mcp_result', call_id=call_id, name=name, result=result,
                         duration_seconds=time.perf_counter() - started)
        return result
