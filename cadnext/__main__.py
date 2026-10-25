"""Bind the loopback socket before starting the MCP lifespan."""

import socket

import uvicorn

from cadnext.app import create_app


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8876))
            listener.listen(128)
        except OSError:
            raise SystemExit("Порт 127.0.0.1:8876 занят или недоступен. Второй MCP-процесс не запущен.") from None
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=8876,
                                                access_log=False, log_level="warning"))
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
