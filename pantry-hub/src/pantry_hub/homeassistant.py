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

The door entity can be unset (``None``): door mode then runs in the
"needs door sensor" state (closed-door schedule) and a persistent
notification points to the web UI's Settings page, where the entity is
picked from :func:`door_candidates`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
import contextlib
from dataclasses import dataclass
import itertools
import json
import logging
from typing import Any

import aiohttp

from .presence import Door

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_WS = "ws://supervisor/core/websocket"
NOTIFICATION_ID = "pantry_hub_door_unavailable"
NEEDED_NOTIFICATION_ID = "pantry_hub_door_needed"
#: Device classes listed first in the picker, in this order.
DOOR_CLASSES = ("door", "garage_door", "opening", "window")

DoorCallback = Callable[[Door], None]


def door_from_state(state: object) -> Door:
    """HA binary_sensor state → :class:`Door` (unknown for anything unusable)."""
    if state == "on":
        return Door.OPEN
    if state == "off":
        return Door.CLOSED
    return Door.UNKNOWN


@dataclass(frozen=True, slots=True)
class DoorCandidate:
    """A binary_sensor offered in the door picker."""

    entity_id: str
    name: str
    state: str
    device_class: str | None

    @property
    def door_like(self) -> bool:
        """Whether its device class is one of :data:`DOOR_CLASSES`."""
        return self.device_class in DOOR_CLASSES


def door_candidates(states: Iterable[Mapping[str, Any]]) -> list[DoorCandidate]:
    """``binary_sensor`` entities from HA ``get_states``, door-like ones first.

    Order: device class door, garage_door, opening, window, then every other
    binary_sensor; within each group by friendly name, then entity id.
    """
    found: list[DoorCandidate] = []
    for item in states:
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("binary_sensor."):
            continue
        attrs = item.get("attributes")
        attrs = attrs if isinstance(attrs, Mapping) else {}
        name = attrs.get("friendly_name")
        device_class = attrs.get("device_class")
        found.append(
            DoorCandidate(
                entity_id=entity_id,
                name=name if isinstance(name, str) and name else entity_id,
                state=str(item.get("state", "unknown")),
                device_class=device_class if isinstance(device_class, str) else None,
            )
        )

    def key(c: DoorCandidate) -> tuple[int, str, str]:
        rank = (
            DOOR_CLASSES.index(c.device_class)
            if c.device_class in DOOR_CLASSES
            else len(DOOR_CLASSES)
        )
        return rank, c.name.casefold(), c.entity_id

    return sorted(found, key=key)


async def _authenticate(ws: aiohttp.ClientWebSocketResponse, token: str) -> None:
    msg = await ws.receive_json(timeout=10)
    if msg.get("type") != "auth_required":
        raise ValueError(f"unexpected first message {msg.get('type')!r}")
    await ws.send_json({"type": "auth", "access_token": token})
    msg = await ws.receive_json(timeout=10)
    if msg.get("type") != "auth_ok":
        raise ValueError(f"Home Assistant refused the token: {msg.get('type')!r}")


async def fetch_states(
    token: str, *, url: str = SUPERVISOR_WS, timeout: float = 10.0
) -> list[dict[str, Any]]:
    """One-shot ``get_states`` over the websocket API."""
    async with asyncio.timeout(timeout):
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(url) as ws,
        ):
            await _authenticate(ws, token)
            await ws.send_json({"id": 1, "type": "get_states"})
            while True:
                msg = await ws.receive_json()
                if msg.get("id") != 1 or msg.get("type") != "result":
                    continue
                if not msg.get("success", False):
                    raise ValueError(f"get_states failed: {msg.get('error')}")
                result = msg.get("result")
                return [s for s in result if isinstance(s, dict)] if result else []


class HomeAssistantDoor:
    """Follow one door entity; call ``on_change`` with every new :class:`Door`.

    ``entity_id`` may be ``None`` (no door sensor chosen yet): the connection
    is still kept for notifications, the door stays unknown, and a
    "needs door sensor" notification links to ``settings_url``.
    """

    def __init__(
        self,
        entity_id: str | None,
        token: str,
        on_change: DoorCallback,
        *,
        url: str = SUPERVISOR_WS,
        settings_url: str | None = None,
        notify_after_s: float = 120.0,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        """Create; call :meth:`start`."""
        self.entity_id = entity_id
        self.url = url
        self.settings_url = settings_url
        self.on_change = on_change
        self.notify_after_s = notify_after_s
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._token = token
        self.door = Door.UNKNOWN
        self.connected = False
        self.notified = False
        #: The "needs door sensor" notification is up.
        self.needed_notified = False
        #: Swap open/closed (for a sensor that reports ``on`` = closed).
        self.inverted = False
        #: Last raw HA state of the entity (``on``/``off``/…), None if unknown.
        self.raw_state: str | None = None
        #: The entity doesn't exist in Home Assistant (renamed or deleted?).
        self.missing = False
        self._seen = False
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

    async def set_entity(self, entity_id: str | None) -> None:
        """Follow another entity (or none) without reconnecting."""
        if entity_id == self.entity_id:
            return
        _LOGGER.info("Door sensor: %s", entity_id or "none (needs door sensor)")
        self.entity_id = entity_id
        self._seen = False
        self.raw_state = None
        self.missing = False
        if self._notify_task is not None:
            self._notify_task.cancel()
            self._notify_task = None
        if self.notified:
            await self._call_service(
                "persistent_notification",
                "dismiss",
                {"notification_id": NOTIFICATION_ID},
            )
            self.notified = False
        self._set(Door.UNKNOWN)
        # The door may already have been unknown (so _set didn't arm it): the
        # new entity gets its own unavailable/not-found timer.
        self._arm_notification()
        ws = self._ws
        if ws is not None and not ws.closed and self.connected:
            await self._on_connected(ws)

    def set_inverted(self, inverted: bool) -> None:
        """Swap open/closed from now on (re-applied to the current raw state)."""
        if inverted == self.inverted:
            return
        self.inverted = inverted
        _LOGGER.info("Door sensor inverted: %s", "yes" if inverted else "no")
        if self.raw_state is not None:
            self._apply_raw(self.raw_state)

    def interpret(self, raw: object) -> Door:
        """Raw HA state → :class:`Door`, honouring :attr:`inverted`."""
        door = door_from_state(raw)
        if self.inverted and door is not Door.UNKNOWN:
            return Door.CLOSED if door is Door.OPEN else Door.OPEN
        return door

    def _apply_raw(self, raw: object) -> None:
        self.raw_state = raw if isinstance(raw, str) else None
        self._set(self.interpret(raw))

    def _set_missing(self) -> None:
        self.missing = True
        self.raw_state = None
        self._set(Door.UNKNOWN)

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
        if self.entity_id is None:
            return  # the "needs door sensor" notification covers it
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._notify_later())

    async def _notify_later(self) -> None:
        await asyncio.sleep(self.notify_after_s)
        if self.door is Door.UNKNOWN and not self.notified and self.entity_id:
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
                    **self._unavailable_text(),
                },
            )

    def _unavailable_text(self) -> dict[str, str]:
        fallback = (
            "Pantry Hub falls back to its closed-door sweep schedule until the "
            "sensor reports again."
        )
        if self.missing:
            where = (
                f"[Pantry Hub → Settings]({self.settings_url})"
                if self.settings_url
                else "Pantry Hub's web UI → Settings"
            )
            return {
                "title": "Pantry Hub: door sensor not found",
                "message": (
                    f"Saved door sensor `{self.entity_id}` not found in Home "
                    f"Assistant (renamed or deleted?). {fallback} Pick it again "
                    f"in {where}, or correct `door_entity` in the app's "
                    "configuration."
                ),
            }
        return {
            "title": "Pantry Hub: door sensor unavailable",
            "message": (
                f"`{self.entity_id}` is unavailable. {fallback} Check the "
                "sensor's battery and connection."
            ),
        }

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
                    self.raw_state = None
                    self._set(Door.UNKNOWN)  # lost connection ≠ open door
            failures += 1
            delay = min(self.max_backoff, self.min_backoff * 2 ** min(failures, 6))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await _authenticate(ws, self._token)
        self.connected = True
        await self._on_connected(ws)

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to the entity, or raise "needs door sensor"."""
        if self.entity_id is None:
            if not self.needed_notified:
                self.needed_notified = await self._call_service(
                    "persistent_notification",
                    "create",
                    {
                        "notification_id": NEEDED_NOTIFICATION_ID,
                        "title": "Pantry Hub: choose the pantry door sensor",
                        "message": self._needed_message(),
                    },
                )
                if self.needed_notified:
                    _LOGGER.warning(
                        "No door sensor set: sweeping every closed_sweep_interval_s "
                        "until one is chosen (web UI → Settings, or door_entity)"
                    )
            return
        if self.needed_notified:
            await self._call_service(
                "persistent_notification",
                "dismiss",
                {"notification_id": NEEDED_NOTIFICATION_ID},
            )
            self.needed_notified = False
        self._seen = False
        await ws.send_json(
            {
                "id": next(self._ids),
                "type": "subscribe_entities",
                "entity_ids": [self.entity_id],
            }
        )

    def _needed_message(self) -> str:
        where = (
            f"[Open Pantry Hub → Settings]({self.settings_url})"
            if self.settings_url
            else "Open Pantry Hub's web UI → Settings"
        )
        return (
            "Door-driven reading needs the pantry door contact. Until one is "
            "chosen, Pantry Hub sweeps every closed_sweep_interval_s (hourly by "
            f"default). {where} and pick the door sensor, or set `door_entity` "
            "in the app's configuration."
        )

    async def _listen(self, ws: aiohttp.ClientWebSocketResponse) -> None:
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
            if data.get("type") != "event" or self.entity_id is None:
                continue
            entity = self.entity_id
            event = data.get("event") or {}
            added = (event.get("a") or {}).get(entity)
            if isinstance(added, dict):
                self._seen = True
                self.missing = False
                self._apply_raw(added.get("s"))
            changed = (event.get("c") or {}).get(entity)
            plus = changed.get("+") if isinstance(changed, dict) else None
            if isinstance(plus, dict) and "s" in plus:
                self._apply_raw(plus["s"])
            if entity in (event.get("r") or []):
                self._set_missing()  # removed from HA
            if not self._seen and not event.get("a"):
                # Subscribed, but HA doesn't know the entity.
                self._set_missing()
