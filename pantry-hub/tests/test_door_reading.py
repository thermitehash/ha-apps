"""Door-driven reading end to end: Hub + aiochainway FakeUR4 (TCP, command
mode) + fake Home Assistant door entity + fake Grocy (+ broker).

Real time with short windows (sweep_s 0.3 s, intervals ~1 s).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
from pathlib import Path
from typing import Any

from aiochainway import DiscoveredReader, UR4Client, WorkMode
from aiochainway.testing import FakeUR4, tag_record
import pytest

from pantry_hub.config import MqttSettings
from pantry_hub.discovery import DISCOVERY_TOPIC, STATE_TOPIC, SWEEP_COMMAND_TOPIC
from pantry_hub.grocy import GrocyClient
from pantry_hub.homeassistant import NOTIFICATION_ID
from pantry_hub.hub import Hub
from pantry_hub.reading import Activity
from pantry_hub.store import Link, Store

from .conftest import BrokerHandle, eventually, make_options
from .fake_grocy import API_KEY, FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .rig import Observer

DOOR = "binary_sensor.pantry_door"
EPC = "E28011700000020F1A2B3C01"
OTHER = "E28011700000020F1A2B3CFF"


class DoorRig:
    def __init__(
        self, tmp: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
    ) -> None:
        self.tmp, self.grocy, self.ha, self.fake = tmp, grocy, ha, fake
        self.hub: Hub | None = None
        self.store: Store | None = None
        self.client: GrocyClient | None = None

    @property
    def h(self) -> Hub:
        assert self.hub is not None
        return self.hub

    async def start(
        self,
        broker: BrokerHandle | None = None,
        discover: Callable[[float], Awaitable[list[Any]]] | None = None,
        probe_every_s: float = 600.0,
        **opts: Any,
    ) -> Hub:
        base: dict[str, Any] = {
            "grocy_url": self.grocy.url,
            "reading_mode": "door",
            "door_entity": DOOR,
            "arrive_reads": 2,
            "arrive_window_s": 2.0,
            "depart_after_s": 30.0,
            "open_continuous_s": 60.0,
            "open_sweep_interval_s": 1.0,
            "sweep_s": 0.3,
            "closed_sweep_interval_s": 60.0,
            "depart_after_missed_sweeps": 2,
        }
        base.update(opts)
        self.store = await Store.open(self.tmp / "hub.db")
        self.client = GrocyClient(self.grocy.url, API_KEY, timeout=0.5)

        async def no_discovery(timeout: float) -> list[Any]:
            return []

        fake = self.fake
        self.hub = Hub(
            make_options(**base),
            self.store,
            self.client,
            mqtt_settings=MqttSettings("127.0.0.1", broker.port) if broker else None,
            version="0.3.0-test",
            tick_s=0.02,
            bind_host="127.0.0.1",
            ha_url=self.ha.url,
            ha_token=TOKEN,
            client_factory=lambda h, p, **kw: UR4Client(
                fake.host, fake.port, **(kw | {"timeout": 1.0})
            ),
            probe_every_s=probe_every_s,
            discover_fn=discover or no_discovery,
            retry_s=0.3,
            door_notify_after_s=0.3,
        )
        self.hub.worker.backoff.base = 0.01
        self.hub.worker.backoff.cap = 0.05
        if self.hub.mqtt is not None:
            self.hub.mqtt.min_backoff = 0.05
        if self.hub.door_source is not None:
            self.hub.door_source.min_backoff = 0.05
        await self.hub.start()
        return self.hub

    async def stop(self) -> None:
        if self.hub is not None:
            await self.hub.stop()
            self.hub = None
        if self.store is not None:
            await self.store.close()
            self.store = None
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def link(self, epc: str, product_id: int) -> None:
        assert self.store is not None
        await self.store.create_link(Link(epc, product_id, "Beans", 0.0), location_id=1)

    def show(self, *epcs: str) -> None:
        self.fake.tags = [tag_record(e, rssi_dbm=-50) for e in epcs]

    async def sweeps(self, n: int, timeout: float = 8.0) -> None:
        await eventually(lambda: self.h.machine.sweeps_completed >= n, timeout=timeout)

    async def settled(self) -> None:
        await self.h.drain_events()
        assert self.store is not None
        store = self.store

        async def empty() -> bool:
            c = await store.counts()
            return c["pending"] == 0 and c["verifying"] == 0

        await eventually(empty)


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> AsyncIterator[DoorRig]:
    r = DoorRig(tmp_path, grocy, ha, fake)
    yield r
    await r.stop()


async def test_startup_sweep_then_closed_schedule(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "off")
    await rig.start(closed_sweep_interval_s=1.0)
    await rig.sweeps(1)
    assert rig.h.last_sweep is not None and rig.h.last_sweep["reason"] == "startup"
    assert not rig.h.machine.departures_allowed  # restart rule: needs 2
    await eventually(lambda: rig.h.activity is Activity.IDLE)
    await rig.sweeps(2, timeout=5)  # closed_sweep_interval_s later
    assert rig.h.machine.sweeps_completed >= 2  # departures now allowed
    assert rig.fake.names().count("start_inventory") >= 2
    assert rig.h.state()["reading"]["sweeps_completed"] >= 2


async def test_switches_reader_to_command_mode(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    rig.fake.state.work_mode = int(WorkMode.AUTO)
    await ha.set_state(DOOR, "off")
    await rig.start()
    await rig.sweeps(1)
    assert rig.fake.state.work_mode == int(WorkMode.COMMAND)


async def test_open_reads_continuously_arrival_then_close_sweep(
    rig: DoorRig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    await rig.start()
    await rig.link(EPC, beans.id)
    await rig.sweeps(1)
    rig.show(EPC, OTHER)
    await ha.set_state(DOOR, "on")
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
    await eventually(lambda: rig.h.machine.state(EPC).value == "present")
    await rig.settled()
    assert grocy.amount(beans.id) == 1
    starts = rig.fake.names().count("start_inventory")
    await ha.set_state(DOOR, "off")
    await eventually(
        lambda: (
            rig.h.last_sweep is not None and rig.h.last_sweep["reason"] == "door_closed"
        )
    )
    await eventually(lambda: rig.h.activity is Activity.IDLE)
    assert rig.fake.names().count("start_inventory") == starts + 1  # the close sweep
    assert rig.fake.names()[-1] == "stop_inventory"  # stopped after it


async def test_departure_after_two_missed_completed_sweeps(
    rig: DoorRig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    rig.show(EPC)
    await rig.start(closed_sweep_interval_s=0.8)
    await rig.link(EPC, beans.id)
    await eventually(lambda: rig.h.machine.state(EPC).value == "present")
    await rig.sweeps(2)  # restart rule satisfied
    await rig.settled()
    assert grocy.amount(beans.id) == 1
    rig.show(OTHER)  # item taken out
    done = rig.h.machine.sweeps_completed
    await rig.sweeps(done + 1)
    assert rig.h.machine.state(EPC).value == "missing"  # one miss is not enough
    assert rig.h.machine.tracks()[EPC].missed == 1
    await rig.sweeps(done + 2)
    await eventually(lambda: rig.h.machine.state(EPC).value == "unknown")
    await rig.settled()
    assert grocy.amount(beans.id) == 0


async def test_reader_drop_mid_sweep_never_counts_as_a_miss(
    rig: DoorRig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    rig.show(EPC)
    await rig.start(closed_sweep_interval_s=0.8)
    await rig.link(EPC, beans.id)
    await rig.sweeps(2)
    await rig.settled()
    rig.show(OTHER)
    rig.fake.faults.disconnect_after_tags = 2  # reader drops during every sweep
    aborted = rig.h.machine.sweeps_aborted
    await eventually(lambda: rig.h.machine.sweeps_aborted >= aborted + 3, timeout=10)
    assert rig.h.machine.state(EPC).value == "present"
    assert rig.h.machine.tracks()[EPC].missed == 0
    assert rig.h.reader_online() is False
    assert (
        rig.h.last_sweep_attempt is not None
        and not rig.h.last_sweep_attempt["completed"]
    )
    rig.fake.faults.disconnect_after_tags = None  # reader back
    await eventually(lambda: rig.h.machine.state(EPC).value == "unknown", timeout=10)
    assert rig.h.reader_online() is True
    await rig.settled()
    assert grocy.amount(beans.id) == 0


async def test_door_flapping(
    rig: DoorRig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    rig.show(EPC)
    await rig.start()
    await rig.link(EPC, beans.id)
    await rig.sweeps(1)
    await eventually(lambda: rig.h.machine.state(EPC).value == "present")
    for _ in range(10):
        await ha.set_state(DOOR, "on")
        await asyncio.sleep(0.03)
        await ha.set_state(DOOR, "off")
        await asyncio.sleep(0.03)
    await eventually(
        lambda: (
            rig.h.activity is Activity.IDLE
            and rig.h.last_sweep is not None
            and rig.h.last_sweep["reason"] == "door_closed"
        ),
        timeout=8,
    )
    assert rig.h.machine.state(EPC).value == "present"  # flapping never departs it
    await rig.settled()
    assert grocy.amount(beans.id) == 1
    assert rig.h.machine.sweeps_aborted >= 1  # the interrupted windows


async def test_door_unavailable_falls_back_and_notifies(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "on")
    await rig.start(closed_sweep_interval_s=0.8)
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS, timeout=5)
    await ha.set_state(DOOR, "unavailable")
    await eventually(lambda: rig.h.activity is not Activity.CONTINUOUS)
    await eventually(lambda: bool(ha.calls("persistent_notification", "create")))
    assert (
        ha.calls("persistent_notification", "create")[0]["notification_id"]
        == NOTIFICATION_ID
    )
    assert rig.h.state()["reading"]["door_unavailable_notified"] is True
    # Falls back to closed-interval sweeps, never continuous.
    done = rig.h.machine.sweeps_completed
    await rig.sweeps(done + 2, timeout=8)
    await asyncio.sleep(0.2)
    assert rig.h.activity is not Activity.CONTINUOUS
    await ha.set_state(DOOR, "off")
    await eventually(lambda: bool(ha.calls("persistent_notification", "dismiss")))


async def test_sweep_now_button_and_entities(
    rig: DoorRig, ha: FakeHomeAssistant, broker: BrokerHandle
) -> None:
    async def mac(timeout: float) -> list[Any]:
        return [DiscoveredReader(mac="AA:BB:CC:DD:EE:01", ip="127.0.0.1", port=8888)]

    obs = Observer(broker.port)
    await obs.start()
    try:
        await ha.set_state(DOOR, "off")
        await rig.start(broker=broker, discover=mac)
        await rig.sweeps(1)
        await eventually(lambda: rig.h.reader_id == "ur4_aabbccddee01")
        await eventually(
            lambda: (
                "chainway_ur4_aabbccddee01"
                in (obs.last(DISCOVERY_TOPIC) or {})
                .get("device", {})
                .get("identifiers", [])
            )
        )
        comps = obs.last(DISCOVERY_TOPIC)["components"]
        assert (
            comps["reader_activity"]["default_entity_id"]
            == "sensor.pantry_hub_reader_activity"
        )
        assert comps["last_sweep"]["device_class"] == "timestamp"
        assert comps["sweep_now"]["command_topic"] == SWEEP_COMMAND_TOPIC
        done = rig.h.machine.sweeps_completed
        await eventually(lambda: rig.h.activity is Activity.IDLE)
        await obs.publish(SWEEP_COMMAND_TOPIC, "PRESS")
        await rig.sweeps(done + 1)
        assert (
            rig.h.last_sweep is not None and rig.h.last_sweep["reason"] == "requested"
        )
        await eventually(
            lambda: (obs.last(STATE_TOPIC) or {}).get("last_sweep") is not None
        )
        state = obs.last(STATE_TOPIC)
        assert state["activity"] in ("idle", "sweep", "continuous")
        assert state["reading"]["reader_id"] == "ur4_aabbccddee01"
        assert rig.store is not None
        assert await rig.store.get_meta("reader_id") == "ur4_aabbccddee01"
    finally:
        await obs.stop()


async def test_sweep_now_during_continuous_then_resumes(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "on")
    await rig.start()
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS, timeout=5)
    done = rig.h.machine.sweeps_completed
    assert rig.h.request_sweep()
    await rig.sweeps(done + 1)
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
    assert rig.h.last_sweep is not None and rig.h.last_sweep["reason"] == "requested"


async def test_restart_needs_two_completed_sweeps(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    first = DoorRig(tmp_path, grocy, ha, fake)
    first.show(EPC)
    await first.start()
    await first.link(EPC, beans.id)
    await eventually(lambda: first.h.machine.state(EPC).value == "present")
    await first.settled()
    await first.stop()

    first.show(OTHER)  # taken out while the hub was down
    second = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await second.start(closed_sweep_interval_s=0.8)
        assert second.h.machine.state(EPC).value == "present"  # restored
        await second.sweeps(1)  # startup sweep: missing, but no departure yet
        assert second.h.machine.state(EPC).value == "missing"
        assert not second.h.machine.departures_allowed
        await second.sweeps(2)
        await eventually(lambda: second.h.machine.state(EPC).value == "unknown")
        await second.settled()
        assert grocy.amount(beans.id) == 0
    finally:
        await second.stop()


async def test_unseen_timer_suspended_while_idle_runs_while_continuous(
    rig: DoorRig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans")
    await ha.set_state(DOOR, "off")
    rig.show(EPC)
    await rig.start(
        arrive_window_s=0.4,
        depart_after_s=1.5,
        depart_after_missed_sweeps=20,  # only the timer can depart it here
        closed_sweep_interval_s=60.0,
    )
    await rig.link(EPC, beans.id)
    await eventually(lambda: rig.h.machine.state(EPC).value == "present")
    await rig.sweeps(1)
    await eventually(lambda: rig.h.activity is Activity.IDLE)
    assert rig.h.request_sweep()
    await rig.sweeps(2)
    rig.show(OTHER)
    await eventually(lambda: rig.h.activity is Activity.IDLE)
    await asyncio.sleep(2.5)  # idle, door closed: the timer must not run
    assert rig.h.machine.state(EPC).value == "present"
    await ha.set_state(DOOR, "on")  # continuous window: now it runs
    await eventually(lambda: rig.h.machine.state(EPC).value == "unknown", timeout=6)
    await rig.settled()
    assert grocy.amount(beans.id) == 0


async def test_state_json_is_serialisable(rig: DoorRig, ha: FakeHomeAssistant) -> None:
    await ha.set_state(DOOR, "off")
    await rig.start()
    await rig.sweeps(1)
    json.dumps(rig.h.state())


async def test_idle_probe_keeps_reader_online_between_windows(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    """Idle with the door closed: connect + ping + disconnect each period."""
    await ha.set_state(DOOR, "off")
    await rig.start(probe_every_s=0.3, closed_sweep_interval_s=60.0)
    await rig.sweeps(1)
    await eventually(lambda: rig.h.activity is Activity.IDLE)
    starts = rig.fake.names().count("start_inventory")
    pings = rig.fake.names().count("get_version")
    conns = rig.fake.connections
    await eventually(lambda: rig.fake.names().count("get_version") >= pings + 2)
    await rig.fake.wait_for_connections(conns + 2)
    assert rig.fake.names().count("start_inventory") == starts  # no window ran
    assert rig.h.reader_online()
    assert rig.h.state()["reading"]["reader_connection"] == "per_window"


async def test_idle_probe_skipped_when_windows_ran(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    """Sweeps more often than the probe period: no probe is ever needed."""
    await ha.set_state(DOOR, "off")
    await rig.start(probe_every_s=1.5, closed_sweep_interval_s=0.4)
    await rig.sweeps(4)
    # Command mode was checked once (one get_version); a probe would add more.
    assert rig.fake.names().count("get_version") == 1


async def test_persistent_connection_option(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "off")
    await rig.start(
        reader_connection="persistent", sweep_s=0.2, closed_sweep_interval_s=0.4
    )
    await rig.sweeps(3)
    await rig.fake.wait_for_connections(1)
    assert rig.fake.connections == 1  # one connection across every sweep
    assert rig.h.session.connection == "persistent"
