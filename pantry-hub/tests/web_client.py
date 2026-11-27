"""Test client for the web UI's JSON API (what the page's JS does)."""

from __future__ import annotations

import html
import json
import re
from typing import Any

import aiohttp

from pantry_hub.web import WebUI

from .conftest import free_port

BOOT_RE = re.compile(r'data-boot="([^"]*)"')


class WebClient:
    """Starts a :class:`WebUI` and talks to it like the page does."""

    def __init__(self, hub: Any, *, prefix: str = "") -> None:
        self.ui = WebUI(hub, allowed_peers=["*"])
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.headers = {"X-Ingress-Path": prefix} if prefix else {}
        self.session: aiohttp.ClientSession | None = None
        self.csrf = ""
        self.boot: dict[str, Any] = {}

    async def __aenter__(self) -> WebClient:
        await self.ui.start("127.0.0.1", self.port)
        self.session = aiohttp.ClientSession()
        shell = await self.text("/")
        m = BOOT_RE.search(shell)
        assert m, "no boot data in the shell"
        self.boot = json.loads(html.unescape(m.group(1)))
        self.csrf = self.boot["csrf"]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.session is not None:
            await self.session.close()
        await self.ui.stop()

    async def text(self, path: str) -> str:
        assert self.session is not None
        async with self.session.get(self.base + path, headers=self.headers) as r:
            assert r.status == 200, (path, r.status)
            return await r.text()

    async def json(self, path: str) -> Any:
        assert self.session is not None
        async with self.session.get(self.base + path, headers=self.headers) as r:
            assert r.status == 200, (path, r.status)
            assert r.headers["Cache-Control"] == "no-store"
            return await r.json()

    async def state(self) -> dict[str, Any]:
        data: dict[str, Any] = await self.json("/api/state")
        return data

    async def post(self, path: str, **fields: Any) -> dict[str, Any]:
        """POST a form like the page does (CSRF in field and header)."""
        assert self.session is not None
        data = {"csrf": self.csrf, **{k: str(v) for k, v in fields.items()}}
        async with self.session.post(
            self.base + path,
            data=data,
            headers={**self.headers, "X-CSRF-Token": self.csrf},
        ) as r:
            assert r.status == 200, (path, r.status)
            assert r.content_type == "application/json"
            assert r.headers["Cache-Control"] == "no-store"
            body: dict[str, Any] = await r.json()
            return body
