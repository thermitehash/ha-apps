"""The Pantry Hub service: UDP reads → presence → Grocy journal + MQTT."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any

from aiochainway import (
    ChainwayError,
    TagRead,
    UR4Client,
    UR4UdpListener,
)

from .config import MqttSettings, Options
from .discovery import (
    DISCOVERY_TOPIC,
    EVENTS_TOPIC,
    HA_STATUS_TOPIC,
    LAST_LINK_TOPIC,
    LINKING_SUB,
    RESYNC_COMMAND_TOPIC,
    RESYNC_RESULT_TOPIC,
    STATE_TOPIC,
    discovery_payload,
    linking_result_topic,
)
from .grocy import GrocyError, StockBackend
from .linking import LinkingManager, LinkResult, best_before_for
from .mqtt import MqttLink
from .presence import Door, PresenceConfig, PresenceEvent, PresenceMachine
from .store import Link, Store, iso
from .worker import GrocyWorker

_LOGGER = logging.getLogger(__name__)

TICK_S = 1.0
STATE_EVERY_S = 60.0
TOUCH_EVERY_S = 30.0
PROBE_EVERY_S = 60.0

_OPEN = {"on", "open", "true", "1", "opened"}
_CLOSED = {"off", "closed", "false", "0", "close"}


def parse_door(payload: bytes) -> Door:
    """Door contact payload → :class:`Door`.

    Accepts ``ON``/``OFF``, ``open``/``closed``, ``true``/``false``, ``1``/``0``
    (``ON`` = open, as HA door binary sensors do) and zigbee2mqtt JSON
    ``{"contact": true}`` (``contact: true`` = closed).
    """
    text = payload.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return Door.UNKNOWN
        if isinstance(data, dict):
            if isinstance(data.get("contact"), bool):
                return Door.CLOSED if data["contact"] else Door.OPEN
            for key in ("state", "door", "value"):
                if key in data:
                    return parse_door(str(data[key]).encode())
        return Door.UNKNOWN
    low = text.lower()
    if low in _OPEN:
        return Door.OPEN
    if low in _CLOSED:
        return Door.CLOSED
    return Door.UNKNOWN


_COUNTERS = ("last_datagram", "datagrams", "tags", "bad_frames", "dropped_sources")


def _significant(state: dict[str, Any]) -> dict[str, Any]:
    """State without the fast-moving reader traffic counters."""
    reader = {k: v for k, v in state["reader"].items() if k not in _COUNTERS}
    return {**state, "reader": reader}


@dataclass(slots=True)
class ReaderHealth:
    """What the hub knows about the UR4."""

    last_traffic: float | None = None  # monotonic
    last_traffic_wall: float | None = None
    last_probe_ok: float | None = None  # monotonic
    probe_version: str | None = None
    probe_error: str | None = None
    seen_frames: int = 0


@dataclass(slots=True)
class ResyncReport:
    """Last reconcile result."""

    checked_at: float
    checked: int
    differences: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class Hub:
    """Owns every moving part; :meth:`start` / :meth:`stop`."""

    def __init__(
        self,
        options: Options,
        store: Store,
        backend: StockBackend,
        *,
        mqtt_settings: MqttSettings | None,
        version: str,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        tick_s: float = TICK_S,
        bind_host: str = "0.0.0.0",
        tag_timeout: float | None = None,
        rssi_window: float | None = None,
    ) -> None:
        """Create; call :meth:`start`."""
        self.options = options
        self.store = store
        self.backend = backend
        self.version = version
        self.monotonic = monotonic
        self.wall = wall
        self.tick_s = tick_s
        self.bind_host = bind_host
        self.machine = PresenceMachine(
            PresenceConfig(
                arrive_reads=options.arrive_reads,
                arrive_window_s=options.arrive_window_s,
                depart_after_s=options.depart_after_s,
                depart_requires_door_closed=options.depart_requires_door_closed,
            )
        )
        self.worker = GrocyWorker(store, backend, clock=wall, on_change=self.mark_dirty)
        linking_kwargs: dict[str, float] = {}
        if tag_timeout is not None:
            linking_kwargs["tag_timeout"] = tag_timeout
        if rssi_window is not None:
            linking_kwargs["rssi_window"] = rssi_window
        self.linking = LinkingManager(
            store,
            backend,
            publish=self._publish_link_result,
            location_id=options.grocy_location_id,
            on_linked=self._on_linked,
            on_needs_product=self.mark_dirty,
            clock=wall,
            **linking_kwargs,
        )
        self.reader = ReaderHealth()
        self.listener: UR4UdpListener | None = None
        self.mqtt: MqttLink | None = None
        if mqtt_settings is not None:
            subs = [LINKING_SUB, HA_STATUS_TOPIC, RESYNC_COMMAND_TOPIC]
            if options.door_entity_topic:
                subs.append(options.door_entity_topic)
            self.mqtt = MqttLink(
                mqtt_settings,
                subscriptions=subs,
                on_message=self._on_mqtt,
                on_connect=self._on_mqtt_connect,
            )
        self.last_resync: ResyncReport | None = None
        self.started_at = wall()
        self._events: asyncio.Queue[PresenceEvent | None] = asyncio.Queue()
        self._seen: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loops: list[asyncio.Task[None]] = []
        self._dirty_flag = True
        self._last_state: dict[str, Any] | None = None
        self._last_state_at = 0.0
        self._unlinked_present = 0
        self._counts: dict[str, int] = {"pending": 0, "done": 0, "failed": 0}

    # Lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Restore state and start every task."""
        restored = await self.store.present_epcs()
        self.machine.restore(restored, self.monotonic())
        if restored:
            _LOGGER.info(
                "Restored %d tag(s) as present; none will depart for %.0f s",
                len(restored),
                self.options.depart_after_s,
            )
        self.worker.start()
        allowed = self.options.reader_source_ip
        self.listener = await UR4UdpListener.start(
            (self.bind_host, self.options.udp_port),
            callback=self._on_tag,
            allowed_sources=[allowed] if allowed else None,
        )
        _LOGGER.info(
            "Listening for UR4 reads on UDP %s:%d (accepting %s)",
            self.bind_host,
            self.options.udp_port,
            allowed or "any source",
        )
        self._loops.append(
            asyncio.create_task(self._event_loop(), name="pantry_hub.events")
        )
        self._loops.append(
            asyncio.create_task(self._tick_loop(), name="pantry_hub.tick")
        )
        if self.options.reader_tcp_probe:
            self._loops.append(
                asyncio.create_task(self._probe_loop(), name="pantry_hub.probe")
            )
        if self.mqtt is not None:
            self.mqtt.start()
        await self.refresh_counts()

    async def stop(self) -> None:
        """Graceful shutdown: flush SQLite, finish the in-flight Grocy call, go offline."""
        _LOGGER.info("Stopping Pantry Hub")
        if self.listener is not None:
            await self.listener.stop()
        tick_tasks = [t for t in self._loops if t.get_name() != "pantry_hub.events"]
        for task in tick_tasks:
            task.cancel()
        await asyncio.gather(*tick_tasks, return_exceptions=True)
        self._events.put_nowait(None)  # drain queued arrivals/departures first
        events = [t for t in self._loops if t.get_name() == "pantry_hub.events"]
        await asyncio.gather(*events, return_exceptions=True)
        self._loops.clear()
        await self.linking.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_seen()
        await self.worker.stop()
        if self.mqtt is not None:
            await self.mqtt.stop()

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Task %s failed", task.get_name(), exc_info=task.exception())

    def mark_dirty(self) -> None:
        """Ask the next tick to re-read counters and publish state."""
        self._dirty_flag = True

    # UDP -----------------------------------------------------------------------

    def _on_tag(self, tag: TagRead) -> None:
        """UDP callback (runs in the event loop; must stay fast)."""
        try:
            now = self.monotonic()
            self.reader.last_traffic = now
            self.reader.last_traffic_wall = self.wall()
            self._seen[tag.epc] = self.wall()
            for event in self.machine.read(tag.epc, now):
                self._events.put_nowait(event)
        except Exception:
            _LOGGER.exception("Error handling a read of %s", tag.epc)

    async def _event_loop(self) -> None:
        while True:
            event = await self._events.get()
            try:
                if event is None:
                    if self._events.empty():
                        return
                    continue
                await self._handle_event(event)
            except Exception:
                _LOGGER.exception("Error handling %s", event)
            finally:
                self._events.task_done()

    async def drain_events(self) -> None:
        """Wait until every queued arrival/departure has been fully handled."""
        await self._events.join()

    async def _handle_event(self, event: PresenceEvent) -> None:
        now = self.wall()
        if event.kind == "arrived":
            link = await self.store.get_link(event.epc)
            best_before = best_before_for(link, now) if link else None
            res = await self.store.apply_arrival(
                event.epc, now, self.options.grocy_location_id, best_before
            )
            if res.add_op_id is not None:
                self.worker.wake()
            product = res.link.product_name if res.link else None
            _LOGGER.info(
                "Arrived: %s (%s)", event.epc, product or "unlinked; no Grocy change"
            )
            await self._publish_event("arrived", event.epc, res.link)
        else:
            dep = await self.store.apply_departure(event.epc, now)
            if dep.consume_op_id is not None:
                self.worker.wake()
            product = dep.add_op.product_name if dep.add_op else None
            _LOGGER.info("Departed: %s (%s)", event.epc, product or "no Grocy change")
            link = dep.link
            if link is None and dep.add_op is not None:
                link = Link(
                    epc=event.epc,
                    product_id=dep.add_op.product_id,
                    product_name=dep.add_op.product_name,
                    linked_at=0.0,
                )
            await self._publish_event("departed", event.epc, link)
        self.mark_dirty()

    async def _publish_event(self, kind: str, epc: str, link: Link | None) -> None:
        if self.mqtt is None:
            return
        payload = {
            "event_type": kind,
            "epc": epc,
            "product": link.product_name if link else None,
            "product_id": link.product_id if link else None,
            "linked": link is not None,
            "ts": round(self.wall(), 3),
        }
        await self.mqtt.publish(EVENTS_TOPIC, json.dumps(payload), qos=1)

    # Timers --------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        last_touch = self.monotonic()
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                now = self.monotonic()
                self._track_reader_traffic(now)
                for event in self.machine.tick(now):
                    self._events.put_nowait(event)
                if now - last_touch >= TOUCH_EVERY_S:
                    last_touch = now
                    await self._flush_seen()
                await self.publish_state()
            except Exception:
                _LOGGER.exception("Error in the hub timer")

    def _track_reader_traffic(self, now: float) -> None:
        if self.listener is None:
            return
        stats = self.listener.stats
        frames = stats.frames + stats.bad_frames
        if frames != self.reader.seen_frames:
            self.reader.seen_frames = frames
            self.reader.last_traffic = now
            self.reader.last_traffic_wall = self.wall()

    async def _flush_seen(self) -> None:
        seen, self._seen = self._seen, {}
        await self.store.touch(seen)

    async def _probe_loop(self) -> None:
        while True:
            try:
                async with UR4Client(self.options.reader_host, timeout=5) as client:
                    self.reader.probe_version = await client.get_version()
                self.reader.last_probe_ok = self.monotonic()
                self.reader.probe_error = None
            except (ChainwayError, OSError) as err:
                self.reader.probe_error = str(err) or type(err).__name__
                _LOGGER.debug("Reader TCP probe failed: %s", err)
            await asyncio.sleep(PROBE_EVERY_S)

    def reader_online(self) -> bool:
        """UDP traffic (or a TCP probe answer) within ``reader_offline_after_s``."""
        now = self.monotonic()
        limit = self.options.reader_offline_after_s
        for ts in (self.reader.last_traffic, self.reader.last_probe_ok):
            if ts is not None and now - ts <= limit:
                return True
        return False

    # State ---------------------------------------------------------------------

    async def refresh_counts(self) -> None:
        """Re-read counters that live in SQLite."""
        self._counts = await self.store.counts()
        in_pantry = set(self.machine.in_pantry())
        self._unlinked_present = sum(
            1 for u in await self.store.unlinked() if u.epc in in_pantry
        )

    def state(self) -> dict[str, Any]:
        """Everything the HA entities show."""
        stats = self.listener.stats if self.listener else None
        return {
            "reader_online": self.reader_online(),
            "reader": {
                "last_datagram": iso(self.reader.last_traffic_wall)
                if self.reader.last_traffic_wall
                else None,
                "datagrams": stats.datagrams if stats else 0,
                "tags": stats.tags if stats else 0,
                "bad_frames": stats.bad_frames if stats else 0,
                "dropped_sources": stats.dropped_sources if stats else 0,
                "probe_version": self.reader.probe_version,
                "probe_error": self.reader.probe_error,
            },
            "present_tags": len(self.machine.in_pantry()),
            "unlinked_tags": self._unlinked_present,
            "grocy_queue": self._counts.get("pending", 0),
            "grocy": {
                "online": self.worker.grocy_ok,
                "failed": self._counts.get("failed", 0),
                "last_error": self.worker.last_error,
            },
            "door": self.machine.door.value,
        }

    async def publish_state(self, force: bool = False) -> None:
        """Publish retained state when it changed (or every minute)."""
        if self._dirty_flag:
            self._dirty_flag = False
            await self.refresh_counts()
        state = self.state()
        key = _significant(state)
        now = self.monotonic()
        # Traffic counters change with every datagram; on their own they are
        # republished once a minute, not every tick (keeps MQTT and the HA
        # recorder quiet while tags are being read continuously).
        if (
            not force
            and key == self._last_state
            and now - self._last_state_at < STATE_EVERY_S
        ):
            return
        self._last_state = key
        self._last_state_at = now
        if self.mqtt is not None:
            await self.mqtt.publish(
                STATE_TOPIC, json.dumps(state), retain=True, queue=False
            )

    # MQTT ----------------------------------------------------------------------

    async def _on_mqtt_connect(self) -> None:
        await self.publish_discovery()

    async def publish_discovery(self) -> None:
        """(Re)publish discovery plus retained state."""
        if self.mqtt is None:
            return
        await self.mqtt.publish(
            DISCOVERY_TOPIC,
            json.dumps(discovery_payload(self.version)),
            retain=True,
            qos=1,
            queue=False,
        )
        await self.publish_state(force=True)
        if self.linking.last_link is not None:
            await self._publish_last_link(self.linking.last_link)

    async def _on_mqtt(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) == 4 and parts[0] == "pantry_hub" and parts[1] == "linking":
            station, kind = parts[2], parts[3]
            if kind == "barcode":
                await self.linking.handle_barcode(station, payload)
            elif kind == "tag":
                await self.linking.handle_tag(station, payload)
            return  # our own "result" messages come back here too
        if topic == HA_STATUS_TOPIC:
            if payload.strip().lower() == b"online":
                _LOGGER.info("Home Assistant came online; republishing discovery")
                await self.publish_discovery()
            return
        if topic == RESYNC_COMMAND_TOPIC:
            self._spawn(self.resync(), "pantry_hub.resync")
            return
        if self.options.door_entity_topic and topic == self.options.door_entity_topic:
            door = parse_door(payload)
            if door is Door.UNKNOWN:
                _LOGGER.warning(
                    "Unrecognised door payload on %s: %r", topic, payload[:50]
                )
            self.machine.set_door(door, self.monotonic())
            self.mark_dirty()

    async def _publish_link_result(self, station: str, result: LinkResult) -> None:
        if self.mqtt is not None:
            await self.mqtt.publish(
                linking_result_topic(station),
                json.dumps(result.payload()),
                qos=1,
                queue=False,
            )

    async def _on_linked(self, link: Link, op_id: int | None) -> None:
        if op_id is not None:
            self.worker.wake()
        await self._publish_last_link(link)
        self.mark_dirty()

    async def _publish_last_link(self, link: Link) -> None:
        if self.mqtt is None:
            return
        payload = {
            "product": link.product_name,
            "product_id": link.product_id,
            "epc": link.epc,
            "barcode": link.barcode,
            "station": link.station,
            "linked_at": iso(link.linked_at),
        }
        await self.mqtt.publish(
            LAST_LINK_TOPIC, json.dumps(payload), retain=True, qos=1
        )

    # Web UI actions ------------------------------------------------------------

    async def unlink(self, epc: str) -> bool:
        """Remove a link (does not touch Grocy stock)."""
        removed = await self.store.delete_link(epc)
        self.mark_dirty()
        return removed

    async def relink(self, epc: str, *, barcode: str = "", product_id: int = 0) -> Link:
        """Link ``epc`` to a product by barcode or Grocy product id (web UI)."""
        if barcode:
            product = await self.backend.product_by_barcode(barcode)
            if product is None:
                raise GrocyError(f"Grocy has no product with barcode {barcode}")
        elif product_id:
            product = await self.backend.product(product_id)
        else:
            raise ValueError("give a barcode or a Grocy product id")
        link = Link(
            epc=epc,
            product_id=product.id,
            product_name=product.name,
            linked_at=self.wall(),
            barcode=barcode or None,
            default_best_before_days=product.default_best_before_days,
            station="web",
        )
        op_id = await self.store.replace_link(
            link,
            location_id=self.options.grocy_location_id,
            best_before=best_before_for(link, self.wall()),
        )
        self.linking.last_link = link
        await self._on_linked(link, op_id)
        return link

    async def resync(self) -> ResyncReport:
        """Compare present linked tags with Grocy stock per product. Report only."""
        now = self.wall()
        links = await self.store.links()
        names = {lk.product_id: lk.product_name for lk in links}
        by_epc = {lk.epc: lk.product_id for lk in links}
        present: dict[int, int] = dict.fromkeys(names, 0)
        for epc in self.machine.in_pantry():
            pid = by_epc.get(epc)
            if pid is not None:
                present[pid] += 1
        report = ResyncReport(checked_at=now, checked=len(present))
        try:
            for pid in sorted(present):
                entries = await self.backend.entries(pid)
                amount = sum(
                    e.amount
                    for e in entries
                    if e.location_id == self.options.grocy_location_id
                )
                if amount != present[pid]:
                    report.differences.append(
                        {
                            "product_id": pid,
                            "product": names[pid],
                            "present_tags": present[pid],
                            "grocy_amount": amount,
                        }
                    )
        except GrocyError as err:
            report.error = str(err)
            _LOGGER.warning("Resync could not finish: %s", err)
        self.last_resync = report
        _LOGGER.info(
            "Resync: %d product(s) checked, %d difference(s)",
            report.checked,
            len(report.differences),
        )
        if self.mqtt is not None:
            await self.mqtt.publish(
                RESYNC_RESULT_TOPIC,
                json.dumps(
                    {
                        "checked_at": iso(now),
                        "checked": report.checked,
                        "pending_ops": self._counts.get("pending", 0),
                        "differences": report.differences,
                        "error": report.error,
                    }
                ),
                retain=True,
                qos=1,
            )
        return report
