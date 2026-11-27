"""A small fake of Home Assistant's websocket API (what the door source uses).

Implements auth, ``subscribe_entities`` (initial ``a`` event, ``c`` changes,
``r`` removals) and records ``call_service`` messages.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from aiohttp import WSMsgType, web

TOKEN = "ha-test-token"


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.attributes: dict[str, dict[str, Any]] = {}
        self.fail_get_states = False
        self.services: list[tuple[str, str, dict[str, Any]]] = []
        self.connections = 0
        self.reject_auth = False
        self._clients: list[tuple[web.WebSocketResponse, int, list[str]]] = []
        self._runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/websocket", self._ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"ws://127.0.0.1:{port}/api/websocket"

    async def stop(self) -> None:
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        """Close every client connection (HA restart / network blip)."""
        clients, self._clients = self._clients, []
        for ws, _, _ in clients:
            with contextlib.suppress(Exception):
                await ws.close()

    def calls(self, domain: str, service: str) -> list[dict[str, Any]]:
        return [d for dm, sv, d in self.services if dm == domain and sv == service]

    def add_entity(self, entity_id: str, state: str, **attributes: Any) -> None:
        """Add an entity with attributes (no event; for ``get_states``)."""
        self.states[entity_id] = state
        self.attributes[entity_id] = attributes

    async def set_state(self, entity_id: str, state: str | None) -> None:
        """Change (or with ``None`` remove) an entity and push it to subscribers."""
        if state is None:
            self.states.pop(entity_id, None)
            event: dict[str, Any] = {"r": [entity_id]}
        elif entity_id in self.states:
            self.states[entity_id] = state
            event = {"c": {entity_id: {"+": {"s": state, "lc": 1.0}}}}
        else:
            self.states[entity_id] = state
            event = {"a": {entity_id: {"s": state, "a": {}, "c": "x", "lc": 1.0}}}
        for ws, sub_id, ids in list(self._clients):
            if entity_id in ids and not ws.closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await ws.send_json({"id": sub_id, "type": "event", "event": event})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.2"})
        auth = await ws.receive_json()
        if self.reject_auth or auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.2"})
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            data = msg.json()
            if data.get("type") == "subscribe_entities":
                ids = list(data.get("entity_ids") or [])
                self._clients.append((ws, data["id"], ids))
                await ws.send_json(
                    {"id": data["id"], "type": "result", "success": True}
                )
                added = {
                    e: {"s": self.states[e], "a": {}, "c": "x", "lc": 1.0}
                    for e in ids
                    if e in self.states
                }
                await ws.send_json(
                    {"id": data["id"], "type": "event", "event": {"a": added}}
                )
            elif data.get("type") == "get_states":
                if self.fail_get_states:
                    await ws.send_json(
                        {
                            "id": data["id"],
                            "type": "result",
                            "success": False,
                            "error": {"code": "unknown_error", "message": "boom"},
                        }
                    )
                else:
                    await ws.send_json(
                        {
                            "id": data["id"],
                            "type": "result",
                            "success": True,
                            "result": [
                                {
                                    "entity_id": e,
                                    "state": s,
                                    "attributes": self.attributes.get(e, {}),
                                }
                                for e, s in self.states.items()
                            ],
                        }
                    )
            elif data.get("type") == "call_service":
                self.services.append(
                    (data["domain"], data["service"], data.get("service_data") or {})
                )
                await ws.send_json(
                    {"id": data["id"], "type": "result", "success": True}
                )
            await asyncio.sleep(0)
        return ws
