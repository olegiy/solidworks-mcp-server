"""Bind the loopback socket before starting the MCP lifespan."""

import argparse
import asyncio
import socket

import uvicorn

from cadnext.app import create_app


def main(control_event=None):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8876))
            listener.listen(128)
        except OSError:
            raise SystemExit("Порт 127.0.0.1:8876 занят или недоступен. Второй MCP-процесс не запущен.") from None
        app = create_app()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8876,
                                                access_log=False, log_level="warning"))
        # Uvicorn closes its sockets before lifespan shutdown. Keep the original
        # reservation until the MCP process (including any timed-out COM) exits.
        with listener.dup() as serving_listener:
            if control_event is None:
                server.run(sockets=[serving_listener])
            else:
                from cadnext.launcher import Control, serve_controlled

                with Control(control_event) as control:
                    asyncio.run(serve_controlled(server, app, serving_listener, control))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Локальный CAD-Agent-Next.")
    parser.add_argument('--control-event', help=argparse.SUPPRESS)
    main(parser.parse_args().control_event)
