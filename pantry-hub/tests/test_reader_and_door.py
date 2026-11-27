"""ReaderSession (aiochainway.testing.FakeUR4), MAC unique id, HA door source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from aiochainway import DiscoveredReader, TagRead, UR4Client, WorkMode
from aiochainway.testing import FakeUR4, ScriptedTag, tag_record
import pytest

from pantry_hub.homeassistant import NOTIFICATION_ID, HomeAssistantDoor
from pantry_hub.presence import Door
from pantry_hub.reader import ReaderSession, reader_unique_id

from .conftest import eventually
from .fake_ha import TOKEN, FakeHomeAssistant

EPC = "E28011700000020F1A2B3C01"
DOOR = "binary_sensor.pantry_door"


@pytest.fixture
async def fake() -> AsyncIterator[FakeUR4]:
    async with FakeUR4() as f:
        f.tags = [tag_record(EPC, rssi_dbm=-50)]
        f.tag_interval = 0.01
        yield f


def session_for(fake: FakeUR4) -> ReaderSession:
    return ReaderSession(
        "192.168.99.202",
        client_factory=lambda h, p, **kw: UR4Client(fake.host, fake.port, **kw),
        timeout=1.0,
    )


async def test_window_completes_and_switches_to_command_mode(fake: FakeUR4) -> None:
    fake.state.work_mode = int(WorkMode.AUTO)
    s = session_for(fake)
    seen: list[TagRead] = []
    result = await s.run_window(0.2, seen.append)
    assert result.completed and result.tags == len(seen) > 3
    assert {t.epc for t in seen} == {EPC}
    assert fake.state.work_mode == int(WorkMode.COMMAND)  # never trigger mode
    assert "set_work_mode" in fake.names()
    assert s.online is True and s.version == "V1.2.3"
    assert fake.names().count("start_inventory") == 1
    assert "stop_inventory" in fake.names()


async def test_already_in_command_mode_is_left_alone(fake: FakeUR4) -> None:
    s = session_for(fake)
    await s.run_window(0.05, lambda tag: None)
    assert "set_work_mode" not in fake.names()


async def test_reader_drop_mid_window_aborts(fake: FakeUR4) -> None:
    fake.faults.disconnect_after_tags = 3
    s = session_for(fake)
    result = await s.run_window(2.0, lambda tag: None)
    assert not result.completed
    assert result.error and s.online is False
    assert not s.mode_checked  # checked again once the reader is back


async def test_reader_unreachable_aborts() -> None:
    s = ReaderSession("127.0.0.1", 9, timeout=0.5)
    result = await s.run_window(0.5, lambda tag: None)
    assert not result.completed and s.online is False


async def test_cancelled_window_stops_the_reader(fake: FakeUR4) -> None:
    s = session_for(fake)
    task = asyncio.create_task(s.run_window(5.0, lambda tag: None))
    await eventually(lambda: "start_inventory" in fake.names())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await eventually(lambda: fake.names().count("stop_inventory") >= 2)


async def test_explicit_empty_result_is_a_completed_window(fake: FakeUR4) -> None:
    """The reader answers but no tag is in the field: still a completed sweep."""
    fake.scenario = [ScriptedTag(EPC, present_from=1e9)]
    s = session_for(fake)
    result = await s.run_window(0.1, lambda tag: None)
    assert result.completed and result.tags == 0
    assert result.end_reason == "duration_elapsed"


async def test_unique_id_from_discovered_mac() -> None:
    async def found(timeout: float) -> list[Any]:
        return [
            DiscoveredReader(mac="11:22:33:44:55:66", ip="10.0.0.9", port=8888),
            DiscoveredReader(mac="AA:BB:CC:DD:EE:FF", ip="192.168.99.202", port=8888),
        ]

    assert await reader_unique_id("192.168.99.202", 8888, discover_fn=found) == (
        "ur4_aabbccddeeff",
        "mac",
    )


async def test_unique_id_falls_back_to_host_port() -> None:
    async def nothing(timeout: float) -> list[Any]:
        return []

    async def port_busy(timeout: float) -> list[Any]:
        raise OSError("address in use")

    for fn in (nothing, port_busy):
        assert await reader_unique_id("192.168.99.202", 8888, discover_fn=fn) == (
            "ur4_192.168.99.202:8888",
            "host",
        )


async def test_real_discover_listens_briefly() -> None:
    _rid, source = await reader_unique_id("10.9.9.9", 8888, timeout=0.05)
    assert source == "host"


# Home Assistant door ----------------------------------------------------------


async def door_source(
    ha: FakeHomeAssistant, notify_after_s: float = 0.2
) -> tuple[HomeAssistantDoor, list[Door]]:
    changes: list[Door] = []
    d = HomeAssistantDoor(
        DOOR,
        TOKEN,
        changes.append,
        url=ha.url,
        notify_after_s=notify_after_s,
        min_backoff=0.05,
        max_backoff=0.1,
    )
    d.start()
    return d, changes


async def test_door_states_from_home_assistant(ha: FakeHomeAssistant) -> None:
    await ha.set_state(DOOR, "off")
    d, changes = await door_source(ha, notify_after_s=10)
    try:
        await eventually(lambda: d.door is Door.CLOSED)
        await ha.set_state(DOOR, "on")
        await eventually(lambda: d.door is Door.OPEN)
        await ha.set_state(DOOR, "unavailable")
        await eventually(lambda: d.door is Door.UNKNOWN)
        await ha.set_state(DOOR, "off")
        await eventually(lambda: d.door is Door.CLOSED)
        await ha.set_state(DOOR, None)  # entity removed
        await eventually(lambda: d.door is Door.UNKNOWN)
        assert changes == [
            Door.CLOSED,
            Door.OPEN,
            Door.UNKNOWN,
            Door.CLOSED,
            Door.UNKNOWN,
        ]
    finally:
        await d.stop()


async def test_missing_entity_is_unavailable_and_notifies(
    ha: FakeHomeAssistant,
) -> None:
    d, _ = await door_source(ha)
    try:
        await eventually(lambda: d.connected)
        await eventually(lambda: bool(ha.calls("persistent_notification", "create")))
        call = ha.calls("persistent_notification", "create")[0]
        assert call["notification_id"] == NOTIFICATION_ID and DOOR in call["message"]
        assert d.door is Door.UNKNOWN and d.notified
        await ha.set_state(DOOR, "off")  # recovers
        await eventually(lambda: bool(ha.calls("persistent_notification", "dismiss")))
        assert not d.notified
    finally:
        await d.stop()


async def test_short_unavailability_does_not_notify(ha: FakeHomeAssistant) -> None:
    await ha.set_state(DOOR, "on")
    d, _ = await door_source(ha, notify_after_s=0.3)
    try:
        await eventually(lambda: d.door is Door.OPEN)
        await ha.set_state(DOOR, "unavailable")
        await asyncio.sleep(0.1)
        await ha.set_state(DOOR, "on")
        await asyncio.sleep(0.4)
        assert ha.calls("persistent_notification", "create") == []
    finally:
        await d.stop()


async def test_websocket_drop_is_unavailable_never_open(ha: FakeHomeAssistant) -> None:
    await ha.set_state(DOOR, "on")
    d, _changes = await door_source(ha, notify_after_s=10)
    try:
        await eventually(lambda: d.door is Door.OPEN)
        await ha.drop()
        await eventually(lambda: d.door is Door.UNKNOWN)
        await eventually(lambda: d.door is Door.OPEN, timeout=5)  # reconnected
        assert ha.connections >= 2
    finally:
        await d.stop()


async def test_bad_token_keeps_door_unavailable(ha: FakeHomeAssistant) -> None:
    ha.reject_auth = True
    await ha.set_state(DOOR, "on")
    d, changes = await door_source(ha, notify_after_s=10)
    try:
        await eventually(lambda: ha.connections >= 2)
        assert d.door is Door.UNKNOWN and changes == []
    finally:
        await d.stop()
