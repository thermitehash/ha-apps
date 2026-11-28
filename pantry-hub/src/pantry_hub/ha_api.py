"""Home Assistant REST actions + event subscription (Kroger Restock integration).

The REST base is derived from the websocket URL the door already uses:
``ws://supervisor/core/websocket`` → ``http://supervisor/core/api`` (the
Supervisor's proxy to Core) and, for development against a real Core,
``ws://host:8123/api/websocket`` → ``http://host:8123/api``.

Only the barcode is ever sent to Home Assistant (and from there to Kroger).
Nothing here logs URLs: an image URL from Kroger may carry a signed token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import itertools
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .homeassistant import _authenticate

_LOGGER = logging.getLogger(__name__)

#: How long a "has this action?" answer is trusted (Kroger Restock may be
#: installed or removed while Pantry Hub runs).
SERVICES_TTL_S = 600.0


class HomeAssistantApiError(Exception):
    """Home Assistant answered with an error or could not be reached."""


def rest_url_from_ws(ws_url: str) -> str:
    """Websocket API URL → REST API base URL (no trailing slash)."""
    parts = urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if path.endswith("/core/websocket"):
        path = path[: -len("websocket")] + "api"
    elif path.endswith("/websocket"):
        path = path[: -len("/websocket")]
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class HomeAssistantApi:
    """Calls actions over Home Assistant's REST API."""

    def __init__(
        self,
        rest_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create; the HTTP session opens on first use."""
        self.base = rest_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._monotonic = monotonic
        self._services: dict[str, set[str]] | None = None
        self._services_at = 0.0

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"}
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, query: str = ""
    ) -> Any:
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        try:
            async with self._get_session().request(
                method, url, json=json_body, timeout=self._timeout
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise HomeAssistantApiError(
                        f"HTTP {resp.status} for {method} {path}: {text[:200]}"
                    )
                return json.loads(text) if text else None
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantApiError(f"{method} {path} failed: {err!r}") from err

    async def services(self, *, refresh: bool = False) -> dict[str, set[str]]:
        """``{domain: {service, ...}}``, cached for :data:`SERVICES_TTL_S`."""
        now = self._monotonic()
        if (
            not refresh
            and self._services is not None
            and now - self._services_at < SERVICES_TTL_S
        ):
            return self._services
        body = await self._request("GET", "/services")
        found: dict[str, set[str]] = {}
        for item in body if isinstance(body, list) else []:
            if isinstance(item, dict) and isinstance(item.get("domain"), str):
                names = item.get("services")
                found[item["domain"]] = set(names) if isinstance(names, dict) else set()
        self._services, self._services_at = found, now
        return found

    async def has_service(self, domain: str, service: str) -> bool:
        """Whether the action exists (False when HA can't be asked)."""
        try:
            return service in (await self.services()).get(domain, set())
        except HomeAssistantApiError as err:
            _LOGGER.debug("Could not list Home Assistant actions: %s", err)
            return False

    async def call(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Call an action that returns a response; return that response."""
        body = await self._request(
            "POST",
            f"/services/{domain}/{service}",
            json_body=data,
            query="return_response",
        )
        response = body.get("service_response") if isinstance(body, dict) else None
        if not isinstance(response, dict):
            raise HomeAssistantApiError(
                f"{domain}.{service} returned no response object"
            )
        return response


EventFn = Callable[[dict[str, Any]], Awaitable[None]]
ConnectFn = Callable[[], Awaitable[None]]


class HomeAssistantEvents:
    """Subscribe to one event type over the websocket; reconnect with backoff.

    ``on_connect`` runs after every (re)subscription, so missed events can be
    caught up (``kroger_restock.recent_received``).
    """

    def __init__(
        self,
        url: str,
        token: str,
        event_type: str,
        on_event: EventFn,
        *,
        on_connect: ConnectFn | None = None,
        extra: dict[str, EventFn] | None = None,
        min_backoff: float = 2.0,
        max_backoff: float = 120.0,
    ) -> None:
        """Create; call :meth:`start`."""
        self.url = url
        self._token = token
        self.event_type = event_type
        self.on_event = on_event
        self.on_connect = on_connect
        #: More event types to follow on the same connection (no catch-up).
        self.extra = dict(extra or {})
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.connected = False
        self.subscribed = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)

    def start(self) -> None:
        """Start the connection loop."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"pantry_hub.ha_events.{self.event_type}"
            )

    async def stop(self) -> None:
        """Stop and wait for the loop."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.ws_connect(self.url, heartbeat=30) as ws,
                ):
                    await _authenticate(ws, self._token)
                    self.connected = True
                    handlers = {self.event_type: self.on_event, **self.extra}
                    subs: dict[int, tuple[str, EventFn]] = {}
                    for event_type, handler in handlers.items():
                        sub_id = next(self._ids)
                        subs[sub_id] = (event_type, handler)
                        await ws.send_json(
                            {
                                "id": sub_id,
                                "type": "subscribe_events",
                                "event_type": event_type,
                            }
                        )
                    failures = 0
                    await self._listen(ws, subs)
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
                _LOGGER.warning("Home Assistant events websocket: %s", err)
            finally:
                self.connected = False
                self.subscribed.clear()
            failures += 1
            delay = min(self.max_backoff, self.min_backoff * 2 ** min(failures, 6))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _listen(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        subs: dict[int, tuple[str, EventFn]],
    ) -> None:
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            sub = subs.get(data.get("id"))
            if sub is None:
                continue
            event_type, handler = sub
            if data.get("type") == "result":
                if not data.get("success", False):
                    raise ValueError(f"subscribe_events refused: {data.get('error')}")
                if event_type == self.event_type:
                    self.subscribed.set()
                    if self.on_connect is not None:
                        await self._guard(self.on_connect())
                continue
            event = data.get("event")
            if data.get("type") == "event" and isinstance(event, dict):
                payload = event.get("data")
                if isinstance(payload, dict):
                    if "time_fired" in event and "time_fired" not in payload:
                        payload = {**payload, "time_fired": event["time_fired"]}
                    await self._guard(handler(payload))

    @staticmethod
    async def _guard(coro: Awaitable[None]) -> None:
        """Run a handler; log its failure instead of dropping the subscription."""
        try:
            await coro
        except Exception:
            _LOGGER.exception("Home Assistant event handler failed")
