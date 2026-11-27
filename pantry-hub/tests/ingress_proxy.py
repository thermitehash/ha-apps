"""A tiny stand-in for the Supervisor's ingress proxy (tests only).

Serves the web UI under ``/api/hassio_ingress/<token>/``: strips that prefix,
forwards the request to the app with ``X-Ingress-Path`` set, and returns the
answer, as Home Assistant does. So a test (or a browser) can check that every
URL the page builds works under a non-root prefix.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web

from .conftest import free_port

HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "host"}


class IngressProxy:
    def __init__(self, upstream: str, token: str = "pantry_hub_test") -> None:
        self.upstream = upstream.rstrip("/")
        self.prefix = f"/api/hassio_ingress/{token}"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}{self.prefix}/"
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self.paths: list[str] = []

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", self.prefix + "/{tail:.*}", self._forward)
        self._session = aiohttp.ClientSession(auto_decompress=False)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    async def _forward(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        tail = request.match_info["tail"]
        self.paths.append("/" + tail)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["X-Ingress-Path"] = self.prefix
        async with self._session.request(
            request.method,
            f"{self.upstream}/{tail}",
            params=request.query,
            headers=headers,
            data=await request.read(),
            allow_redirects=False,
        ) as resp:
            body = await resp.read()
            out = web.Response(status=resp.status, body=body)
            for k, v in resp.headers.items():
                if k.lower() not in HOP and k.lower() != "content-length":
                    out.headers[k] = v
            return out
