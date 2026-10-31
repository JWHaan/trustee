"""Mounting and serving the network agents.

One Render service hosts Trustee, Northwind and Tidewater: `mount_agents` puts the two agents under
`/agents/northwind` and `/agents/tidewater` of the arena app, each with its own Agent Card and `/a2a` endpoint.
The same agents also run alone (`python -m trustee.network.northwind`), and the launcher in
`examples/a2a_network.py` starts all three as separate local servers. The helpers below start and stop a uvicorn
server on a thread; they always bind an explicit address.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from .northwind import Northwind
from .tidewater import Tidewater

NORTHWIND_MOUNT = "/agents/northwind"
TIDEWATER_MOUNT = "/agents/tidewater"


def mount_agents(app: FastAPI, northwind: Northwind, tidewater: Tidewater) -> None:
    """Mount both agents on `app`. Call before a catch-all mount (the UI) is added."""
    app.mount(NORTHWIND_MOUNT, northwind.app)
    app.mount(TIDEWATER_MOUNT, tidewater.app)


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@dataclass
class ServerHandle:
    server: uvicorn.Server
    thread: threading.Thread
    url: str

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def start_server(app: FastAPI, host: str = "127.0.0.1", port: int | None = None) -> ServerHandle:
    """Run `app` on a daemon thread and return once it accepts connections."""
    chosen = port if port is not None else free_port(host)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=chosen, log_level="warning"))
    thread = threading.Thread(target=server.run, name=f"uvicorn-{chosen}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"the server on {host}:{chosen} did not start")
        time.sleep(0.02)
    return ServerHandle(server, thread, f"http://{host}:{chosen}")


@contextmanager
def running_server(
    app: FastAPI, host: str = "127.0.0.1", port: int | None = None, *, keep_alive: bool = False
) -> Iterator[ServerHandle]:
    """Serve `app` for the duration of the block; with `keep_alive`, keep serving after it until Ctrl-C."""
    handle = start_server(app, host, port)
    try:
        yield handle
        if keep_alive:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        handle.stop()


def serve_forever(app: FastAPI, host: str, port: int) -> None:
    uvicorn.run(app, host=host, port=port, log_level="warning")
