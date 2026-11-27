"""v0.3.4: dead antenna port warning from per-antenna read counts."""

from __future__ import annotations

from pathlib import Path

from aiochainway.testing import FakeUR4, ScriptedTag
import pytest

from pantry_hub.dead_ports import DeadPortMonitor, message
from pantry_hub.homeassistant import DEAD_PORT_NOTIFICATION_ID
from pantry_hub.reading import Activity

from .conftest import eventually
from .fake_grocy import FakeGrocy
from .fake_ha import FakeHomeAssistant
from .test_door_reading import DOOR, DoorRig

EPC1 = "E28011700000020F1A2B3D01"
EPC2 = "E28011700000020F1A2B3D02"

# Unit ----------------------------------------------------------------------------


def test_two_ports_flagged_at_sweep_5_not_4() -> None:
    m = DeadPortMonitor((1, 2))
    for n in range(1, 5):
        assert m.sweep_completed({1: 3}, present_at_start=0) == (set(), set())
        assert m.dead == set(), n
    assert m.sweep_completed({1: 3}, present_at_start=0) == ({2}, set())
    assert m.dead == {2}
    assert m.last_sweep == {1: 3, 2: 0}
    # Still dead on the next sweep: reported once, not again.
    assert m.sweep_completed({1: 3}, present_at_start=0) == (set(), set())


def test_other_ports_need_ten_reads_in_total() -> None:
    m = DeadPortMonitor((1, 2))
    for count in (2, 2, 2, 2, 1):  # 9 in total
        m.sweep_completed({1: count}, 0)
    assert m.dead == set()
    m.sweep_completed({1: 3}, 0)  # window is now 2+2+2+1+3 = 10
    assert m.dead == {2}


def test_any_read_breaks_the_streak() -> None:
    m = DeadPortMonitor((1, 2))
    for _ in range(4):
        m.sweep_completed({1: 5}, 0)
    m.sweep_completed({1: 5, 2: 1}, 0)  # port 2 read once
    for _ in range(4):
        m.sweep_completed({1: 5}, 0)
    assert m.dead == set()
    m.sweep_completed({1: 5}, 0)
    assert m.dead == {2}


def test_clears_on_first_read_from_any_window() -> None:
    m = DeadPortMonitor((1, 2, 3))
    for _ in range(5):
        m.sweep_completed({1: 4}, 0)
    assert m.dead == {2, 3}
    assert m.saw({3: 1}) == {3}  # e.g. a continuous window
    assert m.dead == {2}
    assert m.sweep_completed({1: 4, 2: 2}, 0) == (set(), {2})
    assert m.dead == set()


def test_single_port_needs_tags_present() -> None:
    m = DeadPortMonitor((1,))
    for _ in range(8):
        m.sweep_completed({}, present_at_start=0)
    assert m.dead == set()  # nothing to read: silence is expected
    m = DeadPortMonitor((1,))
    for present in (3, 3, 0, 0, 0):  # tags there when the silence began
        m.sweep_completed({}, present_at_start=present)
    assert m.dead == {1}
    m = DeadPortMonitor((1,))
    m.sweep_completed({1: 5}, present_at_start=3)  # read fine, then tags left
    for _ in range(5):
        m.sweep_completed({}, present_at_start=0)
    assert m.dead == set()  # the silence began with nothing to read


def test_message_text() -> None:
    assert message({2}) == (
        "Antenna on port 2 gave no reads in 5 sweeps — check its cable and connection."
    )
    assert message({3, 1}).splitlines()[0].startswith("Antenna on port 1 ")


# Hub with FakeUR4 ----------------------------------------------------------------


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> object:
    r = DoorRig(tmp_path, grocy, ha, fake)
    yield r
    await r.stop()


def dead_notes(
    ha: FakeHomeAssistant, service: str = "create"
) -> list[dict[str, object]]:
    return [
        c
        for c in ha.calls("persistent_notification", service)
        if c.get("notification_id") == DEAD_PORT_NOTIFICATION_ID
    ]


async def test_hub_dead_port_at_sweep_5_and_clears(
    rig: DoorRig, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    fake.scenario = [ScriptedTag(EPC1, antennas=(1,))]  # nothing on port 2
    await ha.set_state(DOOR, "off")
    await rig.start(reader_antennas=[1, 2], closed_sweep_interval_s=1.0)
    await rig.sweeps(4, timeout=10)
    reading = rig.h.state()["reading"]
    assert reading["per_port_reads_last_sweep"]["2"] == 0
    assert reading["per_port_reads_last_sweep"]["1"] > 0
    assert dead_notes(ha) == [] and reading["dead_ports"] == []  # not at 4
    await rig.sweeps(5, timeout=10)
    await eventually(lambda: dead_notes(ha) != [])
    note = dead_notes(ha)[0]
    assert note["message"] == (
        "Antenna on port 2 gave no reads in 5 sweeps — check its cable and connection."
    )
    assert rig.h.state()["reading"]["dead_ports"] == [2]
    # Warning only: reading continues and presence is untouched.
    assert rig.h.machine.state(EPC1).value == "present"
    done = rig.h.machine.sweeps_completed
    # Port 2's cable is fixed: the first read clears it.
    fake.scenario = [ScriptedTag(EPC1, antennas=(1, 2))]
    await rig.sweeps(done + 1, timeout=10)
    await eventually(lambda: dead_notes(ha, "dismiss") != [])
    assert rig.h.state()["reading"]["dead_ports"] == []
    assert rig.h.state()["reading"]["per_port_reads_last_sweep"]["2"] > 0


async def test_hub_continuous_read_clears(
    rig: DoorRig, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    fake.scenario = [ScriptedTag(EPC1, antennas=(1,))]
    await ha.set_state(DOOR, "off")
    await rig.start(reader_antennas=[1, 2], closed_sweep_interval_s=1.0)
    await rig.sweeps(5, timeout=12)
    await eventually(lambda: rig.h.state()["reading"]["dead_ports"] == [2])
    fake.scenario = [ScriptedTag(EPC1, antennas=(2,))]
    await ha.set_state(DOOR, "on")  # continuous reading, not a sweep
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
    await eventually(lambda: rig.h.state()["reading"]["dead_ports"] == [])
    assert dead_notes(ha, "dismiss") != []


async def test_hub_aborted_sweeps_dont_count(
    rig: DoorRig, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    fake.scenario = [ScriptedTag(EPC1, antennas=(1,))]
    fake.faults.disconnect_after_s = 0.1  # every sweep is cut short
    await ha.set_state(DOOR, "off")
    await rig.start(reader_antennas=[1, 2], closed_sweep_interval_s=1.0)
    await eventually(lambda: rig.h.machine.sweeps_aborted >= 6, timeout=15)
    assert rig.h.machine.sweeps_completed == 0
    assert rig.h.dead_ports is not None and rig.h.dead_ports.dead == set()
    assert rig.h.dead_ports.last_sweep == {}
    fake.faults.disconnect_after_s = None
    await rig.sweeps(4, timeout=12)
    assert rig.h.dead_ports.dead == set()  # only 4 completed so far
    await rig.sweeps(5, timeout=12)
    await eventually(
        lambda: rig.h.dead_ports is not None and rig.h.dead_ports.dead == {2}
    )


async def test_hub_single_port_respects_present_tags(
    rig: DoorRig, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    # Nothing in the pantry and nothing to read: silence is fine.
    fake.scenario = [ScriptedTag(EPC1, antennas=(2,))]  # port 2 isn't enabled
    await ha.set_state(DOOR, "off")
    await rig.start(reader_antennas=[1], closed_sweep_interval_s=1.0)
    await rig.sweeps(6, timeout=14)
    assert rig.h.state()["present_tags"] == 0
    assert rig.h.dead_ports is not None and rig.h.dead_ports.dead == set()
    assert dead_notes(ha) == []
    # A tag arrives on port 1, then port 1 goes silent: flagged after 5 sweeps.
    fake.scenario = [ScriptedTag(EPC2, antennas=(1,))]
    await eventually(lambda: rig.h.state()["present_tags"] == 1, timeout=10)
    fake.scenario = [ScriptedTag(EPC2, antennas=(2,))]  # "cable pulled"
    done = rig.h.machine.sweeps_completed
    await rig.sweeps(done + 4, timeout=14)
    assert rig.h.dead_ports.dead == set()
    await rig.sweeps(done + 6, timeout=14)
    await eventually(lambda: dead_notes(ha) != [])
    assert "port 1" in str(dead_notes(ha)[0]["message"])
