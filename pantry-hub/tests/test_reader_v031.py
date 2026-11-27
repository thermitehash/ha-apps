"""aiochainway v0.3.0 adoption: sweep results, connection modes, flicker.

Every reader here is aiochainway's own FakeUR4. Connection counts are read
through ``FakeUR4.wait_for_connections`` (the fake's accept handler can run
after ``connect()`` returns, notably on macOS).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import logging

from aiochainway import TagRead, UR4Client
from aiochainway.testing import FakeUR4, ScriptedTag, tag_record
import pytest

from pantry_hub.presence import PresenceConfig, PresenceMachine
from pantry_hub.reader import ConnectionMode, ReaderSession

EPC = "E28011700000020F1A2B3C31"


@pytest.fixture
async def fake() -> AsyncIterator[FakeUR4]:
    async with FakeUR4(seed=31) as f:
        f.tags = [tag_record(EPC, rssi_dbm=-50)]
        f.tag_interval = 0.01
        yield f


def session_for(
    fake: FakeUR4, connection: ConnectionMode = "per_window", **kwargs: float
) -> ReaderSession:
    return ReaderSession(
        "192.168.99.202",
        connection=connection,
        client_factory=lambda h, p, **kw: UR4Client(fake.host, fake.port, **kw),
        timeout=1.0,
        **kwargs,  # type: ignore[arg-type]
    )


# Client options ------------------------------------------------------------------


async def test_per_window_client_options_and_disconnects() -> None:
    made: list[dict[str, object]] = []
    async with FakeUR4() as fake:
        fake.tags = [tag_record(EPC)]
        fake.tag_interval = 0.01

        def factory(h: str, p: int, **kw: object) -> UR4Client:
            made.append(kw)
            return UR4Client(fake.host, fake.port, **kw)  # type: ignore[arg-type]

        s = ReaderSession("x", client_factory=factory, timeout=1.0)
        for _ in range(3):
            assert (await s.run_window(0.05, lambda tag: None)).completed
        await fake.wait_for_connections(3)
        assert fake.connections == 3  # one connection per window
        assert made[0] == {"timeout": 1.0, "stop_on_connect": True}
        assert s._client is None  # nothing held between windows


async def test_persistent_holds_one_connection(fake: FakeUR4) -> None:
    made: list[dict[str, object]] = []

    def factory(h: str, p: int, **kw: object) -> UR4Client:
        made.append(kw)
        return UR4Client(fake.host, fake.port, **kw)  # type: ignore[arg-type]

    s = ReaderSession("x", connection="persistent", client_factory=factory, timeout=1.0)
    try:
        for _ in range(3):
            assert (await s.run_window(0.05, lambda tag: None)).completed
        assert await s.probe() is not None
        await fake.wait_for_connections(1)
        await asyncio.sleep(0.1)
        assert fake.connections == 1
        assert len(made) == 1
        assert made[0].pop("on_reconnect") == s._on_reconnect  # re-verify config
        assert made[0] == {
            "timeout": 1.0,
            "stop_on_connect": True,
            "keepalive_interval_s": 60.0,
            "auto_reconnect": True,
        }
    finally:
        await s.close()
    assert s._client is None


async def test_persistent_recovers_after_reader_drop(fake: FakeUR4) -> None:
    s = session_for(fake, "persistent")
    try:
        assert (await s.run_window(0.05, lambda tag: None)).completed
        await fake.wait_for_connections(1)
        fake.faults.disconnect_after_s = 0.05
        dropped = await s.run_window(1.0, lambda tag: None)
        assert not dropped.completed and s.online is False
        fake.faults.disconnect_after_s = None
        again = await s.run_window(0.05, lambda tag: None)
        assert again.completed and bool(s.online)
        await fake.wait_for_connections(2)
    finally:
        await s.close()


async def test_probe_per_window_connects_pings_disconnects(fake: FakeUR4) -> None:
    s = session_for(fake)
    rtt = await s.probe()
    assert rtt is not None and rtt >= 0 and s.online is True
    await fake.wait_for_connections(1)
    assert "get_version" in fake.names()
    assert "start_inventory" not in fake.names()


async def test_probe_unreachable_marks_offline() -> None:
    s = ReaderSession("127.0.0.1", 9, timeout=0.3, connect_retries=1)
    assert await s.probe() is None
    assert s.online is False and s.last_error


# Sweep results ---------------------------------------------------------------------


async def test_mid_sweep_drop_is_aborted_never_a_miss(fake: FakeUR4) -> None:
    """The reader drops mid-sweep: connection_lost, not completed, no miss."""
    machine = PresenceMachine(
        PresenceConfig(depart_after_s=600, depart_after_missed_sweeps=1)
    )
    machine.restore([EPC], 0.0)
    fake.scenario = [ScriptedTag(EPC, present_from=1e9)]  # absent: would miss
    fake.faults.disconnect_after_s = 0.1
    s = session_for(fake)
    machine.start_sweep()

    def on_tag(tag: TagRead) -> None:
        machine.read(tag.epc, 1.0)

    result = await s.run_window(1.0, on_tag)
    assert not result.completed
    assert result.end_reason == "connection_lost"
    assert machine.finish_sweep(result.completed, 2.0) == []
    assert machine.tracks()[EPC].missed == 0
    assert machine.sweeps_aborted == 1 and machine.sweeps_completed == 0


async def test_missing_stop_ack_still_completed_and_logged(
    fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    fake.faults.no_stop_ack = True
    s = session_for(fake)
    with caplog.at_level(logging.INFO, logger="pantry_hub.reader"):
        result = await s.run_window(0.2, lambda tag: None)
    assert result.completed and result.end_reason == "duration_elapsed"
    assert result.stop_acknowledged is False
    assert "didn't acknowledge stop" in caplog.text


async def test_cancelled_window_is_stopped_not_completed(fake: FakeUR4) -> None:
    s = session_for(fake)
    task = asyncio.create_task(s.run_window(5.0, lambda tag: None))
    await fake.wait_for("start_inventory")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.last_result is not None
    assert not s.last_result.completed
    assert str(s.last_result.end_reason) == "stopped"


# Flicker ---------------------------------------------------------------------------

FLICKER_TAGS = 20
FLICKER_SWEEPS = 20
SWEEP_S = 0.1  # 10 read rounds per sweep at tag_interval 0.01


async def _flicker_run(probability: float, seed: int) -> tuple[int, int, int]:
    """Return (false departures, missed tag-sweeps, completed sweeps).

    ``FLICKER_TAGS`` tags stay in the field the whole time but each is read
    only with ``probability`` per read round. A departure is false by
    construction.
    """
    epcs = [f"E2801170000002{i:010X}" for i in range(FLICKER_TAGS)]
    machine = PresenceMachine(
        PresenceConfig(
            depart_after_s=600,
            depart_after_missed_sweeps=2,
            min_sweeps_before_departure=0,
        )
    )
    machine.restore(epcs, 0.0)
    departures = misses = 0
    async with FakeUR4(seed=seed) as fake:
        fake.tag_interval = 0.01
        fake.scenario = [ScriptedTag(e, read_probability=probability) for e in epcs]
        s = session_for(fake)
        for n in range(FLICKER_SWEEPS):
            now = float(n + 1)
            seen: set[str] = set()

            def on_tag(tag: TagRead, now: float = now, seen: set[str] = seen) -> None:
                epc = tag.epc
                seen.add(epc)
                machine.read(epc, now)

            machine.start_sweep()
            result = await s.run_window(SWEEP_S, on_tag)
            assert result.completed, result
            misses += sum(1 for e in epcs if e not in seen)
            for event in machine.finish_sweep(result.completed, now):
                assert event.kind == "departed"
                departures += 1
                machine.read(event.epc, now)  # it never left; start over
    return departures, misses, machine.sweeps_completed


async def test_flicker_p06_no_false_departure() -> None:
    departures, misses, completed = await _flicker_run(0.6, seed=6)
    print(
        f"\nflicker p=0.6: {departures} false departure(s), "
        f"{misses}/{FLICKER_TAGS * FLICKER_SWEEPS} tag-sweeps missed"
    )
    assert completed == FLICKER_SWEEPS
    assert departures == 0


async def test_flicker_p03_false_departure_rate_reported(
    record_property: Callable[[str, object], None],
) -> None:
    departures, misses, completed = await _flicker_run(0.3, seed=3)
    tag_sweeps = FLICKER_TAGS * FLICKER_SWEEPS
    rate = departures / tag_sweeps
    record_property("false_departures", departures)
    record_property("false_departure_rate", rate)
    record_property("miss_rate", misses / tag_sweeps)
    print(
        f"\nflicker p=0.3: {departures} false departure(s) in {tag_sweeps} "
        f"tag-sweeps (rate {rate:.4f}); {misses} tag-sweeps missed "
        f"({misses / tag_sweeps:.3f})"
    )
    assert completed == FLICKER_SWEEPS
    # Reported, not a pass/fail: with ~10 read rounds per sweep a miss is
    # 0.7**10 ≈ 2.8 % and two in a row ≈ 0.08 %. Guard only against nonsense.
    assert rate < 0.05
