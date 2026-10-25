import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launch_env(tmp_path):
    with socket.socket() as listener:
        try:
            listener.bind(('127.0.0.1', 8876))
        except OSError:
            pytest.skip('Порт 8876 занят внешним приложением.')
    return {**os.environ, 'LOCALAPPDATA': str(tmp_path), 'PYTHONUTF8': '1'}


def command(env, action, *, check=True):
    result = subprocess.run([sys.executable, '-m', 'cadnext.launcher', action, '--no-browser'],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8',
                            timeout=90)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def state(env):
    files = list((Path(env['LOCALAPPDATA']) / 'cad_agent_next' / 'runtime').glob('*/instance.json'))
    assert len(files) == 1
    return files[0], json.loads(files[0].read_text(encoding='utf-8'))


def test_start_is_idempotent_and_stop_is_owned_and_graceful(launch_env):
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(ROOT / 'scripts' / 'start.ps1'), '-NoBrowser'], cwd=ROOT.parent,
            env=launch_env, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        path, first = state(launch_env)
        with httpx.Client(trust_env=False) as client:
            assert client.get('http://127.0.0.1:8876/api/health').json()['ok']
            assert client.get('http://127.0.0.1:8876/').status_code == 200
        command(launch_env, 'start')
        assert state(launch_env)[1] == first
        result = subprocess.run(['cmd.exe', '/c', str(ROOT / 'stop.bat')], cwd=ROOT.parent,
                                env=launch_env, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not path.exists()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 8876))
        command(launch_env, 'stop')
    finally:
        command(launch_env, 'stop', check=False)


def test_occupied_foreign_port_is_preserved_and_no_instance_is_recorded(launch_env):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 8876))
        listener.listen()
        result = command(launch_env, 'start', check=False)
        assert result.returncode != 0
        assert '8876' in result.stdout + result.stderr
        command(launch_env, 'stop')
        assert listener.getsockname() == ('127.0.0.1', 8876)
        with pytest.raises(OSError), socket.socket() as other:
            other.bind(('127.0.0.1', 8876))


def test_stale_state_never_targets_pid(launch_env):
    from cadnext.launcher import state_path

    path = state_path(Path(launch_env['LOCALAPPDATA']))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'id': '0' * 32, 'pid': os.getpid(), 'root': str(ROOT)}), encoding='utf-8')
    command(launch_env, 'stop')
    assert not path.exists()
    assert os.getpid() > 0


def test_port_stays_reserved_when_uvicorn_closes_its_socket_before_mcp_exit(launch_env, monkeypatch):
    import cadnext.__main__ as entry

    class Server:
        def __init__(self, config):
            pass

        def run(self, *, sockets):
            # Uvicorn closes its listeners before awaiting lifespan (and MCP) shutdown.
            for listener in sockets:
                listener.close()
            with socket.socket() as second, pytest.raises(OSError):
                second.bind(('127.0.0.1', 8876))

    monkeypatch.setattr(entry.uvicorn, 'Server', Server)
    entry.main()


@pytest.mark.parametrize('operation', ['write_text', 'replace'])
def test_state_publication_failure_happens_before_spawning_process(launch_env, monkeypatch, operation):
    from types import SimpleNamespace

    from cadnext.launcher import start

    monkeypatch.setenv('LOCALAPPDATA', launch_env['LOCALAPPDATA'])
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(True)
        return SimpleNamespace(pid=12345)

    original = getattr(Path, operation)

    def failed_publication(path, *args, **kwargs):
        if path.name == 'instance.tmp':
            raise OSError('Injected state publication failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(Path, operation, failed_publication)
    with pytest.raises(OSError):
        start(browser=False)
    assert spawned == []


async def test_control_signal_waits_for_agent_before_server_exit():
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4

    import win32event

    from cadnext.launcher import Control, serve_controlled

    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    class Server:
        started = False
        should_exit = False

        async def serve(self, *, sockets):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.01)

    server = Server()
    app = SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(aclose=close)))
    with Control(uuid4().hex, create=True) as owner, Control(owner.id) as child:
        task = asyncio.create_task(serve_controlled(server, app, None, child))
        win32event.SetEvent(owner.stop)
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
        assert not server.should_exit
        release.set()
        await asyncio.wait_for(task, 2)
        assert server.should_exit
