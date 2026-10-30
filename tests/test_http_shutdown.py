import asyncio
import socket
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Proactor transport regression')
@pytest.mark.parametrize('shutdown_error', [None, 10054, 10053])
def test_http_reset_releases_listener_before_mcp_shutdown(monkeypatch, shutdown_error):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    import cadnext.__main__ as entry

    sockets = socket.socket

    class Listener:
        def __init__(self, *args):
            self.socket = sockets(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.socket.close()

        def bind(self, address):
            self.socket.bind(('127.0.0.1', 0))

        def __getattr__(self, name):
            return getattr(self.socket, name)

    class ResetSocket:
        closed = False

        def fileno(self):
            return 123

        def shutdown(self, how):
            if shutdown_error is None:
                return
            error = ConnectionResetError('Injected peer reset')
            error.winerror = shutdown_error
            raise error

        def close(self):
            self.closed = True

    observed = {}

    class Server:
        def __init__(self, config):
            self.config = config

        def run(self, *, sockets):
            config = self.config
            config.load()
            loop = asyncio.new_event_loop()
            errors = []
            loop.set_exception_handler(lambda loop, context: errors.append(context))
            listener = asyncio.Server(loop, [], None, None, 100, None)
            protocol = config.http_protocol_class(
                config, SimpleNamespace(connections=set(), tasks=set(), default_headers=[]),
                SimpleNamespace(), _loop=loop)
            peer = ResetSocket()
            transport = _ProactorBasePipeTransport(loop, peer, protocol, server=listener)
            try:
                loop.run_until_complete(asyncio.sleep(0))
                listener.close()
                transport.close()
                loop.run_until_complete(asyncio.sleep(0))
                observed.update(active=listener._active_count, closed=peer.closed,
                                errors=[type(e['exception']).__name__ for e in errors])
                if shutdown_error == 10053:
                    assert observed == {'active': 1, 'closed': False,
                                        'errors': ['ConnectionResetError']}
                    return
                assert listener._active_count == 0, observed
                assert peer.closed
                loop.run_until_complete(asyncio.wait_for(listener.wait_closed(), 1))
                observed['mcp_shutdown_can_start'] = True
                # Duplicate notifications must not detach twice or call the protocol again.
                transport._call_connection_lost(None)
                assert listener._active_count == 0
            finally:
                if transport._sock is not None:
                    # Test-only cleanup for the intentionally unhandled error branch.
                    peer.close()
                    transport._sock = None
                    transport._server._detach()
                    transport._server = None
                loop.close()

    monkeypatch.setattr(entry, 'socket', SimpleNamespace(
        socket=Listener, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE=socket.SO_EXCLUSIVEADDRUSE))
    monkeypatch.setattr(entry.uvicorn, 'Server', Server)
    entry.main()
    if shutdown_error != 10053:
        assert observed == {'active': 0, 'closed': True, 'errors': [],
                            'mcp_shutdown_can_start': True}
