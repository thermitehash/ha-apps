"""v0.4.5: shutdown never hangs on a lost cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from pathlib import Path
import time

import pytest

from .conftest import BrokerHandle, FakeClock
from .fake_grocy import FakeGrocy
from .rig import Rig
from .web_client import WebClient


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle, clock: FakeClock
) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, broker, clock)
    await r.start()
    yield r
    await r.stop()
    r.close()


async def test_stop_survives_a_lost_cancel(rig: Rig) -> None:
    """The 0.4.3 macOS CI hang: a cancel lost inside one tick of the timer."""
    hub = rig.h
    lost: list[str] = []
    real = hub.publish_state

    async def publish_state(force: bool = False) -> None:
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            if not lost:
                lost.append("tick")  # swallow it, once, like the CI hang
                return
            raise
        await real(force)

    hub.publish_state = publish_state  # type: ignore[method-assign]
    await asyncio.sleep(0.05)  # the tick loop is inside publish_state now
    t0 = time.monotonic()
    await asyncio.wait_for(hub.stop(), 5)
    assert lost == ["tick"]
    assert time.monotonic() - t0 < 3
    assert all(
        t.done() for t in asyncio.all_tasks() if t.get_name() == "pantry_hub.tick"
    )
    rig.hub = None  # stopped; the fixture closes the store


async def test_stop_is_capped_and_names_the_stuck_loop(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A loop that never ends: stop logs it and returns after the cap."""
    hub = rig.h
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue  # ignores every cancel

    task = asyncio.create_task(stubborn(), name="pantry_hub.stubborn")
    hub._loops.append(task)
    hub.stop_timeout_s = 1.0
    caplog.set_level(logging.WARNING, logger="pantry_hub.hub")
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(hub.stop(), 5)
        took = time.monotonic() - t0
        assert 0.7 < took < 1.5  # gave up on the loop, then finished the rest
        messages = [r.getMessage() for r in caplog.records]
        assert "Background loop didn't end: pantry_hub.stubborn" in messages
    finally:
        # Let the rest of the shutdown finish before the fixture closes the store.
        release.set()
        task.cancel()
        pending = [t for t in asyncio.all_tasks() if t.get_name() == "pantry_hub.stop"]
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 15)
        rig.hub = None


async def test_stop_returns_at_the_cap_when_a_step_blocks(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A shutdown step that never returns: stop() returns at the cap anyway."""
    hub = rig.h
    release = asyncio.Event()
    real_stop = hub.worker.stop

    async def stuck_worker_stop(timeout: float = 0.0) -> None:
        await release.wait()

    hub.worker.stop = stuck_worker_stop  # type: ignore[method-assign]
    hub.stop_timeout_s = 1.0
    caplog.set_level(logging.WARNING, logger="pantry_hub.hub")
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(hub.stop(), 5)
        assert 0.9 < time.monotonic() - t0 < 1.5
        (cap,) = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("Stopping took over 1 s")
        ]
        assert "at: Grocy worker" in cap
    finally:
        release.set()
        pending = [t for t in asyncio.all_tasks() if t.get_name() == "pantry_hub.stop"]
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 15)
        await real_stop()
        rig.hub = None


async def test_continuous_without_door_sensor_is_a_health_warning(rig: Rig) -> None:
    """0.4.5: continuous mode with no door sensor isn't silent."""
    async with WebClient(rig.h) as c:
        state = await c.state()
    issues = {i["title"]: i for i in state["health"]["issues"]}
    issue = issues["No door sensor: reading continuously (test mode)"]
    assert issue["level"] == "warn" and issue["action"] == "settings"
    assert "set reading_mode to door" in issue["detail"]
