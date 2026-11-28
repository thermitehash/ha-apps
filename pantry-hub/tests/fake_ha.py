"""A small fake of Home Assistant's websocket API (what the door source uses).

Implements auth, ``subscribe_entities`` (initial ``a`` event, ``c`` changes,
``r`` removals), ``subscribe_events`` and records ``call_service`` messages.
The REST side serves ``GET /api/services`` and ``POST /api/services/<d>/<s>``
with ``?return_response`` (handlers registered with :meth:`add_action`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
        self.rest_url = ""
        #: (domain, service) → handler(data) → response (or raises web.HTTPException).
        self.actions: dict[
            tuple[str, str], Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
        ] = {}
        #: Every REST action call: (domain, service, data).
        self.rest_calls: list[tuple[str, str, dict[str, Any]]] = []
        self._event_subs: list[tuple[web.WebSocketResponse, int, str]] = []
        self.services_down = False

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/websocket", self._ws)
        app.router.add_get("/api/services", self._services)
        app.router.add_get("/api/states", self._states)
        app.router.add_get("/api/states/{entity_id}", self._state)
        app.router.add_post("/api/services/{domain}/{service}", self._call)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"ws://127.0.0.1:{port}/api/websocket"
        self.rest_url = f"http://127.0.0.1:{port}/api"

    async def stop(self) -> None:
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        """Close every client connection (HA restart / network blip)."""
        clients, self._clients = self._clients, []
        subs, self._event_subs = self._event_subs, []
        for ws in [c[0] for c in clients] + [e[0] for e in subs]:
            with contextlib.suppress(Exception):
                await ws.close()

    def add_action(
        self,
        domain: str,
        service: str,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        """Register an action answering ``?return_response``."""
        self.actions[(domain, service)] = handler

    def rest(self, domain: str, service: str) -> list[dict[str, Any]]:
        return [d for dm, sv, d in self.rest_calls if (dm, sv) == (domain, service)]

    def event_subscribers(self, event_type: str) -> int:
        return sum(
            1 for ws, _, t in self._event_subs if t == event_type and not ws.closed
        )

    async def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Send an event to its subscribers."""
        for ws, sub_id, etype in list(self._event_subs):
            if etype == event_type and not ws.closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await ws.send_json(
                        {
                            "id": sub_id,
                            "type": "event",
                            "event": {"event_type": etype, "data": data},
                        }
                    )

    def _authorized(self, request: web.Request) -> None:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    def _state_json(self, entity_id: str) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "state": self.states[entity_id],
            "attributes": self.attributes.get(entity_id, {}),
        }

    async def _states(self, request: web.Request) -> web.Response:
        self._authorized(request)
        return web.json_response([self._state_json(e) for e in self.states])

    async def _state(self, request: web.Request) -> web.Response:
        self._authorized(request)
        entity_id = request.match_info["entity_id"]
        if entity_id not in self.states:
            raise web.HTTPNotFound(text='{"message": "Entity not found."}')
        return web.json_response(self._state_json(entity_id))

    async def _services(self, request: web.Request) -> web.Response:
        self._authorized(request)
        if self.services_down:
            raise web.HTTPBadGateway()
        domains: dict[str, dict[str, Any]] = {}
        for domain, service in self.actions:
            domains.setdefault(domain, {})[service] = {"fields": {}}
        domains.setdefault("persistent_notification", {})["create"] = {}
        return web.json_response(
            [{"domain": d, "services": s} for d, s in domains.items()]
        )

    async def _call(self, request: web.Request) -> web.Response:
        self._authorized(request)
        domain, service = request.match_info["domain"], request.match_info["service"]
        data = await request.json()
        self.rest_calls.append((domain, service, data))
        handler = self.actions.get((domain, service))
        if handler is None:
            raise web.HTTPBadRequest(text="Service not found")
        assert "return_response" in request.query
        response = await handler(data)
        return web.json_response({"changed_states": [], "service_response": response})

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
            elif data.get("type") == "subscribe_events":
                self._event_subs.append((ws, data["id"], data["event_type"]))
                await ws.send_json(
                    {"id": data["id"], "type": "result", "success": True}
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
