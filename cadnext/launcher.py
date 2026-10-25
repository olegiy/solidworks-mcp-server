"""Own one background instance using random Windows events, never process killing."""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pywintypes
import win32event

from cadnext.mcp import ROOT

URL = 'http://127.0.0.1:8876'
CHECKOUT = hashlib.sha256(os.path.normcase(str(ROOT.resolve())).encode()).hexdigest()[:20]


def state_path(directory: Path | None = None):
    directory = directory if directory is not None else Path(os.environ['LOCALAPPDATA'])
    return directory / 'cad_agent_next' / 'runtime' / CHECKOUT / 'instance.json'


class Control:
    def __init__(self, instance_id: str, *, create=False):
        self.id = UUID(instance_id).hex
        if self.id != instance_id:
            raise ValueError('Некорректный ID экземпляра.')
        self.stop = self.ready = None
        try:
            for attr in ('stop', 'ready'):
                name = f'Local\\cad_agent_next-{self.id}-{attr}'
                handle = (win32event.CreateEvent(None, True, False, name) if create else
                          win32event.OpenEvent(win32event.EVENT_MODIFY_STATE | 0x00100000,
                                               False, name))
                setattr(self, attr, handle)
        except BaseException:
            self.close()
            raise

    def close(self):
        for attr in ('stop', 'ready'):
            handle = getattr(self, attr)
            if handle is not None:
                handle.Close()
                setattr(self, attr, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


async def serve_controlled(server, app, listener, control):
    async def watch():
        while not server.started:
            await asyncio.sleep(0.05)
        win32event.SetEvent(control.ready)
        while win32event.WaitForSingleObject(control.stop, 0) != win32event.WAIT_OBJECT_0:
            await asyncio.sleep(0.1)
        # Mark the agent closed and wait for the real outcome before stopping HTTP/MCP.
        await app.state.agent.aclose()
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@contextmanager
def launch_lock():
    handle = win32event.CreateMutex(None, False, f'Local\\cad_agent_next-launch-{CHECKOUT}')
    owned = False
    try:
        result = win32event.WaitForSingleObject(handle, 30000)
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise RuntimeError('Другая команда запуска/остановки ещё работает. Повторите позднее.')
        owned = True
        yield
    finally:
        if owned:
            win32event.ReleaseMutex(handle)
        handle.Close()


def read_state():
    path = state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['root'] != str(ROOT) or UUID(data['id']).hex != data['id']:
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Файл экземпляра повреждён. Процессы не изменены.') from None


def open_control(data):
    try:
        return Control(data['id'])
    except pywintypes.error as error:
        if error.winerror == 2:
            return None
        raise


def wait_ready(control, child=None, timeout=45):
    until = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < until:
            if child is not None and child.poll() is not None:
                raise RuntimeError('Приложение не запустилось. Проверьте application.log в каталоге журналов.')
            if win32event.WaitForSingleObject(control.ready, 0) == win32event.WAIT_OBJECT_0:
                try:
                    response = client.get(URL + '/api/health')
                    if response.is_success and response.json().get('ok'):
                        return
                except (httpx.HTTPError, ValueError):
                    pass
            time.sleep(0.1)
    raise RuntimeError('Приложение пока не готово. Повторите start или выполните stop; принудительной остановки нет.')


def start(*, browser=True):
    with launch_lock():
        data = read_state()
        control = open_control(data) if data else None
        if control is not None:
            with control:
                wait_ready(control)
            print('CAD-Agent-Next уже запущен: ' + URL)
        else:
            if not (ROOT / 'ui' / 'dist' / 'index.html').is_file():
                raise RuntimeError('Интерфейс не собран. Сначала выполните setup.bat.')
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind(('127.0.0.1', 8876))
                except OSError:
                    raise RuntimeError('Порт 127.0.0.1:8876 занят. Чужой процесс не изменён; MCP не запущен.') from None
            path = state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            log_dir = Path(os.environ['LOCALAPPDATA']) / 'cad_agent_next' / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            with Control(uuid4().hex, create=True) as control:
                data = {'id': control.id, 'root': str(ROOT)}
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(data), encoding='utf-8')
                temporary.replace(path)
                with (log_dir / 'application.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-m', 'cadnext', '--control-event', control.id],
                        cwd=ROOT, env={**os.environ, 'PYTHONUTF8': '1'}, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    wait_ready(control, child)
                except RuntimeError:
                    if child.poll() is not None:
                        path.unlink(missing_ok=True)
                    raise
            print('CAD-Agent-Next запущен: ' + URL)
    if browser:
        webbrowser.open(URL)


def stop(*, timeout=180):
    with launch_lock():
        data = read_state()
        if data is None:
            print('Нет экземпляра, запущенного из этого каталога.')
            return
        control = open_control(data)
        if control is not None:
            with control:
                win32event.SetEvent(control.stop)
            print('Остановка: ждём исход текущей операции и завершение MCP.', flush=True)
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                alive = open_control(data)
                if alive is None:
                    break
                alive.close()
                time.sleep(0.1)
            else:
                raise RuntimeError('Остановка ещё не завершена. Проверьте SolidWorks и повторите stop. Процессы не завершались принудительно.')
        state_path().unlink(missing_ok=True)
        print('CAD-Agent-Next остановлен; чужие процессы не изменены.')


def main():
    parser = argparse.ArgumentParser(description='Запуск и штатная остановка CAD-Agent-Next.')
    parser.add_argument('action', choices=['start', 'stop'], help='Действие.')
    parser.add_argument('--no-browser', action='store_true', help='Не открывать браузер.')
    args = parser.parse_args()
    try:
        if args.action == 'start':
            start(browser=not args.no_browser)
        else:
            stop()
    except (RuntimeError, OSError, ValueError):
        # RuntimeError messages above are fixed local diagnostics; other errors hide paths/data.
        error = sys.exception()
        print(str(error) if isinstance(error, RuntimeError) else
              'Не удалось выполнить команду. Проверьте доступ к каталогу приложения.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
