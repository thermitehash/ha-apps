"""Regression tests for the v0.2.0 open-question decisions (Q1, Q2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
import random
import re

import aiohttp
import pytest

from pantry_hub.discovery import RESYNC_COMMAND_TOPIC, RESYNC_RESULT_TOPIC, STATE_TOPIC
from pantry_hub.grocy import StockEntry
from pantry_hub.store import Link, Store
from pantry_hub.web import WebUI
from pantry_hub.worker import GrocyWorker

from .conftest import BrokerHandle, FakeClock, eventually, free_port
from .fake_grocy import FakeGrocy
from .memory_backend import MemoryBackend
from .rig import Observer, Rig

T0 = 1_800_000_000.0
EPC = "E28011700000020F1A2B3C0A"


def worker(store: Store, backend: MemoryBackend) -> GrocyWorker:
    return GrocyWorker(store, backend, clock=lambda: T0, rng=random.Random(1))


async def drain(w: GrocyWorker) -> None:
    for _ in range(50):
        if await w.process_one() == 3600.0:
            return


def pre_existing(backend: MemoryBackend, sid: str = "old") -> None:
    """A unit Grocy already counted before the item was tagged."""
    backend.stock.append(StockEntry(sid, 1, 1.0, 1, "bought before tagging"))


# Q1: "already in stock" ---------------------------------------------------------


async def test_q1_in_stock_link_while_present_skips_add_consumes_on_departure(
    store: Store,
) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    pre_existing(backend)
    await store.apply_arrival(EPC, T0, 1)  # unlinked arrival (tag already on shelf)
    created, op_id = await store.create_link(
        Link(EPC, 1, "Beans", T0), location_id=1, in_stock=True
    )
    assert created and op_id is None  # no add queued
    link = await store.get_link(EPC)
    assert link is not None and link.skip_add_cycle == 1
    w = worker(store, backend)
    await drain(w)
    assert backend.log == []  # Grocy untouched
    # Departure still takes the unit Grocy counted (by amount, at the location).
    d = await store.apply_departure(EPC, T0 + 10)
    assert d.consume_op_id is not None
    await drain(w)
    assert backend.log == [("consume", 1, "old")] and backend.stock == []
    # The next arrival behaves normally: one add.
    await store.apply_arrival(EPC, T0 + 20, 1)
    await drain(w)
    assert [e[0] for e in backend.log] == ["consume", "add"]


async def test_q1_in_stock_link_while_absent_skips_next_arrival_only(
    store: Store,
) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    pre_existing(backend)
    await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1, in_stock=True)
    link = await store.get_link(EPC)
    assert link is not None and link.skip_add_cycle == 1  # the next arrival
    r = await store.apply_arrival(EPC, T0 + 1, 1)
    assert r.skipped_in_stock and r.add_op_id is None
    w = worker(store, backend)
    await drain(w)
    assert backend.log == []
    await store.apply_departure(EPC, T0 + 2)
    await store.apply_arrival(EPC, T0 + 3, 1)  # cycle 2: normal
    await drain(w)
    assert [e[0] for e in backend.log] == ["consume", "add"]
    assert len(backend.stock) == 1  # the newly added unit


async def test_q1_default_is_off(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await store.apply_arrival(EPC, T0, 1)
    created, op_id = await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1)
    assert created and op_id is not None
    link = await store.get_link(EPC)
    assert link is not None and link.skip_add_cycle is None


async def test_q1_relink_in_stock_does_not_undo_an_add_already_queued(
    store: Store,
) -> None:
    await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1)
    r = await store.apply_arrival(EPC, T0, 1)
    assert r.add_op_id is not None
    op = await store.replace_link(
        Link(EPC, 1, "Beans", T0 + 1), location_id=1, in_stock=True
    )
    assert op is None
    pending = await store.next_pending()
    assert pending is not None and pending.id == r.add_op_id  # still queued once


# Q2: ambiguous consume without a stock entry id ---------------------------------


async def test_q2_ambiguous_consume_without_stock_id_goes_to_review_not_resent(
    store: Store,
) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    pre_existing(backend, "u1")
    pre_existing(backend, "u2")
    await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1, in_stock=True)
    await store.apply_arrival(EPC, T0, 1)
    d = await store.apply_departure(EPC, T0 + 1)
    assert d.consume_op_id is not None
    backend.faults.append("lost")  # consume applied, answer lost
    w = worker(store, backend)
    await drain(w)
    assert [e[0] for e in backend.log] == ["consume"]  # sent once, never resent
    op = await store.get_op(d.consume_op_id)
    assert op is not None and op.needs_review
    counts = await store.counts()
    assert counts["failed"] == 1 and counts["review"] == 1 and counts["pending"] == 0
    assert not await store.retry_failed(op.id)  # review ops can't be blindly retried
    # "Dismiss": the person decided it did reach Grocy.
    assert await w.dismiss(op.id)
    assert not await w.dismiss(op.id)
    op = await store.get_op(op.id)
    assert op is not None and (op.status, op.resolution) == ("done", "dismissed")
    assert len(backend.stock) == 1


async def test_q2_consume_now(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    pre_existing(backend)
    await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1, in_stock=True)
    await store.apply_arrival(EPC, T0, 1)
    d = await store.apply_departure(EPC, T0 + 1)
    w = worker(store, backend)
    # The hub crashed mid-request: the consume may or may not have been sent.
    await store.mark_sending(d.consume_op_id or 0)
    await drain(w)
    op = await store.get_op(d.consume_op_id or 0)
    assert op is not None and op.needs_review
    assert backend.log == []  # nothing resent
    done = await w.consume_now(op.id)
    assert done.id == op.id
    assert backend.log == [("consume", 1, "old")]
    op = await store.get_op(op.id)
    assert op is not None and op.resolution == "consumed_manually"
    with pytest.raises(ValueError):
        await w.consume_now(op.id)


async def test_q2_known_stock_id_still_checked_and_not_reviewed(store: Store) -> None:
    """Ambiguous consumes *with* a stock entry id keep the existing check."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await store.create_link(Link(EPC, 1, "Beans", T0), location_id=1)
    await store.apply_arrival(EPC, T0, 1)
    w = worker(store, backend)
    await drain(w)
    await store.apply_departure(EPC, T0 + 1)
    backend.faults.append("lost")
    await drain(w)
    assert (await store.counts())["review"] == 0
    assert backend.stock == []


async def test_schema_v1_database_is_migrated(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "v1.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE links (epc TEXT PRIMARY KEY, product_id INTEGER NOT NULL, "
        "product_name TEXT NOT NULL, barcode TEXT, best_before TEXT, "
        "default_best_before_days INTEGER, station TEXT, linked_at REAL NOT NULL);"
        "INSERT INTO links VALUES ('AAAA', 1, 'Beans', NULL, NULL, NULL, NULL, 1.0);"
    )
    con.commit()
    con.close()
    s = await Store.open(db)
    try:
        link = await s.get_link("AAAA")
        assert link is not None and link.skip_add_cycle is None
    finally:
        await s.close()
    s = await Store.open(db)  # idempotent
    await s.close()


# End to end: MQTT + web UI --------------------------------------------------------


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


async def test_q1_mqtt_in_stock_field(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    await rig.start()
    assert rig.client is not None
    await rig.client.add(
        beans.id, location_id=1, best_before=None, note="before tagging"
    )
    await observer.publish(
        "pantry_hub/linking/s1/barcode", {"barcode": "0001", "in_stock": True}
    )
    await asyncio.sleep(0.05)
    await observer.publish("pantry_hub/linking/s1/tag", {"epc": EPC, "rssi": -50})
    await eventually(lambda: observer.last("pantry_hub/linking/s1/result") is not None)
    result = observer.last("pantry_hub/linking/s1/result")
    assert result["status"] == "linked" and "already in stock" in result["message"]
    await rig.arrive(EPC)
    await rig.idle()
    assert grocy.count_calls("POST", "/add") == 1  # only the "before tagging" add
    assert grocy.amount(beans.id) == 1
    rig.clock.advance(61)
    await rig.read_until_state(EPC, "unknown")
    await rig.idle()
    assert grocy.amount(beans.id) == 0  # departure consumed the counted unit


async def test_q1_mqtt_in_stock_on_tag_message(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    grocy.add_product("Rice", ["0002"])
    await rig.start()
    await observer.publish("pantry_hub/linking/s1/barcode", {"barcode": "0002"})
    await asyncio.sleep(0.05)
    await observer.publish(
        "pantry_hub/linking/s1/tag", {"epc": EPC, "rssi": -50, "in_stock": True}
    )
    await eventually(lambda: observer.last("pantry_hub/linking/s1/result") is not None)
    assert rig.store is not None
    link = await rig.store.get_link(EPC)
    assert link is not None and link.skip_add_cycle == 1


async def test_q1_q2_web_ui_and_resync(
    rig: Rig, grocy: FakeGrocy, observer: Observer
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    await rig.start()
    assert rig.client is not None
    await rig.client.add(beans.id, location_id=1, best_before=None, note="old")
    await rig.arrive(EPC)  # unlinked, already on the shelf
    await rig.idle()
    ui = WebUI(rig.h, allowed_peers=["*"])
    port = free_port()
    await ui.start("127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                page = await r.text()
            assert 'name="in_stock"' in page  # Q1 checkbox
            m = re.search(r'name="csrf" value="([^"]+)"', page)
            assert m
            csrf = m.group(1)
            async with s.post(
                base + "/relink",
                data={"csrf": csrf, "epc": EPC, "target": "0001", "in_stock": "on"},
            ) as r:
                page = await r.text()
            assert "already in stock" in page
            await rig.idle()
            assert grocy.count_calls("POST", "/add") == 1  # no new add
            # Q2: make the departure's consume ambiguous (answer lost) → review.
            grocy.fault("/api/stock/products/{id}/consume", "apply_then_500")
            rig.clock.advance(61)
            await rig.read_until_state(EPC, "unknown")
            assert rig.store is not None
            await eventually(lambda: _review_count(rig), timeout=5)
            assert grocy.amount(beans.id) == 0  # consumed once, not twice
            await eventually(
                lambda: (
                    (observer.last(STATE_TOPIC) or {}).get("grocy", {}).get("review")
                    == 1
                )
            )
            await observer.publish(RESYNC_COMMAND_TOPIC, "PRESS")
            await eventually(lambda: observer.last(RESYNC_RESULT_TOPIC) is not None)
            assert observer.last(RESYNC_RESULT_TOPIC)["review_ops"][0]["epc"] == EPC
            async with s.get(base + "/") as r:
                page = await r.text()
            assert "Consume now" in page and "waiting for review" in page
            op = (await rig.store.ops("failed"))[0]
            async with s.post(
                base + "/dismiss-op", data={"csrf": csrf, "op": str(op.id)}
            ) as r:
                page = await r.text()
            assert "dismissed" in page
            async with s.post(
                base + "/dismiss-op", data={"csrf": csrf, "op": str(op.id)}
            ) as r:
                page = await r.text()
            assert "not waiting for review" in page
            async with s.post(
                base + "/consume-now", data={"csrf": csrf, "op": str(op.id)}
            ) as r:
                page = await r.text()
            assert "Failed:" in page
    finally:
        await ui.stop()


async def _review_count(rig: Rig) -> bool:
    assert rig.store is not None
    return (await rig.store.counts())["review"] == 1


async def test_q2_web_consume_now(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    await rig.start()
    assert rig.client is not None
    await rig.client.add(beans.id, location_id=1, best_before=None, note="old")
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, "Beans", rig.clock()), location_id=1, in_stock=True
    )
    await rig.arrive(EPC)
    await rig.idle()
    grocy.fault("/api/stock/products/{id}/consume", "500")  # not applied, but 5xx
    rig.clock.advance(61)
    await rig.read_until_state(EPC, "unknown")
    await eventually(lambda: _review_count(rig), timeout=5)
    assert grocy.amount(beans.id) == 1  # under-consume, not double
    ui = WebUI(rig.h, allowed_peers=["*"])
    port = free_port()
    await ui.start("127.0.0.1", port)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                page = await r.text()
            m = re.search(r'name="csrf" value="([^"]+)"', page)
            assert m
            op = (await rig.store.ops("failed"))[0]
            async with s.post(
                f"http://127.0.0.1:{port}/consume-now",
                data={"csrf": m.group(1), "op": str(op.id)},
            ) as r:
                page = await r.text()
            assert "Consumed one Beans" in page
        assert grocy.amount(beans.id) == 0
    finally:
        await ui.stop()
