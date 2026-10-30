"""Keep Python 3.12 Proactor peer resets from leaking HTTP listener ownership."""

import sys

from uvicorn.protocols.http.h11_impl import H11Protocol


class HttpProtocol(H11Protocol):
    def connection_made(self, transport):
        if sys.platform == 'win32':
            from asyncio.proactor_events import _ProactorBasePipeTransport

            if isinstance(transport, _ProactorBasePipeTransport):
                original = transport._call_connection_lost

                def connection_lost(exc):
                    try:
                        original(exc)
                    except ConnectionResetError as error:
                        if getattr(error, 'winerror', None) != 10054 or transport._called_connection_lost:
                            raise
                        # CPython 3.12 calls socket.shutdown before close/detach. A reset
                        # skips both, leaving Server.wait_closed waiting forever.
                        try:
                            transport._sock.close()
                        finally:
                            transport._sock = None
                            try:
                                if transport._server is not None:
                                    transport._server._detach()
                            finally:
                                transport._server = None
                                transport._called_connection_lost = True

                transport._call_connection_lost = connection_lost
        super().connection_made(transport)
