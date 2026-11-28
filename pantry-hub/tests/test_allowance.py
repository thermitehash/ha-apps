"""v0.4.4: booked units are a capped allowance; lowering consumes Pantry only."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from pantry_hub.store import ALLOWANCE_S, Link, Store

from .conftest import BrokerHandle, FakeClock, eventually
from .fake_grocy import Entry, FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .rig import Observer, Rig
from .web_client import WebClient

STATION = "kroger_scanner"
TAG = f"pantry_hub/linking/{STATION}/tag"
RESULT = f"pantry_hub/linking/{STATION}/result"
START = f"pantry_hub/linking/{STATION}/start"
EPCS = [f"E2801170000004{i:010X}" for i in range(1, 9)]


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


async def start(rig: Rig, ha: FakeHomeAssistant) -> None:
    await rig.start(
        ha_url=ha.url,
        ha_token=TOKEN,
        picture_retry_delays=(),
        picture_spacing_s=0.0,
        ha_backoff_s=0.05,
        counted_idle=1.0,
    )


def stock(grocy: FakeGrocy, product_id: int, amount: float, location: int = 1) -> None:
    grocy.entries.append(
        Entry(f"seed{len(grocy.entries)}", product_id, amount, location, "seed", None)
    )


async def tag_and_arrive(rig: Rig, product: Any, *epcs: str) -> None:
    """Link tags (no "already in stock") and let the reader see them."""
    assert rig.store is not None
    for epc in epcs:
        await rig.store.create_link(
            Link(epc, product.id, product.name, rig.clock()), location_id=1
        )
    await rig.arrive(*epcs)
    await rig.idle()


def results(observer: Observer, n: int) -> Callable[[], bool]:
    return lambda: len(observer.json(RESULT)) == n


async def test_allowance_of_two_then_stock_is_added(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    cereal = grocy.add_product("Cereal", ["3001"])
    stock(grocy, cereal.id, 2)  # counted / booked without tags
    await start(rig, ha)
    assert rig.store is not None
    await rig.store.add_allowance(cereal.id, 2, clock(), "stocktake")
    clock.advance(86400)  # a day later, well inside the 7 days
    await tag_and_arrive(rig, cereal, EPCS[0])
    assert grocy.amount(cereal.id) == 2  # 1st newly tagged unit: nothing added
    await tag_and_arrive(rig, cereal, EPCS[1])
    assert grocy.amount(cereal.id) == 2  # 2nd: nothing added
    assert await rig.store.allowance_units(clock()) == {}
    await tag_and_arrive(rig, cereal, EPCS[2])
    assert grocy.amount(cereal.id) == 3  # 3rd: a new unit, added
    matched = [e for e in rig.h.recent_events if e["kind"] == "matched_booked"]
    assert [e["epc"] for e in matched] == [EPCS[0], EPCS[1]]


async def test_count_three_buy_two_tag_five(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    """The example: count 3 cereals, buy 2 more, tag all 5."""
    cereal = grocy.add_product("Cereal", ["3001"])
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        for _ in range(3):
            await c.post("/scan", barcode="3001", mode="stocktake")
        await c.post("/stocktake/finish")
        await c.post("/stocktake/apply", how="set")
        assert grocy.amount(cereal.id) == 3
        state = await c.state()
        assert state["allowances"] == [
            {
                "product_id": cereal.id,
                "name": "Cereal",
                "units": 3,
                "sources": ["stocktake"],
                "expires": rig.clock() + ALLOWANCE_S,
                "text": "3 counted units still to tag",
            }
        ]
        # Shown in Details too.
        assert ["Cereal", "3 counted units still to tag"] in state["diagnostics"]
        # Two more boxes from the shop; Tag N links the 3 counted ones, then
        # the other 2 are linked at the station as usual.
        await c.post("/stocktake/tag", product_id=cereal.id)
    await eventually(lambda: observer.last(START) == {"barcode": "3001", "count": 3})
    for i, epc in enumerate(EPCS[:3], start=1):
        await observer.publish(TAG, {"epc": epc, "rssi": -40})
        await eventually(results(observer, i))
    assert rig.store is not None
    for epc in EPCS[3:5]:
        await rig.store.create_link(
            Link(epc, cereal.id, "Cereal", rig.clock()), location_id=1
        )
    # All five reach the pantry: the first three arrivals add nothing, the
    # last two add stock — inside the 7 days.
    for epc in EPCS[:5]:
        await rig.arrive(epc)
        await rig.idle()
    assert grocy.amount(cereal.id) == 5
    assert await rig.store.allowance_units(rig.clock()) == {}


async def test_allowance_expires_after_seven_days(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    rice = grocy.add_product("Rice", ["3002"])
    stock(grocy, rice.id, 2)
    await start(rig, ha)
    assert rig.store is not None
    await rig.store.add_allowance(rice.id, 2, clock(), "not_tagging")
    clock.advance(ALLOWANCE_S - 60)  # still inside
    await tag_and_arrive(rig, rice, EPCS[0])
    assert grocy.amount(rice.id) == 2
    assert await rig.store.allowance_units(clock()) == {rice.id: 1}
    async with WebClient(rig.h) as c:
        (row,) = (await c.state())["allowances"]
        assert row["text"] == "1 booked unit still to tag"
    clock.advance(120)  # 7 days passed: the last booked unit has expired
    assert await rig.store.allowance_units(clock()) == {}
    await tag_and_arrive(rig, rice, EPCS[1])
    # A new tag adds stock. (The first tag "left" while the clock jumped 7
    # days, so compare the adds sent, not the total.)
    adds = [b for m, path, b in grocy.calls if m == "POST" and path.endswith("/add")]
    assert [EPCS[1] in b["note"] for b in adds] == [True]


async def test_only_first_arrival_of_a_new_tag_uses_the_allowance(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    oats = grocy.add_product("Oats", ["3003"])
    await start(rig, ha)
    assert rig.store is not None
    # A tag linked before the stock-take that comes back later is not new.
    await tag_and_arrive(rig, oats, EPCS[0])
    assert grocy.amount(oats.id) == 1
    stock(grocy, oats.id, 2)
    await rig.store.add_allowance(oats.id, 2, clock(), "stocktake")
    # Its next arrival cycle (after leaving) adds as usual; the allowance stays.
    await rig.store.apply_departure(EPCS[0], clock())
    res = await rig.store.apply_arrival(EPCS[0], clock(), 1)
    assert res.add_op_id is not None and not res.matched_booked
    assert await rig.store.allowance_units(clock()) == {oats.id: 2}
    # A tag linked while already in the pantry: its "first arrival" is now.
    await rig.store.apply_arrival(EPCS[1], clock(), 1)  # read while unlinked
    created, op_id = await rig.store.create_link(
        Link(EPCS[1], oats.id, "Oats", clock()), location_id=1
    )
    assert created and op_id is None  # matched, nothing journaled to add
    assert await rig.store.allowance_units(clock()) == {oats.id: 1}


async def test_tag_them_on_grocy_stock_fills_the_allowance(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    flour = grocy.add_product("Flour", ["3004"])
    stock(grocy, flour.id, 2)
    await start(rig, ha)
    assert rig.h.grocy_untagged is not None
    await rig.h.grocy_untagged.refresh()
    async with WebClient(rig.h) as c:
        await c.post("/grocy-stock/tag", product_id=flour.id)
        # Pressing it again tops up, it doesn't double.
        await c.post("/grocy-stock/tag", product_id=flour.id)
        assert rig.store is not None
        assert await rig.store.allowance_units(rig.clock()) == {flour.id: 2}
        (row,) = (await c.state())["allowances"]
        assert row["text"] == "2 counted units still to tag"
    await eventually(lambda: observer.last(START) == {"barcode": "3004", "count": 2})
    for i, epc in enumerate(EPCS[:2], start=1):
        await observer.publish(TAG, {"epc": epc, "rssi": -40})
        await eventually(results(observer, i))
    await rig.arrive(*EPCS[:2])
    await rig.idle()
    assert grocy.amount(flour.id) == 2  # Grocy's units, not added again
    assert await rig.store.allowance_units(rig.clock()) == {}
    # A third box bought later and tagged: added.
    await tag_and_arrive(rig, flour, EPCS[2])
    assert grocy.amount(flour.id) == 3


async def test_migration_from_043_booked_windows(tmp_path: Path) -> None:
    """0.4.3's booked_credits become allowances with the booked quantities."""
    db = tmp_path / "old.db"
    store = await Store.open(db)  # current schema, then fake a 0.4.3 database
    await store.close()
    con = sqlite3.connect(db)
    con.executescript(
        """
        DROP TABLE allowances;
        INSERT INTO booked_credits (product_id, units, booked_at)
        VALUES (101, 3, 1000.0), (102, 1, 2000.0), (103, 0, 3000.0);
        UPDATE meta SET value = '5' WHERE key = 'schema_version';
        """
    )
    con.commit()
    con.close()
    store = await Store.open(db)
    try:
        assert await store.allowance_units(3000.0) == {101: 3, 102: 1}
        allowances = await store.allowances(3000.0)
        assert allowances[101]["sources"] == ["migrated"]
        assert allowances[101]["expires"] == 1000.0 + ALLOWANCE_S  # same window
        assert await store.allowance_units(1000.0 + ALLOWANCE_S + 1) == {102: 1}
        assert await store.get_meta("schema_version") == "6"
    finally:
        await store.close()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM booked_credits").fetchone() == (0,)
    con.close()
    # Opening again doesn't migrate twice.
    store = await Store.open(db)
    try:
        assert await store.allowance_units(3000.0) == {101: 3, 102: 1}
    finally:
        await store.close()


# Lowering consumes from the Pantry only ------------------------------------------


async def test_lowering_consumes_only_pantry_stock(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    beans = grocy.add_product("Beans", ["3005"])
    stock(grocy, beans.id, 4)  # pantry
    stock(grocy, beans.id, 2, location=2)  # fridge
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        await c.post("/scan", barcode="3005", mode="stocktake")
        await c.post("/stocktake/finish")
        await c.post("/stocktake/apply", how="set")
        (row,) = (await c.state())["stocktake"]["review"]
    assert grocy.amount(beans.id) == 1
    assert grocy.amount(beans.id, location_id=2) == 2  # fridge untouched
    assert grocy.count_calls("POST", "/inventory") == 0
    consumes = [b for m, p, b in grocy.calls if p.endswith("/consume")]
    assert consumes[-1]["location_id"] == 1 and consumes[-1]["spoiled"] is False
    assert (row["shortfall"], row["elsewhere"]) == (0, 2)


async def test_pantry_shortfall_shown_in_review(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    salt = grocy.add_product("Salt", ["3006"])
    stock(grocy, salt.id, 5)
    stock(grocy, salt.id, 2, location=2)
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        await c.post("/scan", barcode="3006", mode="stocktake")
        await c.post("/stocktake/count", product_id=salt.id, counted=0)
        await c.post("/stocktake/finish")
        (row,) = (await c.state())["stocktake"]["review"]
        assert (row["grocy"], row["target"], row["difference"]) == (5, 0, -5)
        # Before Apply, three are used up in the pantry: it holds 2, not 5.
        pantry = next(
            e for e in grocy.entries if e.product_id == salt.id and e.location_id == 1
        )
        pantry.amount = 2
        await c.post("/stocktake/apply", how="set")
        (row,) = (await c.state())["stocktake"]["review"]
    # Only what the pantry held was consumed; nothing from the fridge.
    assert grocy.amount(salt.id) == 0
    assert grocy.amount(salt.id, location_id=2) == 2
    assert (row["applied"], row["shortfall"], row["elsewhere"]) == ("set", 3, 2)
