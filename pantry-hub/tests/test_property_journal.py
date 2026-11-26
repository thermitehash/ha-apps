"""Property test: random read timelines through presence + journal + worker.

Faults (Grocy down, answers lost after applying) and hub restarts are mixed
in. Invariants: no arrival is ever added twice, every add is consumed at most
once, and once Grocy is healthy again its stock equals the linked tags that
are in the pantry.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
import random
import tempfile

from hypothesis import given, strategies as st

from pantry_hub.presence import PresenceConfig, PresenceEvent, PresenceMachine
from pantry_hub.store import Link, Store
from pantry_hub.worker import GrocyWorker

from .memory_backend import MemoryBackend

EPCS = ["AAAA", "BBBB", "CCCC"]
CFG = PresenceConfig(arrive_reads=2, arrive_window_s=10, depart_after_s=100)

step = st.one_of(
    st.tuples(st.just("read"), st.sampled_from(EPCS), st.floats(0, 8)),
    st.tuples(st.just("wait"), st.just(""), st.floats(0, 150)),
    st.tuples(st.just("link"), st.sampled_from(EPCS), st.just(0.0)),
    st.tuples(st.just("fault"), st.sampled_from(["down", "lost"]), st.just(0.0)),
    st.tuples(st.just("work"), st.just(""), st.integers(1, 4).map(float)),
    st.tuples(st.just("restart"), st.just(""), st.just(0.0)),
)


async def scenario(steps: list[tuple[str, str, float]], db: Path) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    store = await Store.open(db)
    now = 1000.0
    machine = PresenceMachine(CFG)
    clock = [now]
    worker = GrocyWorker(store, backend, clock=lambda: clock[0], rng=random.Random(0))
    worker.backoff.base = 0.0

    async def handle(events: list[PresenceEvent]) -> None:
        for e in events:
            if e.kind == "arrived":
                await store.apply_arrival(e.epc, now, 1)
            else:
                await store.apply_departure(e.epc, now)

    for action, arg, value in steps:
        if action == "read":
            now += value
            clock[0] = now
            await handle(machine.read(arg, now))
        elif action == "wait":
            now += value
            clock[0] = now
            await handle(machine.tick(now))
        elif action == "link":
            await store.create_link(Link(arg, 1, "Beans", now), location_id=1)
        elif action == "fault":
            backend.faults.append(arg)
        elif action == "work":
            for _ in range(int(value)):
                await worker.process_one()
        else:  # crash + restart: rebuild from SQLite only
            await store.close()
            store = await Store.open(db)
            machine = PresenceMachine(CFG)
            machine.restore(await store.present_epcs(), now)
            worker = GrocyWorker(
                store, backend, clock=lambda: clock[0], rng=random.Random(0)
            )
            worker.backoff.base = 0.0

    backend.faults.clear()
    for _ in range(200):
        if await worker.process_one() == 3600.0:
            break

    adds = [sid for kind, _, sid in backend.log if kind == "add" and sid]
    consumes = [sid for kind, _, sid in backend.log if kind == "consume" and sid]
    # Each journaled add op reached Grocy at most once.
    op_ids = Counter(backend.notes[sid].split()[3] for sid in adds)
    assert all(n == 1 for n in op_ids.values()), op_ids
    # Every unit is consumed at most once, and only units that were added.
    assert len(consumes) == len(set(consumes))
    assert set(consumes) <= set(adds)
    counts = await store.counts()
    assert counts["pending"] == 0 and counts["failed"] == 0
    # Grocy stock == linked tags in the pantry, one unit each.
    linked = {lk.epc for lk in await store.links()}
    stock_epcs = Counter(backend.notes[e.stock_id].split()[-1] for e in backend.stock)
    assert all(n == 1 for n in stock_epcs.values()), stock_epcs
    assert set(stock_epcs) == {e for e in machine.in_pantry() if e in linked}
    await store.close()


@given(st.lists(step, max_size=60))
def test_journal_invariants(steps: list[tuple[str, str, float]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(asyncio.wait_for(scenario(steps, Path(tmp) / "p.db"), 60))
