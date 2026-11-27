"""Home Assistant door entity over the Supervisor's Core websocket API.

Needs ``homeassistant_api: true`` in config.yaml; the Supervisor injects
``SUPERVISOR_TOKEN``. The door is a ``binary_sensor`` (``on`` = open,
``off`` = closed). Anything else, a missing entity, or a lost websocket
counts as *unavailable*, which the reading planner treats like *closed*
(scheduled sweeps), never as open.

While the door stays unavailable longer than ``notify_after_s`` a persistent
notification (fixed id) is raised in Home Assistant, and dismissed when the
door reports again. (Apps cannot create entries in HA's Repairs dashboard;
that API is for integrations.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import itertools
import json
import logging
from typing import Any

import aiohttp

from .presence import Door

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_WS = "ws://supervisor/core/websocket"
NOTIFICATION_ID = "pantry_hub_door_unavailable"

DoorCallback = Callable[[Door], None]


def door_from_state(state: object) -> Door:
    """HA binary_sensor state → :class:`Door` (unknown for anything unusable)."""
    if state == "on":
        return Door.OPEN
    if state == "off":
        return Door.CLOSED
    return Door.UNKNOWN


class HomeAssistantDoor:
    """Follow one door entity; call ``on_change`` with every new :class:`Door`."""

    def __init__(
        self,
        entity_id: str,
        token: str,
        on_change: DoorCallback,
        *,
        url: str = SUPERVISOR_WS,
        notify_after_s: float = 120.0,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        """Create; call :meth:`start`."""
        self.entity_id = entity_id
        self.url = url
        self.on_change = on_change
        self.notify_after_s = notify_after_s
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._token = token
        self.door = Door.UNKNOWN
        self.connected = False
        self.notified = False
        self._ids = itertools.count(1)
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._notify_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        """Start the connection loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="pantry_hub.ha_door")
            self._arm_notification()

    async def stop(self) -> None:
        """Disconnect."""
        self._stopping.set()
        for task in (self._task, self._notify_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._notify_task = None

    # Door state ----------------------------------------------------------------

    def _set(self, door: Door) -> None:
        if door is self.door:
            return
        self.door = door
        _LOGGER.info("Pantry door (%s): %s", self.entity_id, door.value)
        if door is Door.UNKNOWN:
            self._arm_notification()
        else:
            if self._notify_task is not None:
                self._notify_task.cancel()
                self._notify_task = None
            if self.notified:
                self._spawn_service(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": NOTIFICATION_ID},
                )
                self.notified = False
        self.on_change(door)

    def _arm_notification(self) -> None:
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._notify_later())

    async def _notify_later(self) -> None:
        await asyncio.sleep(self.notify_after_s)
        if self.door is Door.UNKNOWN and not self.notified:
            _LOGGER.warning(
                "Door entity %s unavailable for %.0f s; sweeping every "
                "closed_sweep_interval_s until it reports again",
                self.entity_id,
                self.notify_after_s,
            )
            self.notified = await self._call_service(
                "persistent_notification",
                "create",
                {
                    "notification_id": NOTIFICATION_ID,
                    "title": "Pantry Hub: door sensor unavailable",
                    "message": (
                        f"`{self.entity_id}` is unavailable. Pantry Hub falls back "
                        "to its closed-door sweep schedule until the sensor "
                        "reports again. Check the sensor's battery and connection."
                    ),
                },
            )

    # Websocket -----------------------------------------------------------------

    def _spawn_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        task = asyncio.create_task(self._call_service(domain, service, data))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(
                {
                    "id": next(self._ids),
                    "type": "call_service",
                    "domain": domain,
                    "service": service,
                    "service_data": data,
                }
            )
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as err:
            _LOGGER.warning("Could not call %s.%s: %s", domain, service, err)
            return False
        return True

    async def _run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.ws_connect(self.url, heartbeat=30) as ws,
                ):
                    self._ws = ws
                    await self._handshake(ws)
                    failures = 0
                    await self._listen(ws)
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
                _LOGGER.warning("Home Assistant websocket: %s", err)
            finally:
                self._ws = None
                self.connected = False
                if not self._stopping.is_set():
                    self._set(Door.UNKNOWN)  # lost connection ≠ open door
            failures += 1
            delay = min(self.max_backoff, self.min_backoff * 2 ** min(failures, 6))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_required":
            raise ValueError(f"unexpected first message {msg.get('type')!r}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_ok":
            raise ValueError(f"Home Assistant refused the token: {msg.get('type')!r}")
        self.connected = True
        await ws.send_json(
            {
                "id": next(self._ids),
                "type": "subscribe_entities",
                "entity_ids": [self.entity_id],
            }
        )

    async def _listen(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        seen = False
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if data.get("type") == "result" and not data.get("success", True):
                _LOGGER.warning(
                    "Home Assistant rejected a request: %s", data.get("error")
                )
                continue
            if data.get("type") != "event":
                continue
            event = data.get("event") or {}
            added = (event.get("a") or {}).get(self.entity_id)
            if isinstance(added, dict):
                seen = True
                self._set(door_from_state(added.get("s")))
            changed = (event.get("c") or {}).get(self.entity_id)
            plus = changed.get("+") if isinstance(changed, dict) else None
            if isinstance(plus, dict) and "s" in plus:
                self._set(door_from_state(plus["s"]))
            if self.entity_id in (event.get("r") or []):
                self._set(Door.UNKNOWN)
            if not seen and not event.get("a"):
                # Subscribed, but HA doesn't know the entity: unavailable.
                self._set(Door.UNKNOWN)
