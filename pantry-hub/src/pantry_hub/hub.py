"""The Pantry Hub service: reader windows → presence → Grocy journal + MQTT.

``reading_mode: door`` (default): the hub drives the UR4 in command mode over
TCP, from the Home Assistant door entity (see :mod:`pantry_hub.reading`).
``reading_mode: continuous``: the reader's auto mode pushes UDP (fallback).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
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
    SWEEP_COMMAND_TOPIC,
    discovery_payload,
    linking_result_topic,
)
from .grocy import GrocyError, StockBackend
from .homeassistant import SUPERVISOR_WS, HomeAssistantDoor
from .linking import LinkingManager, LinkResult, best_before_for
from .mqtt import MqttLink
from .presence import Door, PresenceConfig, PresenceEvent, PresenceMachine
from .reader import (
    ClientFactory,
    DiscoverFn,
    ReaderSession,
    WindowResult,
    reader_unique_id,
)
from .reading import Activity, Plan, ReadingConfig, ReadingPlanner
from .store import Link, Store, iso
from .worker import GrocyWorker

_LOGGER = logging.getLogger(__name__)

TICK_S = 1.0
STATE_EVERY_S = 60.0
TOUCH_EVERY_S = 30.0
PROBE_EVERY_S = 60.0
#: Door mode: connect + ping + disconnect this often while idle.
PROBE_IDLE_S = 600.0

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
    #: Ambiguous consumes waiting for a person (Grocy may be one unit high).
    review_ops: list[dict[str, Any]] = field(default_factory=list)


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
        ha_url: str | None = None,
        ha_token: str | None = None,
        client_factory: ClientFactory | None = None,
        discover_fn: DiscoverFn | None = None,
        retry_s: float = 60.0,
        probe_every_s: float = PROBE_IDLE_S,
        door_notify_after_s: float = 120.0,
    ) -> None:
        """Create; call :meth:`start`."""
        door_mode = options.reading_mode == "door"
        self.door_mode = door_mode
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
                depart_after_missed_sweeps=(
                    options.depart_after_missed_sweeps if door_mode else 0
                ),
                # Restart rule: no departures until 2 completed sweeps.
                min_sweeps_before_departure=2 if door_mode else 0,
            )
        )
        self.reading_config = ReadingConfig(
            open_continuous_s=options.open_continuous_s,
            open_sweep_interval_s=options.open_sweep_interval_s,
            sweep_s=options.sweep_s,
            closed_sweep_interval_s=options.closed_sweep_interval_s,
            retry_s=retry_s,
        )
        self.planner: ReadingPlanner | None = (
            ReadingPlanner(self.reading_config, monotonic()) if door_mode else None
        )
        session_kwargs: dict[str, Any] = {}
        if client_factory is not None:
            session_kwargs["client_factory"] = client_factory
        self.session = ReaderSession(
            options.reader_host,
            options.reader_tcp_port,
            connection="persistent"
            if options.reader_connection == "persistent"
            else "per_window",
            **session_kwargs,
        )
        self.probe_every_s = probe_every_s
        #: Last time a window or probe talked to the reader (monotonic).
        self._last_contact = monotonic()
        self.activity = Activity.IDLE if door_mode else Activity.CONTINUOUS
        self.last_sweep: dict[str, Any] | None = None
        self.last_sweep_attempt: dict[str, Any] | None = None
        self.reader_id = f"ur4_{options.reader_host}:{options.reader_tcp_port}"
        self.reader_id_source = "host"
        self._discover_fn = discover_fn
        self._wake = asyncio.Event()
        self._window: asyncio.Task[WindowResult] | None = None
        self.door_source: HomeAssistantDoor | None = None
        if options.door_entity and ha_token:
            self.door_source = HomeAssistantDoor(
                options.door_entity,
                ha_token,
                self._on_door,
                url=ha_url or SUPERVISOR_WS,
                notify_after_s=door_notify_after_s,
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
            subs = [
                LINKING_SUB,
                HA_STATUS_TOPIC,
                RESYNC_COMMAND_TOPIC,
                SWEEP_COMMAND_TOPIC,
            ]
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
        stored_id = await self.store.get_meta("reader_id")
        if stored_id:
            self.reader_id, self.reader_id_source = stored_id, "stored"
        restored = await self.store.present_epcs()
        self.machine.restore(restored, self.monotonic())
        if restored:
            if self.door_mode:
                _LOGGER.info(
                    "Restored %d tag(s) as present; none will depart until 2 "
                    "sweeps have completed",
                    len(restored),
                )
            else:
                _LOGGER.info(
                    "Restored %d tag(s) as present; none will depart for %.0f s",
                    len(restored),
                    self.options.depart_after_s,
                )
        self.worker.start()
        if self.door_mode:
            _LOGGER.info(
                "Door-driven reading: UR4 %s:%d in command mode, door %s",
                self.options.reader_host,
                self.options.reader_tcp_port,
                self.options.door_entity,
            )
            self._loops.append(
                asyncio.create_task(self._reading_loop(), name="pantry_hub.reading")
            )
        else:
            allowed = self.options.reader_source_ip
            self.listener = await UR4UdpListener.start(
                (self.bind_host, self.options.udp_port),
                callback=self._on_tag,
                allowed_sources=[allowed] if allowed else None,
            )
            _LOGGER.info(
                "Continuous reading: listening for UR4 reads on UDP %s:%d "
                "(accepting %s)",
                self.bind_host,
                self.options.udp_port,
                allowed or "any source",
            )
        if self.door_source is not None:
            self.door_source.start()
        self._spawn(self._resolve_reader_id(), "pantry_hub.reader_id")
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
        if self.door_source is not None:
            await self.door_source.stop()
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
        await self.session.close()
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

    # Reading -------------------------------------------------------------------

    async def _resolve_reader_id(self) -> None:
        """Reader MAC from UDP 1111 announcements (else host:port), kept in the DB."""
        if self.reader_id_source == "stored" and not self.reader_id.count(":"):
            return  # a MAC-based id is already stored
        rid, source = await reader_unique_id(
            self.options.reader_host,
            self.options.reader_tcp_port,
            discover_fn=self._discover_fn,
        )
        if source == "mac" or self.reader_id_source != "stored":
            self.reader_id, self.reader_id_source = rid, source
            await self.store.set_meta("reader_id", rid)
            _LOGGER.info("Reader unique id: %s (from %s)", rid, source)
            if self.mqtt is not None and self.mqtt.connected:
                await self.publish_discovery()

    def _on_door(self, door: Door) -> None:
        """Door entity changed (open / closed / unknown)."""
        now = self.monotonic()
        self.machine.set_door(door, now)
        if self.planner is not None and self.planner.set_door(door, now):
            self._abort_window()
            self._wake.set()
        self.mark_dirty()

    def request_sweep(self) -> bool:
        """``button.pantry_hub_sweep_now``: sweep as soon as possible."""
        if self.planner is None or self.activity is Activity.SWEEP:
            return False
        self.planner.request_sweep()
        _LOGGER.info("Sweep requested (reader is %s)", self.activity.value)
        if self.activity is Activity.CONTINUOUS:
            self._abort_window()  # sweep now; continuous resumes afterwards
        self._wake.set()
        return True

    def _abort_window(self) -> None:
        if self._window is not None and not self._window.done():
            _LOGGER.info("Aborting the current %s window", self.activity.value)
            self._window.cancel()

    def _set_activity(self, activity: Activity) -> None:
        if activity is not self.activity:
            self.activity = activity
            _LOGGER.debug("Reader activity: %s", activity.value)
            self.mark_dirty()

    async def _run_window(self, duration: float) -> tuple[WindowResult, bool]:
        """Run one window; return (result, aborted_by_hub)."""
        task = asyncio.create_task(
            self.session.run_window(duration, self._on_tag), name="pantry_hub.window"
        )
        self._window = task
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._window = None
        self._last_contact = self.monotonic()
        if task.cancelled():
            return WindowResult(
                False, 0, 0.0, "aborted (door change or sweep request)"
            ), True
        return task.result(), False

    async def _idle(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), max(seconds, 0.0))

    async def _reading_loop(self) -> None:
        assert self.planner is not None
        planner = self.planner
        while True:
            try:
                plan = planner.next(self.monotonic())
                if plan.activity is Activity.IDLE:
                    self._set_activity(Activity.IDLE)
                    await self._idle_or_probe(plan.duration)
                elif plan.activity is Activity.CONTINUOUS:
                    await self._continuous(plan)
                else:
                    await self._sweep(plan)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Error in the reading loop; retrying")
                self._set_activity(Activity.IDLE)
                await self._idle(self.reading_config.retry_s)

    async def _idle_or_probe(self, seconds: float) -> None:
        """Idle; ping the reader every ``probe_every_s`` without a window."""
        due = self._last_contact + self.probe_every_s
        now = self.monotonic()
        if now >= due:
            rtt = await self.session.probe()
            self._last_contact = self.monotonic()
            if rtt is None:
                _LOGGER.info("Idle reader check failed: %s", self.session.last_error)
            else:
                _LOGGER.debug("Idle reader check: ping %.3f s", rtt)
            self.mark_dirty()
            return
        await self._idle(min(seconds, due - now))

    async def _continuous(self, plan: Plan) -> None:
        assert self.planner is not None
        self._set_activity(Activity.CONTINUOUS)
        _LOGGER.info("Door open: reading continuously for up to %.0f s", plan.duration)
        result, aborted_by_hub = await self._run_window(plan.duration)
        self.mark_dirty()
        if result.completed:
            self.planner.continuous_done()
        elif not aborted_by_hub:
            # Reader offline: don't spin; try again after retry_s (or a door event).
            self._set_activity(Activity.IDLE)
            await self._idle(self.reading_config.retry_s)

    async def _sweep(self, plan: Plan) -> None:
        assert self.planner is not None
        self._set_activity(Activity.SWEEP)
        reason = plan.reason.value if plan.reason else "scheduled"
        _LOGGER.info("Sweep (%s) for %.0f s", reason, plan.duration)
        self.machine.start_sweep()
        result, aborted_by_hub = await self._run_window(plan.duration)
        now = self.monotonic()
        completed = result.completed
        for event in self.machine.finish_sweep(completed, now):
            self._events.put_nowait(event)
        self.planner.sweep_done(completed, now)
        record = {
            "at": iso(self.wall()),
            "reason": reason,
            "completed": completed,
            "tags": result.tags,
            "error": result.error,
        }
        self.last_sweep_attempt = record
        if completed:
            self.last_sweep = record
            _LOGGER.info("Sweep done: %d tag report(s)", result.tags)
        else:
            _LOGGER.info(
                "Sweep %s; it does not count towards departures",
                "aborted" if aborted_by_hub else "failed",
            )
        self.mark_dirty()

    def _accruing(self) -> bool:
        """Whether unseen time counts now (only while reading continuously)."""
        return self.activity is Activity.CONTINUOUS

    # Tag reads -----------------------------------------------------------------

    def _on_tag(self, tag: TagRead) -> None:
        """Tag callback for UDP and TCP windows (runs in the loop; stays fast)."""
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
                for event in self.machine.tick(now, accrue=self._accruing()):
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
        """Door mode: the last window/check reached the reader.

        Continuous mode: UDP traffic (or a TCP probe answer) within
        ``reader_offline_after_s``.
        """
        if self.door_mode:
            return bool(self.session.online)
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
                "review": self._counts.get("review", 0),
                "last_error": self.worker.last_error,
            },
            "door": self.machine.door.value,
            "activity": self.activity.value,
            "last_sweep": self.last_sweep["at"] if self.last_sweep else None,
            "reading": {
                "mode": self.options.reading_mode,
                "door": self.machine.door.value,
                "door_entity": self.options.door_entity or None,
                "door_unavailable_notified": bool(
                    self.door_source and self.door_source.notified
                ),
                "sweeps_completed": self.machine.sweeps_completed,
                "sweeps_aborted": self.machine.sweeps_aborted,
                "departures_allowed": self.machine.departures_allowed,
                "last_sweep": self.last_sweep,
                "last_attempt": self.last_sweep_attempt,
                "reader_id": self.reader_id,
                "reader_connection": self.session.connection,
                "reader_error": self.session.last_error if self.door_mode else None,
            },
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
            json.dumps(discovery_payload(self.version, self.reader_id)),
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
        if topic == SWEEP_COMMAND_TOPIC:
            if not self.request_sweep():
                _LOGGER.info(
                    "Sweep now ignored (a sweep is running, or continuous mode)"
                )
            return

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

    async def relink(
        self,
        epc: str,
        *,
        barcode: str = "",
        product_id: int = 0,
        in_stock: bool = False,
    ) -> Link:
        """Link ``epc`` to a product by barcode or Grocy product id (web UI).

        ``in_stock``: Grocy already counts this unit; skip the add of the
        current (or next) arrival.
        """
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
            in_stock=in_stock,
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
        report.review_ops = [
            {
                "op_id": op.id,
                "product_id": op.product_id,
                "product": op.product_name,
                "epc": op.epc,
                "note": "consume may not have reached Grocy",
            }
            for op in await self.store.ops("failed", limit=500)
            if op.needs_review
        ]
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
                        "review_ops": report.review_ops,
                        "error": report.error,
                    }
                ),
                retain=True,
                qos=1,
            )
        return report
