"""Door opening at every phase of the startup sweep, then "sweep now"."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiochainway.testing import FakeUR4
import pytest

from pantry_hub.reading import Activity

from .conftest import eventually
from .fake_grocy import FakeGrocy
from .fake_ha import FakeHomeAssistant
from .test_door_reading import DOOR, DoorRig


@pytest.mark.parametrize("delay", [i * 0.04 for i in range(0, 16)])
async def test_open_at_any_startup_phase_then_sweep_now(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4, delay: float
) -> None:
    """Opening while the startup sweep connects, checks the mode, reads or
    stops must always lead to continuous reading, and "sweep now" during it
    must always produce a sweep."""
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await ha.set_state(DOOR, "off")
        await rig.start()
        await asyncio.sleep(delay)
        await ha.set_state(DOOR, "on")
        await eventually(lambda: rig.h.activity is Activity.CONTINUOUS, timeout=5)
        done = rig.h.machine.sweeps_completed
        assert rig.h.request_sweep()
        await rig.sweeps(done + 1, timeout=12)
    finally:
        await rig.stop()
