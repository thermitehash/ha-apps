"""Fake UR4 (aiochainway.testing) → hub → fake Grocy, with an in-process broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from pantry_hub.discovery import (
    AVAILABILITY_TOPIC,
    DISCOVERY_TOPIC,
    EVENTS_TOPIC,
    LAST_LINK_TOPIC,
    RESYNC_COMMAND_TOPIC,
    RESYNC_RESULT_TOPIC,
    STATE_TOPIC,
)
from pantry_hub.store import Link

from .conftest import BrokerHandle, FakeClock, eventually
from .fake_grocy import FakeGrocy
from .rig import Observer, Rig

EPC_A = "E28011700000020F1A2B3C01"
EPC_B = "E28011700000020F1A2B3C02"
EPC_C = "E28011700000020F1A2B3C03"


@pytest.fixture
async def observer(broker: BrokerHandle) -> AsyncIterator[Observer]:
    obs = Observer(broker.port)
    await obs.start()
    yield obs
    await obs.stop()


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle, clock: FakeClock
) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, broker, clock)
    yield r
    await r.stop()
    r.close()


async def link(
    rig: Rig, epc: str, product_id: int, name: str, days: int | None = None
) -> None:
    assert rig.store is not None
    await rig.store.create_link(
        Link(epc, product_id, name, rig.clock(), default_best_before_days=days),
        location_id=1,
    )


async def test_arrival_and_departure_update_grocy(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    beans = grocy.add_product("Black beans", ["0001"], days=30)
    await rig.start()
    await link(rig, EPC_A, beans.id, "Black beans", days=30)
    await rig.arrive(EPC_A)
    await rig.idle()
    assert grocy.amount(beans.id) == 1
    entry = grocy.entries[0]
    assert entry.note.endswith(f"EPC {EPC_A}")
    assert entry.best_before_date == "2027-02-14"  # clock + 30 days
    # More reads while present change nothing.
    for _ in range(5):
        rig.send(EPC_A)
    await asyncio.sleep(0.1)
    assert grocy.count_calls("POST", "/add") == 1
    # Unseen for depart_after_s → departed → consumed.
    rig.clock.advance(61)
    await rig.read_until_state(EPC_A, "unknown")
    await rig.idle()
    assert grocy.amount(beans.id) == 0
    await eventually(lambda: len(observer.json(EVENTS_TOPIC)) == 2)
    kinds = [e["event_type"] for e in observer.json(EVENTS_TOPIC)]
    assert kinds == ["arrived", "departed"]
    assert observer.json(EVENTS_TOPIC)[1]["product"] == "Black beans"


async def test_unlinked_tag_counted_no_grocy_call(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    await rig.start()
    await rig.arrive(EPC_B)
    await rig.idle()
    assert grocy.count_calls("POST", "/add") == 0
    await eventually(
        lambda: (observer.last(STATE_TOPIC) or {}).get("unlinked_tags") == 1
    )
    state = observer.last(STATE_TOPIC)
    assert state["present_tags"] == 1 and state["reader_online"] is True
    ev = observer.last(EVENTS_TOPIC)
    assert ev["linked"] is False and ev["product"] is None


async def test_discovery_and_availability(rig: Rig, observer: Observer) -> None:
    await rig.start()
    await eventually(lambda: DISCOVERY_TOPIC in observer.messages)
    disc = observer.last(DISCOVERY_TOPIC)
    comps = disc["components"]
    assert {c["default_entity_id"] for c in comps.values()} == {
        "binary_sensor.pantry_hub_reader_online",
        "sensor.pantry_hub_present_tags",
        "sensor.pantry_hub_unlinked_tags",
        "sensor.pantry_hub_grocy_queue",
        "sensor.pantry_hub_last_link",
        "sensor.pantry_hub_resync_differences",
        "event.pantry_hub_item",
        "button.pantry_hub_resync",
    }
    assert observer.messages[AVAILABILITY_TOPIC][-1] == b"online"
    # HA restart → discovery republished.
    n = len(observer.messages[DISCOVERY_TOPIC])
    await observer.publish("homeassistant/status", "online")
    await eventually(lambda: len(observer.messages[DISCOVERY_TOPIC]) > n)
    await observer.publish("homeassistant/status", "offline")  # ignored
    await rig.stop()
    await eventually(lambda: observer.messages[AVAILABILITY_TOPIC][-1] == b"offline")


async def test_full_linking_flow_over_mqtt(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    rice = grocy.add_product("Rice", ["0002"])
    await rig.start()
    res_topic = "pantry_hub/linking/kitchen/result"
    await observer.publish(
        "pantry_hub/linking/kitchen/barcode", {"barcode": "0002", "ts": 1}
    )
    await asyncio.sleep(0.05)
    await observer.publish(
        "pantry_hub/linking/kitchen/tag", {"epc": EPC_A, "rssi": -61.5, "ts": 2}
    )
    await observer.publish(
        "pantry_hub/linking/kitchen/tag", {"epc": EPC_B, "rssi": -40.0, "ts": 2}
    )
    await eventually(lambda: observer.last(res_topic) is not None)
    result = observer.last(res_topic)
    assert result == {
        "status": "linked",
        "product": "Rice",
        "message": "Linked to Rice",
        "barcode": "0002",
        "epc": EPC_B,
    }
    await eventually(lambda: observer.last(LAST_LINK_TOPIC) is not None)
    assert observer.last(LAST_LINK_TOPIC)["product"] == "Rice"
    # Linking alone does not touch Grocy stock …
    assert grocy.count_calls("POST", "/add") == 0
    # … the pantry arrival does, once.
    await rig.arrive(EPC_B)
    await rig.idle()
    assert grocy.amount(rice.id) == 1

    # Already linked tag.
    await observer.publish("pantry_hub/linking/kitchen/barcode", {"barcode": "0002"})
    await asyncio.sleep(0.05)
    await observer.publish(
        "pantry_hub/linking/kitchen/tag", {"epc": EPC_B, "rssi": -40}
    )
    await eventually(lambda: len(observer.json(res_topic)) == 2)
    assert observer.last(res_topic)["status"] == "tag_already_linked"

    # Unknown barcode.
    await observer.publish("pantry_hub/linking/kitchen/barcode", {"barcode": "4242"})
    await eventually(lambda: len(observer.json(res_topic)) == 3)
    assert observer.last(res_topic)["status"] == "unknown_barcode"
    assert rig.store is not None
    assert [n.barcode for n in await rig.store.needs_product()] == ["4242"]

    # Timeout.
    await observer.publish("pantry_hub/linking/kitchen/barcode", {"barcode": "0002"})
    await eventually(lambda: len(observer.json(res_topic)) == 4, timeout=3)
    assert observer.last(res_topic)["status"] == "timeout"


async def test_linking_tag_already_in_pantry_adds_once(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    oats = grocy.add_product("Oats", ["0003"])
    await rig.start()
    await rig.arrive(EPC_C)  # unlinked arrival
    await rig.idle()
    await observer.publish("pantry_hub/linking/s1/barcode", {"barcode": "0003"})
    await asyncio.sleep(0.05)
    await observer.publish("pantry_hub/linking/s1/tag", {"epc": EPC_C, "rssi": -50})
    await eventually(lambda: observer.last("pantry_hub/linking/s1/result") is not None)
    await rig.idle()
    assert grocy.amount(oats.id) == 1
    rig.send(EPC_C)
    await asyncio.sleep(0.1)
    await rig.idle()
    assert grocy.count_calls("POST", "/add") == 1


async def test_grocy_down_queues_and_replays_in_order(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    beans = grocy.add_product("Beans")
    rice = grocy.add_product("Rice")
    await rig.start()
    await link(rig, EPC_A, beans.id, "Beans")
    await link(rig, EPC_B, rice.id, "Rice")
    grocy.down = True
    await rig.arrive(EPC_A)
    await rig.arrive(EPC_B)
    await eventually(
        lambda: (observer.last(STATE_TOPIC) or {}).get("grocy_queue") == 2, timeout=3
    )
    assert observer.last(STATE_TOPIC)["grocy"]["online"] is False
    grocy.down = False
    await rig.idle()
    # Grocy received them in arrival order, once each.
    assert [e.product_id for e in grocy.entries] == [beans.id, rice.id]
    await eventually(
        lambda: (observer.last(STATE_TOPIC) or {}).get("grocy_queue") == 0, timeout=3
    )


@pytest.mark.parametrize(
    "fault", ["500", "apply_then_500", "apply_then_hang", "timeout"]
)
async def test_grocy_faults_never_double_add(
    rig: Rig, grocy: FakeGrocy, fault: str
) -> None:
    grocy.hang_s = 1.0
    beans = grocy.add_product("Beans")
    grocy.fault("/api/stock/products/{id}/add", fault)
    await rig.start()
    await link(rig, EPC_A, beans.id, "Beans")
    await rig.arrive(EPC_A)
    await rig.idle()
    assert grocy.amount(beans.id) == 1
    rig.clock.advance(61)
    await rig.read_until_state(EPC_A, "unknown")
    grocy.fault("/api/stock/products/{id}/consume", fault)
    await rig.idle()
    assert grocy.amount(beans.id) == 0


async def test_restart_mid_arrival_and_grace_window(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle, clock: FakeClock
) -> None:
    beans = grocy.add_product("Beans")
    rig = Rig(tmp_path, grocy, broker, clock)
    try:
        await rig.start()
        await link(rig, EPC_A, beans.id, "Beans")
        await link(rig, EPC_B, beans.id, "Beans")
        await rig.arrive(EPC_A)
        rig.send(EPC_B)  # one read only: still arriving
        await rig.read_until_state(EPC_B, "arriving")
        grocy.down = True  # A's add is journaled but not yet in Grocy
        await eventually(lambda: rig.h._events.empty())
        await asyncio.sleep(0.1)
        await rig.stop()
        grocy.down = False

        clock.advance(30)
        await rig.start()
        assert rig.h.machine.state(EPC_A).value == "present"  # restored
        assert rig.h.machine.state(EPC_B).value == "unknown"  # arriving not persisted
        await rig.arrive(EPC_B)
        await rig.idle()
        assert grocy.amount(beans.id) == 2
        assert grocy.count_calls("POST", "/add") == 2
        # Restored tags do not depart before a full depart window from restart.
        clock.advance(59)
        await asyncio.sleep(0.1)
        assert rig.h.machine.state(EPC_A).value != "unknown"
        rig.send(EPC_B)  # B keeps being read; A does not
        await eventually(lambda: rig.h.machine.tracks()[EPC_B].last_seen == clock())
        clock.advance(2)
        await rig.read_until_state(EPC_A, "unknown")
        await rig.idle()
        assert grocy.amount(beans.id) == 1
    finally:
        await rig.stop()
        rig.close()


async def test_udp_garbage_and_wrong_source(rig: Rig, grocy: FakeGrocy) -> None:
    await rig.start()
    rig.send_raw(b"\x00\x01garbage")
    rig.send_raw(b"\xa5\x5a\x00\x08\x83\x00\x0d\x0a")  # bad checksum/short frame
    rig.send_raw(b"")
    await rig.arrive(EPC_A)  # still works after garbage
    assert rig.h.listener is not None
    await eventually(
        lambda: rig.h.listener is not None and rig.h.listener.stats.bad_frames >= 1
    )
    state = rig.h.state()
    assert state["reader"]["bad_frames"] >= 1


async def test_source_filter_drops_other_senders(
    tmp_path: Path, grocy: FakeGrocy, clock: FakeClock
) -> None:
    rig = Rig(tmp_path, grocy, None, clock, allowed_reader_ip="10.9.9.9")
    try:
        await rig.start()
        rig.send(EPC_A)
        await eventually(
            lambda: (
                rig.h.listener is not None and rig.h.listener.stats.dropped_sources == 1
            )
        )
        assert rig.h.machine.state(EPC_A).value == "unknown"
        assert rig.h.reader_online() is False
    finally:
        await rig.stop()
        rig.close()


async def test_door_gating_over_mqtt(
    tmp_path: Path,
    grocy: FakeGrocy,
    broker: BrokerHandle,
    clock: FakeClock,
    observer: Observer,
) -> None:
    beans = grocy.add_product("Beans")
    rig = Rig(
        tmp_path,
        grocy,
        broker,
        clock,
        depart_requires_door_closed=True,
        door_entity_topic="zigbee2mqtt/pantry_door",
    )
    try:
        await rig.start()
        await link(rig, EPC_A, beans.id, "Beans")
        await rig.arrive(EPC_A)
        await rig.idle()
        await observer.publish("zigbee2mqtt/pantry_door", {"contact": False})  # open
        await eventually(lambda: rig.h.machine.door.value == "open")
        clock.advance(120)
        await asyncio.sleep(0.1)
        assert rig.h.machine.state(EPC_A).value == "missing"
        await observer.publish("zigbee2mqtt/pantry_door", {"contact": True})  # closed
        await eventually(lambda: rig.h.machine.door.value == "closed")
        clock.advance(6)  # door_settle_s = arrive_window_s = 5
        await rig.read_until_state(EPC_A, "unknown")
        await rig.idle()
        assert grocy.amount(beans.id) == 0
        await observer.publish("zigbee2mqtt/pantry_door", "weird")
        await eventually(lambda: rig.h.machine.door.value == "unknown")
    finally:
        await rig.stop()
        rig.close()


async def test_mqtt_broker_restart_reconnects_and_republishes(
    rig: Rig, broker: BrokerHandle, grocy: FakeGrocy
) -> None:
    await rig.start()
    assert rig.h.mqtt is not None
    connects = rig.h.mqtt.connects
    # Regression: stopping the broker while discovery (QoS 1) is still being
    # published used to leave the hub "connected" until aiomqtt's 10 s timeout.
    await broker.stop()
    await eventually(
        lambda: rig.h.mqtt is not None and not rig.h.mqtt.connected, timeout=8
    )
    # Events while disconnected go to the outbox, not lost.
    await rig.arrive(EPC_A)
    await broker.start()
    await eventually(
        lambda: rig.h.mqtt is not None and rig.h.mqtt.connected, timeout=10
    )
    assert rig.h.mqtt.connects == connects + 1
    obs = Observer(broker.port)
    await obs.start()
    try:
        await eventually(
            lambda: DISCOVERY_TOPIC in obs.messages
        )  # retained, republished
        await eventually(lambda: STATE_TOPIC in obs.messages)
    finally:
        await obs.stop()


async def test_resync_reports_differences(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    beans = grocy.add_product("Beans")
    rice = grocy.add_product("Rice")
    await rig.start()
    await link(rig, EPC_A, beans.id, "Beans")
    await link(rig, EPC_B, rice.id, "Rice")
    await rig.arrive(EPC_A, EPC_B)
    await rig.idle()
    # Someone adds 2 rice by hand in Grocy; the hub only sees 1 tag.
    assert rig.client is not None
    await rig.client.add(rice.id, location_id=1, best_before=None, note="manual")
    await rig.client.add(rice.id, location_id=2, best_before=None, note="fridge")
    await observer.publish(RESYNC_COMMAND_TOPIC, "PRESS")
    await eventually(lambda: observer.last(RESYNC_RESULT_TOPIC) is not None)
    result: dict[str, Any] = observer.last(RESYNC_RESULT_TOPIC)
    assert result["checked"] == 2
    assert result["differences"] == [
        {
            "product_id": rice.id,
            "product": "Rice",
            "present_tags": 1,
            "grocy_amount": 2.0,
        }
    ]
    # Report only: nothing was corrected.
    assert grocy.amount(rice.id) == 2
    grocy.down = True
    report = await rig.h.resync()
    assert report.error is not None


async def test_relink_and_unlink(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    rice = grocy.add_product("Rice", ["0002"])
    await rig.start()
    await link(rig, EPC_A, beans.id, "Beans")
    linked = await rig.h.relink(EPC_A, barcode="0002")
    assert linked.product_id == rice.id
    linked = await rig.h.relink(EPC_A, product_id=beans.id)
    assert linked.product_name == "Beans"
    with pytest.raises(ValueError):
        await rig.h.relink(EPC_A)
    from pantry_hub.grocy import GrocyError

    with pytest.raises(GrocyError):
        await rig.h.relink(EPC_A, barcode="nope")
    assert await rig.h.unlink(EPC_A)
    assert not await rig.h.unlink(EPC_A)


async def test_tcp_probe_with_fake_ur4(
    tmp_path: Path, grocy: FakeGrocy, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiochainway.testing import FakeUR4

    import pantry_hub.hub as hub_mod

    async with FakeUR4() as fake:
        from aiochainway import UR4Client as real_client

        def client(host: str, timeout: float) -> Any:
            return real_client(fake.host, fake.port, timeout=timeout)

        monkeypatch.setattr(hub_mod, "UR4Client", client)
        rig = Rig(tmp_path, grocy, None, clock, reader_tcp_probe=True)
        try:
            await rig.start()
            await eventually(lambda: rig.h.reader.probe_version is not None)
            assert rig.h.reader_online() is True
            assert rig.h.state()["reader"]["probe_version"] == "V1.2.3"
        finally:
            await rig.stop()
            rig.close()
        fake.faults.drop = {"get_version"}
        rig = Rig(tmp_path, grocy, None, clock, reader_tcp_probe=True)
        try:
            monkeypatch.setattr(hub_mod, "PROBE_EVERY_S", 0.05)
            await rig.start()
            await eventually(lambda: rig.h.reader.probe_error is not None, timeout=10)
            assert rig.h.reader_online() is False
        finally:
            await rig.stop()
            rig.close()


async def test_state_not_republished_for_traffic_counters_alone(
    rig: Rig, observer: Observer
) -> None:
    """Regression: every datagram used to republish the retained state."""
    await rig.start()
    await rig.arrive(EPC_A)
    await eventually(
        lambda: (observer.last(STATE_TOPIC) or {}).get("present_tags") == 1
    )
    n = len(observer.messages[STATE_TOPIC])
    for _ in range(20):
        rig.send(EPC_A)
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    assert len(observer.messages[STATE_TOPIC]) == n
    rig.clock.advance(61)  # but counters still go out once a minute
    rig.send(EPC_A)
    await eventually(lambda: len(observer.messages[STATE_TOPIC]) > n)
