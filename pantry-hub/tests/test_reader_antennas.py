"""v0.3.3: reader antenna mask + power enforced before any inventory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiochainway import UR4Client
from aiochainway.testing import FakeUR4, ReaderState, tag_record
import pytest

from pantry_hub.config import ConfigError, parse_options
from pantry_hub.homeassistant import READER_CONFIG_NOTIFICATION_ID
from pantry_hub.reader import ReaderSession
from pantry_hub.reader_config import ReaderConfig, ensure_config

from .conftest import eventually
from .fake_grocy import FakeGrocy
from .fake_ha import FakeHomeAssistant
from .test_config import GOOD
from .test_door_reading import DOOR, DoorRig

EPC = "E28011700000020F1A2B3C33"
WANT = ReaderConfig.of([1], 27)


class StuckState(ReaderState):
    """A reader that acknowledges antenna/power writes but ignores them."""

    stuck = False

    def __setattr__(self, name: str, value: Any) -> None:
        if self.stuck and name == "antenna_mask":
            return
        super().__setattr__(name, value)


@pytest.fixture
async def fake() -> AsyncIterator[FakeUR4]:
    async with FakeUR4() as f:  # factory defaults: antennas 1-4 at 30 dBm
        f.tags = [tag_record(EPC, rssi_dbm=-50)]
        f.tag_interval = 0.01
        yield f


def session_for(
    fake: FakeUR4, config: ReaderConfig | None = WANT, **kwargs: Any
) -> ReaderSession:
    return ReaderSession(
        "192.168.99.202",
        client_factory=lambda h, p, **kw: UR4Client(fake.host, fake.port, **kw),
        timeout=1.0,
        config=config,
        **kwargs,
    )


def writes(fake: FakeUR4) -> list[str]:
    return [n for n in fake.names() if n in ("set_antennas", "set_power")]


async def test_factory_default_mask_corrected_before_inventory(fake: FakeUR4) -> None:
    assert fake.state.antenna_mask == 0x0F
    s = session_for(fake)
    result = await s.run_window(0.1, lambda tag: None)
    assert result.completed
    assert fake.state.antenna_mask == 0x01
    assert fake.state.power[1] == 27
    assert fake.state.power[2] == 30  # disabled ports are left alone
    names = fake.names()
    # read, write, read back: all before the inventory starts
    first_start = names.index("start_inventory")
    assert names.index("get_antennas") < names.index("set_antennas") < first_start
    assert names.index("set_power") < first_start
    assert names[:first_start].count("get_antennas") == 2  # verified
    assert writes(fake) == ["set_antennas", "set_power"]
    assert s.config_writes == 2
    assert s.antennas_enabled == [1] and s.power_dbm == 27
    assert s.config_applied is not None and s.last_config_error is None


async def test_matching_reader_gets_zero_writes(fake: FakeUR4) -> None:
    fake.state.antenna_mask = 0x03
    fake.state.power = {1: 25, 2: 25, 3: 30, 4: 30}
    s = session_for(fake, ReaderConfig.of([2, 1], 25))
    for _ in range(3):  # per_window: verified on every new connection
        assert (await s.run_window(0.05, lambda tag: None)).completed
    assert writes(fake) == []
    assert s.config_writes == 0
    assert fake.names().count("get_antennas") == 3
    assert s.antennas_enabled == [1, 2] and s.power_dbm == 25


async def test_only_wrong_power_is_written(fake: FakeUR4) -> None:
    fake.state.antenna_mask = 0x03
    fake.state.power = {1: 27, 2: 20, 3: 30, 4: 30}
    applied = await _ensure(fake, ReaderConfig.of([1, 2], 27))
    assert writes(fake) == ["set_power"]  # antennas already right
    assert applied.writes == 1 and fake.state.power[2] == 27


async def _ensure(fake: FakeUR4, want: ReaderConfig) -> Any:
    async with UR4Client(fake.host, fake.port, timeout=1.0) as client:
        return await ensure_config(client, want)


async def test_verify_mismatch_means_no_inventory(fake: FakeUR4) -> None:
    fake.state = StuckState()
    fake.state.stuck = True
    s = session_for(fake)
    result = await s.run_window(0.5, lambda tag: None)
    assert not result.completed and result.end_reason == "config_failed"
    assert "start_inventory" not in fake.names()
    assert s.online is False
    assert s.last_config_error is not None
    assert "read back antennas [1, 2, 3, 4], wanted [1]" in s.last_config_error
    assert s.antennas_enabled is None


async def test_set_failure_means_no_inventory(fake: FakeUR4) -> None:
    fake.faults.fail = {"set_antennas"}
    s = session_for(fake)
    result = await s.run_window(0.5, lambda tag: None)
    assert result.end_reason == "config_failed" and s.online is False
    assert "start_inventory" not in fake.names()
    fake.faults.fail = set()
    assert (await s.run_window(0.05, lambda tag: None)).completed  # retried
    assert fake.state.antenna_mask == 0x01 and s.last_config_error is None


async def test_reconnect_reapplies_persistent(fake: FakeUR4) -> None:
    s = session_for(fake, connection="persistent")
    try:
        assert (await s.run_window(0.05, lambda tag: None)).completed
        assert fake.state.antenna_mask == 0x01
        # The reader is reset to factory defaults and the connection drops.
        fake.state.antenna_mask = 0x0F
        await fake.drop_connections()
        await fake.wait_for_connections(2, timeout=5)  # auto_reconnect
        assert (await s.run_window(0.05, lambda tag: None)).completed
        assert fake.state.antenna_mask == 0x01
        assert fake.names().count("set_antennas") == 2
        # Same connection, nothing changed: no further reads or writes.
        reads = fake.names().count("get_antennas")
        assert (await s.run_window(0.05, lambda tag: None)).completed
        assert fake.names().count("get_antennas") == reads
    finally:
        await s.close()


async def test_new_connection_reapplies_per_window(fake: FakeUR4) -> None:
    s = session_for(fake)
    assert (await s.run_window(0.05, lambda tag: None)).completed
    fake.state.antenna_mask = 0x0F  # changed behind our back (reboot, other tool)
    assert (await s.run_window(0.05, lambda tag: None)).completed
    assert fake.state.antenna_mask == 0x01
    assert fake.names().count("set_antennas") == 2


async def test_probe_never_configures(fake: FakeUR4) -> None:
    s = session_for(fake)
    assert await s.probe() is not None
    assert "get_antennas" not in fake.names() and writes(fake) == []


async def test_no_config_leaves_reader_alone(fake: FakeUR4) -> None:
    s = session_for(fake, config=None)
    assert (await s.run_window(0.05, lambda tag: None)).completed
    assert "get_antennas" not in fake.names() and fake.state.antenna_mask == 0x0F


# Options -------------------------------------------------------------------------


def test_options_defaults_and_validation() -> None:
    o = parse_options(GOOD, env={})
    assert o.reader_antennas == (1,) and o.reader_power_dbm == 27
    o = parse_options({**GOOD, "reader_antennas": [2, 1, 2]}, env={})
    assert o.reader_antennas == (1, 2)
    for bad, fragment in [
        ([], "non-empty"),
        ([5], "not an antenna port"),
        ([0], "not an antenna port"),
        ([True], "not an antenna port"),
        ("1", "non-empty list"),
    ]:
        with pytest.raises(ConfigError, match=fragment):
            parse_options({**GOOD, "reader_antennas": bad}, env={})
    for bad in (4, 31):
        with pytest.raises(ConfigError, match="reader_power_dbm"):
            parse_options({**GOOD, "reader_power_dbm": bad}, env={})


def test_power_over_cap_refuses_to_start() -> None:
    with pytest.raises(ConfigError) as err:
        parse_options(
            {**GOOD, "reader_power_dbm": 27, "max_reader_power_dbm": 20}, env={}
        )
    assert "reader_power_dbm 27 is above max_reader_power_dbm 20" in str(err.value)
    ok = parse_options(
        {**GOOD, "reader_power_dbm": 20, "max_reader_power_dbm": 20}, env={}
    )
    assert ok.reader_power_dbm == 20


def test_schema_has_the_new_options() -> None:
    text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    schema = text[text.index("\nschema:") :]
    assert "  reader_antennas:\n    - int(1,4)\n" in schema
    assert "  reader_power_dbm: int(5,30)\n" in schema
    options = text[text.index("\noptions:") : text.index("\nschema:")]
    assert "  reader_antennas:\n    - 1\n" in options
    assert "  reader_power_dbm: 27\n" in options


# Hub -----------------------------------------------------------------------------


async def test_hub_notifies_blocks_and_recovers(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    fake.state = StuckState()
    fake.state.stuck = True
    await ha.set_state(DOOR, "off")
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start()
        await eventually(
            lambda: any(
                c.get("notification_id") == READER_CONFIG_NOTIFICATION_ID
                for c in ha.calls("persistent_notification", "create")
            )
        )
        note = next(
            c
            for c in ha.calls("persistent_notification", "create")
            if c.get("notification_id") == READER_CONFIG_NOTIFICATION_ID
        )
        assert note["title"] == (
            "Pantry Hub: Reader antenna settings could not be applied"
        )
        assert "every enabled port has an antenna" in note["message"]
        assert "start_inventory" not in fake.names()
        assert rig.h.reader_online() is False
        reading = rig.h.state()["reading"]
        assert reading["last_config_error"] and reading["config_applied"] is None
        assert rig.h.machine.sweeps_completed == 0
        # The reader starts accepting the settings: next retry applies them.
        fake.state.stuck = False
        await rig.sweeps(1, timeout=8)
        assert fake.state.antenna_mask == 0x01
        await eventually(
            lambda: (
                {"notification_id": READER_CONFIG_NOTIFICATION_ID}
                in ha.calls("persistent_notification", "dismiss")
            )
        )
        reading = rig.h.state()["reading"]
        assert reading["antennas_enabled"] == [1]
        assert reading["reader_power_dbm"] == 27
        assert reading["config_applied"] is not None
        assert reading["last_config_error"] is None
        assert rig.h.reader_online() is True
    finally:
        await rig.stop()


async def test_hub_continuous_mode_never_configures(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start(reading_mode="continuous", door_entity="", udp_port=0)
        assert rig.h.session.config is None
        await asyncio.sleep(0.2)
        assert writes(fake) == []
    finally:
        await rig.stop()
